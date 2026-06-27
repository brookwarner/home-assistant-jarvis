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


# --- Habituation: stop re-headlining a standing deviation day after day ---------------

def _anom(sid, day, z, value, sev="medium"):
    from jarvis.anomaly import Anomaly
    return Anomaly(sid, sid, "L", value, 100.0, z, 1.0, sev, "d", date=f"2025-03-{day:02d}")


def test_habituation_suppresses_after_grace_window(monkeypatch):
    """A persistent deviation surfaces during the grace window, then becomes 'the new
    normal' and is suppressed — this is the fix for Jarvis going on about water daily."""
    from jarvis import anomaly
    monkeypatch.setattr(anomaly, "ANOMALY_HABITUATE_DAYS", 3)
    state: dict = {}
    surfaced = []
    for day in range(1, 8):  # seven consecutive days of the same ~950L water spike
        kept, state = anomaly.filter_habituated([_anom("sensor.water_daily", day, 8.0, 950)], state)
        surfaced.append(bool(kept))
    # Days 1-3 are news; days 4-7 are suppressed (the new normal).
    assert surfaced == [True, True, True, False, False, False, False]


def test_habituation_reescalates_when_materially_worse(monkeypatch):
    from jarvis import anomaly
    monkeypatch.setattr(anomaly, "ANOMALY_HABITUATE_DAYS", 3)
    monkeypatch.setattr(anomaly, "ANOMALY_REESCALATE_PCT", 0.5)
    state: dict = {}
    for day in range(1, 5):  # days 1-3 news, day 4 habituated (still ~950L)
        kept, state = anomaly.filter_habituated([_anom("sensor.water_daily", day, 8.0, 950)], state)
    assert kept == []  # day 4 suppressed
    # Day 5: usage jumps from 950 to 1500 (+58%) — a real worsening, speak up again.
    kept, state = anomaly.filter_habituated([_anom("sensor.water_daily", 5, 12.0, 1500)], state)
    assert len(kept) == 1


def test_habituation_fresh_again_after_quiet_gap(monkeypatch):
    from jarvis import anomaly
    monkeypatch.setattr(anomaly, "ANOMALY_HABITUATE_DAYS", 3)
    state: dict = {}
    for day in range(1, 5):  # habituated by day 4
        kept, state = anomaly.filter_habituated([_anom("sensor.water_daily", day, 8.0, 950)], state)
    assert kept == []
    # Day 5 the metric is normal (not flagged) — its memory is carried forward but quiet.
    kept, state = anomaly.filter_habituated([], state)
    assert "sensor.water_daily" in state
    # Day 6 it spikes again: the run was broken, so it's fresh news.
    kept, state = anomaly.filter_habituated([_anom("sensor.water_daily", 6, 8.0, 950)], state)
    assert len(kept) == 1


async def test_detect_and_surface_persists_and_habituates(tmp_path):
    """End-to-end: the same spike on two consecutive days surfaces once, then habituates,
    with state persisted to disk between calls."""
    from jarvis import anomaly
    state_file = tmp_path / "anomaly_state.json"

    def ha_for(day):
        ha = MagicMock()
        ha.list_statistic_ids = AsyncMock(return_value=["sensor.water_daily"])
        # 16 baseline days at ~100, then a run of 300 ending on the given day.
        run_len = day
        daily = _daily(_BASE + [300] * run_len, start="2025-02-01")
        ha.get_statistics = AsyncMock(return_value={"sensor.water_daily": {"daily": daily, "unit": "L"}})
        return ha

    # ANOMALY_HABITUATE_DAYS default is 3, so the 4th consecutive flagged day suppresses.
    seen = []
    for day in range(1, 6):
        descs = await anomaly.detect_and_surface(ha_for(day), state_path=str(state_file))
        seen.append(bool(descs))
    assert state_file.exists()
    assert seen[0] is True and seen[-1] is False
