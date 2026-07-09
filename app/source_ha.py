"""Print data source: the ha-bambulab integration via the HA websocket.

Auto-discovers the printer's sensors and streams state changes into the
tracker. Used when no direct printer connection is configured.
"""
import asyncio
import json
import logging

import aiohttp

import ha_sync

log = logging.getLogger("source.ha")

# role -> entity_id suffixes to auto-discover, in preference order
SUFFIXES = {
    "status": ("_print_status",),
    "weight": ("_print_weight",),
    "progress": ("_print_progress",),
    "task": ("_task_name", "_gcode_filename"),
    "external": ("_external_spool", "_externalspool"),
}
OPTION_KEYS = {
    "status": "print_status_entity",
    "weight": "print_weight_entity",
    "progress": "print_progress_entity",
    "task": "task_name_entity",
    "external": "external_spool_entity",
}


class HASource:
    name = "Home Assistant (ha-bambulab)"

    def __init__(self, options, tracker, session):
        self.options = options
        self.tracker = tracker
        self.session = session
        self.entities = {}
        self.connected = False

    async def run(self):
        while True:
            try:
                await self._discover()
                await self._ws_loop()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("HA connection problem: %s", exc)
            self.connected = False
            await asyncio.sleep(10)

    async def _discover(self):
        async with self.session.get(f"{ha_sync.API}/states") as resp:
            resp.raise_for_status()
            states = await resp.json()

        by_id = {s["entity_id"]: s for s in states}
        for role, suffixes in SUFFIXES.items():
            override = (self.options.get(OPTION_KEYS[role]) or "").strip()
            entity = override if override in by_id else None
            if not entity:
                for suffix in suffixes:
                    entity = next(
                        (e for e in by_id if e.startswith("sensor.") and e.endswith(suffix)),
                        None,
                    )
                    if entity:
                        break
            if entity:
                self.entities[role] = entity
                await self.tracker.apply(role, by_id[entity]["state"],
                                         by_id[entity].get("attributes") or {})

        if "status" not in self.entities or "weight" not in self.entities:
            log.warning(
                "Could not find Bambu Lab print sensors. Is the ha-bambulab "
                "integration installed? Found so far: %s", self.entities
            )
        else:
            log.info("Watching entities: %s", self.entities)
            self.tracker.arm_if_printing()

    async def _ws_loop(self):
        roles_by_entity = {v: k for k, v in self.entities.items()}
        if not roles_by_entity:
            await asyncio.sleep(30)
            return

        async with self.session.ws_connect(ha_sync.WS_URL) as ws:
            await ws.receive_json()  # auth_required
            await ws.send_json({"type": "auth", "access_token": ha_sync.TOKEN})
            auth = await ws.receive_json()
            if auth.get("type") != "auth_ok":
                raise RuntimeError(f"HA websocket auth failed: {auth}")
            await ws.send_json(
                {"id": 1, "type": "subscribe_events", "event_type": "state_changed"}
            )
            self.connected = True
            log.info("Connected to Home Assistant websocket")

            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                data = json.loads(msg.data)
                if data.get("type") != "event":
                    continue
                ev = data["event"]["data"]
                role = roles_by_entity.get(ev.get("entity_id"))
                if not role:
                    continue
                new = ev.get("new_state") or {}
                await self.tracker.apply(role, new.get("state"),
                                         new.get("attributes") or {})
