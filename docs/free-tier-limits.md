# Free-tier limits (verified 2026-10-07)

Every external service Tech-Toppings depends on, with the limits that shape the design.
Limits change often (Groq removed its Llama models from the free tier on 2026-08-16; Gemini
model IDs churn every few months). For that reason **no limit, model name, or URL below is
hard-coded**: each one maps to an environment variable in `.env.example`.

Re-verify this file before Phase 12 (deployment) and whenever a provider returns
unexpected `429`/`403` responses.

Legend: **Official** = read from the vendor's own docs or pricing page on the date above.
**Secondary** = vendor page was not machine-readable; figure taken from third-party sources
and must be confirmed in the vendor console.

---

## 1. Hosting and data

### MongoDB Atlas M0 (production database) — Official
Source: <https://www.mongodb.com/docs/atlas/reference/free-shared-limitations/>

| Limit | Value | Design impact |
|---|---|---|
| Storage | 0.5 GB | TTL indexes on caches, logs, transcripts, idempotency keys; compact documents; admin "DB size" widget; export script |
| Ops/second | 100 | Pool small (`MONGO_MAX_POOL_SIZE`), aggregate server-side, cache dashboard results |
| Connections | 500 | Single Render instance with pool of ~10 is far below |
| Data transfer | 10 GB in / 10 GB out per rolling 7 days | Paginate everything; no bulk polling |
| Collections | 500 total, 100 databases | One database, ~25 collections |
| Aggregation | Max 50 stages; `allowDiskUse` ignored; 32 MB in-memory sort | Keep pipelines short; always `$match` on indexed `user_id` first |
| Server-side JS | `$where`, map-reduce unsupported | Never used |
| Backups | None | `scripts/backup_atlas.py` (mongodump wrapper) + user data export |
| Idle pause | After 30 days with zero connections | Not a concern while the app is used; documented in runbook |
| Version | MongoDB 8.0 | Local dev DB is 8.0.26 — same major version |
| Transactions | Supported (3-node replica set) | **Local dev DB is standalone → no transactions.** Revision writes are designed to be transaction-free (ADR-0002, `docs/plan.md` §5) |

Local development uses `mongodb://localhost:27017/tech-toppings-v1` (no limits).

### Render free web service (backend) — Official + Secondary
Sources: <https://render.com/docs/free> (official); RAM/CPU/bandwidth/build minutes from
secondary sources (render.com/pricing was not machine-readable).

| Limit | Value | Design impact |
|---|---|---|
| Idle spin-down | After 15 min without inbound traffic | Frontend "waking up server" state (health probe + retry with backoff) |
| Cold start | ~1 minute | Same; keep startup work minimal (no heavy imports, lazy AI clients) |
| Instance hours | 750 / month per workspace (shared by all free services) | One backend service only |
| RAM / CPU | 512 MB / 0.1 CPU (secondary) | No in-process ML; LLM calls are remote; stream CSV exports |
| Bandwidth | 100 GB / month (secondary); overage needs a card or service is suspended | Gzip responses; paginate |
| Build minutes | 500 / month (secondary) | Deploy only from `main`, not every `develop` push |
| Filesystem | Ephemeral; lost on deploy/restart/spin-down | **Never write files locally.** Imports are parsed in memory; logs go to stdout |
| SMTP | Ports 25/465/587 blocked | Email (if enabled) must use an HTTP API provider |
| Scaling | Single instance, no shell, no one-off jobs, no cron | In-memory state is a cache only; scheduled work runs lazily or via GitHub Actions cron |
| Docker | Supported on free instance type (secondary) | Deploy with the repo `Dockerfile` |
| Card | Not required to start | — |

### Frontend hosting — Official

**Cloudflare Pages (recommended)** — <https://developers.cloudflare.com/pages/platform/limits>

| Limit | Value |
|---|---|
| Builds | 500 / month, 1 concurrent, 20-minute timeout |
| Files | 20,000 per site, 25 MiB max per file |
| Static bandwidth | Unmetered |
| Projects / custom domains | 100 per account / 100 per project |
| Pages Functions | Count against the Workers free quota: 100,000 requests/day, 10 ms CPU per request, 50 subrequests (<https://developers.cloudflare.com/workers/platform/limits/>) |
| Commercial use | The limits page states no non-commercial restriction (unlike Vercel Hobby); re-check Cloudflare's terms before monetizing |

**Vercel Hobby (alternative)** — <https://vercel.com/docs/plans/hobby> (page dated 2026-09-14)

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
proxy `/api/*` to Render so the refresh-token cookie stays first-party (see plan §6).

### GitHub Actions (CI/CD) — Official
Source: <https://docs.github.com/en/billing/concepts/product-billing/github-actions>

Both repositories are **public** (checked via the GitHub API on 2026-10-07), so standard
GitHub-hosted runners are free with no minute cap. (Private repos on GitHub Free get 2,000
min/month and 500 MB artifact storage.) Scheduled workflows in public repos are disabled
after 60 days without repository activity.

---

## 2. AI providers

### Google Gemini API — Official (models, data policy) + Secondary (rate numbers)
Sources: <https://ai.google.dev/gemini-api/docs/pricing> (updated 2026-10-07),
<https://ai.google.dev/gemini-api/docs/rate-limits> (updated 2026-09-02)

- Models with a free tier today include `gemini-3.8-flash`, `gemini-3.7-flash`,
  `gemini-3.6-flash`, `gemini-3.5-flash`, `gemini-3.5-flash-lite`, `gemini-3.1-flash-lite`,
  `gemini-2.5-flash`, `gemini-2.5-flash-lite`, `gemini-3-flash-preview`, and
  `gemini-embedding-2`. Pro models have **no** free tier.
- Google no longer publishes a public free-tier rate table: limits are per project and shown
  in AI Studio (<https://aistudio.google.dev/rate-limit>). Secondary sources report on the
  order of 10–15 RPM, ~250K TPM and 250–1,500 RPD for Flash/Flash-Lite. **Confirm in AI
  Studio before setting `AI_GEMINI_RPM` / `AI_GEMINI_RPD`.**
- **Privacy: free-tier prompts and responses are "used to improve our products"** (paid tier:
  not used). Consequences: PII redaction before every call, an explicit per-user "Allow AI
  processing" opt-in, and disclosure in the privacy policy and Play Store Data Safety form.
- No card required.

### Groq — Official (via docs search) + Secondary
Sources: <https://console.groq.com/docs/rate-limits> (blocks automated fetch; figures
confirmed through search snippets of that page and a dated secondary article)

| Model (free plan) | RPM | RPD | TPM | TPD |
|---|---|---|---|---|
| `openai/gpt-oss-20b` | 30 | 1,000 | 8,000 | 200,000 |
| `openai/gpt-oss-120b` | 30 | 1,000 | 8,000 | 200,000 |
| `qwen/qwen3.6-27b`, `qwen/qwen3.8-27b` (secondary) | 30 | 1,000 | 8,000 | 200,000 |

- Limits apply per organization, not per key.
- `llama-3.1-8b-instant` and `llama-3.3-70b-versatile` were **removed from the free tier on
  2026-08-16** (secondary) — a concrete example of why model names must be config.
- OpenAI-compatible chat-completions API with JSON mode, so the provider adapter is small.
- No card required for the free plan (the "Developer" tier needs one — not used).

### Optional future providers (not planned for implementation)
The `LLMProvider` interface makes these one-file additions if Gemini/Groq limits tighten:

| Provider | Free allowance (secondary) | Note |
|---|---|---|
| OpenRouter `:free` models | 20 RPM, 50 requests/day without a credit purchase | Too small as primary |
| Cerebras | ~30 RPM, ~1M tokens/day | Verify before use |

### Rule-based fallback
Runs in-process (regex + per-language dictionaries). Zero cost, zero quota. Used when the
provider chain is exhausted, the user has not opted in to AI, or the per-user daily quota
(`AI_DAILY_QUOTA_PER_USER`) is used up.

---

## 3. Browser capabilities (free, client-side)

### Web Speech API (voice capture)
- Supported: Chrome/Chromium (desktop + Android), Edge, Safari 14.1+ (macOS) / 14.5+ (iOS).
- **Not supported in Firefox** (disabled by default, never shipped) → typed input fallback.
- Chrome sends audio to Google's servers; Edge to Microsoft's. No raw audio ever reaches
  our backend, but this must be disclosed in the privacy policy.
- `ta-IN` and `hi-IN` work with the cloud recognizers; on-device recognition
  (`processLocally`) does **not** list Tamil or Hindi yet → voice requires a network
  connection; offline mode falls back to typed entry queued for sync.

### SpeechSynthesis (optional voice read-back)
- Built into browsers.
- Whether Tamil and Hindi voices are available depends on the voices installed on the device.

### Web Push (reminders, Phase 11)
- Uses the standard Push API with VAPID keys we generate ourselves (`scripts/gen_vapid_keys.py`).
- Delivery goes through each browser's own push service. No account, key or quota is needed.
- **iOS/iPadOS** only delivers Web Push to PWAs installed on the home screen (iOS 16.4+).
- A push has to be *sent* while the backend is awake. Render free sleeps, so a GitHub Actions
  cron job wakes it (see below). Delivery is therefore best effort; in-app reminders are the
  reliable channel.

### GitHub Actions scheduled workflows (reminder dispatch, recurring entries)
- Free on public repos.
- The shortest interval is 5 minutes, and runs can be delayed when GitHub is busy.
- **Scheduled workflows in public repos are disabled after 60 days without repository
  activity.** The deployment runbook will say how to re-enable them.
- Each run wakes Render and uses instance hours. At 30-minute cron intervals a single service
  still fits in 750 h/month, because 24 h × 31 days = 744 h at most.

---

## 4. Supporting APIs

### Frankfurter (exchange rates) — Official
Source: <https://frankfurter.dev/>
- Free, **no API key**, no quotas (abuse rate-limiting only), daily updates, history to 1948.
- INR verified live on 2026-10-07: `GET https://api.frankfurter.dev/v1/latest?base=USD&symbols=INR`.
- Cache rates in Mongo once per day per base currency (`FX_PROVIDER_URL`, `FX_CACHE_TTL_HOURS`).

### Transactional email (only if password reset / verification is enabled) — Secondary
Render blocks SMTP, so only HTTP-API providers work:

| Provider | Free allowance |
|---|---|
| Brevo | 300 emails/day |
| Resend | 100 emails/day, 3,000/month, 1 verified domain |

### Google Sign-In (optional OAuth) — free; ID-token flow needs only a client ID
(`GOOGLE_OAUTH_CLIENT_ID`), no client secret on the backend.

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
