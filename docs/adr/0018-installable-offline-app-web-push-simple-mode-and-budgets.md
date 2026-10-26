# ADR-0018: Installable offline app, Web Push, Simple mode and budgets

- **Status:** Accepted
- **Date:** 2026-10-23
- **Deciders:** project owner (Phase 11)

## Context

Phase 11 makes the app installable and usable offline, pushes reminders to a closed app, adds
Simple mode (U8) and an onboarding tour, completes en/ta/hi, and sets budgets: Lighthouse
(mobile) Performance ≥ 90, Accessibility ≥ 95 and Best Practices ≥ 95, first-load JavaScript
≤ 200 KB gzipped, and axe clean.

The constraints still hold: free tier only, no new dependencies, everything configurable,
enforcement on the backend.

## Decision

### A hand-written service worker (instead of vite-plugin-pwa)

The plan named `vite-plugin-pwa`. The owner chose a small hand-written worker instead (D23):

- `src/sw/service-worker.ts` is built by a Vite plugin (`vite-plugins/pwa.ts`) into one script,
  `sw.js`. The plugin also writes `manifest.webmanifest` (named from `VITE_APP_NAME`) and hands
  the worker the list of the build's files plus a version hashed from their contents.
- **Pages** are fetched from the network, with the cached shell when it's down. The shell is
  kept under the base URL (`/`), not `index.html`: hosts such as Cloudflare Pages redirect
  `/index.html` to `/`, and a navigation refuses a redirected answer. Any precached file that
  did come through a redirect is stored as a plain copy.
- **The build's files** come from the cache (their names change with their content; `Vary` is
  ignored for them): this build's first, then the previous build's, then the network. The
  precache leaves out source maps, `sw.js`, dot files and folders (`.vite/manifest.json`) and
  host files (`_headers`, `_redirects`, `_routes.json`). **The API** is never cached: the cache
  holds nobody's data, and nothing is stored at run time.
- **Updates** wait. A new version shows "A new version is ready — Reload", and only then takes
  over. Reload in one tab moves every tab to it; the others are not reloaded under the user and
  keep the banner, whose Reload then just reloads (it can always be pressed again, for example
  after the user chose to stay on a form with unsaved input). The previous build's cache is kept,
  so a tab still on it can load the pages it hasn't opened yet.
- **Install**: the browser's offer is kept for the app's own Install button and palette command.
- Icons are rendered from `favicon.svg` with Playwright's Chromium (`npm run gen:icons`), with a
  white-on-transparent badge for Android's status bar.

### Offline outbox (ADR-0010, implemented)

- `core/offline` keeps an outbox in IndexedDB (memory, for the open page only, where IndexedDB
  is missing or won't open). A write counts once its transaction commits. Each item has a kind,
  the account that made it, a label, and an id that is its Idempotency-Key.
- Changes never pause offline: mutations run with `networkMode: 'always'`, so a save with no
  network fails at once and goes to the outbox (or shows an error), instead of waiting in memory.
- A module declares a sender per kind (`ModuleDefinition.outbox`), loaded only when such an item
  waits. Expenses has two:
  - `expenses.entry`: a new entry saved with no network is sent as it was;
  - `expenses.capture`: words said or typed offline are read by the assistant once back
    online, and land as drafts to check. Their keys come from the item's id, so reading it
    twice adds nothing twice.
- Drafts are kept on the device per account (`core/offline/kept-lists`), so they survive a
  reload. Each change is made to the latest stored lists, and other tabs' changes are picked up,
  so two tabs never overwrite each other. A palette hand-over is taken once: a reload or Back
  doesn't add its drafts again.
- **Sending** happens in the page, not the worker: the access token lives only in the page, so
  there is no Background Sync. It runs when the app opens signed in, when the device is back
  online, and every `VITE_HEALTH_POLL_MS` while something waits, in one tab at a time (a Web
  Lock). Items go oldest first, and only while their account is the one signed in.
- **What waits, what is refused.** A network failure, 401, 408, 429, a 5xx or a 403 about the
  whole account (a password change due, a disabled account) is about the moment: the item and
  everything after it wait. Any other 4xx refuses that item: it is kept with its error code and
  shown, with Try again and Discard, and the rest go on. Nothing made offline is dropped unseen.
- **Unsaved input.** A form that holds input (a new entry, the quick-add box) asks before the
  page is left in the app (a link, the palette, a notification, Back), and the browser asks
  before the tab is closed.

### Web Push without a library

- `app/platform/push` implements message encryption (RFC 8291, `aes128gcm`) and VAPID
  (RFC 8292) on `cryptography` and PyJWT, both already installed. A test reproduces RFC 8291's
  worked example byte for byte.
- `PUSH_*` settings hold the key pair (`poe vapid-keys`) and a contact. Push is off until both
  are set. Startup checks that the public key is the private key's.
- **Subscriptions** are owned documents. Their keys are `PRIVATE_FIELDS`: exported without
  them, erased with the account. A browser has one subscription; subscribing it again moves it
  to whoever did, as new to them. Past `PUSH_MAX_SUBSCRIPTIONS`, the oldest of the others is let
  go.
- **Tied to a signed-in device.** A subscription records the session that made it (the access
  token's `sid`) and a fingerprint of the VAPID public key. A browser is pushed to only while
  that session is live and the key is current; otherwise it is dropped when next counted or sent
  to. Signing out, signing the device out from another, signing out everywhere and a password
  change delete it at once.
- **Allowed hosts.** The server only sends to known push services (`PUSH_ALLOWED_HOSTS`, plain
  lower-case host names). An endpoint is a URL the user supplies, so this stops the server being
  used to call arbitrary hosts. It must be `https`, printable ASCII, with no user name and no
  port but 443; the VAPID audience is its scheme and host only.
- **Reminders.** Telling a reminder makes its push owed (`push_at`) when the user has a browser.
  The `reminders_push` job, after `reminders_deliver` in the same catch-up, claims what is owed
  in one write, under a claim of its own held for `JOBS_LEASE_SECONDS`, and pushes it to every
  browser in one round, within `PUSH_TIMEOUT_SECONDS`, at most `PUSH_MAX_PARALLEL` pushes at a
  time. With push on, startup checks that the round leaves the job half a second
  (`PUSH_JOB_HEADROOM_SECONDS`) of `JOBS_LAZY_TIMEOUT_SECONDS` and `JOBS_RUN_TIMEOUT_SECONDS`
  for claiming and clearing. Each browser has its own, shorter deadline
  (`PUSH_DEVICE_TIMEOUT_SECONDS`), counted from when its push gets a slot: a push service that
  fails, refuses or is slower costs that one try, and the round still finishes. A browser that
  fails on the network is not tried again in that round, so it holds no slot another browser
  could use; recording when each browser last took a push is only a note, and its failure never
  undoes a round. Progress is per message: a
  reminder tried on every browser is done, even in a round cut short (a stall elsewhere, such as
  the database). The rest, and everything of a run killed (the job's time limit, a restart), keep
  their claim and are pushed again once it passes. Each claim counts a try, and a told round is
  tried at most `PUSH_MAX_TRIES` times; the inbox still has it after that. A repeat is tagged
  `reminder:{id}:{round}`, so it replaces the notification on the device. The inbox never waits
  on a push. A gone browser (404/410) is forgotten, and one browser that fails never stops the
  others.
- **Wording.** The worker words a push from a text pack the page writes into Cache Storage
  (notification wording, module names and links in the user's language). It uses the same
  `fill`/`withMoney`/`withDates` as the bell, so a push reads like the bell. Every push shows a
  notification, even with the app in front: Safari drops a subscription after a few pushes that
  show nothing. Open windows refresh their bell too. A click focuses an open app window and
  routes it in place (same-origin paths only); a new window opens only when none is open.
- **In the browser.** "On" means a subscription made with the server's current key. While
  signed in, the page registers it again once per session, which heals a browser the server let
  go (a password change, an eviction, another account on a shared browser). Signing out
  unsubscribes the browser and drops its text pack; if the server refuses a new subscription,
  the browser is unsubscribed again. Only a denied permission counts as blocked.

### Simple mode and the tour

- **Simple mode** is `ui_mode` on the account (it follows the person). It makes the root font
  18.75 % larger (everything in rem grows, tap targets too), drops the "More details" part of a
  new entry, and starts the home with a large microphone. What is said shows as it is heard and
  goes to the assistant once, with the final words, in the command palette (the same flow as
  typing there). A failed save of the setting is undone and said.
- **The tour** is five short steps, opened for an account made in the last 10 minutes. It is
  remembered as seen per account on the device, skippable in one press, and in the palette as
  "Show the tour again". Its code loads only when shown.

### Languages and budgets

- **Missing keys.** `src/core/i18n/completeness.test.ts` (`npm run i18n:check`, its own CI step)
  checks every text in en, ta and hi, for the shell, each module's pages and each manifest. It
  checks keys and `{{placeholders}}`, and Tamil or Hindi left in English.
- **Languages load on demand.** English ships with the first page; Tamil and Hindi load when
  chosen. The app renders once the first language is in (no flash of English). A failed
  download is retried; if it still fails, the app says so and shows English, with `lang` and
  the language picker saying English too, and choosing the language again retries.
- **The JavaScript budget** (`BUNDLE_BUDGET_KB`, default 200) is checked by `npm run budget`
  after every build, in `npm run check` and in CI. It counts what each first page needs before
  it can render: the entry and its imports, plus the route's own lazy chunk and its imports
  (read from Vite's build manifest). Getting under it:
  - the shared vendor chunk holds only what every page needs;
  - the startup config check no longer uses zod, and the sign-in form needs neither
    react-hook-form nor zod (same checks and messages);
  - Tamil and Hindi load on demand;
  - the shell's small animations (toasts, the navigation highlight, filter chips, the
    greeting, counting numbers) are CSS, so no animation component loads first;
  - the home loads its tile sections, the arrange board (prefetched on Customize) and the
    module details dialog as their own chunks.
- **The session hint.** A browser known to be signed out (a `tt-session` hint, no secret) opens
  without asking the server: one round trip and one 401 fewer. The hint is written only on a
  definite ending (sign-out, a 401 from refresh, account deletion); a missing hint still asks,
  so storage cleared by the browser never loses a valid session. A failed restore (offline, a
  5xx) leaves it, and the next start restores the session.
- **First paint.** `index.html` paints the header before the code arrives.
- **Accessibility** is swept by axe-core in the browser on every page, in light and dark
  (`e2e/smoke.spec.ts`), alongside the `vitest-axe` checks.

## Consequences

- No new dependencies on either side.
- Lighthouse (mobile) on the sign-in page, three runs each:
  - over HTTP/2, as Cloudflare Pages serves it: Performance 95–96, Accessibility 100, Best
    Practices 100;
  - over the local `vite preview` (HTTP/1.1, so the first-load files queue six at a time):
    Performance 75–77.
  Phase 12 measures the live URL.
- Push needs HTTPS, or localhost. On iPhone and iPad it works only for the app added to the Home
  Screen; the card says so. The Android check (a push from the cron job) is manual, after
  deployment.
- Changing the VAPID keys drops every subscription made with the old ones (`devices` goes to 0),
  so each browser turns notifications on again; the card shows them as off. A password change
  drops the other devices' subscriptions; the device that made it registers its own again.
- Drafts and the outbox stay on the device until sent or dismissed. Signing out keeps them, for
  the same account's next sign-in; deleting the account removes them, in every open tab. The
  privacy template says so.
