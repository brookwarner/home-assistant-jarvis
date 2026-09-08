import pytest
from unittest.mock import AsyncMock, patch

pytestmark = pytest.mark.asyncio

@pytest.fixture(autouse=True)
def mock_env(monkeypatch):
    for k, v in [("TELEGRAM_BOT_TOKEN","t"),("TELEGRAM_CHAT_ID","1"),
                 ("HA_TOKEN","h"),("ANTHROPIC_API_KEY","sk")]:
        monkeypatch.setenv(k, v)

async def test_generate_returns_string():
    from jarvis.agents.briefing import generate

    with patch("jarvis.router.complete", new_callable=AsyncMock, return_value="Good morning! Energy: 2.3kWh overnight."):
        result = await generate(ha_state_summary="sensor.spa: on\nsensor.temp: 18°C")

    assert isinstance(result, str)
    assert len(result) > 10

async def test_generate_includes_time_context():
    from jarvis.agents.briefing import generate
    import jarvis.agents.briefing as b_module

    captured_messages = []
    async def capture(*args, **kwargs):
        captured_messages.extend(kwargs.get("messages", args[1] if len(args) > 1 else []))
        return "Morning summary."

    with patch("jarvis.router.complete", new_callable=AsyncMock, side_effect=capture):
        await generate(ha_state_summary="sensor.temp: 18°C")

    full_text = " ".join(m["content"] for m in captured_messages)
    assert "morning" in full_text.lower() or "briefing" in full_text.lower()


async def test_fetch_water_context_includes_real_watercare_figures():
    from unittest.mock import MagicMock
    from jarvis.agents import briefing
    from jarvis.config import config
    config.WATERCARE_SENSOR = "sensor.watercare"
    ha = MagicMock()
    ha.get_state = AsyncMock(return_value={
        "entity_id": "sensor.watercare", "state": "17000",
        "attributes": {
            "daily_average": 548, "household_efficiency_band": 4,
            "current_period_cost": 65.36, "cost_currency": "NZD",
            "billing_period_from": "2026-05-15T12:00:00.000Z",
            "billing_period_to": "2026-06-14T12:00:00.000Z",
        },
    })
    ctx = await briefing.fetch_water_context(ha)
    assert ctx is not None
    assert "548" in ctx
    assert "efficiency band 4" in ctx.lower()
    assert "own" in ctx.lower()  # framed as this home's own data


async def test_fetch_water_context_none_when_sensor_unavailable():
    from unittest.mock import MagicMock
    from jarvis.agents import briefing
    ha = MagicMock()
    ha.get_state = AsyncMock(return_value={"state": "unavailable", "attributes": {}})
    assert await briefing.fetch_water_context(ha) is None


async def test_generate_includes_water_context():
    from jarvis.agents.briefing import generate
    captured = []

    async def capture(*args, **kwargs):
        captured.extend(kwargs.get("messages", args[1] if len(args) > 1 else []))
        return "Morning."

    with patch("jarvis.router.complete", new_callable=AsyncMock, side_effect=capture):
        await generate(
            "sensor.x: 1", anomalies=None,
            water_context="Watercare (this home's own): efficiency band 4, ~548 L/day.",
        )
    full = " ".join(m["content"] for m in captured)
    assert "efficiency band 4" in full


def _cal_client(events_by_entity, fail=()):
    """An HA client stub whose get_calendar_events serves per-entity events and raises for
    entities listed in `fail` (the real box returns HTTP 400 for dead calendar entities)."""
    from unittest.mock import MagicMock
    ha = MagicMock()

    async def get_events(entity_id, start, end):
        if entity_id in fail:
            raise RuntimeError(f"400 for {entity_id}")
        return events_by_entity.get(entity_id, [])

    ha.get_calendar_events = get_events
    return ha


async def test_fetch_calendar_context_skips_unavailable_calendars():
    """This home has ~75 calendar entities and 50 of them are stale duplicates left behind by
    a re-added integration — they sit at state 'unavailable' and the events endpoint returns
    HTTP 400 for them. Querying them is pure waste, so they must be filtered out by state
    before any request is made."""
    from jarvis.agents import briefing
    from jarvis.config import config
    config.BRIEFING_CALENDARS = []

    queried = []

    async def get_events(entity_id, start, end):
        queried.append(entity_id)
        return []

    from unittest.mock import MagicMock
    ha = MagicMock()
    ha.get_calendar_events = get_events

    states = [
        {"entity_id": "calendar.personal", "state": "unavailable"},
        {"entity_id": "calendar.personal_2", "state": "on"},
        {"entity_id": "calendar.recycle", "state": "off"},
        {"entity_id": "sensor.temp", "state": "18"},
    ]
    await briefing.fetch_calendar_context(ha, states)

    assert "calendar.personal" not in queried  # dead duplicate
    assert set(queried) == {"calendar.personal_2", "calendar.recycle"}


async def test_fetch_calendar_context_formats_timed_and_all_day_events():
    """Times must render in the home's own timezone, not UTC — a 09:30 appointment reported
    as 21:30 is worse than not reporting it."""
    from jarvis.agents import briefing
    from jarvis.config import config
    config.BRIEFING_CALENDARS = []
    config.TIMEZONE = "Pacific/Auckland"

    ha = _cal_client({
        "calendar.work_2": [
            {"start": {"dateTime": "2026-07-27T09:30:00+12:00"},
             "end": {"dateTime": "2026-07-27T10:30:00+12:00"},
             "summary": "Dentist"},
        ],
        "calendar.recycle": [
            {"start": {"date": "2026-07-27"}, "end": {"date": "2026-07-28"},
             "summary": "Recycling collection"},
        ],
    })
    states = [
        {"entity_id": "calendar.work_2", "state": "on"},
        {"entity_id": "calendar.recycle", "state": "off"},
    ]
    ctx = await briefing.fetch_calendar_context(ha, states)

    assert ctx is not None
    assert "09:30" in ctx
    assert "Dentist" in ctx
    assert "Recycling collection" in ctx
    assert "all day" in ctx.lower()


async def test_fetch_calendar_context_none_when_no_events_today():
    """A quiet day must add nothing to the prompt — not an empty 'Calendar:' header that the
    model might narrate as 'nothing on today'."""
    from jarvis.agents import briefing
    from jarvis.config import config
    config.BRIEFING_CALENDARS = []

    ha = _cal_client({})
    states = [{"entity_id": "calendar.work_2", "state": "on"}]
    assert await briefing.fetch_calendar_context(ha, states) is None


async def test_fetch_calendar_context_survives_a_failing_calendar():
    """One broken calendar must not cost the briefing the others' events."""
    from jarvis.agents import briefing
    from jarvis.config import config
    config.BRIEFING_CALENDARS = []

    ha = _cal_client(
        {"calendar.good": [{"start": {"date": "2026-07-27"}, "end": {"date": "2026-07-28"},
                            "summary": "Bin day"}]},
        fail=("calendar.broken",),
    )
    states = [
        {"entity_id": "calendar.broken", "state": "on"},
        {"entity_id": "calendar.good", "state": "on"},
    ]
    ctx = await briefing.fetch_calendar_context(ha, states)

    assert ctx is not None
    assert "Bin day" in ctx


async def test_fetch_calendar_context_dedupes_event_shared_across_calendars():
    """The same Google event is often synced into more than one calendar entity. Listing it
    twice makes the briefing read as though there are two commitments."""
    from jarvis.agents import briefing
    from jarvis.config import config
    config.BRIEFING_CALENDARS = []

    event = {"start": {"dateTime": "2026-07-27T09:30:00+12:00"},
             "end": {"dateTime": "2026-07-27T10:30:00+12:00"}, "summary": "Dentist"}
    ha = _cal_client({"calendar.shared_2": [event], "calendar.personal_2": [dict(event)]})
    states = [
        {"entity_id": "calendar.shared_2", "state": "on"},
        {"entity_id": "calendar.personal_2", "state": "on"},
    ]
    ctx = await briefing.fetch_calendar_context(ha, states)

    assert ctx.count("Dentist") == 1


async def test_fetch_calendar_context_respects_explicit_allowlist():
    """BRIEFING_CALENDARS narrows a couple of dozen live calendars to the few that matter."""
    from jarvis.agents import briefing
    from jarvis.config import config
    config.BRIEFING_CALENDARS = ["calendar.work_2"]

    queried = []

    async def get_events(entity_id, start, end):
        queried.append(entity_id)
        return []

    from unittest.mock import MagicMock
    ha = MagicMock()
    ha.get_calendar_events = get_events

    states = [
        {"entity_id": "calendar.work_2", "state": "on"},
        {"entity_id": "calendar.someday_2", "state": "on"},
    ]
    await briefing.fetch_calendar_context(ha, states)
    config.BRIEFING_CALENDARS = []

    assert queried == ["calendar.work_2"]


async def test_generate_includes_calendar_context():
    from jarvis.agents.briefing import generate
    captured = []

    async def capture(*args, **kwargs):
        captured.extend(kwargs.get("messages", args[1] if len(args) > 1 else []))
        return "Morning."

    with patch("jarvis.router.complete", new_callable=AsyncMock, side_effect=capture):
        await generate("sensor.x: 1", calendar_context="Today's calendar: 09:30 Dentist.")

    full = " ".join(m["content"] for m in captured)
    assert "Dentist" in full


def test_briefing_prompt_tells_it_to_frame_the_day_around_the_calendar():
    """Without explicit guidance the model treats calendar lines as just more state to recite.
    The point of the calendar is to connect it to the house — out all day means the spa can
    idle, rain plus an evening out means bring the washing in first."""
    from jarvis.agents import briefing
    p = briefing._load_system_prompt().lower()
    assert "calendar" in p
    assert "presence" in p or "person." in p


def test_briefing_prompt_forbids_narrating_correct_behaviour():
    """Live briefings on 25 and 26 July spent most of their words confirming things were
    normal ('that's correct', 'idling normally', 'everything else is quiet'). Absence of
    problems should cost a clause, not a paragraph."""
    from jarvis.agents import briefing
    p = briefing._load_system_prompt().lower()
    assert "correct" in p and "normal" in p


def test_briefing_prompt_includes_voice():
    from jarvis.agents import briefing
    p = briefing._load_system_prompt()
    assert "briefing" in p.lower()


def test_briefing_prompt_forbids_asking_the_caravan_question():
    """The scheduler appends a fixed caravan question after generate() returns (see
    scheduler.CARAVAN_QUESTION). The shared system prompt also carries a note — reused from
    conversation mode — that says 'the morning briefing asks whether the user will use the
    caravan that day', which reads as a self-instruction when the model is the one WRITING
    the briefing. Left unchecked, the model asks its own version of the question and the
    user is asked twice. The briefing-mode addendum must explicitly forbid that."""
    from jarvis.agents import briefing
    p = briefing._load_system_prompt().lower()
    assert "do not ask whether the caravan will be used today" in p
    assert "asked twice" in p


def test_briefing_prompt_forbids_invented_benchmarks_but_allows_real_ones():
    """Anomaly baselines are this home's OWN recent median, and the only real peer signal
    is Watercare's household_efficiency_band. The prompt must forbid inventing an external
    average ('174% of the NZ average') while permitting figures that ARE in the data."""
    from jarvis.agents import briefing
    p = briefing._load_system_prompt().lower()
    assert "this home's own" in p
    # Forbids the confabulated external benchmark...
    assert "nz average" in p or "external benchmark" in p
    # ...but explicitly allows the real Watercare figures it could cite instead.
    assert "household_efficiency_band" in p
    assert "no markdown" in p.lower()


async def test_fetch_calendar_context_drops_excluded_calendars():
    """Some 'calendars' are synthetic — calendar.workday_sensor_calendar exists to drive a
    workday binary_sensor and emits an all-day 'Workday Sensor' event most days. That is
    plumbing, not a commitment, and it shows up in the block as noise."""
    from jarvis.agents import briefing
    from jarvis.config import config
    config.BRIEFING_CALENDARS = []
    config.BRIEFING_CALENDARS_EXCLUDE = ["calendar.workday_sensor_calendar"]

    queried = []

    events = {
        "calendar.workday_sensor_calendar": [
            {"start": {"date": "2026-07-26"}, "end": {"date": "2026-07-27"},
             "summary": "Workday Sensor"},
        ],
        "calendar.family": [
            {"start": {"date": "2026-07-26"}, "end": {"date": "2026-07-27"},
             "summary": "Birthday party"},
        ],
    }

    async def get_events(entity_id, start, end):
        queried.append(entity_id)
        return events.get(entity_id, [])

    from unittest.mock import MagicMock
    ha = MagicMock()
    ha.get_calendar_events = get_events

    states = [
        {"entity_id": "calendar.workday_sensor_calendar", "state": "on"},
        {"entity_id": "calendar.family", "state": "on"},
    ]
    ctx = await briefing.fetch_calendar_context(ha, states)

    assert queried == ["calendar.family"]
    assert "Workday Sensor" not in ctx
    assert "Birthday party" in ctx


def test_workday_sensor_calendar_excluded_by_default():
    """Verified against the live box: this calendar emits an all-day event most days."""
    from jarvis.config import config
    assert "calendar.workday_sensor_calendar" in config.BRIEFING_CALENDARS_EXCLUDE


async def test_fetch_calendar_context_does_not_claim_all_day_for_unparseable_times():
    """A timed event whose dateTime can't be parsed must not be relabelled '(all day)' — that
    asserts something false to the model, which will then say it out loud. Drop the time
    claim, keep the event."""
    from jarvis.agents import briefing
    from jarvis.config import config
    config.BRIEFING_CALENDARS = []

    ha = _cal_client({"calendar.x": [
        {"start": {"dateTime": "not-a-timestamp"}, "end": {"dateTime": "also-not"},
         "summary": "Site visit"},
    ]})
    ctx = await briefing.fetch_calendar_context(ha, [{"entity_id": "calendar.x", "state": "on"}])

    assert "Site visit" in ctx
    assert "all day" not in ctx.lower()


async def test_fetch_calendar_context_sorts_timed_before_all_day_by_event_data():
    """Ordering must key off the event's own start, not the rendered string. A timed event
    whose title happens to contain '(all day)' belongs with the timed events."""
    from jarvis.agents import briefing
    from jarvis.config import config
    config.BRIEFING_CALENDARS = []
    config.TIMEZONE = "Pacific/Auckland"

    ha = _cal_client({"calendar.x": [
        # All-day summary starting with a digit sorts lexically ahead of "14:00-...", so a
        # sort keyed on rendered text puts the timed event last.
        {"start": {"date": "2026-07-26"}, "end": {"date": "2026-07-27"},
         "summary": "0730 school run swap"},
        {"start": {"dateTime": "2026-07-26T14:00:00+12:00"},
         "end": {"dateTime": "2026-07-26T15:00:00+12:00"},
         "summary": "Parking restricted (all day)"},
    ]})
    ctx = await briefing.fetch_calendar_context(ha, [{"entity_id": "calendar.x", "state": "on"}])

    assert ctx.index("Parking restricted") < ctx.index("0730 school run swap")


async def test_fetch_calendar_context_propagates_cancellation():
    """asyncio.gather(return_exceptions=True) hands back CancelledError as a *result*. Treating
    it like a dead calendar swallows shutdown, leaving the task un-cancellable."""
    import asyncio
    from jarvis.agents import briefing
    from jarvis.config import config
    config.BRIEFING_CALENDARS = []

    from unittest.mock import MagicMock
    ha = MagicMock()

    async def get_events(entity_id, start, end):
        raise asyncio.CancelledError()

    ha.get_calendar_events = get_events

    with pytest.raises(asyncio.CancelledError):
        await briefing.fetch_calendar_context(
            ha, [{"entity_id": "calendar.x", "state": "on"}]
        )
