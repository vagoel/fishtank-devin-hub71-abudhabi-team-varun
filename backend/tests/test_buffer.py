import numpy as np

from telemetry.buffer import BufferStore, RingBuffer


def _fill(buf: RingBuffer, start: int, n: int):
    ts = np.arange(start, start + n, dtype=np.float64)
    buf.append_many(ts, ts.reshape(-1, 1).astype(np.float32))


def test_append_and_last_in_order():
    buf = RingBuffer(capacity=10, ncols=1)
    _fill(buf, 0, 4)
    ts, vals = buf.last(3)
    assert ts.tolist() == [1, 2, 3]
    assert vals[:, 0].tolist() == [1, 2, 3]


def test_wraparound_keeps_newest():
    buf = RingBuffer(capacity=5, ncols=1)
    _fill(buf, 0, 3)
    _fill(buf, 3, 4)  # 7 rows total -> keeps 2..6
    assert len(buf) == 5
    ts, _ = buf.last(5)
    assert ts.tolist() == [2, 3, 4, 5, 6]
    ts, _ = buf.last(2)
    assert ts.tolist() == [5, 6]


def test_batch_larger_than_capacity():
    buf = RingBuffer(capacity=4, ncols=1)
    _fill(buf, 0, 10)
    ts, _ = buf.last(10)
    assert ts.tolist() == [6, 7, 8, 9]
    _fill(buf, 10, 1)
    ts, _ = buf.last(4)
    assert ts.tolist() == [7, 8, 9, 10]


def test_empty_and_missing():
    buf = RingBuffer(capacity=4, ncols=3)
    ts, vals = buf.last(5)
    assert len(ts) == 0 and vals.shape == (0, 3)
    store = BufferStore(capacity=4)
    assert store.last("gyro", "nope", 3) is None
    assert store.count("gyro", "nope") == 0


def test_late_batch_is_merged_in_order():
    store = BufferStore(capacity=4)
    store.append("t", "d", np.array([20.0, 30.0]), np.array([[20], [30]], np.float32), 1)
    store.append("t", "d", np.array([0.0, 10.0]), np.array([[0], [10]], np.float32), 1)
    ts, vals = store.last("t", "d", 4)
    assert ts.tolist() == [0, 10, 20, 30]
    assert vals[:, 0].tolist() == [0, 10, 20, 30]
    # Late batch that pushes the buffer over capacity keeps only the newest samples.
    store.append("t", "d", np.array([5.0, 25.0]), np.array([[5], [25]], np.float32), 1)
    ts, _ = store.last("t", "d", 10)
    assert ts.tolist() == [10, 20, 25, 30]


def test_store_multicolumn():
    store = BufferStore(capacity=100)
    vals = np.array([[1, 2, 3], [4, 5, 6]], dtype=np.float32)
    store.append("gyro", "d", np.array([1.0, 2.0]), vals, 3)
    ts, got = store.last("gyro", "d", 10)
    assert ts.tolist() == [1.0, 2.0]
    assert got.tolist() == vals.tolist()
    assert store.devices("gyro") == ["d"]
