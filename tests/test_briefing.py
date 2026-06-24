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


def test_briefing_prompt_includes_voice():
    from jarvis.agents import briefing
    p = briefing._load_system_prompt()
    assert "briefing" in p.lower()


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
