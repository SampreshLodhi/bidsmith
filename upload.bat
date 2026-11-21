@echo off
cd /d "%~dp0"
echo.
echo This uploads the BidSmith cloud files to YOUR GitHub repository.
echo Your keys (.env) are NOT uploaded.
echo.
set /p REPO=Paste your repository address (like https://github.com/yourname/bidsmith.git) and press Enter:
if "%REPO%"=="" goto :eof

if not exist .git git init -b main
git add .
git ls-files | findstr /i "\.env$" >nul && (echo STOP: a .env file would be uploaded. Not continuing. & pause & exit /b 1)
git -c user.name=BidSmith -c user.email=bidsmith@users.noreply.github.com commit -m "BidSmith cloud bidder"
git remote remove origin 2>nul
git remote add origin %REPO%
echo.
echo A browser window may open so you can sign in to GitHub. Do that, then come back here.
git push -u origin main
echo.
echo Done. Next: add your two secrets on GitHub (see README.md, step 4).
pause
