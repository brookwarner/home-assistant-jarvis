"""Daily anomaly detection (v1).

Detects meaningful deviations in daily home metrics (water, energy, ...) versus a
robust baseline learned from Home Assistant long-term statistics. Self-contained:
takes an HA client, reads nothing else, and returns human-readable descriptors that
the morning briefing explains in Jarvis's voice.

Tuning is read from env vars (with sane defaults) so this module touches no shared
config. See docs/superpowers/specs/2026-06-09-anomaly-detection-design.md.
"""
from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except (ValueError, TypeError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except (ValueError, TypeError):
        return default


# Flag when robust z exceeds this; HIGH_Z also forces surfacing regardless of the list.
ANOMALY_Z = _env_float("ANOMALY_Z", 3.5)
ANOMALY_HIGH_Z = _env_float("ANOMALY_HIGH_Z", 6.0)
ANOMALY_MED_Z = _env_float("ANOMALY_MED_Z", 4.0)
# Floors that kill trivial deviations on low-variance signals.
ANOMALY_MIN_ABS = _env_float("ANOMALY_MIN_ABS", 1.0)
ANOMALY_MIN_PCT = _env_float("ANOMALY_MIN_PCT", 0.25)
# Need at least this many baseline days; window to pull.
ANOMALY_MIN_DAYS = _env_int("ANOMALY_MIN_DAYS", 14)
ANOMALY_WINDOW_HOURS = _env_int("ANOMALY_WINDOW_HOURS", 24 * 31)

# --- Habituation -------------------------------------------------------------------
# A deviation that persists is the new normal, not news. After it has been surfaced on
# this many consecutive days it stops headlining the briefing (so Jarvis quits going on
# about the same standing water/energy story morning after morning) UNLESS it materially
# worsens. Re-escalation = |z| rose by ANOMALY_REESCALATE_Z over the level we last spoke
# at, OR the daily value moved a further ANOMALY_REESCALATE_PCT in the same direction, OR
# the deviation reversed direction. A metric that goes quiet for a day is fresh news again
# when it returns; untouched state is forgotten after ANOMALY_FORGET_DAYS.
ANOMALY_HABITUATE_DAYS = _env_int("ANOMALY_HABITUATE_DAYS", 3)
ANOMALY_REESCALATE_Z = _env_float("ANOMALY_REESCALATE_Z", 2.0)
ANOMALY_REESCALATE_PCT = _env_float("ANOMALY_REESCALATE_PCT", 0.5)
ANOMALY_FORGET_DAYS = _env_int("ANOMALY_FORGET_DAYS", 45)


def _state_path() -> Path:
    raw = os.environ.get("ANOMALY_STATE_PATH", "")
    return Path(raw) if raw else (Path(__file__).parent / "anomaly_state.json")


def _surface_substrings() -> tuple[str, ...]:
    raw = os.environ.get("ANOMALY_SURFACE", "") or "water,energy,power,spa,heater,gas"
    return tuple(s.strip().lower() for s in raw.split(",") if s.strip())


def _local_today() -> str:
    try:
        import zoneinfo
        from jarvis.config import config
        tz = zoneinfo.ZoneInfo(config.TIMEZONE) if config.TIMEZONE else None
    except Exception:
        tz = None
    return datetime.now(tz).strftime("%Y-%m-%d")


@dataclass
class Anomaly:
    statistic_id: str
    name: str
    unit: str
    yesterday: float
    median: float
    z: float
    pct: float
    severity: str
    descriptor: str
    date: str = ""  # the date of the scored (latest complete) day, for habituation tracking


def compute_baseline(values: list[float]) -> dict | None:
    """Robust baseline (median + MAD) over the given daily values, or None if there is
    not enough clean history (< ANOMALY_MIN_DAYS finite points)."""
    vals = [float(v) for v in values if isinstance(v, (int, float)) and math.isfinite(v)]
    if len(vals) < ANOMALY_MIN_DAYS:
        return None
    med = median(vals)
    mad = median([abs(v - med) for v in vals])
    return {"median": med, "mad": mad, "n": len(vals)}


def score_day(value: float, baseline: dict) -> dict | None:
    """Robust z-score + percent deviation + severity band, or None if the signal is
    constant (MAD == 0) and therefore unscoreable."""
    med = baseline["median"]
    mad = baseline["mad"]
    if mad <= 0 or not math.isfinite(value):
        return None
    z = 0.6745 * (value - med) / mad
    pct = (value - med) / med if med != 0 else math.inf
    az = abs(z)
    severity = "high" if az >= ANOMALY_HIGH_Z else "medium" if az >= ANOMALY_MED_Z else "low"
    return {"z": z, "pct": pct, "severity": severity}


def _pretty_name(statistic_id: str) -> str:
    return statistic_id.split(".")[-1].replace("_", " ").strip() or statistic_id


def _descriptor(name: str, yesterday: float, med: float, unit: str) -> str:
    u = f" {unit}" if unit else ""
    delta = yesterday - med
    sign = "+" if delta >= 0 else ""
    mult = (yesterday / med) if med else math.inf
    mult_s = f"{mult:.1f}x" if math.isfinite(mult) else "n/a"
    return (
        f"{name}: {yesterday:g}{u} yesterday vs ~{med:g}{u} this home's own recent "
        f"typical ({sign}{delta:g}{u}, {mult_s})"
    )


def _daily_usages(info: dict) -> list[tuple[str, float]]:
    """(date, usage) pairs from a get_statistics entry, dropping today's partial day."""
    daily = info.get("daily") or []
    pairs = [
        (d.get("date"), d.get("usage"))
        for d in daily
        if d.get("usage") is not None and d.get("date")
    ]
    today = _local_today()
    if pairs and pairs[-1][0] == today:
        pairs = pairs[:-1]
    return pairs


async def detect(ha_client: Any) -> list[Anomaly]:
    """Discover statistics, baseline each, and flag the latest complete day's deviation."""
    out: list[Anomaly] = []
    try:
        ids = await ha_client.list_statistic_ids()
    except Exception as e:
        logger.debug(f"anomaly: list_statistic_ids failed: {e}")
        return out
    if not ids:
        return out
    try:
        stats = await ha_client.get_statistics(ids, period="day", hours=ANOMALY_WINDOW_HOURS)
    except Exception as e:
        logger.debug(f"anomaly: get_statistics failed: {e}")
        return out

    for sid, info in (stats or {}).items():
        try:
            if not isinstance(info, dict) or "daily" not in info:
                continue
            pairs = _daily_usages(info)
            if len(pairs) < ANOMALY_MIN_DAYS + 1:
                continue
            usages = [p[1] for p in pairs]
            yesterday = usages[-1]
            yesterday_date = pairs[-1][0]
            baseline = compute_baseline(usages[:-1])
            if not baseline:
                continue
            sc = score_day(yesterday, baseline)
            if not sc:
                continue
            if abs(sc["z"]) < ANOMALY_Z:
                continue
            if abs(yesterday - baseline["median"]) < ANOMALY_MIN_ABS:
                continue
            if abs(sc["pct"]) < ANOMALY_MIN_PCT:
                continue
            name = _pretty_name(sid)
            unit = info.get("unit") or ""
            out.append(Anomaly(
                statistic_id=sid,
                name=name,
                unit=unit,
                yesterday=yesterday,
                median=baseline["median"],
                z=sc["z"],
                pct=sc["pct"],
                severity=sc["severity"],
                descriptor=_descriptor(name, yesterday, baseline["median"], unit),
                date=yesterday_date,
            ))
        except Exception as e:
            logger.debug(f"anomaly: scoring {sid} failed: {e}")
            continue
    return out


def surface(anomalies: list[Anomaly]) -> list[Anomaly]:
    """Keep anomalies on the curated surface-list OR of high severity."""
    subs = _surface_substrings()
    kept = [
        a for a in anomalies
        if a.severity == "high" or any(s in a.statistic_id.lower() for s in subs)
    ]
    # Most deviant first.
    return sorted(kept, key=lambda a: abs(a.z), reverse=True)


def _load_state(path: Path) -> dict:
    """Per-statistic habituation memory, or {} if missing/unreadable."""
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(path: Path, state: dict) -> None:
    """Best-effort persist; never raise (state is an optimisation, not correctness)."""
    try:
        path.write_text(json.dumps(state))
    except Exception as e:
        logger.debug(f"anomaly: could not save state: {e}")


def _parse_date(s: Any) -> Any:
    try:
        return datetime.strptime(str(s), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _is_next_day(prev: Any, cur: Any) -> bool:
    p, c = _parse_date(prev), _parse_date(cur)
    return bool(p and c and (c - p).days == 1)


def _worsened(a: Anomaly, rec: dict) -> bool:
    """True if this day's deviation is materially worse than the level we last surfaced —
    enough to be worth re-headlining a deviation we'd otherwise treated as the new normal."""
    anchor_z = rec.get("anchor_z")
    if anchor_z is not None:
        # Direction reversed (a spike became a drop, or vice versa) -> genuinely new.
        if (a.z >= 0) != (anchor_z >= 0):
            return True
        if abs(a.z) - abs(anchor_z) >= ANOMALY_REESCALATE_Z:
            return True
    anchor_val = rec.get("anchor_value")
    if anchor_val not in (None, 0):
        rise = (a.yesterday - anchor_val) / abs(anchor_val)
        if a.z >= 0 and rise >= ANOMALY_REESCALATE_PCT:
            return True
        if a.z < 0 and rise <= -ANOMALY_REESCALATE_PCT:
            return True
    return False


def _decide(a: Anomaly, rec: dict | None) -> tuple[bool, dict]:
    """Given prior memory for this statistic, return (surface_it, new_record)."""
    fresh = {"last_date": a.date, "streak": 1, "anchor_z": a.z, "anchor_value": a.yesterday}
    if not rec:
        return True, fresh

    last = rec.get("last_date")
    if last == a.date:
        # Idempotent re-run of an already-scored day: don't advance the streak.
        streak = max(1, int(rec.get("streak", 1)))
    elif _is_next_day(last, a.date):
        streak = int(rec.get("streak", 0)) + 1
    else:
        # A quiet gap broke the run — the deviation is news again.
        return True, fresh

    if streak <= ANOMALY_HABITUATE_DAYS:
        # Still inside the grace window: surface, and keep the anchor current.
        return True, {"last_date": a.date, "streak": streak, "anchor_z": a.z, "anchor_value": a.yesterday}
    if _worsened(a, rec):
        # Re-escalation: speak up again and re-anchor to the new, worse level.
        return True, {"last_date": a.date, "streak": streak, "anchor_z": a.z, "anchor_value": a.yesterday}
    # Habituated and unchanged: suppress, but preserve the anchor from when we last spoke
    # so re-escalation is always measured against the level the user last heard about.
    return False, {
        "last_date": a.date,
        "streak": streak,
        "anchor_z": rec.get("anchor_z", a.z),
        "anchor_value": rec.get("anchor_value", a.yesterday),
    }


def filter_habituated(anomalies: list[Anomaly], state: dict) -> tuple[list[Anomaly], dict]:
    """Drop standing deviations that have already been surfaced for several days and haven't
    materially changed. Returns (kept, new_state). Pure: persistence is the caller's job."""
    kept: list[Anomaly] = []
    new_state: dict = {}
    seen: set[str] = set()
    for a in anomalies:
        surface_it, new_rec = _decide(a, state.get(a.statistic_id))
        new_state[a.statistic_id] = new_rec
        seen.add(a.statistic_id)
        if surface_it:
            kept.append(a)
    # Carry forward (and age out) memory for statistics not flagged today, so a metric that
    # falls quiet is fresh news when it returns but the file doesn't grow without bound.
    newest = max((d for d in (_parse_date(a.date) for a in anomalies) if d), default=None)
    for sid, rec in state.items():
        if sid in seen:
            continue
        d = _parse_date(rec.get("last_date"))
        if newest and d and (newest - d).days > ANOMALY_FORGET_DAYS:
            continue
        new_state[sid] = rec
    return kept, new_state


async def detect_and_surface(ha_client: Any, state_path: str | Path | None = None) -> list[str]:
    """Convenience for the briefing: surfaced anomaly descriptor strings, [] on failure.
    Standing deviations already reported on recent days are habituated out (see
    filter_habituated) so the briefing stops re-headlining the same story daily."""
    try:
        anomalies = surface(await detect(ha_client))
        path = Path(state_path) if state_path else _state_path()
        kept, new_state = filter_habituated(anomalies, _load_state(path))
        _save_state(path, new_state)
        return [a.descriptor for a in kept]
    except Exception as e:
        logger.warning(f"anomaly detect_and_surface failed: {e}")
        return []
