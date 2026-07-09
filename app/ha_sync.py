"""Best-effort sync back into Home Assistant.

Publishes the active spool as a sensor and raises persistent notifications.
Every call tolerates HA being down; the republish loop brings the sensor
back once HA returns. Inside the add-on the Supervisor proxy + token are
used automatically; standalone (plain Docker), set HA_URL and HA_TOKEN.
"""
import asyncio
import logging
import os

import aiohttp

log = logging.getLogger("ha_sync")

_BASE = (os.environ.get("HA_URL") or "http://supervisor/core").rstrip("/")
TOKEN = os.environ.get("HA_TOKEN") or os.environ.get("SUPERVISOR_TOKEN", "")
API = WS_URL = ""


def configure(url=None, token=None):
    """Recompute endpoints; lets options.json override env at startup."""
    global _BASE, TOKEN, API, WS_URL
    if url:
        _BASE = url.rstrip("/")
    if token:
        TOKEN = token
    API = f"{_BASE}/api"
    if _BASE == "http://supervisor/core":
        WS_URL = "ws://supervisor/core/websocket"
    else:
        WS_URL = _BASE.replace("http", "ws", 1) + "/api/websocket"


configure()

SENSOR_ENTITY = "sensor.filament_tracker_remaining"


class HASync:
    def __init__(self, spool_provider):
        self.spool_provider = spool_provider  # () -> spool dict | None
        self.available = False
        self._session = None

    def session(self):
        if self._session is None:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {TOKEN}"}
            )
        return self._session

    async def run(self):
        # Re-push periodically: HA loses REST-pushed states on restart, and
        # this doubles as the "sync when HA comes back up" recovery path.
        while True:
            await self.push_sensor()
            await asyncio.sleep(300)

    async def _post(self, path, payload):
        if not TOKEN:
            return False
        try:
            async with self.session().post(
                f"{API}/{path}", json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                resp.raise_for_status()
            self.available = True
            return True
        except Exception as exc:
            if self.available:
                log.warning("HA unreachable (%s) — will keep retrying", exc)
            self.available = False
            return False

    async def push_sensor(self):
        spool = self.spool_provider()
        if spool:
            state = round(spool["remaining_g"], 1)
            attrs = {
                "friendly_name": "Filament Remaining",
                "unit_of_measurement": "g",
                "icon": "mdi:printer-3d-nozzle",
                "spool": f"{spool['brand']} {spool['name']}".strip(),
                "material": spool["material"],
                "color": spool["color_hex"],
                "initial_weight_g": spool["initial_weight_g"],
                "percent_remaining": round(
                    100 * spool["remaining_g"] / spool["initial_weight_g"], 1
                ) if spool["initial_weight_g"] else 0,
            }
        else:
            state = "unknown"
            attrs = {
                "friendly_name": "Filament Remaining",
                "unit_of_measurement": "g",
                "icon": "mdi:printer-3d-nozzle",
                "spool": "none active",
            }
        await self._post(f"states/{SENSOR_ENTITY}", {"state": state, "attributes": attrs})

    async def notify(self, title, message, notification_id="pmft"):
        await self._post("services/persistent_notification/create", {
            "title": title, "message": message, "notification_id": notification_id,
        })

    async def dismiss_notification(self, notification_id="pmft"):
        await self._post("services/persistent_notification/dismiss",
                         {"notification_id": notification_id})
