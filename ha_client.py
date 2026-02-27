from __future__ import annotations
import aiohttp
from typing import Any


class HAClient:
    def __init__(self, url: str, token: str):
        self._url = url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    async def get_state(self, entity_id: str) -> dict:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self._url}/api/states/{entity_id}",
                headers=self._headers,
            ) as resp:
                resp.raise_for_status()
                return await resp.json()

    async def get_states(self) -> list[dict]:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self._url}/api/states",
                headers=self._headers,
            ) as resp:
                resp.raise_for_status()
                return await resp.json()

    async def call_service(
        self, domain: str, service: str, data: dict | None = None
    ) -> list:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self._url}/api/services/{domain}/{service}",
                headers=self._headers,
                json=data or {},
            ) as resp:
                resp.raise_for_status()
                return await resp.json()

    async def get_history(self, entity_id: str, hours: int = 24) -> list[dict]:
        from datetime import datetime, timedelta, timezone
        start = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self._url}/api/history/period/{start}",
                headers=self._headers,
                params={"filter_entity_id": entity_id, "minimal_response": "true"},
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
                return data[0] if data else []

    async def get_statistics(
        self,
        statistic_ids: list[str],
        period: str = "hour",
        hours: int = 48,
    ) -> dict:
        """Fetch long-term statistics by querying the HA SQLite DB directly.

        Returns a summarised dict per statistic_id with:
          - total: usage over the requested window (last sum minus first sum)
          - latest: most recent cumulative sum
          - unit: unit of measurement
          - daily: list of {date, usage} for each day in the window
        """
        import sqlite3
        import asyncio
        from datetime import datetime, timedelta, timezone

        DB_PATH = "/homeassistant/home-assistant_v2.db"
        start_ts = (datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp()

        def _query() -> dict:
            conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            # Resolve statistic_ids → metadata_id
            placeholders = ",".join("?" * len(statistic_ids))
            cur.execute(
                f"SELECT id, statistic_id, unit_of_measurement FROM statistics_meta "
                f"WHERE statistic_id IN ({placeholders})",
                statistic_ids,
            )
            meta = {row["statistic_id"]: {"id": row["id"], "unit": row["unit_of_measurement"]}
                    for row in cur.fetchall()}

            result: dict = {}
            for sid, info in meta.items():
                cur.execute(
                    "SELECT start_ts, sum FROM statistics "
                    "WHERE metadata_id = ? AND start_ts >= ? "
                    "ORDER BY start_ts ASC",
                    (info["id"], start_ts),
                )
                rows = cur.fetchall()
                if not rows:
                    result[sid] = {"error": "no data in range", "unit": info["unit"]}
                    continue

                # Compute total usage = last sum - first sum (running total column)
                first_sum = rows[0]["sum"] or 0.0
                last_sum = rows[-1]["sum"] or 0.0
                total = round(last_sum - first_sum, 3)

                # Daily breakdown: group by date (UTC), take max sum per day
                from collections import defaultdict
                daily_max: dict = defaultdict(float)
                daily_min: dict = {}
                for row in rows:
                    day = datetime.fromtimestamp(row["start_ts"], tz=timezone.utc).strftime("%Y-%m-%d")
                    s = row["sum"] or 0.0
                    daily_max[day] = max(daily_max[day], s)
                    if day not in daily_min:
                        daily_min[day] = s

                days = sorted(daily_max.keys())
                daily = []
                prev_val = daily_min.get(days[0], 0.0) if days else 0.0
                for i, day in enumerate(days):
                    start_val = daily_min[day] if i == 0 else daily_max[days[i - 1]]
                    usage = round(daily_max[day] - start_val, 3)
                    daily.append({"date": day, "usage": usage, "unit": info["unit"]})

                result[sid] = {
                    "total": total,
                    "unit": info["unit"],
                    "latest_cumulative": round(last_sum, 3),
                    "daily": daily,
                }

            conn.close()
            return result

        return await asyncio.get_event_loop().run_in_executor(None, _query)

    async def search_statistics(self, query: str) -> list[dict]:
        """Search statistics_meta by keyword. Returns matching statistic IDs and units."""
        import sqlite3
        import asyncio

        DB_PATH = "/homeassistant/home-assistant_v2.db"
        q = query.lower()

        def _query() -> list[dict]:
            conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(
                "SELECT statistic_id, unit_of_measurement, source FROM statistics_meta "
                "WHERE lower(statistic_id) LIKE ? OR lower(source) LIKE ?",
                (f"%{q}%", f"%{q}%"),
            )
            results = [
                {"statistic_id": r["statistic_id"], "unit": r["unit_of_measurement"], "source": r["source"]}
                for r in cur.fetchall()
            ]
            conn.close()
            return results

        return await asyncio.get_event_loop().run_in_executor(None, _query)

    async def get_entities_by_domain(self, domain: str) -> list[dict]:
        all_states = await self.get_states()
        return [s for s in all_states if s["entity_id"].startswith(f"{domain}.")]

    def get_state_summary(self, states: list[dict], domains: list[str] | None = None) -> str:
        """Return a compact text summary of states for LLM context."""
        filtered = states
        if domains:
            filtered = [s for s in states if any(
                s["entity_id"].startswith(f"{d}.") for d in domains
            )]
        lines = []
        for s in filtered:
            unit = s.get("attributes", {}).get("unit_of_measurement", "")
            lines.append(f"{s['entity_id']}: {s['state']}{unit}")
        return "\n".join(lines)
