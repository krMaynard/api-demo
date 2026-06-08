# Going live with the DSA VLOP Transparency site + API

The application is **code-complete for production**. Going live is now mostly
*provisioning and configuration*, not engineering: stand up the infrastructure,
wire the secrets, point a domain at it, and flip auth into production mode.

This doc is the **go-live checklist and the decisions behind it**. The exact
shell commands live in `README.md` → **Deploying** (Cloud Run, continuous
deployment, custom domain) — this file deliberately doesn't duplicate them so
they can't drift. Read this to know *what* has to happen and *why*; read the
README to run it.

---

## What's already done (no code changes required)

| Area | What shipped |
|------|-------------|
| Single combined site | Dashboard at `/`, JSON API under `/api/*`, portal at `/portal` — one origin, no CORS needed |
| Config from env | Every knob reads from the environment with safe defaults (`.env.example` documents all of them) |
| Authentication | **Google sign-in** (GIS/FedCM) → `POST /api/auth/google`, admin-approval gate (`ADMIN_EMAILS`), revocable session keys. Hard-coded demo keys + open `/api/portal/register` are gated behind `ALLOW_DEMO_KEYS` (turn off in prod) |
| Persistent state | Redis-backed job / session / registration / issued-key stores when `REDIS_URL` is set; in-memory fallback for a single instance |
| Self-contained image | Dockerfile seeds `demo.db` at build time from the vendored snapshot, runs uvicorn on `$PORT` as non-root — no external data source at runtime |
| Cloud Run manifest | `service.yaml` (Knative) with prod env + startup/liveness probes |
| Continuous deployment | `.github/workflows/deploy.yml` — build → push → deploy *no-traffic* revision → smoke-test `/readyz` → promote, via Workload Identity Federation. Skips cleanly until GCP is configured |
| CI gate | `pyflakes` + `mypy` + 113 `pytest` tests on every PR/push (Python 3.11 & 3.12), hermetic — no Redis/server/DB needed |
| Health & readiness | `GET /healthz` (liveness), `GET /readyz` (DB check), `GET /version` (commit SHA) |
| Rate limiting | Per-key throttle on `POST /api/query`; per-IP limits on the public `/api/explore` and `/api/ask`; portal-registration limits — all `429` + `Retry-After` |
| Structured logging | JSON logs by default, per-request `request_id` (`X-Request-ID`), job lifecycle events. Keys never logged |
| Metrics | Prometheus at `GET /metrics` (request + job metrics, route-template labels) |
| Secure downloads | HMAC-signed, key-less `download_urls` (`DOWNLOAD_URL_SECRET`) |
| Webhook callbacks | Optional `callback_url`, HMAC-signed, retried, SSRF-guarded |
| Browser hardening | Per-page CSP (`script-src 'self'` + inline-script hash), HSTS, `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`, `Permissions-Policy`; Chart.js vendored same-origin |
| Accessibility | Skip links, landmarks, focus rings, live regions, screen-reader data tables, `prefers-reduced-motion` |

---

## What you must do to go live

These are ordered. Run the exact commands from `README.md` → **Deploying**; the
notes here are the decisions and the *why*.

### 1. Pick a host

**Cloud Run is the wired-up path** — `service.yaml` + the CD workflow target it,
so it's the lowest-effort route to a running, auto-deploying service. Railway /
Fly.io / a VPS also work (see the table at the bottom); if you pick one of those
you'll skip the GCP-specific steps but still do everything else below.

### 2. Provision the infrastructure

- **GCP project + Artifact Registry repo** for the image (Cloud Run path).
- **Redis** — required only if you'll run **more than one instance** (Cloud Run
  `maxScale > 1`, or any horizontal scaling). With a single instance the
  in-memory stores are fine, but jobs/sessions reset on every revision. Managed
  options: Upstash (serverless, pay-per-request — matches `.env.example`) or GCP
  Memorystore. Store the connection string as a secret, not an env literal.

### 3. Create the production secrets

- **`DOWNLOAD_URL_SECRET`** — a stable random value (`openssl rand -hex 32`).
  Must be stable so signed download links survive restarts and validate across
  instances. Put it in Secret Manager.
- **`REDIS_URL`** — the connection string from step 2, in Secret Manager.

### 4. Set up Google sign-in (the production auth path)

- Create an **OAuth 2.0 Web client ID** in the GCP console.
- Add the Cloud Run URL (and later your custom domain) to the client's
  **Authorized JavaScript origins**.
- Set `GOOGLE_CLIENT_ID` to that client ID.
- Set `ADMIN_EMAILS` to your address (comma-separated for several) — the first
  admin is auto-approved and approves everyone else from `/portal`.

### 5. Flip into production mode

In `service.yaml` (or your host's env):

- `ALLOW_DEMO_KEYS=0` — disables the hard-coded `alice`/`bob` keys and the open
  `/api/portal/register` endpoint, so **only Google sign-in works**.
- `LOG_FORMAT=json`.
- `PUBLIC_BASE_URL=https://<your-domain>` — makes callback/download links
  absolute.
- `ALLOWED_ORIGINS` — leave empty (the bundled portal is same-origin). Set it
  only if a *separate* front-end origin will call the API cross-origin.
- *(Optional)* `ANTHROPIC_API_KEY` — enables the natural-language "Ask" box
  (`POST /api/ask`). The feature is off until this is set. It's IP-rate-limited
  and goes through the same `compile_query` trust boundary as everything else.

### 6. First deploy, then wire continuous deployment

- Do the **first** deploy with `service.yaml` — it sets env/secrets/scaling/probes.
- Then configure the CD workflow (Workload Identity pool + provider + deployer
  service account + the `GCP_*` repo variables). After that, every push to
  `main` ships a new revision and preserves the `service.yaml` config.
- Make the service public (`run.invoker` → `allUsers`) — it's a public
  transparency dashboard. Omit for an IAM-gated internal service (then the CD
  smoke test needs an identity-token header).

### 7. Map a custom domain

- Create the Cloud Run domain mapping and add the DNS records at your registrar.
- **Add the domain to the OAuth client's Authorized JavaScript origins** (else
  Google sign-in breaks on the real domain).
- Update `PUBLIC_BASE_URL` to the custom domain.
- The domain must be a real, registrable public TLD (`.org`, `.dev`, a
  subdomain of one) — non-delegated names won't resolve.

### 8. Verify before announcing

- `GET /readyz` returns 200 (DB opens).
- `GET /version` shows the deployed commit SHA.
- Sign in at `/portal` with an `ADMIN_EMAILS` account; approve a test account.
- A demo key is rejected (confirms `ALLOW_DEMO_KEYS=0` took effect).
- Submit a query end-to-end (`POST /api/query` → poll → result + signed download).
- *(If on Cloud Run with `minScale: 0`)* expect a cold-start delay on the first
  request; set `minScale: 1` if polling UX matters.

---

## Go-live checklist

- [ ] Host chosen (Cloud Run wired by default)
- [ ] GCP project + Artifact Registry repo created
- [ ] Redis provisioned (if running > 1 instance) and stored as a secret
- [ ] `DOWNLOAD_URL_SECRET` generated and stored in Secret Manager
- [ ] OAuth Web client created; Cloud Run URL added to Authorized origins
- [ ] `GOOGLE_CLIENT_ID` + `ADMIN_EMAILS` set
- [ ] `ALLOW_DEMO_KEYS=0`, `LOG_FORMAT=json`, `PUBLIC_BASE_URL` set
- [ ] First deploy via `service.yaml` succeeded; `/readyz` green
- [ ] CD workflow configured (WIF + repo variables); push-to-`main` deploys
- [ ] Custom domain mapped; domain added to OAuth origins; `PUBLIC_BASE_URL` updated
- [ ] Dataset snapshot current (`scripts/refresh-dataset.sh` if stale) and image rebuilt
- [ ] End-to-end verification done (sign-in, approval, query, download)

---

## Optional hardening — only when you need it

Everything above gets you live and safe for a public transparency dataset. The
items below are genuinely *not* built; reach for them only when the load or
threat model demands it.

| When | Do this |
|------|---------|
| You issue long-lived API keys and need rotation | Back `API_KEYS_JSON` with a secret store loaded at startup + refreshed on a schedule, with per-key `expires_at` / `scopes` / `last_used_at`. (With Google sign-in as the primary path this is lower priority.) |
| Results regularly exceed ~1 MB | Offload large payloads to S3/GCS and return a pre-signed URL instead of buffering in Redis |
| Query volume outgrows one process | Move the in-process `ThreadPoolExecutor` to Celery workers scaled independently (`_execute_job` maps cleanly onto a Celery task) |
| Clients need to page large results | Add cursor pagination to `/api/jobs/{id}/result` (`limit` + `after`) |
| Webhooks must survive restarts | Put callback delivery on a durable queue; add per-caller callback-domain allowlisting; enforce SSRF protection at the network egress layer (closes the DNS-rebind race the app-level guard can't) |
| You need burst / multi-endpoint throttling | Add an edge limiter (Cloud Armor / API Gateway / nginx / `slowapi`) on top of the per-key limit |
| The API contract will change | Introduce `/api/v1` routing and deprecate old versions with a `Sunset` header |
| You switch to a **writable** DB (mounted volume, not the baked image) | Enable SQLite WAL (`PRAGMA journal_mode=WAL;`) once after seeding and make the DB dir writable. Not applicable to the default `mode=ro` baked image |

---

## Deployment options

| Option | Best for | Notes |
|--------|----------|-------|
| **GCP Cloud Run** | The wired-up path | Ships with `service.yaml` + CD workflow + self-seeding image. Set `minScale: 1` to avoid cold starts on polling |
| **Railway** | Fastest to ship elsewhere | Push-to-deploy, managed Redis add-on, automatic HTTPS |
| **Fly.io** | Low-latency / multi-region | Built-in secrets, automatic HTTPS, `fly redis create` |
| **AWS ECS + ElastiCache** | Existing AWS footprint | More ops overhead, more control |
| **Hetzner / VPS + Caddy** | Lowest cost, full control | Single server + Caddy for automatic Let's Encrypt TLS; fine until you need HA |

For Railway/Fly.io: push the repo, set the same env vars from step 5 in their
dashboard, provision a Redis add-on for `REDIS_URL`, and you get HTTPS + a domain
without the GCP-specific setup. Never expose uvicorn directly on 443 — always
terminate TLS at a proxy / load balancer / platform edge.
