# Deployment runbook

How Tech-Toppings goes live on free hosting, and how to keep it running (Phase 12, ADR-0006,
ADR-0019). Everything here is free with no card. The limits behind these choices are in
[`free-tier-limits.md`](free-tier-limits.md), last checked 2026-10-28.

I prepared everything in the two repositories. You create the accounts, paste the secrets and
press deploy. Never put a secret in git, a chat, an issue or a screenshot.

## What runs where

```
browser ──► Cloudflare Pages  (https://<project>.pages.dev)
             ├─ the app's files: dist/, with public/_headers (CSP, HSTS, caching)
             └─ /api/*  ──►  Pages Function functions/api/[[path]].ts
                              adds the proxy secret and the caller's address
                              ──►  Render web service (Docker, Singapore)
                                    answers only with that secret (but /healthz)
                                    ──►  MongoDB Atlas Free cluster (AWS Singapore)
GitHub Actions ── jobs-cron (every 30 min) and smoke-live (daily) ──► https://<project>.pages.dev/api
```

- The browser only ever talks to the app's own address, so the sign-in cookie stays
  first-party (ADR-0003).
- The API refuses anything that didn't come through the proxy: nobody can reach it around
  Cloudflare, or claim another address to slip past the rate limits (ADR-0019).

## 0. Release to `main`

Render, Cloudflare Pages and GitHub's scheduled workflows all use the `main` branch, while the
work is on `develop`. In **each** repository:
1. commit on `develop`;
2. open a pull request from `develop` to `main`;
3. wait for its CI to pass;
4. merge it.

Every later release goes out the same way. Pages publishes each push to `main` at once (only
Render waits for CI), so merge only once CI has passed.

## 1. Make the secrets (on your machine)

Run these in `tech-toppings-backend-v1` and keep each value in a password manager.

| Secret | Command | Goes to |
|---|---|---|
| Proxy secret | `.venv\Scripts\python -c "import secrets; print(secrets.token_urlsafe(48))"` | Render `SECURITY_PROXY_SECRET`, Cloudflare `API_PROXY_SECRET` |
| Jobs token | same command, a new value | Render `JOBS_TOKEN`, GitHub secret `JOBS_TOKEN` |
| Field encryption key (two-step sign-in) | `.venv\Scripts\python -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"` | Render `AUTH_FIELD_ENCRYPTION_KEY` |
| Backup key | same command, a new value | your `environments/production.env` only |
| Web Push keys (optional) | `.venv\Scripts\poe vapid-keys` | Render `PUSH_VAPID_PUBLIC_KEY`, `PUSH_VAPID_PRIVATE_KEY` |

Render makes `AUTH_JWT_SECRET` itself. Don't change the field encryption key once people use
two-step sign-in (see *Rotating a secret*).

## 2. MongoDB Atlas (database)

1. Sign up at <https://www.mongodb.com/cloud/atlas/register> (free, no card).
2. Create a project, then a **Free** cluster: provider **AWS**, region **Singapore
   (ap-southeast-1)**, next to Render's Singapore region.
3. **Database Access** → *Add New Database User*, twice, each with a long generated password.
   Under *Database User Privileges* → *Specific Privileges*:
   - `tt-api`: *readWrite* on database `tech-toppings-v1` (the server; seeding and restoring
     from your machine);
   - `tt-backup`: *read* on database `tech-toppings-v1` (backups).
   Don't pick a built-in role: those reach every database.
4. **Connect** → *Drivers*: copy the `mongodb+srv://` string, put in the user and password, and
   add the database name before the `?`:
   `mongodb+srv://tt-api:<password>@<cluster-host>/tech-toppings-v1?retryWrites=true&w=majority`.
5. **Network Access**: done in step 3.6, once Render's service exists.
6. **Alerts** (free, by email): disk usage above 80 %, connections above 400.

## 3. Render (API)

1. Sign up at <https://render.com> with GitHub (free, no card).
2. **New** → **Blueprint** → the `tech-toppings-backend-v1` repository. Render reads
   [`render.yaml`](../render.yaml) from `main`: one free Docker web service in Singapore, health
   check `/healthz`, production settings filled in.
3. Render asks for each `sync: false` value:

   | Variable | Value |
   |---|---|
   | `MONGO_URI` | the `tt-api` string from step 2 |
   | `SECURITY_PROXY_SECRET` | the proxy secret |
   | `JOBS_TOKEN` | the jobs token |
   | `AUTH_FIELD_ENCRYPTION_KEY` | the field encryption key |
   | `AUTH_ALLOWED_EMAILS` | who may register, comma-separated (your own email at least) |
   | `AUTH_GOOGLE_CLIENT_ID` | empty, or a Google OAuth web client ID (see *Sign in with Google*) |
   | `AGENT_PROVIDERS` | `[]` for no AI, or the JSON list in `.env.example` with your keys |
   | `PUSH_VAPID_PUBLIC_KEY`, `PUSH_VAPID_PRIVATE_KEY`, `PUSH_SUBJECT` | empty, or the Web Push keys and `mailto:you@example.com` |

4. **Apply**. The first build takes a few minutes. The server refuses to start if a production
   setting is unsafe, and the log line `unsafe for APP_ENV=production: ...` says which.
5. Note the address, e.g. `https://tech-toppings-api.onrender.com`. Check it:
   - `https://<api>/healthz` in a browser gives `{"status":"ok", ...}`;
   - `https://<api>/v1/me` gives `403` with `"code":"FORBIDDEN"`: the API answers only
     through the app.
6. **Atlas network access**:
   1. Render: your service → **Connect** → **Outbound**, and copy its addresses.
   2. Atlas → **Network Access** → add each one.
   3. If Render shows none for the free plan, add `0.0.0.0/0`; the long password and TLS then
      protect the database.
7. Deploys: each push to `main` deploys once its CI checks have passed
   (`autoDeployTrigger: checksPass`).

Every other setting has a safe default; all of them are explained in
[`.env.example`](../.env.example) (a test keeps that file complete). Set one in Render →
*Environment* only to change it.

## 4. Cloudflare Pages (app and proxy)

1. Sign up at <https://dash.cloudflare.com/sign-up> (free, no card).
2. **Workers & Pages** → **Create** → the **Pages** tab → **Import an existing Git repository**
   → the `tech-toppings-frontend-v1` repository. Make sure it's Pages, not a Worker: the address
   must end in `.pages.dev`.
3. Build settings: production branch `main`, framework preset *None*, build command
   `npm run build`, build output directory `dist`. Node comes from `.node-version` (24).
4. **Save and Deploy**. This first build can't reach the API yet.
5. The project → **Settings**:
   - **Variables and Secrets** (Production):

     | Name | Type | Value |
     |---|---|---|
     | `API_ORIGIN` | Text | the Render address, `https://tech-toppings-api.onrender.com` (no path, no `/` at the end) |
     | `API_PROXY_SECRET` | Secret | the proxy secret |
     | `VITE_*` | Text | only to change a default from `.env.example` (built into the app) |

   - **Runtime** → **Compatibility date**: today's date.
6. **Deployments** → the latest → **Retry deployment**, so the settings apply.
7. Check it: open `https://<project>.pages.dev/api/readyz`.

   | Answer | Meaning |
   |---|---|
   | `{"status":"ready", ...}` | connected |
   | `500` *The app is not connected to its server yet* | `API_ORIGIN` or `API_PROXY_SECRET` is missing, or `API_ORIGIN` isn't a bare `https://` address: fix it and retry the deployment |
   | `403` `FORBIDDEN` | the proxy secret differs between Pages and Render |
   | `503` | Render is asleep (wait a minute), or the database is unreachable: see step 3.6 |

What Pages adds to every file is in `public/_headers`:
- the Content-Security-Policy, HSTS, framing and permission rules;
- long caching for the content-named files.

`npm run preview` and the e2e tests serve the same policy, so a page that breaks under it
breaks there first.

## 5. The first admin

On your machine, in `tech-toppings-backend-v1`:

1. `copy environments\production.env.example environments\production.env` and fill it in:
   - the `tt-api` URI;
   - any 32+ characters for `SECURITY_PROXY_SECRET` and `AUTH_JWT_SECRET` (not the real
     ones: no command here uses them);
   - `BOOTSTRAP_ADMIN_EMAIL` and a strong `BOOTSTRAP_ADMIN_PASSWORD`.
2. Atlas → *Network Access*: add your current address for now.
3. `$env:APP_ENV = "production"; .venv\Scripts\poe seed; $env:APP_ENV = $null`
   (`$env:APP_ENV` picks `environments/production.env`, and `poe` keeps it).
4. Delete `BOOTSTRAP_ADMIN_PASSWORD` from the file, and remove your address from Atlas.
5. Sign in at `https://<project>.pages.dev`, and change the password.

## 6. GitHub (scheduled work and checks)

In the **backend** repository → *Settings* → *Secrets and variables* → *Actions*:

| Kind | Name | Value |
|---|---|---|
| Secret | `JOBS_API_URL` | `https://<project>.pages.dev/api` (through the proxy) |
| Secret | `JOBS_TOKEN` | the jobs token |
| Variable | `SMOKE_APP_URL` | `https://<project>.pages.dev` |
| Variable | `SMOKE_API_URL` | the Render address (to check it refuses strangers) |
| Secret | `SMOKE_EMAIL`, `SMOKE_PASSWORD` | optional: a test account (see below) |

The test account is a normal user without two-step sign-in, never an admin:
1. add its email in the admin console → *Allowlist*;
2. register it at `https://<project>.pages.dev/register`.

Then *Actions* → **jobs-cron** → *Run workflow*, and **smoke-live** → *Run workflow*. Both
should pass.

## 7. Check it live

1. `.venv\Scripts\poe smoke-live --app https://<project>.pages.dev --api https://<api>`.
   Set `$env:SMOKE_EMAIL` and `$env:SMOKE_PASSWORD` first to also sign in.
   - Every line should say `PASS`:
     - the page and its headers;
     - a deep link;
     - install files;
     - asset caching;
     - the API awake and its database reachable;
     - strangers refused;
     - the CSRF rule;
     - docs hidden;
     - the origin lock;
     - sign-in and sign-out.
   - On a network that inspects TLS (an office), point `SSL_CERT_FILE` at a CA bundle that
     includes its root certificate, or use the GitHub run instead. Never turn verification off.
2. Lighthouse (mobile) on the live address:
   `npx --yes lighthouse@12 https://<project>.pages.dev/login --only-categories=performance,accessibility,best-practices --view`.
   The targets are Performance ≥ 90, Accessibility ≥ 95 and Best Practices ≥ 95.
3. On Android Chrome:
   1. open the app and install it (menu → *Install app*);
   2. Notifications → *Turn on* → *Send a test*;
   3. make a bill reminder due today. Reminders go out from `REMINDERS_SEND_HOUR` (9:00) in
      your time zone;
   4. close the app, and run **jobs-cron** by hand: the reminder arrives as a notification.

## Running it

**Deploys and rollbacks.**
- Merge `develop` into `main` (step 0). Render deploys after CI passes; Pages publishes at
  once.
- To roll back: Render → *Deploys* → an earlier one → *Rollback*; Pages → *Deployments* →
  an earlier one → *Rollback to this deployment*.

**Backups** (weekly, and before anything risky). Atlas Free keeps none.
- Make one: `$env:APP_ENV = "production"; .venv\Scripts\poe backup; $env:APP_ENV = $null`.
  - In `environments/production.env`, use the `tt-backup` URI and set `BACKUP_ENCRYPTION_KEY`.
    A database that isn't on your machine is never backed up unencrypted.
  - It first prints the host it reads from: check it's the Atlas cluster.
  - It writes `backups/production/<database>-<time>/`, one encrypted, gzipped file per
    collection plus a manifest, and keeps the newest `BACKUP_KEEP`.
  - Keep a copy off this machine (a private drive), never in git or a GitHub artifact: artifacts
    of a public repository can be downloaded by anyone signed in to GitHub.
- **Practise a restore** now and then, into a database on your own machine, never Atlas (its
  512 MB holds one copy):
  `$env:MONGO_URI = "mongodb://localhost:27017/tt-restore-drill"; $env:BACKUP_ENCRYPTION_KEY = "<the backup key>"; .venv\Scripts\poe restore --from backups\production\<folder>; $env:MONGO_URI = $null; $env:BACKUP_ENCRYPTION_KEY = $null`.
- **Restore the live database** (after a disaster):
  1. Render: the service → *Settings* → *Suspend Web Service*. In GitHub, *Actions* →
     **jobs-cron** → *Disable workflow*. Nothing may write meanwhile.
  2. Put the `tt-api` URI in `environments/production.env`.
  3. Run `$env:APP_ENV = "production"; .venv\Scripts\poe restore --from backups\production\<folder> --drop; $env:APP_ENV = $null`.
     - It checks every file before it changes anything.
     - It shows where the backup came from and where it goes, and asks you to type that
       `host/database`.
     - If it stops part-way, it says which collections it already replaced.
  4. Resume the service, and enable jobs-cron again.

**Monitoring** (all free):
- **smoke-live** runs daily, and a failed run emails you.
- **jobs-cron** failures email you too.
- **Render**: *Logs* holds JSON lines with a `request_id` per request, matching the
  `X-Request-ID` header of each answer (the browser's network panel shows it). *Metrics* shows
  memory and response times. Render emails a failed deploy.
- **The admin console** → *Usage*:
  - database size against 512 MB;
  - errors by code;
  - AI calls and the fallback rate.
- **Atlas** alerts (step 2).
- **Cloudflare** → the Pages project: requests, and Functions' daily count against 100,000.

**Limits to watch:**

| Limit | What happens past it | What to do |
|---|---|---|
| Render 5 GB outbound a month | free services suspended for the rest of the month | answers over 1 KB are gzipped; avoid repeated large exports |
| Render 750 instance hours a month | the same | one service only; no keep-alive pings (Render treats them as abuse) |
| Atlas 512 MB | writes fail | the admin console shows it; TTLs bound most collections |
| Workers 100,000 requests a day | the proxy stops answering until 00:00 UTC | far above one person's use |
| GitHub schedules | disabled after 60 days without a commit | *Actions* → enable the workflow again |
| Atlas idle | paused after 30 days without a connection | the cron's connections keep it awake |

**Rotating a secret.**
- **Proxy secret**: set the new value on Render, then on Pages, and retry the Pages
  deployment. Between the two, the app gets 403s, so do it at a quiet time.
- **Jobs token**: Render, then the GitHub secret.
- **VAPID keys**: every browser has to turn notifications on again.
- **JWT secret**: Render → *Environment* → `AUTH_JWT_SECRET` → *Generate*. Anyone with the app
  open is signed out and signs in again; apps that were closed carry on.
- **Database password**: change it in Atlas, then `MONGO_URI` on Render.
- **Field encryption key**: only if it leaked. Tell users first.
  - Afterwards, people with two-step sign-in sign in with a recovery code, then set it up again.
  - For anyone without a recovery code, run
    `$env:APP_ENV = "production"; .venv\Scripts\poe reset-password --email <their email>; $env:APP_ENV = $null`.
    That also turns two-step sign-in off.

**AI providers.** `AGENT_PROVIDERS` lists free models in order. The current free ones:
- Gemini: `gemini-3.5-flash-lite` or `gemini-3.8-flash`, with a key from AI Studio;
- Groq: `openai/gpt-oss-20b`.

Free-tier prompts may be used by Google to improve its products. Users choose whether to
allow AI.

**Sign in with Google** (optional):
1. Create an OAuth *web* client in Google Cloud.
2. Add `https://<project>.pages.dev` to its *Authorized JavaScript origins*.
3. Set `AUTH_GOOGLE_CLIENT_ID` on Render.

The CSP already allows Google's button.

## When something is wrong

| You see | Likely cause |
|---|---|
| "Waking up the server…" for a minute | Render was asleep (after 15 idle minutes): normal |
| "Waking up", then "Unreachable", right after deploying | open `/api/readyz` (step 4.7): it says which setting is wrong |
| "Unreachable" later on | Render suspended (a limit), or a failed deploy: see Render |
| Every action fails with 403 `FORBIDDEN` | the proxy secret differs between Render and Pages |
| `/api/readyz` answers 503 for minutes, or smoke-live's *database reachable* fails | Atlas *Network Access* lacks Render's addresses, or `MONGO_URI` is wrong |
| Render deploy fails, `unsafe for APP_ENV=production` | the log names the setting |
| Signed out on every reload | `AUTH_REFRESH_COOKIE_PATH` must stay `/api/v1/auth` |
| A blocked resource in the browser console | `public/_headers` (the CSP) lacks its source |
| Reminders on the bell but no pushes | `PUSH_*` empty on Render, or the browser's notifications off |
| A maintenance command prints `localhost` | `$env:APP_ENV = "production"` wasn't set in that window |
