from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger(__name__)

DEFAULT_ALERTS_PATH = str(Path(__file__).parent / "user_alerts.json")
RECOMMENDATION_FEEDBACK_PATH = Path(__file__).parent / "recommendation_feedback.json"

WATCHED_DOMAINS = ["sensor", "binary_sensor", "switch", "climate", "lock"]
BINARY_DOMAINS = {"binary_sensor", "switch", "lock", "input_boolean"}

NUMERIC_ABS_THRESHOLD = 2.0
NUMERIC_PCT_THRESHOLD = 0.05

MIN_RECOMMENDATION_SCORE = 85
MIN_RECOMMENDATION_CONFIDENCE = 0.70
MAX_RECOMMENDATION_ANNOYANCE = 35
DEDUP_WINDOW_HOURS = 6
RESEND_SCORE_DELTA = 10

ACTIVE_DEVICE_STATES = {"on", "open", "opening", "heat", "cool", "heating", "cooling", "running"}
READY_DEVICE_STATES = {"off", "idle", "standby", "ready"}
WEATHER_RISK_STATES = {"rainy", "pouring", "windy", "lightning", "hail", "snowy", "exceptional"}
HOT_WEATHER_STATES = {"sunny", "clear", "partlycloudy", "hot"}

SHIFTABLE_POSITIVE_KEYWORDS = (
    "spa",
    "pool",
    "charger",
    "dishwasher",
    "dryer",
    "washer",
    "water_heater",
    "heater",
    "heat_pump",
    "hvac",
    "ac",
    "aircon",
    "fan",
    "dehumidifier",
    "pump",
    "irrigation",
    "towel",
    "floor",
    "underfloor",
    "boiler",
    "cylinder",
)
SHIFTABLE_NEGATIVE_KEYWORDS = (
    "fridge",
    "freezer",
    "alarm",
    "security",
    "smoke",
    "camera",
    "router",
    "network",
    "server",
    "medical",
    "critical",
    "door_lock",
    "lock",
)
SHIFTABLE_EXCLUDED_DOMAINS = {"lock", "alarm_control_panel", "camera", "binary_sensor", "person", "device_tracker"}

OPENING_KEYWORDS = ("window", "door", "cover", "blind", "curtain", "awning")
TARIFF_KEYWORDS = ("tariff", "price", "rate", "cost")
SOLAR_KEYWORDS = ("solar", "export")
PRESENCE_KEYWORDS = ("presence", "occupancy", "away", "guest", "home")
HEAT_KEYWORDS = ("blind", "cover", "curtain", "shade")

_last_snapshot: dict[str, str] = {}
_last_recommendations: dict[str, tuple[datetime, int]] = {}


@dataclass(frozen=True)
class RecommendationSignal:
    diff_lines: tuple[str, ...]
    signal_lines: tuple[str, ...]
    tariff_entities: tuple[dict, ...]
    solar_entities: tuple[dict, ...]
    weather_entities: tuple[dict, ...]
    away_entities: tuple[dict, ...]
    home_entities: tuple[dict, ...]
    open_entities: tuple[dict, ...]
    shiftable_loads: tuple["ShiftableLoadProfile", ...]
    active_shiftable_loads: tuple["ShiftableLoadProfile", ...]
    available_shiftable_loads: tuple["ShiftableLoadProfile", ...]


@dataclass(frozen=True)
class RecommendationCandidate:
    category: str
    recommendation_type: str
    score: int
    confidence: float
    impact: int
    urgency: int
    annoyance_risk: int
    action: str
    reason: str
    entities: tuple[str, ...]
    evidence: tuple[str, ...]
    dedupe_key: str


@dataclass(frozen=True)
class ShiftableLoadProfile:
    entity_id: str
    shiftability_score: int
    is_active: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class RecommendationDecision:
    candidate: RecommendationCandidate | None
    suppressed_reason: str | None
    suppressed_candidates: tuple[str, ...] = ()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _is_numeric(value: str) -> bool:
    try:
        float(value)
        return True
    except (ValueError, TypeError):
        return False


def _state_as_float(entity: dict) -> float | None:
    try:
        return float(entity.get("state", ""))
    except (TypeError, ValueError):
        return None


def _state_as_lower(entity: dict) -> str:
    return str(entity.get("state", "")).lower()


def _matches_keywords(entity_id: str, keywords: tuple[str, ...]) -> bool:
    entity_id = entity_id.lower()
    return any(keyword in entity_id for keyword in keywords)


def _format_state_line(entity: dict) -> str:
    unit = entity.get("attributes", {}).get("unit_of_measurement", "")
    return f"{entity.get('entity_id', '')}: {entity.get('state', '')}{unit}"


def _entity_haystack(entity: dict) -> str:
    return f"{entity.get('entity_id', '')} {json.dumps(entity.get('attributes', {}), sort_keys=True)}".lower()


def _entity_primary_id(candidate: RecommendationCandidate) -> str:
    return candidate.entities[0] if candidate.entities else "global"


def _feedback_template() -> dict[str, Any]:
    return {"devices": {}, "recommendation_types": {}}


def load_feedback_store(path: Path = RECOMMENDATION_FEEDBACK_PATH) -> dict[str, Any]:
    if not path.exists():
        return _feedback_template()
    try:
        raw = json.loads(path.read_text())
    except Exception:
        logger.warning("Could not parse recommendation feedback store, starting fresh")
        return _feedback_template()
    return {
        "devices": raw.get("devices", {}),
        "recommendation_types": raw.get("recommendation_types", {}),
    }


def save_feedback_store(store: dict[str, Any], path: Path = RECOMMENDATION_FEEDBACK_PATH) -> None:
    path.write_text(json.dumps(store, indent=2, sort_keys=True))


def _feedback_decay(last_feedback_at: str | None) -> float:
    if not last_feedback_at:
        return 1.0
    try:
        timestamp = datetime.fromisoformat(last_feedback_at)
    except ValueError:
        return 1.0
    age = _utc_now() - timestamp.astimezone(timezone.utc)
    if age > timedelta(days=90):
        return 0.25
    if age > timedelta(days=30):
        return 0.5
    return 1.0


def _feedback_adjustment(entry: dict[str, Any]) -> tuple[int, float]:
    decay = _feedback_decay(entry.get("last_feedback_at"))
    accepted = int(round(entry.get("accepted", 0) * decay))
    ignored = int(round(entry.get("ignored", 0) * decay))
    dismissed = int(round(entry.get("dismissed", 0) * decay))
    corrected = int(round(entry.get("corrected", 0) * decay))

    score = accepted * 1 + ignored * -1 + dismissed * -1 + corrected * -1
    confidence = accepted * 0.01 + ignored * -0.01 + corrected * -0.02
    return score, confidence


def update_feedback_store(
    metadata: dict[str, Any],
    feedback_type: str,
    note: str | None = None,
    path: Path = RECOMMENDATION_FEEDBACK_PATH,
) -> dict[str, Any]:
    store = load_feedback_store(path)
    entity_id = ""
    entities = metadata.get("entities") or []
    if entities:
        entity_id = entities[0]
    recommendation_type = metadata.get("recommendation_type", "")
    now = _utc_now().isoformat()

    if entity_id:
        device_entry = store["devices"].setdefault(
            entity_id,
            {"accepted": 0, "ignored": 0, "dismissed": 0, "corrected": 0, "last_feedback_at": now, "notes": []},
        )
        device_entry[feedback_type] = device_entry.get(feedback_type, 0) + 1
        device_entry["last_feedback_at"] = now
        if note and feedback_type == "corrected":
            notes = device_entry.setdefault("notes", [])
            if note not in notes:
                notes.append(note)

    if recommendation_type:
        rec_entry = store["recommendation_types"].setdefault(
            recommendation_type,
            {"accepted": 0, "ignored": 0, "dismissed": 0, "corrected": 0, "last_feedback_at": now},
        )
        rec_entry[feedback_type] = rec_entry.get(feedback_type, 0) + 1
        rec_entry["last_feedback_at"] = now

    save_feedback_store(store, path)
    return store


def _is_active_load(entity: dict) -> bool:
    entity_id = entity.get("entity_id", "")
    state = _state_as_lower(entity)
    domain = entity_id.split(".", 1)[0]
    return domain in {"switch", "climate", "light", "fan", "cover", "water_heater"} and state in ACTIVE_DEVICE_STATES


def classify_shiftable_load(entity: dict) -> ShiftableLoadProfile | None:
    entity_id = entity.get("entity_id", "")
    if not entity_id:
        return None
    domain = entity_id.split(".", 1)[0]
    if domain in SHIFTABLE_EXCLUDED_DOMAINS:
        return None

    state = _state_as_lower(entity)
    attrs = entity.get("attributes", {})
    haystack = _entity_haystack(entity)
    score = 0
    reasons: list[str] = []

    if domain in {"switch", "water_heater"}:
        score += 28
        reasons.append("switch-like controllable load")
    elif domain in {"climate", "fan", "cover"}:
        score += 18
        reasons.append("controllable comfort device")
    elif domain == "light":
        score += 4
        reasons.append("controllable but usually low-value load")

    positive_hits = [keyword for keyword in SHIFTABLE_POSITIVE_KEYWORDS if keyword in haystack]
    negative_hits = [keyword for keyword in SHIFTABLE_NEGATIVE_KEYWORDS if keyword in haystack]
    if positive_hits:
        score += min(40, 14 * len(positive_hits))
        reasons.append("matched shiftable keywords: " + ", ".join(sorted(set(positive_hits))[:3]))
    if negative_hits:
        score -= min(50, 20 * len(negative_hits))
        reasons.append("matched protected keywords: " + ", ".join(sorted(set(negative_hits))[:3]))

    device_class = str(attrs.get("device_class", "")).lower()
    if device_class in {"outlet", "switch"}:
        score += 8
        reasons.append("device class suggests discretionary outlet load")
    if device_class in {"door", "garage", "window", "lock"}:
        score -= 25
        reasons.append("device class suggests non-deferrable opening/security device")

    if _is_active_load(entity):
        score += 10
        reasons.append("currently active")
    elif state in READY_DEVICE_STATES:
        score += 6
        reasons.append("currently available to schedule")

    friendly_name = str(attrs.get("friendly_name", "")).lower()
    if any(token in friendly_name for token in ("bedroom", "nursery", "medical", "security")):
        score -= 15
        reasons.append("friendly name suggests comfort/safety sensitivity")

    if score < 25:
        return None
    return ShiftableLoadProfile(entity_id=entity_id, shiftability_score=score, is_active=_is_active_load(entity), reasons=tuple(reasons))


def extract_recommendation_signals(states: list[dict], diff_lines: list[str], max_signals: int = 18) -> RecommendationSignal:
    signal_lines: list[str] = []
    shiftable: list[ShiftableLoadProfile] = []
    tariff_entities: list[dict] = []
    solar_entities: list[dict] = []
    weather_entities: list[dict] = []
    away_entities: list[dict] = []
    home_entities: list[dict] = []
    open_entities: list[dict] = []
    seen: set[str] = set()

    for entity in states:
        entity_id = entity.get("entity_id", "")
        if not entity_id:
            continue
        haystack = _entity_haystack(entity)
        state = _state_as_lower(entity)
        domain = entity_id.split(".", 1)[0]

        profile = classify_shiftable_load(entity)
        if profile:
            shiftable.append(profile)

        if _matches_keywords(entity_id, TARIFF_KEYWORDS):
            tariff_entities.append(entity)
        if _matches_keywords(entity_id, SOLAR_KEYWORDS):
            solar_entities.append(entity)
        if entity_id.startswith("weather."):
            weather_entities.append(entity)
        if domain in {"person", "device_tracker"} and state == "not_home":
            away_entities.append(entity)
        if domain in {"person", "device_tracker"} and state == "home":
            home_entities.append(entity)
        if _matches_keywords(entity_id, OPENING_KEYWORDS) and state in {"on", "open", "opening"}:
            open_entities.append(entity)

        matches_signal = (
            any(keyword in haystack for keyword in TARIFF_KEYWORDS + SOLAR_KEYWORDS + PRESENCE_KEYWORDS + OPENING_KEYWORDS + HEAT_KEYWORDS)
            or entity_id.startswith("weather.")
            or (domain in {"switch", "climate", "light", "fan", "cover", "water_heater"} and state in ACTIVE_DEVICE_STATES)
            or domain in {"person", "device_tracker"}
        )
        if matches_signal and entity_id not in seen and len(signal_lines) < max_signals:
            signal_lines.append(_format_state_line(entity))
            seen.add(entity_id)

    shiftable.sort(key=lambda item: item.shiftability_score, reverse=True)
    active_shiftable = [item for item in shiftable if item.is_active]
    available_shiftable = [item for item in shiftable if not item.is_active]
    return RecommendationSignal(
        diff_lines=tuple(diff_lines),
        signal_lines=tuple(signal_lines),
        tariff_entities=tuple(tariff_entities),
        solar_entities=tuple(solar_entities),
        weather_entities=tuple(weather_entities),
        away_entities=tuple(away_entities),
        home_entities=tuple(home_entities),
        open_entities=tuple(open_entities),
        shiftable_loads=tuple(shiftable),
        active_shiftable_loads=tuple(active_shiftable),
        available_shiftable_loads=tuple(available_shiftable),
    )


def _build_candidate(
    *,
    category: str,
    recommendation_type: str,
    primary_entity: str,
    action: str,
    reason: str,
    impact: int,
    confidence: float,
    urgency: int,
    annoyance_risk: int,
    evidence: list[str],
    entities: tuple[str, ...],
) -> RecommendationCandidate:
    dedupe_key = f"{category}:{primary_entity}:{recommendation_type.split('.')[-1]}"
    novelty_bonus = 6
    score = round(
        impact * 0.45
        + confidence * 100 * 0.30
        + urgency * 0.20
        - annoyance_risk * 0.25
        + novelty_bonus
    )
    return RecommendationCandidate(
        category=category,
        recommendation_type=recommendation_type,
        score=score,
        confidence=confidence,
        impact=impact,
        urgency=urgency,
        annoyance_risk=annoyance_risk,
        action=action,
        reason=reason,
        entities=entities,
        evidence=tuple(evidence),
        dedupe_key=dedupe_key,
    )


def generate_energy_candidates(signals: RecommendationSignal) -> list[RecommendationCandidate]:
    candidates: list[RecommendationCandidate] = []
    peak_tariff = any(_state_as_lower(entity) in {"peak", "high", "expensive", "on_peak"} for entity in signals.tariff_entities)
    strong_solar = any((_state_as_float(entity) or 0) >= 1500 for entity in signals.solar_entities)
    everyone_away = len(signals.away_entities) > 0 and len(signals.home_entities) == 0

    if peak_tariff and signals.active_shiftable_loads:
        load = signals.active_shiftable_loads[0]
        candidates.append(
            _build_candidate(
                category="energy",
                recommendation_type="energy.delay_load",
                primary_entity=load.entity_id,
                action=f"Delay or turn off {load.entity_id} until cheaper power.",
                reason="Power pricing is high and a likely shiftable load is running now.",
                impact=95,
                confidence=0.92,
                urgency=90,
                annoyance_risk=10,
                evidence=[_format_state_line(entity) for entity in signals.tariff_entities[:1]] + [load.entity_id],
                entities=(load.entity_id,),
            )
        )

    if strong_solar and signals.available_shiftable_loads:
        load = signals.available_shiftable_loads[0]
        candidates.append(
            _build_candidate(
                category="energy",
                recommendation_type="energy.use_solar_window",
                primary_entity=load.entity_id,
                action=f"Use the solar window now if you want to run {load.entity_id}.",
                reason="Solar production/export is healthy and a likely shiftable load is available.",
                impact=88,
                confidence=0.84,
                urgency=72,
                annoyance_risk=12,
                evidence=[_format_state_line(entity) for entity in signals.solar_entities[:1]] + [load.entity_id],
                entities=(load.entity_id,),
            )
        )

    if everyone_away and signals.active_shiftable_loads:
        load = signals.active_shiftable_loads[0]
        candidates.append(
            _build_candidate(
                category="energy",
                recommendation_type="energy.stop_away_load",
                primary_entity=load.entity_id,
                action=f"Check whether {load.entity_id} should still be running while the house is empty.",
                reason="Everyone appears to be away, but a likely discretionary load is still active.",
                impact=90,
                confidence=0.87,
                urgency=84,
                annoyance_risk=15,
                evidence=[entity["entity_id"] + ": not_home" for entity in signals.away_entities[:1]] + [load.entity_id],
                entities=(load.entity_id,) + tuple(entity["entity_id"] for entity in signals.away_entities[:1]),
            )
        )
    return candidates


def generate_weather_candidates(signals: RecommendationSignal) -> list[RecommendationCandidate]:
    candidates: list[RecommendationCandidate] = []
    risky_weather = [entity for entity in signals.weather_entities if _state_as_lower(entity) in WEATHER_RISK_STATES]
    hot_weather = [entity for entity in signals.weather_entities if _state_as_lower(entity) in HOT_WEATHER_STATES]

    if risky_weather and signals.open_entities:
        exposed = signals.open_entities[0]
        weather = risky_weather[0]
        candidates.append(
            _build_candidate(
                category="weather",
                recommendation_type="weather.close_exposed_opening",
                primary_entity=exposed["entity_id"],
                action=f"Close {exposed['entity_id']} before the weather turns against you.",
                reason=f"Current weather is {weather['state']} and something exposed is still open.",
                impact=94,
                confidence=0.91,
                urgency=94,
                annoyance_risk=10,
                evidence=[_format_state_line(weather), _format_state_line(exposed)],
                entities=(exposed["entity_id"], weather["entity_id"]),
            )
        )

    if hot_weather:
        cover_candidates = [entity for entity in signals.open_entities if _matches_keywords(entity["entity_id"], HEAT_KEYWORDS)]
        if cover_candidates and len(signals.home_entities) == 0:
            exposed = cover_candidates[0]
            weather = hot_weather[0]
            candidates.append(
                _build_candidate(
                    category="weather",
                    recommendation_type="weather.prep_for_heat",
                    primary_entity=exposed["entity_id"],
                    action=f"Close {exposed['entity_id']} to reduce heat gain before the house warms up.",
                    reason="Hot/bright weather is building and exposed shading is still open while nobody is home.",
                    impact=70,
                    confidence=0.72,
                    urgency=60,
                    annoyance_risk=22,
                    evidence=[_format_state_line(weather), _format_state_line(exposed)],
                    entities=(exposed["entity_id"], weather["entity_id"]),
                )
            )
    return candidates


def generate_presence_candidates(signals: RecommendationSignal) -> list[RecommendationCandidate]:
    candidates: list[RecommendationCandidate] = []
    everyone_away = len(signals.away_entities) > 0 and len(signals.home_entities) == 0
    if everyone_away and signals.active_shiftable_loads:
        load = signals.active_shiftable_loads[0]
        candidates.append(
            _build_candidate(
                category="presence",
                recommendation_type="presence.stop_when_away",
                primary_entity=load.entity_id,
                action=f"Consider shutting down {load.entity_id} while nobody is home.",
                reason="Presence says the house is empty and a controllable load is still running.",
                impact=86,
                confidence=0.84,
                urgency=78,
                annoyance_risk=18,
                evidence=[entity["entity_id"] + ": not_home" for entity in signals.away_entities[:1]] + [load.entity_id],
                entities=(load.entity_id,) + tuple(entity["entity_id"] for entity in signals.away_entities[:1]),
            )
        )
    return candidates


def apply_feedback_adjustments(candidates: list[RecommendationCandidate], feedback_store: dict[str, Any]) -> list[RecommendationCandidate]:
    adjusted: list[RecommendationCandidate] = []
    for candidate in candidates:
        primary_entity = _entity_primary_id(candidate)
        device_entry = feedback_store.get("devices", {}).get(primary_entity, {})
        type_entry = feedback_store.get("recommendation_types", {}).get(candidate.recommendation_type, {})

        score_adjustment = 0
        confidence_adjustment = 0.0

        decay = _feedback_decay(device_entry.get("last_feedback_at"))
        score_adjustment += round(device_entry.get("accepted", 0) * 6 * decay)
        score_adjustment += round(device_entry.get("ignored", 0) * -5 * decay)
        score_adjustment += round(device_entry.get("dismissed", 0) * -10 * decay)
        score_adjustment += round(device_entry.get("corrected", 0) * -10 * decay)
        confidence_adjustment += device_entry.get("accepted", 0) * 0.05 * decay
        confidence_adjustment += device_entry.get("ignored", 0) * -0.04 * decay
        confidence_adjustment += device_entry.get("corrected", 0) * -0.08 * decay

        decay = _feedback_decay(type_entry.get("last_feedback_at"))
        score_adjustment += round(type_entry.get("accepted", 0) * 4 * decay)
        score_adjustment += round(type_entry.get("ignored", 0) * -3 * decay)
        score_adjustment += round(type_entry.get("dismissed", 0) * -8 * decay)
        score_adjustment += round(type_entry.get("corrected", 0) * -8 * decay)
        confidence_adjustment += type_entry.get("accepted", 0) * 0.05 * decay
        confidence_adjustment += type_entry.get("ignored", 0) * -0.04 * decay
        confidence_adjustment += type_entry.get("corrected", 0) * -0.08 * decay

        adjusted_confidence = max(0.10, min(0.95, candidate.confidence + confidence_adjustment))
        adjusted.append(replace(candidate, score=candidate.score + score_adjustment, confidence=adjusted_confidence))
    return adjusted


def rank_recommendation_candidates(candidates: list[RecommendationCandidate]) -> list[RecommendationCandidate]:
    return sorted(candidates, key=lambda item: (item.score, item.confidence, item.impact, -item.annoyance_risk), reverse=True)


def select_recommendation(candidates: list[RecommendationCandidate], recent_alerts: list[str]) -> RecommendationDecision:
    if not candidates:
        return RecommendationDecision(candidate=None, suppressed_reason="no candidates generated")

    now = _utc_now()
    suppressed: list[str] = []

    for candidate in candidates:
        if candidate.score < MIN_RECOMMENDATION_SCORE:
            suppressed.append(f"{candidate.category}: score {candidate.score}, below send threshold")
            continue
        if candidate.confidence < MIN_RECOMMENDATION_CONFIDENCE:
            suppressed.append(f"{candidate.category}: confidence {candidate.confidence:.2f}, below threshold")
            continue
        if candidate.annoyance_risk > MAX_RECOMMENDATION_ANNOYANCE:
            suppressed.append(f"{candidate.category}: annoyance risk {candidate.annoyance_risk}, too high")
            continue

        recent_key = _last_recommendations.get(candidate.dedupe_key)
        if recent_key:
            last_at, last_score = recent_key
            if now - last_at < timedelta(hours=DEDUP_WINDOW_HOURS):
                if candidate.score < last_score + RESEND_SCORE_DELTA:
                    suppressed.append(f"{candidate.category}: duplicate within dedupe window")
                    continue

        if any(candidate.action in alert for alert in recent_alerts):
            suppressed.append(f"{candidate.category}: already sent recently")
            continue

        _last_recommendations[candidate.dedupe_key] = (now, candidate.score)
        return RecommendationDecision(candidate=candidate, suppressed_reason=None, suppressed_candidates=tuple(suppressed))

    return RecommendationDecision(candidate=None, suppressed_reason=suppressed[0] if suppressed else "all candidates suppressed", suppressed_candidates=tuple(suppressed))


def build_recommendation_context(
    decision: RecommendationDecision,
    signals: RecommendationSignal,
    recent_alerts: list[str] | None = None,
    top_candidates: list[RecommendationCandidate] | None = None,
) -> str:
    sections = ["Recommendation pass for proactive home optimisation."]

    if signals.diff_lines:
        sections.append("Recent meaningful changes:\n" + "\n".join(signals.diff_lines))

    candidate = decision.candidate
    if candidate:
        sections.append(
            "Selected recommendation:\n"
            f"[{candidate.category}] {candidate.action}\n"
            f"Score: {candidate.score}\n"
            f"Confidence: {candidate.confidence:.2f}\n"
            f"Impact: {candidate.impact}\n"
            f"Annoyance risk: {candidate.annoyance_risk}\n"
            f"Reason: {candidate.reason}"
        )
        if candidate.evidence:
            sections.append("Supporting evidence:\n" + "\n".join(f"- {line}" for line in candidate.evidence))

    alternatives = []
    for alt in top_candidates or []:
        if candidate and alt.dedupe_key == candidate.dedupe_key:
            continue
        if len(alternatives) >= 3:
            break
        alternatives.append(f"- {alt.category}: score {alt.score}, confidence {alt.confidence:.2f}")
    if decision.suppressed_candidates:
        alternatives.extend(f"- {item}" for item in decision.suppressed_candidates[:3])
    if alternatives:
        sections.append("Suppressed alternatives:\n" + "\n".join(alternatives[:4]))

    if recent_alerts:
        sections.append("Recent alerts already sent:\n" + "\n".join(f"- {alert}" for alert in recent_alerts))

    sections.append(
        "Message only the selected recommendation in brief plain prose. "
        "Do not mention suppressed alternatives. If no selected recommendation exists, return SILENT."
    )
    return "\n\n".join(sections)


def compute_state_diff(
    states: list[dict],
    last_snapshot: dict[str, str],
    domains: list[str] | None = None,
) -> tuple[dict[str, str], list[str]]:
    watched = set(domains) if domains else None
    snapshot: dict[str, str] = {}

    for entity in states:
        eid = entity.get("entity_id", "")
        domain = eid.split(".")[0] if "." in eid else ""
        if watched and domain not in watched:
            continue
        snapshot[eid] = entity.get("state", "")

    if not last_snapshot:
        return snapshot, []

    diff: list[str] = []
    for eid, new_val in snapshot.items():
        if eid not in last_snapshot:
            diff.append(f"{eid}: new entity ({new_val})")
            continue

        old_val = last_snapshot[eid]
        if old_val == new_val:
            continue

        domain = eid.split(".")[0] if "." in eid else ""
        if not _is_numeric(new_val) or not _is_numeric(old_val):
            diff.append(f"{eid}: {old_val} -> {new_val}")
            continue
        if domain in BINARY_DOMAINS:
            diff.append(f"{eid}: {old_val} -> {new_val}")
            continue

        old_f = float(old_val)
        new_f = float(new_val)
        abs_change = abs(new_f - old_f)
        pct_change = abs_change / abs(old_f) if old_f != 0 else float("inf")
        if abs_change >= NUMERIC_ABS_THRESHOLD or pct_change >= NUMERIC_PCT_THRESHOLD:
            diff.append(f"{eid}: {old_val} -> {new_val}")

    for eid in last_snapshot:
        if eid not in snapshot:
            diff.append(f"{eid}: removed")
    return snapshot, diff


async def check_user_alerts(
    ha_client: Any,
    on_trigger: Callable[[str], Awaitable[None]],
    alerts_path: str = DEFAULT_ALERTS_PATH,
) -> None:
    try:
        alerts = json.loads(Path(alerts_path).read_text()) if Path(alerts_path).exists() else []
    except Exception as exc:
        logger.warning(f"Could not load user_alerts.json: {exc}")
        return

    for alert in alerts:
        if not alert.get("enabled", True):
            continue
        try:
            state_data = await ha_client.get_state(alert["entity_id"])
            value = float(state_data.get("state", 0))
            threshold = float(alert["threshold"])
            condition = alert["condition"]
            triggered = (
                (condition == "above" and value > threshold)
                or (condition == "below" and value < threshold)
                or (condition == "equals" and value == threshold)
            )
            if triggered:
                await on_trigger(f"Alert: {alert['message']} ({alert['entity_id']}: {value})")
        except Exception as exc:
            logger.debug(f"Alert check failed for {alert.get('entity_id')}: {exc}")


def build_scheduler(
    ha_client: Any,
    triage_agent_fn: Callable[..., Awaitable[None]],
    briefing_agent_fn: Callable | None,
    send_fn: Callable[[str], Awaitable[None]],
    poll_interval: int = 15,
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()

    async def morning_briefing() -> None:
        logger.info("Running morning briefing")
        try:
            states = await ha_client.get_states()
            summary = ha_client.get_state_summary(states, domains=WATCHED_DOMAINS)
            from jarvis.agents.briefing import generate
            text = await generate(summary)
            await send_fn(text)
        except Exception as exc:
            logger.error(f"Morning briefing failed: {exc}")
            try:
                await send_fn(f"Morning briefing failed: {exc}")
            except Exception:
                pass

    async def insight_poll() -> None:
        global _last_snapshot
        try:
            await check_user_alerts(ha_client, send_fn)
            states = await ha_client.get_states()
            new_snapshot, diff = compute_state_diff(states, _last_snapshot, domains=WATCHED_DOMAINS)
            _last_snapshot = new_snapshot
            if not diff:
                logger.debug("insight_poll: no state changes, skipping recommendation engine")
                return

            signals = extract_recommendation_signals(states, diff)
            feedback_store = load_feedback_store()
            candidates = generate_energy_candidates(signals) + generate_weather_candidates(signals) + generate_presence_candidates(signals)
            candidates = apply_feedback_adjustments(candidates, feedback_store)
            ranked = rank_recommendation_candidates(candidates)
            decision = select_recommendation(ranked, [])

            if not decision.candidate:
                logger.info("insight_poll: suppressed recommendation: %s", decision.suppressed_reason)
                return

            context = build_recommendation_context(decision, signals, top_candidates=ranked)
            logger.info(
                "insight_poll: selected %s score=%s confidence=%.2f",
                decision.candidate.recommendation_type,
                decision.candidate.score,
                decision.candidate.confidence,
            )
            await triage_agent_fn(context, asdict(decision.candidate))
        except Exception as exc:
            logger.debug(f"Insight poll error: {exc}")

    async def sunday_briefing() -> None:
        logger.info("Running Sunday weekly briefing")
        try:
            from jarvis.agents.sunday_briefing import generate
            text = await generate(ha_client)
            await send_fn(text)
        except Exception as exc:
            logger.error(f"Sunday briefing failed: {exc}")
            try:
                await send_fn(f"Sunday briefing failed: {exc}")
            except Exception:
                pass

    scheduler.add_job(morning_briefing, "cron", hour=7, minute=30, id="morning_briefing")
    scheduler.add_job(
        sunday_briefing,
        "cron",
        day_of_week="sun",
        hour=8,
        minute=0,
        id="sunday_briefing",
    )
    scheduler.add_job(insight_poll, "interval", minutes=poll_interval, id="insight_poll")
    return scheduler
