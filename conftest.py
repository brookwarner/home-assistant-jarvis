"""Make this worktree importable as the `jarvis` package during tests.

The package imports as `jarvis` (e.g. `from jarvis.config import config`), and on
the deployed box the directory is literally `/homeassistant/jarvis`. This worktree
dir has a different name, so register it as the `jarvis` package before collection.
"""
import sys
import types
from pathlib import Path

import pytest

_here = Path(__file__).resolve().parent

if "jarvis" not in sys.modules:
    pkg = types.ModuleType("jarvis")
    pkg.__path__ = [str(_here)]
    sys.modules["jarvis"] = pkg


@pytest.fixture(autouse=True)
def _reset_caravan_state():
    """The caravan daily-decision flags are module globals; reset them between tests so
    one test's prompt/decision can't leak into another."""
    from jarvis import caravan
    caravan._prompt_sent_day = None
    caravan._decided_day = None
    yield
