# MarkeVita AI Series Studio

Admin panel and production backend for the content-agnostic **v0.3 series
engine** in [`../pipeline`](../pipeline).

MarkeVita is the design and production company that operates this studio. It is
not part of any series' fiction. The studio hosts any number of independent
series; the first is **Island of No Witnesses**.

```
Admin Panel  ──►  Backend API  ──►  Job queue / worker  ──►  v0.3 engine (unchanged)
                       │                                          │
                  Supabase                                Cloudflare R2
             (records, auth)                       (references, takes, masters)
```

The engine is used as a finished core, not rebuilt. The studio materialises the
panel's records into the series package the engine already consumes, runs its
stages in a worker, and projects the run state back into the database.

---

## Status

| Piece | State |
|---|---|
| Admin panel | Working — series, seasons, episodes, scripts, bible, references, takes, approvals, costs, jobs |
| Backend API | Working — every panel action is also a JSON route |
| Job queue and worker | Working — start, pause, resume, cancel; resumable and idempotent |
| Supabase | Schema and driver ready; the panel runs on a local driver until you configure it |
| Cloudflare R2 | Wired through the engine's existing adapter; without credentials media stays under `pipeline/runs/` |
| Providers | Configured by environment variable; all calls are mocked |
| Instagram publishing | Disabled |

**This build is mock-only.** No paid API call can be made from it, nothing is
published, and the public MarkeVita website at the repository root is untouched.

---

## Run it locally

From the repository root:

```bash
python3 -m venv .venv
.venv/bin/pip install -r pipeline/requirements.txt -r studio/requirements.txt
# ffmpeg must be on PATH (the assemble and QA stages use it)

cd studio
cp .env.example .env          # then set STUDIO_ADMIN_EMAIL and STUDIO_ADMIN_PASSWORD
../.venv/bin/python seed/island_of_no_witnesses.py    # optional: first series
../.venv/bin/python run.py
```

Open **http://127.0.0.1:8800** and sign in with the email and password from
`.env`.

The studio starts with `STUDIO_STORE=local`, which keeps records in
`.studio-data/` so the panel opens with no cloud setup. Point it at Supabase
whenever you are ready — see below.

---

## What the panel does

**Dashboard** — every series, running jobs, simulated spend, integration gaps.

**Series** — format, language, captions, production limits, and the immutable
visual-style sentence appended to every image and video prompt. Live package
validation: schema, continuity, knowledge ledger, cliffhanger, budget
projection. All of it free.

**Characters** — identity, camera-ready appearance, behaviour, immutable rules;
wardrobe variants (one full-body reference is generated per variant); voice
binding. A voice is bound by the **name** of the environment variable holding
its id — the id itself never reaches the database or the package.

**Locations** — description, lighting states, marks, and what must never move.
Plus continuity props.

**World** — relationships with allowed states, story secrets and who holds them,
and the knowledge state recorded after each episode. The engine simulates these
across scenes and episodes and rejects a brief where a character acts on a
secret nobody told them.

**Episode** — paste or upload a script, review the parsed scenes, run
production stage by stage, watch the engine log, review takes and select
between them, read the QA report, record a budget override, approve the
finished episode.

**References** — the reference pack for the current bible version, and its
approval gate. Change the bible and the approval is invalidated.

**Costs** — per episode and per call, reserved before submission and settled
after.

**Integrations** — Connected or Missing, the model selected, last test, last
error. Never a secret value.

---

## Script format

Paste a complete JSON brief, or this structured text:

```
SCENE sc01 | 8s | shore | night
CHARACTERS: lead_a
WARDROBE: lead_a=w_default
SHOT: medium shot | 50mm | slow push-in
ACTION: What visibly happens in one continuous shot.   (required)
IN:  state at the first frame
OUT: state at the last frame
PROPS: passenger_list=folded in jacket pocket
KNOWS:  lead_a needs secret_one
LEARNS: lead_b gains secret_one via overhears
REL: rel_a_b -> suspicious
CLIFFHANGER
lead_a (quiet, certain): A spoken line.
vo narrator: A voice-over line.
```

Clips are 4, 6 or 8 seconds; an episode is 12–18 clips totalling 90–120
seconds. A scene where two visible characters both speak is split automatically
into one sub-shot per speaker. Every script version is kept.

---

## Production, approvals and safety

Stages run in order: `intake · direction · references · keyframes · video ·
voice · lipsync · assemble · qa · deliver`. `publish` is deliberately excluded.

Three gates stop a run rather than letting it drift:

1. **References approval** — keyframes and video refuse to use an unapproved
   reference pack.
2. **Budget cap** — reaching it sets `needs_budget_override`. Continuing
   requires an override recorded with actor, time, old value, new value and
   reason. No agent can raise a limit by itself.
3. **Final approval** — recorded per episode. It does not publish anything.

Pausing stops at the next stage boundary. Resuming skips completed stages and
reuses finished takes, so no work — or money — is repeated. Takes are
immutable: a forced regeneration creates new takes and keeps the old ones.

Paid calls need `--live`, `PIPELINE_ALLOW_PAID=true` **and** an approved series,
and the studio refuses to start at all if `STUDIO_ALLOW_PAID` is true. Every
layer independently blocks spending.

---

## Supabase

Apply [`supabase/migrations/0001_studio.sql`](supabase/migrations/0001_studio.sql)
to your project, then set in `.env`:

```
STUDIO_STORE=supabase
SUPABASE_URL=…
SUPABASE_ANON_KEY=…
SUPABASE_SERVICE_ROLE_KEY=…
```

The backend uses the service role key server-side and bypasses RLS; the
policies in the migration exist so the tables stay safe if a browser ever
reaches them with a user token — administrators can read, nobody writes
directly.

### Supabase Auth

Sign-in uses Supabase Auth whenever `SUPABASE_URL` and `SUPABASE_ANON_KEY`
are set. This is **independent of `STUDIO_STORE`**: which database holds the
series has nothing to do with who may sign in. (An earlier build coupled the
two, so a degraded record store silently downgraded sign-in to the local
administrator — the bug behind "No administrator is configured" in
production.)

An authenticated user must also be an administrator: their email must appear
in `studio_admins`. To bootstrap the first one, set `STUDIO_ADMIN_EMAIL` to
that user's email; it is added to the allow-list on first sign-in. No local
password is needed — the password lives in Supabase.

**Password recovery** is built in: *Forgot password?* on the sign-in page
emails a link that opens `/studio/reset`, where the user sets a new password.
Both of Supabase's link formats are handled — `?token_hash=` is verified
server-side, and an implicit-flow `#access_token=` fragment is moved into the
form by the page. The page answers identically whether or not the address has
an account, so it cannot be used to enumerate users.

Two settings in the Supabase dashboard, **Authentication → URL Configuration**:

- **Site URL**: `https://markevita.com`
- **Redirect URLs**: add `https://markevita.com/studio/reset`

Without the second, Supabase refuses to send users back to the studio. The
link target is derived from the forwarded headers Netlify sends; set
`STUDIO_PUBLIC_URL=https://markevita.com` if a platform does not forward them.

### Render environment checklist

```
STUDIO_BASE_PATH=/studio
STUDIO_SESSION_SECRET=<fixed random value>      python -c "import secrets;print(secrets.token_hex(32))"
STUDIO_STORE=supabase
SUPABASE_URL=…
SUPABASE_ANON_KEY=…
SUPABASE_SERVICE_ROLE_KEY=…
STUDIO_ADMIN_EMAIL=<the first administrator's email>
STUDIO_ALLOW_PAID=false
```

The service prints a configuration report at boot and the sign-in page shows
any fault that would lock an operator out, so a wrong or missing variable is
visible rather than guessed at. `GET /healthz` reports the count.

Supabase stores records only. Media lives in R2; the database keeps object
keys, checksums and metadata.

---

## Cloudflare R2

Set `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`,
`R2_S3_ENDPOINT` and `R2_PUBLIC_BASE_URL`. The engine's existing adapter then
uploads references, takes, voices, subtitles, masters and manifests, keeping
everything private; only the `/public/` namespace is ever exposed, and nothing
is promoted there without approval. Without R2 configured the media stays under
`pipeline/runs/` and the panel says so.

---

## Deploying to markevita.com/studio

The studio is a stateful Python service with background workers and ffmpeg, so
it **cannot run on Netlify's static hosting**. The public site stays on Netlify
exactly as it is; the studio runs on its own host and Netlify proxies the
`/studio` path to it.

**1. Host the service.** Any platform that runs a container works — Fly.io,
Railway, Render, Cloudflare Containers, a VPS:

```bash
docker build -f studio/Dockerfile -t markevita-studio .   # from the repo root
```

The image sets `STUDIO_BASE_PATH=/studio` already. Supply the rest of `.env`
as the platform's environment variables — secrets never go in the image.

**2. Point Netlify at it.** In [`../_redirects`](../_redirects), uncomment the
rule and replace the host:

```
/studio/*  https://your-backend-host/:splat  200
/*  /index.html  200
```

The `/studio` rule must stay **above** the catch-all, which would otherwise
swallow it. Netlify strips the prefix, the service answers at its own root, and
`STUDIO_BASE_PATH` puts the prefix back into every link, redirect and cookie.

**3. Persist state.** The container filesystem is ephemeral. Before deploying,
switch `STUDIO_STORE=supabase` and configure R2 — otherwise records and media
are lost on every restart.

Two caveats worth knowing before you rely on this:

- Run **one** worker. Production jobs are in-process background threads, so a
  second worker would keep its own job table. Scaling out means moving the
  queue into Supabase first.
- The admin panel is an internal tool. On a public domain it is protected only
  by the sign-in; consider IP restriction or an access proxy in front of it.

---

## Tests

```bash
cd studio && ../.venv/bin/python -m pytest tests -q -m "not slow"   # 19 tests, instant
cd studio && ../.venv/bin/python -m pytest tests -q -m slow         # full mock run, ~2.5 min
cd pipeline && ../.venv/bin/python -m pytest tests -q               # the engine's own 23 tests
```

The slow test drives the studio exactly as the panel does: seed a series,
validate it, run until the references gate blocks it, approve, run to
completion, check the real MP4 and QA report, approve the episode, and confirm
a re-run spends nothing more.

---

## API

Every panel action is also JSON, under `/api`, with the same administrator
session:

```
GET  /api/series                                         list series
POST /api/series/{id}/validate                           free validation
GET  /api/series/{id}/episodes/{ep}                      episode, scenes, runtime, takes, costs
POST /api/series/{id}/episodes/{ep}/script               save a script
POST /api/series/{id}/episodes/{ep}/start|pause|resume|cancel
POST /api/series/{id}/approve/references
POST /api/series/{id}/episodes/{ep}/approve/publish
POST /api/series/{id}/episodes/{ep}/override
POST /api/series/{id}/takes/{take}/select
GET  /api/costs   ·   GET /api/history   ·   GET /api/integrations
```

Interactive documentation is at `/docs`.
