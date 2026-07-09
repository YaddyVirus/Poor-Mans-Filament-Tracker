"""Two-way spool selection from Home Assistant.

Maintains an `input_select` helper ("Loaded Filament Spool") in HA whose
options mirror the spool library. Picking an option in HA loads that spool
here; loading a spool here (manually or via auto-detect) updates the
selection in HA. Everything is best-effort: if HA is down, the tracker
keeps working and this module reconnects when HA returns.
"""
import asyncio
import json
import logging

import aiohttp

import db
import ha_sync

log = logging.getLogger("ha_control")

HELPER_NAME = "Loaded Filament Spool"
NONE_OPTION = "— none —"


class HAControl:
    def __init__(self, on_select):
        self.on_select = on_select      # async (spool_id) — user picked in HA
        self.available = False
        self.entity_id = None
        self.helper_id = None
        self._options_map = {}          # option label -> spool_id
        self._ws = None
        self._msg_id = 10               # ids 1-9 reserved for auth/subscribe
        self._pending = {}
        self._reader_live = False

    # ---------- public API (called from main on spool changes) ----------

    async def refresh(self):
        """Sync options + selection into HA. No-op while disconnected."""
        if not self.available:
            return
        try:
            await self._sync_options()
            await self._push_selection()
        except Exception as exc:
            log.warning("Failed to sync spool selector to HA: %s", exc)

    # ---------- connection ----------

    async def run(self):
        if not ha_sync.TOKEN:
            log.info("No HA token configured — HA spool selector disabled")
            return
        session = aiohttp.ClientSession()
        try:
            while True:
                try:
                    await self._session_loop(session)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.debug("HA control connection problem: %s", exc)
                self.available = False
                self._reader_live = False
                await asyncio.sleep(15)
        finally:
            await session.close()

    async def _session_loop(self, session):
        async with session.ws_connect(ha_sync.WS_URL) as ws:
            await ws.receive_json()  # auth_required
            await ws.send_json({"type": "auth", "access_token": ha_sync.TOKEN})
            auth = await ws.receive_json()
            if auth.get("type") != "auth_ok":
                raise RuntimeError(f"HA websocket auth failed: {auth}")
            self._ws = ws

            # setup phase: no event traffic yet, use read-until-result
            await self._ensure_helper()
            await self._sync_options()
            await self._push_selection()
            await ws.send_json(
                {"id": 2, "type": "subscribe_events", "event_type": "state_changed"}
            )
            self._reader_live = True
            self.available = True
            log.info("HA spool selector live: %s", self.entity_id)

            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                data = json.loads(msg.data)
                if data.get("type") == "result" and data.get("id") in self._pending:
                    self._pending.pop(data["id"]).set_result(data)
                elif data.get("type") == "event":
                    await self._on_event(data["event"]["data"])
        self._ws = None

    async def _call(self, payload):
        """Send a command and await its result. Works in both phases:
        before events are subscribed we read frames directly; after, the
        reader loop resolves us via _pending."""
        self._msg_id += 1
        payload["id"] = self._msg_id
        if self._reader_live:
            fut = asyncio.get_running_loop().create_future()
            self._pending[payload["id"]] = fut
            await self._ws.send_json(payload)
            data = await asyncio.wait_for(fut, timeout=10)
        else:
            await self._ws.send_json(payload)
            while True:
                data = await self._ws.receive_json()
                if data.get("id") == payload["id"] and data.get("type") == "result":
                    break
        if not data.get("success", False):
            raise RuntimeError(f"HA command failed: {data.get('error')}")
        return data.get("result")

    async def _ensure_helper(self):
        items = await self._call({"type": "input_select/list"}) or []
        item = next((i for i in items if i.get("name") == HELPER_NAME), None)
        if not item:
            options, _ = self._build_options()
            item = await self._call({
                "type": "input_select/create",
                "name": HELPER_NAME,
                "options": options,
                "icon": "mdi:printer-3d-nozzle",
            })
            log.info("Created HA helper '%s'", HELPER_NAME)
        self.helper_id = item["id"]
        self.entity_id = f"input_select.{item['id']}"

    # ---------- options / selection ----------

    def _build_options(self):
        """Unique option labels for all non-archived spools. Twins (same
        brand+name) get an [id] suffix so both stay selectable in HA."""
        spools = [s for s in db.list_spools() if not s["archived"]]
        counts = {}
        for s in spools:
            label = f"{s['brand']} {s['name']}".strip()
            counts[label] = counts.get(label, 0) + 1
        options, mapping = [NONE_OPTION], {}
        for s in spools:
            label = f"{s['brand']} {s['name']}".strip()
            if counts[label] > 1:
                label = f"{label} [{s['id']}]"
            options.append(label)
            mapping[label] = s["id"]
        self._options_map = mapping
        return options, mapping

    def _active_label(self):
        active = db.active_spool()
        if not active:
            return NONE_OPTION
        for label, sid in self._options_map.items():
            if sid == active["id"]:
                return label
        return NONE_OPTION

    async def _sync_options(self):
        options, _ = self._build_options()
        await self._call({
            "type": "input_select/update",
            "input_select_id": self.helper_id,
            "name": HELPER_NAME,
            "options": options,
            "icon": "mdi:printer-3d-nozzle",
        })

    async def _push_selection(self):
        await self._call({
            "type": "call_service",
            "domain": "input_select",
            "service": "select_option",
            "service_data": {"entity_id": self.entity_id,
                             "option": self._active_label()},
        })

    async def _on_event(self, ev):
        if ev.get("entity_id") != self.entity_id:
            return
        option = (ev.get("new_state") or {}).get("state")
        if not option or option == NONE_OPTION:
            return
        spool_id = self._options_map.get(option)
        if spool_id is None:
            return
        active = db.active_spool()
        if active and active["id"] == spool_id:
            return  # echo of our own push
        log.info("Spool switched from Home Assistant: %s", option)
        await self.on_select(spool_id)
