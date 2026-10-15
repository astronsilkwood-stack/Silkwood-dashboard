# Silkwood Daily Ops dashboard (v2)
Python stdlib server (`server.py`) + `index.html`. Storage:
- no DATABASE_URL -> SQLite file `silkwood.db` (local/box use)
- DATABASE_URL=postgresql://... -> Postgres (Neon in the cloud). Tables + starter data are created on first start.

Env vars: PORT (default 8787), DATABASE_URL, STAFF_PIN (shared staff PIN asked once per device and stored there;
empty = no PIN, local only), MANAGER_PIN (reopen a submitted day; default 1945).

Run on the box: ./run.sh (keep-alive; re-run after a box restart). Local URL http://localhost:8787
Cloud: see DEPLOY.md (Render free web service + Neon free Postgres). Files: requirements.txt, Dockerfile, render.yaml, Procfile.
Temperature rules: fridge <=5, freezer <=-15, hot hold >=60, cooked core >=75 (°C).
Legacy v1 (Apps Script/Drive, ../build.py) untouched; its link error = the Apps Script deploy was never done.
