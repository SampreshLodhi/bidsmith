"""BidSmith Cloud: one check per run. GitHub Actions starts it every ~5 minutes.

Finds brand-new Freelancer.com projects in your skills, asks Gemini to score them and write a bid,
and posts the strong matches. Secrets come from environment variables (GitHub Secrets):
  FREELANCER_TOKEN   (required)
  GEMINI_API_KEY     (required to score/write bids; without it the run only lists candidates)
Optional: DRY_RUN=1 (never posts), GEMINI_MODEL.
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
API = "https://www.freelancer.com/api"
IST = timezone(timedelta(hours=5, minutes=30))
MAX_CHARS, MIN_CHARS = 1500, 100
STARTED = time.time()
RUN_BUDGET_SECONDS = 6 * 60  # stay well inside the job time limit


def load_dotenv():
    p = ROOT / ".env"
    if p.exists():  # local testing only; .env is never committed
        for line in p.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                if v.strip():
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def log(msg):
    print(f"{datetime.now(IST):%H:%M:%S} {msg}", flush=True)


# ---------------------------------------------------------------- Freelancer API

class FL:
    def __init__(self, token):
        self.http = requests.Session()
        self.http.headers["freelancer-oauth-v1"] = token
        self._me = None

    @staticmethod
    def _result(r):
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:150]}")
        if r.status_code >= 400 or data.get("status") != "success":
            raise RuntimeError(data.get("message") or f"HTTP {r.status_code}")
        return data.get("result")

    def me(self):
        if self._me is None:
            self._me = self._result(self.http.get(f"{API}/users/0.1/self/", timeout=30))["id"]
        return self._me

    def search(self, cfg):
        since = int(time.time()) - 60 * int(cfg["max_project_age_minutes"])
        params = [("limit", 50), ("full_description", "true"), ("job_details", "true"), ("compact", "true"),
                  ("from_time", since)]
        params += [("jobs[]", s) for s in cfg["skill_ids"]] + [("project_types[]", t) for t in cfg["project_types"]]
        res = self._result(self.http.get(f"{API}/projects/0.1/projects/active/", params=params, timeout=30)) or {}
        out = []
        for p in res.get("projects", []):
            budget, cur, stats = p.get("budget") or {}, p.get("currency") or {}, p.get("bid_stats") or {}
            out.append({
                "id": p["id"], "title": p.get("title", ""), "type": p.get("type", "fixed"),
                "description": p.get("description") or p.get("preview_description") or "",
                "min": budget.get("minimum") or 0, "max": budget.get("maximum") or budget.get("minimum") or 0,
                "currency": cur.get("code", "USD"), "rate": cur.get("exchange_rate") or 1,
                "bids": stats.get("bid_count") or 0, "avg": stats.get("bid_avg") or 0,
                "submitted": p.get("time_submitted") or int(time.time()),
                "skills": ", ".join(j.get("name", "") for j in (p.get("jobs") or [])),
                "url": f"https://www.freelancer.com/projects/{p.get('seo_url') or p['id']}",
            })
        return out

    def my_bids(self, limit=100):
        r = self.http.get(f"{API}/projects/0.1/bids/", params=[("bidders[]", self.me()), ("limit", limit)], timeout=30)
        return (self._result(r) or {}).get("bids", [])

    def post_bid(self, pid, amount, days, text, milestone):
        body = {"project_id": int(pid), "bidder_id": int(self.me()), "amount": float(amount), "period": int(days),
                "milestone_percentage": int(milestone), "description": text}
        return self._result(self.http.post(f"{API}/projects/0.1/bids/", json=body, timeout=30))


# ---------------------------------------------------------------- rules

def passes_rules(p, cfg):
    age = (time.time() - p["submitted"]) / 60
    usd_max = p["max"] * p["rate"]
    text = (p["title"] + " " + p["description"]).lower()
    if age > cfg["max_project_age_minutes"]:
        return "older than limit"
    if p["bids"] > cfg["max_existing_bids"]:
        return f"already {p['bids']} bids"
    if p["type"] == "fixed" and usd_max and usd_max < cfg["min_budget_usd"]:
        return "budget too low"
    if p["type"] == "fixed" and cfg.get("max_budget_usd") and usd_max > cfg["max_budget_usd"]:
        return "budget above range"
    for w in cfg["exclude_words"]:
        if re.search(r"\b" + re.escape(w.lower()) + r"\b", text):
            return f"contains '{w}'"
    return None


# ---------------------------------------------------------------- Gemini (free tier)

MODELS = ["gemini-flash-latest", "gemini-2.5-flash", "gemini-2.0-flash"]


def gemini_json(prompt):
    key = os.environ["GEMINI_API_KEY"]
    first = os.environ.get("GEMINI_MODEL")
    models = ([first] if first else []) + [m for m in MODELS if m != first]
    last = ""
    for m in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"
        body = {"contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0.6}}
        r = None
        for attempt in range(2):
            try:
                r = requests.post(url, params={"key": key}, json=body, timeout=50)
            except requests.RequestException as e:  # slow or unreachable: treat like a busy server
                last = f"{m}: {type(e).__name__}"
                r = None
                continue
            if r.status_code in (429, 500, 503):  # busy or rate limited: wait a little, retry, then try the next model
                last = f"{m}: HTTP {r.status_code}"
                time.sleep(6 * (attempt + 1))
                continue
            break
        if r is None or r.status_code in (404, 429, 500, 503):
            last = last or f"model {m} not found"
            continue
        if r.status_code != 200:
            raise RuntimeError(f"Gemini HTTP {r.status_code}: {r.text[:200]}")
        try:
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError):
            raise RuntimeError("Gemini returned no answer (blocked or empty)")
        s, e = text.find("{"), text.rfind("}")
        return json.loads(text[s:e + 1])
    raise RuntimeError(last or "no Gemini model worked")


def draft(cfg, p):
    unit = "hourly rate" if p["type"] == "hourly" else "fixed price"
    prompt = f"""You are an elite freelancer who wins most bids on Freelancer.com. Judge this project for the freelancer below and, only if it is a good match, write the proposal.

FREELANCER PROFILE (the ONLY source of facts about the freelancer):
\"\"\"{cfg['profile']}\"\"\"

PROJECT
Title: {p['title']}
Type: {p['type']} | Budget: {p['min']}-{p['max']} {p['currency']} | Skills: {p['skills']} | Bids so far: {p['bids']}
Description:
\"\"\"{p['description'][:6000]}\"\"\"

Return ONLY a JSON object with these keys:
"fit_score": integer 0-100. How well the freelancer's real skills match the MAIN technology of the project. Below 40 if the core need is something not in the profile (WooCommerce, Shopify, WordPress themes, Python or PHP backends, mobile-native apps, data science, SEO, design, QA, Telegram or WhatsApp bots, etc.).
"is_risky": true only for scam signs, payment or contact outside Freelancer.com, illegal or deceptive work, academic cheating.
"amount": number, a competitive {unit} in {p['currency']} between {p['min']} and {p['max']}.
"delivery_days": integer, realistic.
"bid_text": the proposal (empty string if fit_score is below 60).

RULES FOR bid_text:
- Under {MAX_CHARS} characters; aim for 700-1300. Plain text, no markdown, no emoji.
- Open with THEIR problem or goal in their words plus one specific detail from the post. Never open with "Dear Sir", "Hi, I am" or "I am interested".
- Then 1-2 concrete matching results from the profile. Then a 3-4 step numbered plan. Address the client's unspoken worries.
- State price and delivery time. End with ONE smart question and a simple call to action.
- Tone: {cfg['tone']}. NEVER invent experience, clients, numbers or links that are not in the profile.
- Sign off with: {cfg['your_name']}"""
    return gemini_json(prompt)


def notify_whatsapp(p, amount, days, score):
    """WhatsApp message through the free CallMeBot service. Phone and key come from GitHub Secrets (never in the code)."""
    phone, key = os.environ.get("WHATSAPP_PHONE"), os.environ.get("CALLMEBOT_APIKEY")
    if not phone or not key:
        return
    text = f"BidSmith placed a bid\n{p['title'][:90]}\n{amount} {p['currency']} in {days} days | fit {score}/100 | {p['bids']} bids on it\n{p['url']}"
    try:
        r = requests.get("https://api.callmebot.com/whatsapp.php", timeout=30,
                         params={"phone": re.sub(r"\D", "", phone), "text": text, "apikey": key})
        log("   WhatsApp message sent" if r.status_code == 200 else f"   (WhatsApp not sent: HTTP {r.status_code})")
    except requests.RequestException as e:  # never print the exception text: it contains the URL with your key
        log(f"   (WhatsApp not sent: {type(e).__name__})")


def notify_bid(p, amount, days, score, bids_today):
    """Tell you about the bid: a WhatsApp message (if set up) and an email through GitHub (opens a short issue,
    which GitHub emails to the repo owner, then closes it). The bid text is NOT included: the repository is public."""
    notify_whatsapp(p, amount, days, score)
    token, repo, owner = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_REPOSITORY_OWNER")
    if not token or not repo:
        return
    body = (f"@{owner} BidSmith placed a bid.\n\n"
            f"- Project: [{p['title']}]({p['url']})\n"
            f"- Your bid: {amount} {p['currency']} in {days} days\n"
            f"- Fit score: {score}/100\n"
            f"- Bids already on the project: {p['bids']}\n"
            f"- Bids placed today: {bids_today}\n\n"
            "Open the project link to read your bid.")
    h = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    try:
        r = requests.post(f"https://api.github.com/repos/{repo}/issues", headers=h, timeout=30,
                          json={"title": f"Bid placed: {p['title'][:80]}", "body": body, "assignees": [owner] if owner else []})
        if r.status_code == 201:
            requests.patch(r.json()["url"], headers=h, json={"state": "closed"}, timeout=30)
        else:
            log(f"   (email note not sent: HTTP {r.status_code})")
    except requests.RequestException as e:
        log(f"   (email note not sent: {type(e).__name__})")


def shorten(text):
    cut = text[: MAX_CHARS - 20]
    end = max(cut.rfind(". "), cut.rfind("\n"))
    return (cut[: end + 1] if end > 600 else cut).rstrip()


# ---------------------------------------------------------------- state (kept between runs by the Actions cache)

def load_state():
    f = STATE_DIR / "state.json"
    try:
        s = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        s = {}
    cutoff = time.time() - 3 * 86400
    s["seen"] = {k: v for k, v in s.get("seen", {}).items() if v.get("t", 0) > cutoff}
    return s


def save_state(s):
    STATE_DIR.mkdir(exist_ok=True)
    (STATE_DIR / "state.json").write_text(json.dumps(s), encoding="utf-8")


PERMANENT_ERRORS = ("preferred freelancer", "different language", "verified by freelancer", "already bid",
                    "not available in your country")


# ---------------------------------------------------------------- main

def main():
    load_dotenv()
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    dry = os.environ.get("DRY_RUN") == "1" or "--dry" in sys.argv
    if not os.environ.get("FREELANCER_TOKEN"):
        sys.exit("FREELANCER_TOKEN is not set (add it under Settings > Secrets > Actions).")
    have_ai = bool(os.environ.get("GEMINI_API_KEY"))
    fl = FL(os.environ["FREELANCER_TOKEN"])
    state = load_state()
    seen = state["seen"]
    log(f"check started ({'DRY RUN' if dry else 'LIVE'}; AI {'on' if have_ai else 'OFF: listing only'})")

    mine = fl.my_bids()
    my_project_ids = {str(b["project_id"]) for b in mine}
    midnight = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    bids_today = sum(1 for b in mine if b["time_submitted"] >= midnight)
    log(f"bids placed today (IST): {bids_today}/{cfg['max_bids_per_day']}")

    projects = fl.search(cfg)
    new = [p for p in projects if str(p["id"]) not in seen and str(p["id"]) not in my_project_ids]
    log(f"{len(projects)} recent projects in your skills, {len(new)} not seen before")

    posted = 0
    for p in new:
        if time.time() - STARTED > RUN_BUDGET_SECONDS:
            log("time budget reached; the rest waits for the next run")
            break
        why = passes_rules(p, cfg)
        if why:
            seen[str(p["id"])] = {"t": time.time(), "r": why}
            continue
        if not have_ai:
            log(f"candidate: {p['title'][:60]} | {p['bids']} bids | {p['skills'][:50]}")
            continue
        if bids_today + posted >= cfg["max_bids_per_day"]:
            log("daily cap reached; stopping")
            break
        try:
            d = draft(cfg, p)
        except Exception as e:  # leave unseen so the next run retries while the project is still fresh
            log(f"AI failed for '{p['title'][:40]}': {str(e)[:120]}")
            continue
        score, text = int(d.get("fit_score", 0)), (d.get("bid_text") or "").strip()
        log(f"[{score:>3}] {p['title'][:55]} | bids {p['bids']} | risky={d.get('is_risky')}")
        seen[str(p["id"])] = {"t": time.time(), "r": f"fit {score}"}
        if d.get("is_risky") or score < cfg["min_fit_score"] or not text:
            continue
        if len(text) > MAX_CHARS:
            text = shorten(text)
        if len(text) < MIN_CHARS:
            continue
        amount = float(d.get("amount") or 0)
        if p["max"] and p["min"] <= p["max"]:
            amount = round(min(max(amount, p["min"]), p["max"]), 2)
        days = max(1, int(d.get("delivery_days") or 5))
        if dry:
            log(f"   DRY RUN: would bid {amount} {p['currency']} in {days} days\n   {text[:200]}...")
            continue
        try:
            fl.post_bid(p["id"], amount, days, text, cfg["milestone_percentage"])
            posted += 1
            seen[str(p["id"])]["r"] = "BID PLACED"
            log(f"   BID PLACED: {amount} {p['currency']}, {days} days")
            notify_bid(p, amount, days, score, bids_today + posted)
        except Exception as e:
            msg = str(e)
            log(f"   bid refused: {msg[:140]}")
            if not any(w in msg.lower() for w in PERMANENT_ERRORS):
                seen[str(p["id"])]["r"] = "post failed"
    save_state(state)
    log(f"check finished: {posted} bid(s) placed")


if __name__ == "__main__":
    main()
