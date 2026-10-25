# Deploy: Render (free) + Neon (free Postgres). No card needed.

## Astron does once (about 10 minutes, sign in with Google using the shop account)
1. https://neon.com : Sign up, then create a project "silkwood" in region AWS Asia Pacific (Sydney).
   Copy the connection string (Dashboard > Connect, starts postgresql://, ends ?sslmode=require).
2. https://github.com : make an account (or use an existing one). The code has to be in a GitHub repo for Render.
   The repo can be private; it holds no secrets.
3. https://render.com : Sign up with GitHub. Account settings > API Keys > Create API key (only if the agent should manage deploys).

## Then (agent or Astron)
4. Put the contents of this folder (server.py, index.html, requirements.txt, render.yaml, Procfile, Dockerfile, .gitignore)
   in the repo root and push.
5. Render > New > Blueprint > pick the repo. render.yaml creates "silkwood-ops" on the free plan. When asked, set:
   DATABASE_URL = the Neon string, STAFF_PIN = staff PIN, MANAGER_PIN = manager PIN.
   Checklist forwards (optional until that form should go out): CLOSING_WEBHOOK_URL + CLOSING_WEBHOOK_KEY,
   and KITCHEN_WEBHOOK_URL + KITCHEN_WEBHOOK_KEY. Each pair is a POST URL and a bearer token. The server sends
   `Authorization: Bearer <key>`. If a pair is missing, that checklist’s Submit returns 503 and nothing is forwarded.
   After this kitchen checklist ships, set KITCHEN_WEBHOOK_URL and KITCHEN_WEBHOOK_KEY on the Render service
   (Environment) before staff use Submit. The payload type is `kitchen_daily_checklist` for site `United Silkwood`.
   (Render's API create-service call can need a card on file, so the first create is done in the dashboard.
   Later redeploys happen automatically on git push, or via the API/CLI: `render deploys create <service-id>` with RENDER_API_KEY.)
6. Open https://silkwood-ops.onrender.com (the exact URL is shown in Render). Enter the staff PIN once on each device.
   Add it to the home screen.

## Notes
- Free Render sleeps after 15 min idle. The first open after that takes about 1 minute. Data is safe because it lives in Neon.
  For instant loading: Render > service > Settings > Instance type > Starter (US$7/mo).
- Neon free gives 1 GB storage, which is many years of daily logs.
- Move existing box data: start the app once with DATABASE_URL set (it creates the tables), or start fresh.
  The box copy currently holds only starter data.
