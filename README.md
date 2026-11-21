# BidSmith Cloud (runs on GitHub Actions, even when your PC is off)

Every ~5 minutes GitHub starts this program. It finds brand-new Freelancer.com projects in your skills,
asks Google Gemini (free) to score them and write a bid, and posts the strong matches (fit 75+, project posted in the
last 20 minutes, 40 bids or fewer, at most 10 bids a day counting the bids you place yourself).

## Setup (about 15 minutes, free)

1. **GitHub account + repository.** Sign up at https://github.com if you don't have one. Create a new repository named
   `bidsmith`, set it to **Public** (public repositories get free Actions minutes), and leave "Add a README" unticked.
   Copy its address, like `https://github.com/yourname/bidsmith.git`.
2. **Upload the files.** Double-click `upload.bat`, paste the address, and sign in to GitHub if a browser opens.
3. **Free Gemini key.** Go to https://aistudio.google.com/apikey, sign in with Google, click **Create API key**, copy it.
4. **Add two secrets** on GitHub: your repository > **Settings** > **Secrets and variables** > **Actions** > **New repository secret**.
   - Name `FREELANCER_TOKEN`, value: the token from the `.env` file in your `bidsmith-agent` folder (the text after `FREELANCER_TOKEN=`).
   - Name `GEMINI_API_KEY`, value: the key from step 3.
   Never paste these anywhere else, and never into a chat.
5. **Turn it on.** Open the **Actions** tab, click **I understand my workflows, go ahead and enable them**, open **BidSmith**
   in the left list, then **Run workflow**. Open the run to read its log. After that it runs by itself every ~5 minutes.

## Good to know
- The repository is public, so `config.json` (your skills, rules and profile text) can be read by anyone. Your keys can not:
  they live in GitHub Secrets.
- GitHub may start a run a few minutes late. Bids will land a little later than on your PC.
- GitHub switches scheduled runs off after 60 days with no activity in the repository. If that happens, open the Actions
  tab and enable it again.
- You can keep the PC version running as well. Neither bids twice on the same project.
- To change rules (fit score, daily cap, skills), edit `config.json` on GitHub (pencil icon) and commit.
