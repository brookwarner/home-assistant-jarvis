import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import date, timedelta

pytestmark = pytest.mark.asyncio


def _daily(values, start="2025-01-01", unit="L"):
    d0 = date.fromisoformat(start)
    return [
        {"date": (d0 + timedelta(days=i)).isoformat(), "usage": v, "unit": unit}
        for i, v in enumerate(values)
    ]


# A baseline with mild variance (median ~100, MAD > 0) repeated to exceed MIN_DAYS.
_BASE = [90, 100, 110, 95, 105, 100, 92, 108, 98, 102, 96, 104, 99, 101, 103, 97]


def test_compute_baseline_median_mad():
    from jarvis import anomaly
    b = anomaly.compute_baseline([90, 100, 110, 95, 105] * 4)
    assert b is not None
    assert b["median"] == 100
    assert b["mad"] > 0


def test_compute_baseline_insufficient_history():
    from jarvis import anomaly
    assert anomaly.compute_baseline([100, 101, 102]) is None  # < MIN_DAYS


def test_descriptor_frames_baseline_as_this_homes_own():
    """The 'typical' figure is this home's own recent median, not a regional/national
    average. The descriptor must say so explicitly so the model stops confabulating
    'X% of the NZ/Auckland average' from the bare multiplier."""
    from jarvis.anomaly import _descriptor
    d = _descriptor("water", 0.42, 0.25, "m³").lower()
    assert "this home's own recent" in d
    # And it must NOT imply an external benchmark.
    for bad in ("nz average", "auckland", "regional", "national"):
        assert bad not in d


def test_score_day_bands():
    from jarvis import anomaly
    base = {"median": 100.0, "mad": 7.5}
    assert anomaly.score_day(300, base)["severity"] == "high"   # huge z
    assert abs(anomaly.score_day(105, base)["z"]) < anomaly.ANOMALY_Z  # in-noise
    assert anomaly.score_day(100, {"median": 100, "mad": 0}) is None   # constant -> unscoreable


async def _detect_with(daily):
    from jarvis import anomaly
    ha = MagicMock()
    ha.list_statistic_ids = AsyncMock(return_value=["sensor.water_daily"])
    ha.get_statistics = AsyncMock(return_value={
        "sensor.water_daily": {"daily": daily, "unit": "L"}
    })
    return await anomaly.detect(ha)


async def test_detect_flags_spike():
    res = await _detect_with(_daily(_BASE + [300]))
    assert len(res) == 1
    assert res[0].statistic_id == "sensor.water_daily"
    assert res[0].yesterday == 300
    assert res[0].z > 0
    assert "300" in res[0].descriptor


async def test_detect_flags_drop():
    res = await _detect_with(_daily(_BASE + [1]))
    assert len(res) == 1
    assert res[0].z < 0


async def test_detect_ignores_in_noise():
    res = await _detect_with(_daily(_BASE + [104]))
    assert res == []


async def test_detect_respects_min_history():
    res = await _detect_with(_daily([100, 101, 102, 300]))  # too few baseline days
    assert res == []


async def test_detect_skips_constant_baseline():
    # MAD == 0 -> unscoreable even with a spike
    res = await _detect_with(_daily([100] * 16 + [300]))
    assert res == []


def test_surface_filter():
    from jarvis import anomaly
    def mk(sid, sev, z):
        return anomaly.Anomaly(sid, sid, "", 1, 1, z, 1, sev, "d")
    items = [
        mk("sensor.living_room_voltage", "low", 3.6),   # not listed, low -> drop
        mk("sensor.water_daily", "low", 3.6),           # 'water' listed -> keep
        mk("sensor.obscure_thing", "high", 7.0),        # high severity -> keep
    ]
    kept = {a.statistic_id for a in anomaly.surface(items)}
    assert kept == {"sensor.water_daily", "sensor.obscure_thing"}


async def test_detect_and_surface_returns_descriptors():
    descs = []
    from jarvis import anomaly
    ha = MagicMock()
    ha.list_statistic_ids = AsyncMock(return_value=["sensor.water_daily"])
    ha.get_statistics = AsyncMock(return_value={
        "sensor.water_daily": {"daily": _daily(_BASE + [300]), "unit": "L"}
    })
    descs = await anomaly.detect_and_surface(ha)
    assert descs and isinstance(descs[0], str) and "water" in descs[0].lower()


async def test_detect_and_surface_graceful_on_failure():
    from jarvis import anomaly
    ha = MagicMock()
    ha.list_statistic_ids = AsyncMock(side_effect=Exception("db locked"))
    assert await anomaly.detect_and_surface(ha) == []
