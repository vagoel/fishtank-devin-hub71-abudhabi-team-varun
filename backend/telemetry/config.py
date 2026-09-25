from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = Field(
        default="postgresql://postgres:postgres@localhost:5432/telemetry",
        description="Postgres DSN. For Supabase use the Session or Transaction pooler URI.",
    )
    db_pool_min: int = 1
    db_pool_max: int = 5
    # Supabase's transaction pooler (port 6543) does not support prepared statements.
    db_statement_cache_size: int = 0
    db_auto_migrate: bool = True

    # Background writer
    flush_interval_ms: int = 200
    flush_max_rows: int = 10_000
    queue_max_rows: int = 500_000
    max_batch_size: int = 50_000

    # In-memory analytics window per device
    ring_buffer_size: int = 20_000
    max_analytics_points: int = 100_000

    # How often device metadata (boot_id, sequence, configuration) is upserted
    device_upsert_interval_ms: int = 2_000

    log_level: str = "info"


settings = Settings()
