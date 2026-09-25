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

-- Retention helper: delete readings older than the given interval.
-- Schedule with pg_cron on Supabase, e.g.
--   select cron.schedule('purge-telemetry', '0 3 * * *', $$select purge_readings('30 days')$$);
create or replace function purge_readings(keep interval)
returns void language sql as $$
    delete from temperature_readings where ts < now() - keep;
    delete from gyro_readings where ts < now() - keep;
    delete from accel_readings where ts < now() - keep;
$$;
