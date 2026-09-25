-- Time-series schema for device telemetry.
-- Plain Postgres (works on Supabase free tier, no extensions required).
-- Rows are append-only and keyed by (device_id, ts); the composite index serves
-- all "last N points for a device" queries via a backwards index scan.

create table if not exists temperature_readings (
    device_id text        not null,
    ts        timestamptz not null,
    value     real        not null
);

create index if not exists temperature_readings_device_ts_idx
    on temperature_readings (device_id, ts desc);

create table if not exists gyro_readings (
    device_id text        not null,
    ts        timestamptz not null,
    x         real        not null,
    y         real        not null,
    z         real        not null
);

create index if not exists gyro_readings_device_ts_idx
    on gyro_readings (device_id, ts desc);

create table if not exists accel_readings (
    device_id text        not null,
    ts        timestamptz not null,
    x         real        not null,
    y         real        not null,
    z         real        not null
);

create index if not exists accel_readings_device_ts_idx
    on accel_readings (device_id, ts desc);

-- Latest known state per device, upserted from sticks3 frames (metadata, not time-series).
create table if not exists devices (
    device_id      text        primary key,
    boot_id        text,
    last_sequence  bigint,
    frame          text,
    configuration  jsonb,
    last_seen      timestamptz not null
);

-- HeatGuard incidents (heatguard.incident.v1, docs/heatguard/incidents.md): falls, heat
-- stroke, SOS... Low volume, written directly by heatguard/incidents.py (not the COPY writer).
create table if not exists incidents (
    incident_id   text        primary key,
    device_id     text        not null,
    person        jsonb,
    type          text        not null,
    status        text        not null,
    severity      text        not null,
    occurred_at   timestamptz not null,
    received_at   timestamptz not null default now(),
    updated_at    timestamptz not null default now(),
    location      jsonb,
    details       jsonb,
    source        text,
    alert_id      text,
    escalated     jsonb,
    boot_id       text,
    read_time_us  bigint
);

create index if not exists incidents_occurred_idx on incidents (occurred_at desc);
create index if not exists incidents_device_occurred_idx on incidents (device_id, occurred_at desc);

-- Retention helper: delete readings older than the given interval.
-- Schedule with pg_cron on Supabase, e.g.
--   select cron.schedule('purge-telemetry', '0 3 * * *', $$select purge_readings('30 days')$$);
create or replace function purge_readings(keep interval)
returns void language sql as $$
    delete from temperature_readings where ts < now() - keep;
    delete from gyro_readings where ts < now() - keep;
    delete from accel_readings where ts < now() - keep;
$$;
