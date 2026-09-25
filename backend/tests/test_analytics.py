import numpy as np
import pytest

from telemetry.analytics import accel_analytics, gyro_analytics, temperature_analytics


def test_accel_gravity_orientation_and_vibration():
    ts = np.arange(0, 1, 0.01, dtype=np.float64)  # 100 Hz, 1 s
    # Device tilted 30 degrees about y: gravity has an x component of -sin(30), z of cos(30).
    g = np.array([-0.5, 0.0, np.sqrt(3) / 2])
    noise = 0.02 * np.sin(2 * np.pi * 10 * ts)  # 10 Hz vibration along z
    vals = np.tile(g, (100, 1)).astype(np.float32)
    vals[:, 2] += noise.astype(np.float32)
    out = accel_analytics(ts, vals)
    assert out["count"] == 100
    assert out["gravity"]["magnitude"] == pytest.approx(1.0, abs=1e-3)
    assert out["gravity"]["pitch_deg"] == pytest.approx(30.0, abs=0.1)
    assert out["gravity"]["roll_deg"] == pytest.approx(0.0, abs=0.1)
    assert out["gravity"]["dominant_axis"] == "+z"
    assert out["vibration"]["rms_g"] == pytest.approx(0.02 * np.sqrt(3) / 2 / np.sqrt(2), abs=2e-3)
    assert out["vibration"]["peak_g"] == pytest.approx(0.02 * np.sqrt(3) / 2, abs=2e-3)


def test_accel_flat_face_down():
    vals = np.tile([0.0, 0.0, -1.0], (5, 1)).astype(np.float32)
    out = accel_analytics(np.arange(5, dtype=np.float64), vals)
    assert out["gravity"]["dominant_axis"] == "-z"
    assert out["vibration"]["rms_g"] == 0


def test_temperature_linear_trend():
    ts = np.arange(0, 600, 60, dtype=np.float64)  # 10 points, one per minute
    vals = (20 + 0.5 * np.arange(10)).reshape(-1, 1).astype(np.float32)
    out = temperature_analytics(ts, vals)
    assert out["count"] == 10
    assert out["trend"]["slope_per_min"] == pytest.approx(0.5, abs=1e-4)
    assert out["trend"]["delta"] == pytest.approx(4.5, abs=1e-4)
    assert out["stats"]["min"] == pytest.approx(20)
    assert out["stats"]["max"] == pytest.approx(24.5)
    assert out["window"]["sample_rate_hz"] == pytest.approx(1 / 60, abs=1e-3)
    assert out["latest"]["value"] == pytest.approx(24.5)
    assert out["anomalies"]["count"] == 0


def test_temperature_anomaly_detected():
    ts = np.arange(100, dtype=np.float64)
    x = np.full(100, 21.0)
    x[42] = 60.0
    out = temperature_analytics(ts, x.reshape(-1, 1).astype(np.float32))
    assert out["anomalies"]["count"] == 1
    assert out["anomalies"]["values"] == [60.0]


def test_temperature_single_point():
    out = temperature_analytics(np.array([5.0]), np.array([[1.0]], dtype=np.float32))
    assert out["count"] == 1
    assert out["trend"]["slope_per_min"] is None
    assert out["window"]["sample_rate_hz"] is None


def test_gyro_rotation_and_motion():
    ts = np.linspace(0, 10, 101)  # 10 Hz for 10 s
    vals = np.zeros((101, 3), dtype=np.float32)
    vals[:, 2] = 1.0  # constant 1 rad/s around z
    out = gyro_analytics(ts, vals)
    assert out["rotation"]["z"] == pytest.approx(10.0, abs=1e-3)
    assert out["rotation"]["x"] == 0
    assert out["magnitude"]["mean"] == pytest.approx(1.0)
    assert out["motion"]["is_moving"] is True
    assert out["motion"]["active_fraction"] == 1.0
    assert out["peak"]["magnitude"] == pytest.approx(1.0)


def test_gyro_stationary():
    ts = np.arange(50, dtype=np.float64)
    vals = np.full((50, 3), 0.001, dtype=np.float32)
    out = gyro_analytics(ts, vals)
    assert out["motion"]["is_moving"] is False
    assert out["motion"]["active_fraction"] == 0
