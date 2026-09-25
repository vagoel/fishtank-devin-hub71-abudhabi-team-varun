from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .config import settings

Vec3 = tuple[float, float, float]


class _Batch(BaseModel):
    """Common timestamp handling for columnar sensor batches.

    Timestamps can be provided explicitly (`timestamps`, one per sample, ISO-8601 or
    epoch seconds/milliseconds) or implicitly as a regular grid (`start_ts` +
    `interval_ms`). If neither is given the samples are assumed to end "now".
    """

    model_config = ConfigDict(extra="forbid")

    device_id: str = Field(min_length=1, max_length=128)
    timestamps: list[datetime] | None = None
    start_ts: datetime | None = None
    interval_ms: float | None = Field(default=None, gt=0)

    def _n(self) -> int:  # pragma: no cover - overridden
        raise NotImplementedError

    @model_validator(mode="after")
    def _check(self) -> _Batch:
        n = self._n()
        if n == 0:
            raise ValueError("batch must contain at least one sample")
        if n > settings.max_batch_size:
            raise ValueError(f"batch exceeds max_batch_size ({settings.max_batch_size})")
        if self.timestamps is not None and len(self.timestamps) != n:
            raise ValueError("timestamps must have the same length as the samples")
        if self.timestamps is None and (self.start_ts is None) != (self.interval_ms is None):
            raise ValueError("start_ts and interval_ms must be provided together")
        return self

    def resolve_timestamps(self) -> list[datetime]:
        n = self._n()
        if self.timestamps is not None:
            return [_utc(t) for t in self.timestamps]
        if self.start_ts is not None and self.interval_ms is not None:
            step = timedelta(milliseconds=self.interval_ms)
            start = _utc(self.start_ts)
            return [start + step * i for i in range(n)]
        now = datetime.now(timezone.utc)
        return [now] * n


class TemperatureBatch(_Batch):
    values: list[float]

    def _n(self) -> int:
        return len(self.values)


class GyroBatch(_Batch):
    samples: list[Vec3] = Field(description="[[x, y, z], ...] angular velocity samples")

    def _n(self) -> int:
        return len(self.samples)


class AccelBatch(_Batch):
    samples: list[Vec3] = Field(description="[[x, y, z], ...] acceleration samples")

    def _n(self) -> int:
        return len(self.samples)


class IngestEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    temperature: list[TemperatureBatch] = []
    gyro: list[GyroBatch] = []
    accel: list[AccelBatch] = []

    def is_empty(self) -> bool:
        return not (self.temperature or self.gyro or self.accel)

    def total_samples(self) -> int:
        return (
            sum(len(t.values) for t in self.temperature)
            + sum(len(g.samples) for g in self.gyro)
            + sum(len(a.samples) for a in self.accel)
        )


class IngestResponse(BaseModel):
    accepted: int = Field(description="Sensor rows queued for persistence")
    queued_rows: int = Field(description="Total rows pending in the writer (all devices)")
    frames: int | None = Field(default=None, description="Device frames processed")


# -- device frame contract (sticks3.telemetry.v1) ------------------------------------------

FRAME_SCHEMA = "sticks3.telemetry.v1"


class Axes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: float
    y: float
    z: float

    def as_tuple(self) -> Vec3:
        return (self.x, self.y, self.z)


class FreshFlags(BaseModel):
    """Which sensors were actually sampled for this frame (stale values are skipped)."""

    model_config = ConfigDict(extra="forbid")

    accelerometer: bool = True
    gyroscope: bool = True
    temperature: bool = True


class Imu(BaseModel):
    model_config = ConfigDict(extra="forbid")

    acceleration_g: Axes | None = None
    angular_velocity_dps: Axes | None = None
    die_temperature_c: float | None = None


class DeviceConfiguration(BaseModel):
    """Sensor/firmware configuration; extra keys are kept so firmware can add fields."""

    model_config = ConfigDict(extra="allow")

    accelerometer_range_g: float | None = None
    accelerometer_odr_hz: float | None = None
    gyroscope_range_dps: float | None = None
    gyroscope_odr_hz: float | None = None
    library: str | None = None
    library_version: str | None = None


class TelemetryFrame(BaseModel):
    """One IMU sample as pushed by the device.

    `read_time_us` is the device's monotonic clock (microseconds since boot, reset when
    `boot_id` changes); the server converts it to wall-clock time per boot.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_id: Literal["sticks3.telemetry.v1"] = Field(alias="schema")
    device_id: str = Field(min_length=1, max_length=128)
    boot_id: str = Field(min_length=1, max_length=64)
    sequence: int = Field(ge=0)
    read_time_us: int = Field(ge=0)
    frame: str | None = Field(default=None, max_length=64, description="Axis frame convention")
    fresh: FreshFlags = FreshFlags()
    imu: Imu
    configuration: DeviceConfiguration | None = None

    @model_validator(mode="after")
    def _check(self) -> TelemetryFrame:
        if self.fresh.accelerometer and self.imu.acceleration_g is None:
            raise ValueError("fresh.accelerometer is true but imu.acceleration_g is missing")
        if self.fresh.gyroscope and self.imu.angular_velocity_dps is None:
            raise ValueError("fresh.gyroscope is true but imu.angular_velocity_dps is missing")
        if self.fresh.temperature and self.imu.die_temperature_c is None:
            raise ValueError("fresh.temperature is true but imu.die_temperature_c is missing")
        return self

    def sample_count(self) -> int:
        return (
            int(self.fresh.accelerometer) + int(self.fresh.gyroscope) + int(self.fresh.temperature)
        )


FramePayload = Annotated[
    TelemetryFrame | list[TelemetryFrame],
    Field(description="A single frame or an array of frames (batching is strongly preferred)"),
]


def _utc(t: datetime) -> datetime:
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)
