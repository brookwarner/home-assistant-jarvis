from __future__ import annotations

import sys
import types
from pathlib import Path
import inspect
import asyncio

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if "jarvis" not in sys.modules:
    package = types.ModuleType("jarvis")
    package.__path__ = [str(ROOT)]
    sys.modules["jarvis"] = package

if "apscheduler" not in sys.modules:
    aps = types.ModuleType("apscheduler")
    aps.schedulers = types.ModuleType("apscheduler.schedulers")
    aps.schedulers.asyncio = types.ModuleType("apscheduler.schedulers.asyncio")

    class AsyncIOScheduler:
        def __init__(self):
            self._jobs = []

        def add_job(self, func, trigger, **kwargs):
            self._jobs.append(types.SimpleNamespace(id=kwargs["id"], func=func))

        def get_jobs(self):
            return list(self._jobs)

        def start(self):
            return None

        def shutdown(self):
            return None

    aps.schedulers.asyncio.AsyncIOScheduler = AsyncIOScheduler
    sys.modules["apscheduler"] = aps
    sys.modules["apscheduler.schedulers"] = aps.schedulers
    sys.modules["apscheduler.schedulers.asyncio"] = aps.schedulers.asyncio

if "litellm" not in sys.modules:
    litellm = types.ModuleType("litellm")

    async def acompletion(*args, **kwargs):
        raise RuntimeError("litellm.acompletion was not patched in this test")

    litellm.acompletion = acompletion
    litellm.set_verbose = False
    sys.modules["litellm"] = litellm


def pytest_pyfunc_call(pyfuncitem):
    testfunction = pyfuncitem.obj
    if inspect.iscoroutinefunction(testfunction):
        kwargs = {name: pyfuncitem.funcargs[name] for name in pyfuncitem._fixtureinfo.argnames}
        asyncio.run(testfunction(**kwargs))
        return True
    return None
