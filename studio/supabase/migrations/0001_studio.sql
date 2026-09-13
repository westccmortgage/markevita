-- ═══════════════════════════════════════════════════════════════════════════
-- MarkeVita AI Series Studio — Supabase schema.
--
-- Holds series/episode records, the bible, scripts, scenes, production jobs,
-- approvals, costs, knowledge state and generation history.
-- Media itself lives in Cloudflare R2; this schema stores only object keys,
-- checksums and metadata. No provider secret is ever stored here.
-- ═══════════════════════════════════════════════════════════════════════════

create extension if not exists "pgcrypto";

-- ── Administrators ─────────────────────────────────────────────────────────
-- Allow-list mapped onto Supabase Auth users. A signed-in auth user with no
-- row here is not an administrator.
create table if not exists studio_admins (
  id          uuid primary key default gen_random_uuid(),
  email       text not null unique,
  role        text not null default 'producer',   -- owner | producer | reviewer
  created_at  timestamptz not null default now()
);

-- ── Series / seasons / episodes ────────────────────────────────────────────
create table if not exists series (
  id                 text primary key,            -- series_id, ^[a-z0-9][a-z0-9_]{0,63}$
  title              text not null,
  logline            text not null default '',
  genre              text not null default '',
  language           text not null default 'en-US',
  format             jsonb not null default '{"aspect_ratio":"9:16","width":1080,"height":1920,"captions":"both"}'::jsonb,
  production_limits  jsonb not null default '{}'::jsonb,
  approval           jsonb not null default '{"status":"draft"}'::jsonb,
  style              jsonb not null default '{}'::jsonb,
  status             text not null default 'draft',
  bible_version      text,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now()
);

create table if not exists seasons (
  id             uuid primary key default gen_random_uuid(),
  series_id      text not null references series(id) on delete cascade,
  season_id      text not null,                   -- s01
  number         int  not null,
  title          text not null default '',
  arc            text not null default '',
  episode_order  jsonb not null default '[]'::jsonb,
  unique (series_id, season_id)
);

create table if not exists episodes (
  id             uuid primary key default gen_random_uuid(),
  series_id      text not null references series(id) on delete cascade,
  season_id      text not null default '',
  episode_id     text not null,                   -- s01e01, unique within series
  number         int  not null default 1,
  title          text not null default '',
  logline        text not null default '',
  status         text not null default 'draft',
  brief          jsonb not null default '{}'::jsonb,
  opening_state  jsonb not null default '{}'::jsonb,
  cliffhanger    jsonb not null default '{}'::jsonb,
  brief_hash     text,
  target_seconds int,
  budget_usd     numeric(10,2) not null default 50,
  spent_usd      numeric(10,2) not null default 0,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  unique (series_id, episode_id)
);

-- ── Scripts ────────────────────────────────────────────────────────────────
-- Pasted or uploaded source text, versioned. `parsed` holds the scene
-- structure extracted from it before it is written into `scenes`.
create table if not exists scripts (
  id          uuid primary key default gen_random_uuid(),
  series_id   text not null references series(id) on delete cascade,
  episode_id  text not null default '',
  version     int  not null default 1,
  source      text not null default 'paste',      -- paste | upload
  filename    text not null default '',
  content     text not null default '',
  parsed      jsonb not null default '{}'::jsonb,
  created_by  text not null default '',
  created_at  timestamptz not null default now()
);

-- ── Bible: characters, clothing, voices, locations, props ─────────────────
create table if not exists characters (
  id            uuid primary key default gen_random_uuid(),
  series_id     text not null references series(id) on delete cascade,
  character_id  text not null,
  name          text not null,
  visual        boolean not null default true,
  role          text not null default '',
  age           text not null default '',
  appearance    text not null default '',
  behavior      text not null default '',
  immutable     jsonb not null default '[]'::jsonb,
  props         jsonb not null default '[]'::jsonb,
  seed_assets   jsonb not null default '[]'::jsonb,
  updated_at    timestamptz not null default now(),
  unique (series_id, character_id)
);

-- Wardrobe variants. One full-body reference is generated per variant.
create table if not exists clothing (
  id            uuid primary key default gen_random_uuid(),
  series_id     text not null references series(id) on delete cascade,
  character_id  text not null,
  variant_id    text not null,
  is_default    boolean not null default false,
  description   text not null default '',
  immutable     jsonb not null default '[]'::jsonb,
  unique (series_id, character_id, variant_id)
);

-- Voice assignment. Stores the NAME of the environment variable holding the
-- provider voice id — never the voice id itself.
create table if not exists voices (
  id            uuid primary key default gen_random_uuid(),
  series_id     text not null references series(id) on delete cascade,
  character_id  text not null,
  provider      text not null default 'elevenlabs',
  voice_env     text not null default '',
  model_id      text not null default 'eleven_v3',
  language      text not null default 'en-US',
  style_notes   text not null default '',
  phone_fx      boolean not null default false,
  locked        boolean not null default false,
  unique (series_id, character_id)
);

create table if not exists locations (
  id               uuid primary key default gen_random_uuid(),
  series_id        text not null references series(id) on delete cascade,
  location_id      text not null,
  name             text not null,
  description      text not null default '',
  lighting_states  jsonb not null default '{"default":""}'::jsonb,
  marks            text not null default '',
  immutable        jsonb not null default '[]'::jsonb,
  seed_assets      jsonb not null default '[]'::jsonb,
  unique (series_id, location_id)
);

create table if not exists props (
  id           uuid primary key default gen_random_uuid(),
  series_id    text not null references series(id) on delete cascade,
  prop_id      text not null,
  description  text not null default '',
  unique (series_id, prop_id)
);

-- ── Relationships and secrets ─────────────────────────────────────────────
create table if not exists relationships (
  id              uuid primary key default gen_random_uuid(),
  series_id       text not null references series(id) on delete cascade,
  rel_id          text not null,
  a               text not null,
  b               text not null,
  type            text not null default '',
  state           text not null default '',
  is_public       boolean not null default false,
  allowed_states  jsonb not null default '[]'::jsonb,
  note            text not null default '',
  unique (series_id, rel_id)
);

-- Story secrets (plot knowledge). Not credentials.
create table if not exists secrets_bible (
  id               uuid primary key default gen_random_uuid(),
  series_id        text not null references series(id) on delete cascade,
  secret_id        text not null,
  description      text not null default '',
  holders_initial  jsonb not null default '[]'::jsonb,
  stakes           text not null default '',
  unique (series_id, secret_id)
);

-- Who knows what at the end of each episode; carried into the next episode.
create table if not exists knowledge_state (
  id           uuid primary key default gen_random_uuid(),
  series_id    text not null references series(id) on delete cascade,
  episode_id   text not null default '',
  kind         text not null default 'knowledge',  -- knowledge | relationship
  subject_id   text not null,                      -- secret_id or rel_id
  value        jsonb not null default '[]'::jsonb, -- holders array, or state string
  recorded_at  timestamptz not null default now(),
  unique (series_id, episode_id, kind, subject_id)
);

-- ── Scenes ─────────────────────────────────────────────────────────────────
create table if not exists scenes (
  id                    uuid primary key default gen_random_uuid(),
  series_id             text not null references series(id) on delete cascade,
  episode_id            text not null,
  scene_id              text not null,
  sequence              int  not null default 1,
  duration_seconds      int  not null default 6,
  location              text not null default '',
  lighting_state        text not null default 'default',
  characters_in_frame   jsonb not null default '[]'::jsonb,
  wardrobe              jsonb not null default '{}'::jsonb,
  action                text not null default '',
  dialogue              jsonb not null default '[]'::jsonb,
  shot_type             text not null default '',
  lens                  text not null default '',
  camera_motion         text not null default '',
  continuity_in         text not null default '',
  continuity_out        text not null default '',
  props                 jsonb not null default '[]'::jsonb,
  knowledge_required    jsonb not null default '[]'::jsonb,
  knowledge_gained      jsonb not null default '[]'::jsonb,
  relationship_changes  jsonb not null default '[]'::jsonb,
  is_cliffhanger        boolean not null default false,
  status                text not null default 'draft',
  qa                    jsonb not null default '{}'::jsonb,
  unique (series_id, episode_id, scene_id)
);

-- ── Generated media (keys + metadata only; bytes live in R2) ──────────────
create table if not exists reference_assets (
  id             uuid primary key default gen_random_uuid(),
  series_id      text not null references series(id) on delete cascade,
  bible_version  text not null default '',
  kind           text not null,                   -- character | location | prop
  owner_id       text not null,
  name           text not null default '',
  r2_key         text not null default '',
  checksum       text not null default '',
  approval       text not null default 'pending', -- pending | approved | rejected
  metadata       jsonb not null default '{}'::jsonb,
  created_at     timestamptz not null default now()
);

create table if not exists takes (
  id              uuid primary key default gen_random_uuid(),
  series_id       text not null references series(id) on delete cascade,
  episode_id      text not null default '',
  scene_id        text not null default '',
  take_id         text not null,
  stage           text not null default '',
  attempt         int  not null default 0,
  provider        text not null default '',
  endpoint        text not null default '',
  request_id      text not null default '',
  prompt          text not null default '',
  negative        text not null default '',
  params          jsonb not null default '{}'::jsonb,
  r2_key          text not null default '',
  checksum        text not null default '',
  duration_seconds numeric(8,2),
  estimated_usd   numeric(10,4) not null default 0,
  actual_usd      numeric(10,4) not null default 0,
  qc              jsonb not null default '{}'::jsonb,
  selected        boolean not null default false,
  forced          boolean not null default false,
  created_at      timestamptz not null default now(),
  unique (series_id, take_id)
);

create table if not exists episode_manifests (
  id          uuid primary key default gen_random_uuid(),
  series_id   text not null references series(id) on delete cascade,
  episode_id  text not null,
  version     text not null default 'v1',
  r2_key      text not null default '',
  manifest    jsonb not null default '{}'::jsonb,
  created_at  timestamptz not null default now()
);

-- ── Production jobs ────────────────────────────────────────────────────────
-- One row per start/resume request. `idempotency_key` stops a redelivered or
-- double-clicked request from starting a second billable run.
create table if not exists production_jobs (
  id               uuid primary key default gen_random_uuid(),
  series_id        text not null references series(id) on delete cascade,
  episode_id       text not null default '',
  stages           jsonb not null default '[]'::jsonb,
  state            text not null default 'queued', -- queued|running|paused|done|failed|cancelled
  mode             text not null default 'mock',
  requested_by     text not null default '',
  idempotency_key  text not null unique,
  force            jsonb not null default '[]'::jsonb,
  progress         jsonb not null default '{}'::jsonb,
  error            text,
  log              text not null default '',
  started_at       timestamptz,
  finished_at      timestamptz,
  created_at       timestamptz not null default now()
);

-- ── Approvals, costs, history ──────────────────────────────────────────────
create table if not exists approvals (
  id            uuid primary key default gen_random_uuid(),
  series_id     text not null references series(id) on delete cascade,
  episode_id    text not null default '',
  subject_type  text not null,                    -- references | publish | episode | take | scene
  subject_id    text not null default '',
  decision      text not null,                    -- approved | rejected
  actor         text not null,
  note          text not null default '',
  created_at    timestamptz not null default now()
);

create table if not exists costs (
  id             uuid primary key default gen_random_uuid(),
  series_id      text not null references series(id) on delete cascade,
  episode_id     text not null default '',
  stage          text not null default '',
  provider       text not null default '',
  endpoint       text not null default '',
  take_id        text not null default '',
  estimated_usd  numeric(10,4) not null default 0,
  actual_usd     numeric(10,4) not null default 0,
  created_at     timestamptz not null default now()
);

create table if not exists generation_history (
  id           uuid primary key default gen_random_uuid(),
  series_id    text not null default '',
  episode_id   text not null default '',
  entity_type  text not null default '',
  entity_id    text not null default '',
  event        text not null,
  detail       jsonb not null default '{}'::jsonb,
  actor        text not null default 'system',
  created_at   timestamptz not null default now()
);

-- ── Integration status (presence and health only — never secret values) ────
create table if not exists integration_status (
  provider      text primary key,
  connected     boolean not null default false,
  model         text not null default '',
  last_test_at  timestamptz,
  last_error    text,
  updated_at    timestamptz not null default now()
);

-- ── Indexes ────────────────────────────────────────────────────────────────
create index if not exists idx_episodes_series      on episodes(series_id);
create index if not exists idx_scenes_episode       on scenes(series_id, episode_id, sequence);
create index if not exists idx_takes_episode        on takes(series_id, episode_id, scene_id);
create index if not exists idx_jobs_episode         on production_jobs(series_id, episode_id, created_at desc);
create index if not exists idx_costs_episode        on costs(series_id, episode_id);
create index if not exists idx_history_episode      on generation_history(series_id, episode_id, created_at desc);
create index if not exists idx_refs_series          on reference_assets(series_id, bible_version, kind);
create index if not exists idx_scripts_episode      on scripts(series_id, episode_id, version desc);

-- ── Row level security ─────────────────────────────────────────────────────
-- The backend uses the service role key and bypasses RLS. These policies make
-- the tables safe if a browser ever reaches them with an anon/user token:
-- only allow-listed administrators can read, and nobody can write directly.
do $$
declare t text;
begin
  foreach t in array array[
    'studio_admins','series','seasons','episodes','scripts','characters','clothing','voices',
    'locations','props','relationships','secrets_bible','knowledge_state','scenes',
    'reference_assets','takes','episode_manifests','production_jobs','approvals','costs',
    'generation_history','integration_status'
  ] loop
    execute format('alter table %I enable row level security', t);
    execute format('drop policy if exists %I on %I', t || '_admin_read', t);
    execute format($f$
      create policy %I on %I for select to authenticated
      using (exists (select 1 from studio_admins a where a.email = auth.jwt() ->> 'email'))
    $f$, t || '_admin_read', t);
  end loop;
end $$;
