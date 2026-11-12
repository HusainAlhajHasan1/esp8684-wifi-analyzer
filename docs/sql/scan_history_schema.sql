-- ============================================================================
--  ESP8684 Wi-Fi Network Analyzer — optional cloud scan history (Phase 5)
--
--  NOT APPLIED. This is a draft migration: no Supabase project exists yet in
--  the connected workspace, and provisioning one is your call, not mine.
--
--  Design intent
--  -------------
--  The Android app is offline-first: Room is the source of truth and the app is
--  fully functional with zero network access. This schema only mirrors scan
--  sessions to the cloud so a user can compare environments across time and
--  devices. Nothing here is required for scanning, monitoring or export.
--
--  What is deliberately NOT stored:
--    * no passwords, PSKs, API keys or AI credentials (never leave the device)
--    * no client lists, no captured frames, no probe requests
--    * BSSID is stored because it is broadcast in the clear by every AP and is
--      the only stable identifier for a network across scans
--
--  Apply with:  supabase db push   (or paste into the SQL editor)
-- ============================================================================

create extension if not exists "pgcrypto";

-- ---------------------------------------------------------------------------
-- Devices: one row per physical analyzer (identified by the STA MAC from the
-- firmware's `info` frame, which is read from eFuse and is therefore stable).
-- ---------------------------------------------------------------------------
create table if not exists public.devices (
    id              uuid primary key default gen_random_uuid(),
    owner_id        uuid not null references auth.users (id) on delete cascade,
    mac_sta         text not null check (mac_sta ~ '^([0-9A-F]{2}:){5}[0-9A-F]{2}$'),
    model           text not null default 'ESP8684-DevKitM-1',
    fw_version      text,
    proto_version   int  not null default 1,
    board_flash_bytes bigint,
    label           text,
    last_seen_at    timestamptz not null default now(),
    created_at      timestamptz not null default now(),
    unique (owner_id, mac_sta)
);

-- ---------------------------------------------------------------------------
-- Scan sessions: one row per completed scan (maps to one scan_id on device).
-- ---------------------------------------------------------------------------
create table if not exists public.scan_sessions (
    id              uuid primary key default gen_random_uuid(),
    owner_id        uuid not null references auth.users (id) on delete cascade,
    device_id       uuid not null references public.devices (id) on delete cascade,
    -- Device uptime-based timestamp is useless as a wall clock, so the phone
    -- records the real time at receipt and that is what we store.
    scanned_at      timestamptz not null,
    scan_ms         int,
    network_count   int  not null check (network_count >= 0),
    truncated       boolean not null default false,
    link_quality    jsonb,          -- e.g. {"seq_gaps":0,"rtt_p95_ms":14}
    label           text,           -- user-supplied place, e.g. "Office" / "المنزل"
    created_at      timestamptz not null default now()
);

create index if not exists scan_sessions_owner_time_idx
    on public.scan_sessions (owner_id, scanned_at desc);
create index if not exists scan_sessions_device_time_idx
    on public.scan_sessions (device_id, scanned_at desc);

-- ---------------------------------------------------------------------------
-- Networks observed in a session. Mirrors the protocol's network object so the
-- device contract and the storage contract cannot drift apart.
-- ---------------------------------------------------------------------------
create table if not exists public.scan_networks (
    id              bigserial primary key,
    session_id      uuid not null references public.scan_sessions (id) on delete cascade,
    ssid            text not null default '',   -- '' for hidden; never invented
    ssid_hex        text,                        -- raw bytes when SSID is not valid UTF-8
    bssid           text not null check (bssid ~ '^([0-9A-F]{2}:){5}[0-9A-F]{2}$'),
    rssi            int  not null check (rssi between -120 and 10),
    quality         int  check (quality between 0 and 100),
    channel         int  not null check (channel between 1 and 196),
    freq_mhz        int,
    band            text not null default '2.4ghz' check (band in ('2.4ghz','5ghz','6ghz')),
    width_mhz       int,
    security        text not null,
    hidden          boolean not null default false,
    wps             boolean not null default false,
    pmf             boolean not null default false,
    phy             text[],
    flags           text[],        -- classification tags from the firmware
    unique (session_id, bssid)
);

create index if not exists scan_networks_session_idx on public.scan_networks (session_id);
create index if not exists scan_networks_bssid_idx   on public.scan_networks (bssid);

-- ---------------------------------------------------------------------------
-- Signal samples: the live monitor's history, downsampled on the phone (1 Hz)
-- before upload. BSSIDs are joined through sessions, not duplicated here.
-- ---------------------------------------------------------------------------
create table if not exists public.signal_samples (
    id              bigserial primary key,
    session_id      uuid not null references public.scan_sessions (id) on delete cascade,
    bssid           text not null,
    ts              timestamptz not null,
    rssi            int  not null check (rssi between -120 and 10),
    unique (session_id, bssid, ts)
);

create index if not exists signal_samples_session_idx
    on public.signal_samples (session_id, ts);

-- ---------------------------------------------------------------------------
-- AI reports: stored so the user can revisit an analysis, and audited so it is
-- always clear what was sent to the model (metadata only, never credentials).
-- ---------------------------------------------------------------------------
create table if not exists public.ai_reports (
    id              uuid primary key default gen_random_uuid(),
    owner_id        uuid not null references auth.users (id) on delete cascade,
    session_id      uuid references public.scan_sessions (id) on delete cascade,
    provider        text,           -- e.g. "openai-compatible"
    model           text,
    prompt_tokens   int,
    response_tokens int,
    report          text not null,
    locale          text not null default 'en',
    created_at      timestamptz not null default now()
);

create index if not exists ai_reports_owner_idx on public.ai_reports (owner_id, created_at desc);

-- ---------------------------------------------------------------------------
-- Row Level Security: every table is owner-scoped. Cloud history must never be
-- readable by another account, and the anon key alone must grant nothing.
-- ---------------------------------------------------------------------------
alter table public.devices         enable row level security;
alter table public.scan_sessions   enable row level security;
alter table public.scan_networks   enable row level security;
alter table public.signal_samples  enable row level security;
alter table public.ai_reports      enable row level security;

drop policy if exists devices_owner_all on public.devices;
create policy devices_owner_all on public.devices
    for all using (owner_id = auth.uid()) with check (owner_id = auth.uid());

drop policy if exists sessions_owner_all on public.scan_sessions;
create policy sessions_owner_all on public.scan_sessions
    for all using (owner_id = auth.uid()) with check (owner_id = auth.uid());

drop policy if exists networks_owner_all on public.scan_networks;
create policy networks_owner_all on public.scan_networks
    for all using (
        exists (
            select 1 from public.scan_sessions s
            where s.id = scan_networks.session_id and s.owner_id = auth.uid()
        )
    ) with check (
        exists (
            select 1 from public.scan_sessions s
            where s.id = scan_networks.session_id and s.owner_id = auth.uid()
        )
    );

drop policy if exists samples_owner_all on public.signal_samples;
create policy samples_owner_all on public.signal_samples
    for all using (
        exists (
            select 1 from public.scan_sessions s
            where s.id = signal_samples.session_id and s.owner_id = auth.uid()
        )
    ) with check (
        exists (
            select 1 from public.scan_sessions s
            where s.id = signal_samples.session_id and s.owner_id = auth.uid()
        )
    );

drop policy if exists ai_reports_owner_all on public.ai_reports;
create policy ai_reports_owner_all on public.ai_reports
    for all using (owner_id = auth.uid()) with check (owner_id = auth.uid());
