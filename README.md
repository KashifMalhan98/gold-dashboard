# Gold buy-readiness dashboard (fully automatic)

You need a free GitHub account and a laptop for the one-time setup (about 10 minutes).
After that it runs by itself and you just open one link on your phone.

1. github.com > New repository. Name: gold-dashboard. Set it to Public (free Pages needs this). Create.
2. Unzip this folder. In the repo click Add file > Upload files. Drag in: fetch_data.py, config.json,
   requirements.txt, README.md and the whole docs folder. Commit.
3. The hidden .github folder often does not upload by drag and drop. Instead click
   Add file > Create new file, type the name   .github/workflows/update.yml   (typing the slashes makes folders),
   paste the contents of that file from this zip, and commit.
4. Settings > Pages > Source: Deploy from a branch > Branch: main, folder: /docs > Save.
5. Actions tab > Update gold data > Run workflow. Wait about 2 minutes for the green tick.
   If it is red, open the run and send me the log.
6. Open https://YOUR-USERNAME.github.io/gold-dashboard/ on your phone.
   iPhone: Share > Add to Home Screen. Android: menu > Add to Home screen.
   It refreshes automatically at 8:00 and 13:30 IST, Monday to Saturday.

Notes: the repo is public, so anyone with the link can see the dashboard. Leave wedding_date empty in config.json if you prefer.
Import duty is set to 15% (in force since 13 May 2026). Update config.json if it changes.
