"""K 线形态找历史相似走势（dj 2026-11-11）：形状向量、最近邻、给交易员看的那一行统计。样本外检验本身是实验，不在这里跑。"""
import pytest

np = pytest.importorskip("numpy")

from research import pattern_analog as pa  # noqa: E402


def _window(step=0.01, vol=1000.0):
    close = np.array([10.0 * (1 + step) ** i for i in range(pa.WINDOW + 1)])
    return close, np.full(pa.WINDOW, vol)


def test_features_keep_the_shape_and_ignore_the_price_level_and_reject_dirty_windows():
    close, vol = _window()
    a, b = pa.features(close, vol), pa.features(close * 7.3, vol * 50)
    assert a.shape == (46,) and np.allclose(a, b, atol=1e-4)                 # 同一个形状，价位和量级不同，向量一样
    assert not np.allclose(a, pa.features(*_window(step=-0.01)), atol=1e-2)   # 一路跌和一路涨不一样
    dirty = close.copy()
    dirty[10] *= 0.7                                                          # 除权造成的假大跌
    assert pa.features(dirty, vol) is None and pa.features(close[:-1], vol) is None


def test_nearest_finds_the_closest_rows_and_summary_reports_what_happened_next():
    lib = np.array([[0, 0], [1, 1], [5, 5], [5.1, 5.0], [9, 9]], dtype=np.float32)
    idx = pa.nearest(lib, np.array([[5.05, 5.0], [0.2, 0.1]], dtype=np.float32), k=2)
    assert set(idx[0]) == {2, 3} and set(idx[1]) == {0, 1}
    s = pa.summarize(np.array([1.0, -2.0, 3.0, 0.5]), np.array([2.0, -1.0, 4.0, 1.0]))
    assert s == {"n": 4, "up1": 75.0, "mean1": 0.62, "mean3": 1.5, "worst1": -2.0, "best1": 3.0}


def test_samples_are_only_strong_days_and_carry_what_came_after():
    rng = np.random.default_rng(1)
    close = 10 * np.cumprod(1 + rng.normal(0, 0.02, 80))
    close[40] = close[39] * 1.05                                              # 第 40 天涨 5%：一条样本
    bars = {"600001": (np.array([f"2024-01-{i:03d}" for i in range(80)]), close, np.full(80, 1e6))}
    s = pa.build_samples(bars)
    i = list(s["date"]).index(202401040)
    assert s["f1"][i] == pytest.approx((close[41] / close[40] - 1) * 100, abs=1e-3)
    assert s["f3"][i] == pytest.approx((close[43] / close[40] - 1) * 100, abs=1e-3) and s["X"].shape[1] == 46
