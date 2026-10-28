# Free-tier limits (verified 2026-10-28)

Every external service Tech-Toppings depends on, with the limits that shape the design.
Limits change often (Groq removed its Llama models from the free tier on 2026-08-16 and retired
`qwen/qwen3.6-27b` on 2026-09-14; Render cut Hobby bandwidth from 100 GB to 5 GB a month in
2026; Gemini model IDs churn every few months). For that reason **no limit, model name, or URL
below is hard-coded**: each one maps to an environment variable in `.env.example`.

Re-verified on 2026-10-28 for Phase 12 (deployment). Re-verify whenever a provider returns
unexpected `429`/`403` responses.

Legend: **Official** = read from the vendor's own docs or pricing page on the date in the
section heading. **Official (snippet)** = the vendor page blocks automated fetching; the figure
was read from a search-engine snippet of that vendor page. **Secondary** = figure taken from
third-party sources and must be confirmed in the vendor console. Sections marked "not
re-verified" still carry their 2026-10-07 figures.

---

## 1. Hosting and data

### MongoDB Atlas M0 (production database) — Official, verified 2026-10-28
Sources: <https://www.mongodb.com/docs/atlas/reference/free-shared-limitations/>,
<https://www.mongodb.com/docs/atlas/reference/amazon-aws/> (region table),
<https://www.mongodb.com/pricing>,
<https://www.mongodb.com/docs/atlas/backup/cloud-backup/free-cluster-backup/>

The Atlas UI and docs now call this tier the **"Free cluster"** (formerly `M0`).

| Limit | Value | Design impact |
|---|---|---|
| Storage | 0.5 GB (uncompressed BSON + indexes) | TTL indexes on caches, logs, transcripts, idempotency keys; compact documents; admin "DB size" widget; export script |
| Ops/second | 100 reads + writes; throttled above that. Query time must also stay under 100% utilization over any 5-minute window | Pool small (`MONGO_MAX_POOL_SIZE`), aggregate server-side, cache dashboard results |
| Connections | 500 | Single Render instance with pool of ~10 is far below |
| Data transfer | 10 GB in / 10 GB out per rolling 7 days; throttled above that | Paginate everything; no bulk polling |
| Collections | 500 total, 100 databases; one Free cluster per Atlas project | One database, ~25 collections |
| Aggregation | Max 50 stages; `allowDiskUse` ignored; 32 MB in-memory sort | Keep pipelines short; always `$match` on indexed `user_id` first |
| Server-side JS | `$where`, map-reduce unsupported | Never used |
| Backups | None included. An optional Free-cluster backup subscription (private preview) costs $0.10/day, max $3/month, so it needs billing | Not used. `poe backup` / `poe restore` (scripts/backup_db.py, restore_db.py; PyMongo, Fernet-encrypted; ADR-0019) + user data export |
| Idle pause | After 30 days with zero connections (resumable). Atlas may also deactivate idle Free clusters under its Terms of Service | Not a concern while the app is used; documented in runbook |
| Version | MongoDB 8.0; Atlas upgrades Free clusters itself (version cannot be pinned) | Local dev DB is 8.0.26 — same major version |
| Transactions | Supported (3-node replica set; the limits page lists no transaction restriction) | **Local dev DB is standalone → no transactions.** Revision writes are designed to be transaction-free (ADR-0002, `docs/plan.md` §5) |
| Regions (AWS) | Free clusters exist only in a subset of regions. **Singapore `ap-southeast-1`: yes. Mumbai `ap-south-1`: yes.** (Hyderabad `ap-south-2`: no) | Create the cluster in AWS Singapore to sit in the same city as the Render `singapore` region (lowest backend↔DB latency); Mumbai is the alternative if Render runs elsewhere |
| RAM / CPU | Shared | — |
| Card | Not required ("Free forever" on the pricing page; MongoDB's free-tier blog states no credit card is required) | — |

Local development uses `mongodb://localhost:27017/tech-toppings-v1` (no limits).

### Render free web service (backend) — Official, verified 2026-10-28
Sources: <https://render.com/docs/free>, <https://render.com/pricing> (Hobby column),
<https://render.com/docs/outbound-bandwidth>, <https://render.com/docs/blueprint-spec>,
<https://render.com/docs/regions>,
<https://render.com/changelog/updated-plans-for-render-workspaces> (2026-04-23)

| Limit | Value | Design impact |
|---|---|---|
| Idle spin-down | After 15 min without inbound traffic (HTTP requests or WebSocket messages) | Frontend "waking up server" state (health probe + retry with backoff) |
| Cold start | ~1 minute | Same; keep startup work minimal (no heavy imports, lazy AI clients) |
| Instance hours | 750 / month per workspace (shared by all free services); spun-down services use none. When used up, all free web services are suspended until next month | One backend service only |
| RAM / CPU | 512 MB / 0.1 CPU (pricing page and Blueprint spec) | No in-process ML; LLM calls are remote; stream CSV exports |
| Bandwidth | **5 GB / month** on the Hobby workspace (was 100 GB on the legacy plan; new plans since 2026-04-23, legacy workspaces auto-moved on 2026-08-01). All outbound traffic to the public internet counts: responses to clients **and** requests the service sends (Gemini, Groq, Atlas, Web Push). Without a payment method, overage suspends all free services for the rest of the month | Budget ≈ 170 MB/day. Answers of `API_GZIP_MIN_BYTES` or more are gzipped (ADR-0019); paginate; keep JSON lean; keep AI prompts short; large CSV exports are the main risk; watch the Billing page |
| Build minutes | 500 / month (Hobby). Without a payment method, overage disables new builds for the month (running service keeps serving) | Deploy only from `main`, not every `develop` push |
| Filesystem | Ephemeral; lost on deploy/restart/spin-down; persistent disks not available | **Never write files locally.** Imports are parsed in memory; logs go to stdout |
| SMTP | Ports 25/465/587 blocked | Email (if enabled) must use an HTTP API provider |
| Scaling | Single instance, no shell, no one-off jobs, no pre-deploy command (paid only), no edge caching, no cron; Render may restart a free service at any time | In-memory state is a cache only; scheduled work runs lazily or via GitHub Actions cron |
| Docker | Supported: "Docker builds" is included on the Hobby plan, and the Blueprint spec accepts `runtime: docker` with `plan: free` | Deploy with the repo `Dockerfile` |
| Regions | Oregon, Ohio, Virginia, Frankfurt, **Singapore** (`region: singapore`). The free-instance page states no region restriction (confirm Singapore + Free in the create-service form) | Use `singapore`, next to the Atlas cluster; region cannot be changed after creation |
| Keep-alive pings | No rule in the docs. A Render staff reply on the community forum calls pinging a free service only to keep it awake "abuse" (Secondary). Officially, Render may suspend a free service that sends an "uncommonly high volume" of outbound traffic | No keep-alive pinger. The GitHub Actions cron runs only for real scheduled work (reminders, recurring entries) |
| Card | Not required to start (Render docs: "No payment is required" for free resources). Without a card, overages suspend services instead of billing. An old forum thread says a card is sometimes asked for verification (Secondary) | — |

### Frontend hosting — Official, verified 2026-10-28 (Cloudflare); Vercel not re-verified

**Cloudflare Pages (recommended)** — <https://developers.cloudflare.com/pages/platform/limits/>,
<https://developers.cloudflare.com/pages/functions/pricing/>,
<https://developers.cloudflare.com/workers/platform/limits/>

| Limit | Value |
|---|---|
| Builds | 500 / month, 1 concurrent, 20-minute timeout |
| Files | 20,000 per site, 25 MiB max per file |
| Static requests / bandwidth | Free and unlimited for requests that do not invoke a Function |
| Projects / custom domains | 100 per account / 100 per project |
| `_headers` / `_redirects` | 100 header rules, 2,000 characters per line / 2,000 static + 100 dynamic redirects |
| Pages Functions | Count against the Workers Free quota: 100,000 requests/day (shared with Workers, resets 00:00 UTC), 10 ms CPU per request (time waiting on `fetch` does not count), 50 subrequests, 128 MB memory, 6 open connections waiting for headers |
| Commercial use | The limits page states no non-commercial restriction (unlike Vercel Hobby); re-check Cloudflare's terms before monetizing (not re-verified) |

**Vercel Hobby (alternative, not re-verified 2026-10-28; not used)** —
<https://vercel.com/docs/plans/hobby> (page dated 2026-09-14)

| Limit | Value |
|---|---|
| Use | **Personal, non-commercial only** (fair-use guidelines) |
| Fast Data Transfer | 100 GB / month |
| Fast Origin Transfer | 10 GB / month |
| CDN requests | 1,000,000 / month |
| Function invocations | 1,000,000 / month (4 active CPU-hrs) |
| Deployments | 100 / day |
| Over-limit behaviour | Feature paused until 30 days pass |

Why Cloudflare Pages is recommended: no stated commercial-use restriction (matters if the
app is later published on the Play Store), unmetered static bandwidth, and a Pages Function can
proxy `/api/*` to Render so the refresh-token cookie stays first-party (see plan §6). With
Render's bandwidth now 5 GB/month, serving all static files from Pages is also what keeps the
backend inside its quota.

### GitHub Actions (CI/CD) — Official, verified 2026-10-28
Sources: <https://docs.github.com/en/billing/concepts/product-billing/github-actions>,
<https://docs.github.com/en/actions/how-tos/manage-workflow-runs/download-workflow-artifacts>,
<https://docs.github.com/en/actions/how-tos/monitor-workflows/use-workflow-run-logs>

Both repositories are **public** (checked via the GitHub API on 2026-10-07), so standard
GitHub-hosted runners are free with no minute cap. (Private repos on GitHub Free get 2,000
min/month and 500 MB artifact storage.) Scheduled workflows in public repos are disabled
after 60 days without repository activity.

**Artifacts of a public repo are effectively public.** Downloading artifacts and viewing run
logs needs only a signed-in GitHub account with read access, and every GitHub user has read
access to a public repository. **Never upload database backups (or anything with user data or
secrets) as workflow artifacts**; backups must go to storage only the owner can read.

---

## 2. AI providers

### Google Gemini API — Official (models, data policy) + Secondary (rate numbers), verified 2026-10-28
Sources: <https://ai.google.dev/gemini-api/docs/pricing> (updated 2026-10-27),
<https://ai.google.dev/gemini-api/docs/rate-limits> (updated 2026-10-26),
<https://ai.google.dev/gemini-api/docs/deprecations> (updated 2026-10-22),
<https://ai.google.dev/gemini-api/terms> (modified 2026-04-28)

- Text models with a free tier today: `gemini-3.8-flash`, `gemini-3.6-flash`,
  `gemini-3.5-flash-lite`, `gemini-3.1-flash-lite`, `gemini-3-flash-preview`,
  `gemini-2.5-flash`, `gemini-2.5-flash-lite`, plus `gemini-embedding-2` and Gemma 4.
  Pro models (and `gemini-4-argon`) have **no** free tier.
- Changed since 2026-10-07: `gemini-3.7-flash` and `gemini-3.5-flash` are gone from the
  pricing page. Their endpoints are not shut down but silently route to `gemini-3.8-flash` and
  `gemini-3.6-flash` respectively. The 2.5 models are now limited to projects that used them
  before; for a new project Google recommends `gemini-3.5-flash-lite` or `gemini-3.8-flash`.
- Google still publishes no free-tier rate table: limits are per project and shown in AI
  Studio (<https://aistudio.google.dev/rate-limit>). RPD resets at midnight Pacific time.
  Secondary sources report on the order of 10–15 RPM, ~250K TPM and 250–1,500 RPD for
  Flash/Flash-Lite. **Confirm in AI Studio before tuning `AGENT_USER_DAILY_CALLS` / `AGENT_GLOBAL_DAILY_CALLS`.**
- **Privacy: free-tier prompts and responses are "used to improve our products"** (paid tier:
  not used), and human reviewers may read them; the terms say not to submit sensitive,
  confidential or personal information to the free tier. (Users in the EEA, UK and Switzerland
  get the paid-tier data terms even on free access.) Consequences: PII redaction before every
  call, an explicit per-user "Allow AI processing" opt-in, and disclosure in the privacy policy
  and Play Store Data Safety form.
- No card required.

### Groq — Official (snippet) + Secondary, verified 2026-10-28
Sources: <https://console.groq.com/docs/rate-limits>, <https://console.groq.com/docs/deprecations>,
<https://console.groq.com/docs/your-data> (all block automated fetch; figures confirmed through
search snippets of those pages and dated secondary articles)

| Model (free plan) | RPM | RPD | TPM | TPD |
|---|---|---|---|---|
| `openai/gpt-oss-20b` | 30 | 1,000 | 8,000 | 200,000 |
| `openai/gpt-oss-120b` | 30 | 1,000 | 8,000 | 200,000 |
| `qwen/qwen3.8-27b` (secondary) | 30 | 1,000 | 8,000 | 200,000 |

- Limits apply per organization, not per key. TPD usually binds before RPD.
- `qwen/qwen3.6-27b` was **shut down on 2026-09-14** (replacement `qwen/qwen3.8-27b`), and
  `llama-3.1-8b-instant` / `llama-3.3-70b-versatile` were **removed on 2026-08-16** — concrete
  examples of why model names must be config. No retirement is announced for the gpt-oss models.
- Data: by default Groq does not retain inference requests (logs kept up to 30 days for
  reliability and abuse monitoring), and its Services Agreement bars using inputs/outputs for
  training without permission (Official (snippet)).
- OpenAI-compatible chat-completions API with JSON mode, so the provider adapter is small.
- No card required for the free plan (the "Developer" tier needs one — not used).

### Optional future providers (not planned for implementation; not re-verified)
The `LLMProvider` interface makes these one-file additions if Gemini/Groq limits tighten:

| Provider | Free allowance (secondary) | Note |
|---|---|---|
| OpenRouter `:free` models | 20 RPM, 50 requests/day without a credit purchase | Too small as primary |
| Cerebras | ~30 RPM, ~1M tokens/day | Verify before use |

### Rule-based fallback
Runs in-process (regex + per-language dictionaries). Zero cost, zero quota. Used when the
provider chain is exhausted, the user has not opted in to AI, or the per-user daily quota
(`AGENT_USER_DAILY_CALLS`) is used up.

---

## 3. Browser capabilities (free, client-side)

### Web Speech API (voice capture) — not re-verified
- Supported: Chrome/Chromium (desktop + Android), Edge, Safari 14.1+ (macOS) / 14.5+ (iOS).
- **Not supported in Firefox** (disabled by default, never shipped) → typed input fallback.
- Chrome sends audio to Google's servers; Edge to Microsoft's. No raw audio ever reaches
  our backend, but this must be disclosed in the privacy policy.
- `ta-IN` and `hi-IN` work with the cloud recognizers; on-device recognition
  (`processLocally`) does **not** list Tamil or Hindi yet → voice requires a network
  connection; offline mode falls back to typed entry queued for sync.

### SpeechSynthesis (optional voice read-back) — not re-verified
- Built into browsers.
- Whether Tamil and Hindi voices are available depends on the voices installed on the device.

### Web Push (reminders, Phase 11) — not re-verified
- Uses the standard Push API with VAPID keys we generate ourselves (`scripts/gen_vapid_keys.py`).
- Delivery goes through each browser's own push service. No account, key or quota is needed.
- **iOS/iPadOS** only delivers Web Push to PWAs installed on the home screen (iOS 16.4+).
- A push has to be *sent* while the backend is awake. Render free sleeps, so a GitHub Actions
  cron job wakes it (see below). Delivery is therefore best effort; in-app reminders are the
  reliable channel.

### GitHub Actions scheduled workflows (reminder dispatch, recurring entries) — Official, verified 2026-10-28
Source: <https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows>
- Free on public repos.
- The shortest interval is 5 minutes. Runs can be delayed under high load, and the start of
  every hour is a known high-load time (so avoid `0 * * * *`-style minute 0 schedules).
- Scheduled workflows run only on the default branch; cron is UTC unless the optional
  `timezone` key (IANA name) is set.
- **Scheduled workflows in public repos are disabled after 60 days without repository
  activity.** The deployment runbook will say how to re-enable them.
- Each run wakes Render and uses instance hours. At 30-minute cron intervals a single service
  still fits in 750 h/month, because 24 h × 31 days = 744 h at most.

---

## 4. Supporting APIs

### Frankfurter (exchange rates) — Official, verified 2026-10-28
Source: <https://frankfurter.dev/>
- Free, **no API key**, no quotas (abuse rate-limiting only), daily updates, history to 1948,
  228 currencies from 106 central banks and official sources.
- Base host `https://api.frankfurter.dev`. **v2 is now current**; v1 is "deprecated in favor of
  v2, but remains available indefinitely". Both verified live on 2026-10-28:
  - v1: `GET https://api.frankfurter.dev/v1/latest?base=USD&symbols=INR` →
    `{"base":"USD","date":…,"rates":{"INR":…}}`
  - v2: `GET https://api.frankfurter.dev/v2/rates?base=USD&quotes=INR` →
    `[{"date":…,"base":"USD","quote":"INR","rate":…}]` (v2 uses `quotes`; `symbols` returns 422,
    and v2 rates are blended across providers by default)
- The current v1 `FX_PROVIDER_URL` keeps working; moving to v2 needs an adapter change because
  the parameter name and response shape differ.
- Cache rates in Mongo once per day per base currency (`FX_PROVIDER_URL`, `FX_CACHE_TTL_HOURS`).

### Transactional email (only if password reset / verification is enabled) — Secondary, not re-verified
Render blocks SMTP, so only HTTP-API providers work:

| Provider | Free allowance |
|---|---|
| Brevo | 300 emails/day |
| Resend | 100 emails/day, 3,000/month, 1 verified domain |

### Google Sign-In (optional OAuth) — free; ID-token flow needs only a client ID
(`AUTH_GOOGLE_CLIENT_ID`), no client secret on the backend. CSP and COOP requirements are in
"Deployment-relevant answers" below (Official, verified 2026-10-28).

---

## 5. Capacity estimate for Atlas M0 (0.5 GB)

| Item | Approx. size |
|---|---|
| Expense document (with indexes) | ~0.8 KB |
| Revision document (diff + snapshot) | ~1 KB |
| 1 user × 10 entries/day × 1 year × ~1.5 revisions | ~3,650 expenses ≈ 3 MB + 5.5 MB revisions ≈ **8.5 MB/user/year** |

≈ 40–50 active user-years fit comfortably before cleanup is needed. TTL collections
(LLM cache, AI usage, transcripts, idempotency keys, rate-limit windows, audit beyond
retention) keep everything else bounded. Admin console shows live `dbStats` usage.

---

## 6. Deployment-relevant answers (verified 2026-10-28)

### Render Blueprint (`render.yaml`) on the free plan — Official
Sources: <https://render.com/docs/blueprint-spec>, <https://render.com/docs/health-checks>,
<https://render.com/docs/deploys>, <https://render.com/docs/free>
- `plan: free` is valid for a web service (0.1 CPU / 512 MB); `runtime: docker` is valid.
  `dockerfilePath` defaults to `./Dockerfile` and `dockerContext` to the repo root (both
  relative to the repo root). `region: singapore` is accepted and cannot be changed later.
- `healthCheckPath` (web services only, must start with `/`): no plan restriction stated. An
  HTTP check passes on 2xx/3xx within 5 s. A deploy is cancelled if the new instance is not
  healthy within 15 minutes; at runtime, 15 s of failures stops routing and 60 s restarts it.
- `autoDeployTrigger`: `commit` (default for new services), `checksPass` (deploy only after the
  branch's CI checks pass), or `off`. `autoDeploy` is deprecated (`true` → `commit`,
  `false` → `off`); if both are set, `autoDeployTrigger` wins.
- `envVars` with `sync: false`: Render asks for the value only when the Blueprint is first
  created and ignores it on later syncs (set or change it in the dashboard); such vars are not
  copied to preview environments and cannot live in environment groups.
- `envVars` with `generateValue: true`: Render creates a random base64-encoded 256-bit value,
  only if the variable does not already exist. No free-plan restriction is stated for either.
- Not usable on free: `preDeployCommand` (paid instances only), persistent `disk`, more than
  one instance.

### Cloudflare Pages Functions, `_headers`, SPA, Node — Official
Sources: <https://developers.cloudflare.com/pages/functions/routing/>,
<https://developers.cloudflare.com/pages/configuration/headers/>,
<https://developers.cloudflare.com/pages/configuration/serving-pages/>,
<https://developers.cloudflare.com/pages/configuration/build-image/>,
<https://developers.cloudflare.com/pages/functions/wrangler-configuration/>,
<https://developers.cloudflare.com/workers/runtime-apis/nodejs/>,
<https://developers.cloudflare.com/workers/platform/limits/>
- Routing: `functions/api/[[path]].ts` matches `/api/<seg>` at any depth (`/api/a/b/c`), and
  `context.params.path` is an **array** of segments (`[path]` would be one segment, a string).
  Whether bare `/api` matches is not documented, so the frontend only calls `/api/...` subpaths.
  If no Function matches, the request falls through to static assets. Pages auto-generates
  `_routes.json` when `functions/` exists; a manual one (≥1 include rule, ≤100 rules, ≤100 chars
  each) can limit invocations to `/api/*` so static requests never touch the Functions quota.
- `_headers` applies **only to static assets**: "Custom headers defined in the `_headers` file
  are not applied to responses generated by Pages Functions." Headers for proxied API responses
  (CORS, `Cache-Control: no-store`, etc.) must be set on the Function's `Response`.
- SPA fallback: with no top-level `404.html`, Pages assumes a single-page app and serves the
  root (`/`) for any path that does not match a static asset. `/page.html` is redirected to
  `/page`.
- Default caching headers on static assets: `Cache-Control: public, max-age=0, must-revalidate`
  plus `ETag` (304 on `If-None-Match`). Override per path in `_headers` (e.g. long
  `immutable` caching for hashed `/assets/*`). Pages also adds `Access-Control-Allow-Origin: *`,
  `Referrer-Policy: strict-origin-when-cross-origin` and `X-Content-Type-Options: nosniff`.
- Node: build image v3 defaults to Node 22.23.4. Select another with the `NODE_VERSION`
  environment variable or a `.node-version` / `.nvmrc` file in the project root.
  `package.json` `engines` is **not** read by v3. pnpm/Yarn are pinned with `PNPM_VERSION` /
  `YARN_VERSION` (lockfile version detection is not supported).
- Compatibility: a plain fetch proxy uses only Web-standard `fetch`/`Request`/`Response`, so it
  needs **no `nodejs_compat`** flag (that flag is for Node.js APIs; for compatibility dates on or
  after 2026-08-04 it is on by default anyway). Set an explicit compatibility date (dashboard,
  or `compatibility_date` in a wrangler file with `pages_build_output_dir`; once a wrangler file
  exists it is the source of truth for those settings).
- Time limits for the proxy: "There is no set time limit on individual subrequests", and
  HTTP-triggered Workers have no hard duration limit while the client stays connected; the
  10 ms CPU limit excludes time spent waiting on `fetch`. A ~1-minute Render cold start therefore
  fits. Cloudflare's 125 s Proxy Read Timeout (524) is documented for proxied origins, not for
  Worker subrequests; the frontend keeps its wake-up retry instead of one long request.

### Google Identity Services (Sign in with Google button) — Official
Source: <https://developers.google.com/identity/gsi/web/guides/get-google-api-clientid>
(updated 2026-04-06), <https://developers.google.com/identity/gsi/web/guides/fedcm-migration>
- CSP: `script-src https://accounts.google.com/gsi/client`;
  `frame-src https://accounts.google.com/gsi/`; `connect-src https://accounts.google.com/gsi/`
  (use the parent URL, not individual endpoints such as `/gsi/status`);
  `style-src https://accounts.google.com/gsi/style`; if `default-src` is the fallback for any of
  these, add `https://accounts.google.com/gsi/` to it.
- COOP: the button uses the non-FedCM popup flow unless `use_fedcm_for_button` is enabled. With
  FedCM disabled, Google says COOP "may need to be adjusted to same-origin and include
  same-origin-allow-popups", i.e. **`Cross-Origin-Opener-Policy: same-origin-allow-popups`**;
  plain `same-origin` breaks window communication (blank popup). With FedCM enabled no COOP
  change is needed.
- Referrer: `strict-origin-when-cross-origin` is recommended (Pages already sends it);
  `no-referrer-when-downgrade` only for http://localhost testing.
- These go in `_headers` for the static HTML, which is correct because `_headers` does not
  apply to Function responses.
