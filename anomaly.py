"""Daily anomaly detection (v1).

Detects meaningful deviations in daily home metrics (water, energy, ...) versus a
robust baseline learned from Home Assistant long-term statistics. Self-contained:
takes an HA client, reads nothing else, and returns human-readable descriptors that
the morning briefing explains in Jarvis's voice.

Tuning is read from env vars (with sane defaults) so this module touches no shared
config. See docs/superpowers/specs/2026-06-09-anomaly-detection-design.md.
"""
from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime
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
        f"{name}: {yesterday:g}{u} yesterday vs ~{med:g}{u} typical "
        f"({sign}{delta:g}{u}, {mult_s})"
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


async def detect_and_surface(ha_client: Any) -> list[str]:
    """Convenience for the briefing: surfaced anomaly descriptor strings, [] on failure."""
    try:
        return [a.descriptor for a in surface(await detect(ha_client))]
    except Exception as e:
        logger.warning(f"anomaly detect_and_surface failed: {e}")
        return []
