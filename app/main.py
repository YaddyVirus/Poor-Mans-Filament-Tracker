"""Poor Man's Filament Tracker: web UI + REST API + print tracking.

Print data comes from a direct printer MQTT connection when configured
(works with HA down), otherwise from the ha-bambulab integration via HA.
Spool state is synced back to HA on a best-effort basis either way.
"""
import asyncio
import json
import logging
import os
from pathlib import Path

from aiohttp import web

import db
import ha_sync
import matching
from ha_control import HAControl
from ha_sync import HASync
from tracker import PrintTracker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
log = logging.getLogger("main")

APP_DIR = Path(__file__).parent
WEB_DIR = APP_DIR / "web"
OPTIONS_FILE = Path(os.environ.get("OPTIONS_FILE", "/data/options.json"))
PORT = int(os.environ.get("PORT", "8099"))

ENV_OPTIONS = (
    "printer_host", "printer_serial", "printer_access_code",
    "ha_url", "ha_token",
    "deduct_on_failed", "auto_select_spool",
)


def load_options():
    options = json.loads(OPTIONS_FILE.read_text()) if OPTIONS_FILE.exists() else {}
    for key in ENV_OPTIONS:  # standalone Docker: configure via env vars
        val = os.environ.get(key.upper())
        if val is not None and key not in options:
            options[key] = val.lower() in ("1", "true", "yes") \
                if key.startswith(("deduct", "auto")) else val
    return options


OPTIONS = load_options()

# Result of the last external-spool detection, surfaced in /api/status.
DETECTION = {
    "detected": None, "spool_id": None, "note": "",
    "ambiguous": False, "candidates": [],
}


async def push_state(app):
    """Reflect spool changes into HA: sensor + input_select selector."""
    await app["sync"].push_sensor()
    await app["control"].refresh()


def spool_payload(spool):
    return web.json_response(spool if spool else {"error": "not found"},
                             status=200 if spool else 404)


def _candidate_view(spools):
    return [
        {"id": s["id"], "label": f"{s['brand']} {s['name']}".strip(),
         "color_hex": s["color_hex"], "remaining_g": s["remaining_g"]}
        for s in spools
    ]


def make_detection_handler(app):
    async def on_filament_detected(detected, job_open):
        DETECTION.update(detected=detected, spool_id=None, note="",
                         ambiguous=False, candidates=[])
        if not detected:
            await app["sync"].dismiss_notification("pmft_verify")
            return
        if not OPTIONS.get("auto_select_spool", True):
            DETECTION["note"] = "auto-select disabled"
            return
        label = detected.get("type") or detected.get("name") or "?"
        result = matching.match_result(detected, db.list_spools())
        if not result["spool"]:
            DETECTION["note"] = "no matching spool"
            log.warning(
                "Printer reports %s %s on the external spool but no spool in "
                "the library matches — add it or Load one manually.",
                label, detected.get("color"),
            )
            return

        if result["ambiguous"]:
            signature = matching.detection_signature(detected, result["candidates"])
            confirmed = db.get_meta("confirmed_detection") or {}
            if confirmed.get("signature") == signature and db.get_spool(confirmed.get("spool_id")):
                # user already told us which twin this is
                result["spool"] = db.get_spool(confirmed["spool_id"])
            else:
                DETECTION.update(
                    ambiguous=True,
                    candidates=_candidate_view(result["candidates"]),
                    note="multiple spools match — verify",
                )
                active = db.active_spool()
                if active:
                    DETECTION["spool_id"] = active["id"]
                    DETECTION["note"] = "multiple spools match — using last loaded, please verify"
                log.warning(
                    "%d spools match the loaded filament (%s); keeping '%s'. "
                    "Verify in the panel.", len(result["candidates"]), label,
                    f"{active['brand']} {active['name']}" if active else "none",
                )
                await app["sync"].notify(
                    "Poor Man's Filament Tracker",
                    f"Two or more spools match the loaded filament ({label}). "
                    "Open the Filament panel to verify which one is on the printer.",
                    "pmft_verify",
                )
                return

        match = result["spool"]
        DETECTION["spool_id"] = match["id"]
        await app["sync"].dismiss_notification("pmft_verify")
        active = db.active_spool()
        if active and active["id"] == match["id"]:
            DETECTION["note"] = "matches loaded spool"
            return
        if job_open:
            DETECTION["note"] = "match found, but not switching mid-print"
            log.warning(
                "Printer filament changed to %s mid-print; not switching the "
                "loaded spool until the job ends.", label,
            )
            return
        db.set_active(match["id"])
        DETECTION["note"] = "auto-loaded"
        log.info(
            "Auto-loaded '%s %s' to match printer filament (%s %s)",
            match["brand"], match["name"], label, detected.get("color"),
        )
        await push_state(app)

    return on_filament_detected


async def rematch(app):
    """Re-run auto-match after the spool library changes."""
    tracker = app["tracker"]
    if tracker.printer["detected"] and tracker.on_filament_detected:
        await tracker.on_filament_detected(tracker.printer["detected"], tracker.job_open)


def on_job_finished(grams, job_name, status):
    spool = db.active_spool()
    if not spool:
        log.warning(
            "Print '%s' used %.1f g but no spool is active — nothing recorded. "
            "Set an active spool in the Filament panel.", job_name, grams,
        )
        return
    kind = "print" if status == "finish" else "failed"
    db.deduct(spool["id"], grams, job_name, kind)
    log.info(
        "Recorded %.1f g from '%s %s' (%.1f g left)",
        grams, spool["brand"], spool["name"],
        db.get_spool(spool["id"])["remaining_g"],
    )


# ---------- HTTP handlers ----------

async def index(request):
    return web.FileResponse(WEB_DIR / "index.html")


async def api_catalog(request):
    return web.FileResponse(APP_DIR / "catalog.json")


async def api_status(request):
    source = request.app["source"]
    return web.json_response({
        "app": "Poor Man's Filament Tracker",
        "mode": "direct" if request.app["direct_mode"] else "ha",
        "source": source.name,
        "connected": source.connected,
        "entities": source.entities,
        "ha_available": request.app["sync"].available,
        "ha_control": {
            "available": request.app["control"].available,
            "entity_id": request.app["control"].entity_id,
        },
        "printer": request.app["tracker"].printer,
        "active_spool": db.active_spool(),
        "detection": DETECTION,
        "last_spool_change": db.last_spool_change(),
        "currency": db.get_meta("currency", "INR"),
    })


async def api_set_currency(request):
    data = await request.json()
    currency = str(data.get("currency", "INR"))[:8]
    db.set_meta("currency", currency)
    return web.json_response({"currency": currency})


async def api_list_spools(request):
    include_archived = request.query.get("archived") == "1"
    return web.json_response(db.list_spools(include_archived))


async def api_create_spool(request):
    data = await request.json()
    spool = db.create_spool(data)
    if data.get("active"):
        db.set_active(spool["id"])
    await rematch(request.app)  # a just-added spool may match the loaded filament
    await push_state(request.app)
    return web.json_response(db.get_spool(spool["id"]))


async def api_update_spool(request):
    spool = db.update_spool(int(request.match_info["id"]), await request.json())
    await rematch(request.app)
    await push_state(request.app)
    return spool_payload(spool)


async def api_delete_spool(request):
    db.delete_spool(int(request.match_info["id"]))
    await push_state(request.app)
    return web.json_response({"ok": True})


async def api_activate_spool(request):
    spool = db.set_active(int(request.match_info["id"]))
    if spool and DETECTION["ambiguous"]:
        await _confirm_spool(request.app, spool["id"])
    await push_state(request.app)
    return spool_payload(spool)


async def api_archive_spool(request):
    data = await request.json()
    spool = db.set_archived(int(request.match_info["id"]), data.get("archived", True))
    await push_state(request.app)
    return spool_payload(spool)


async def api_use_spool(request):
    """Manual deduction, e.g. a print made while the tracker was down."""
    data = await request.json()
    grams = float(data.get("grams", 0))
    if grams <= 0:
        return web.json_response({"error": "grams must be > 0"}, status=400)
    spool = db.deduct(
        int(request.match_info["id"]), grams,
        data.get("job_name", "Manual entry"), kind="manual",
    )
    await push_state(request.app)
    return spool_payload(spool)


async def api_usage(request):
    spool_id = request.query.get("spool_id")
    since = db.last_spool_change() if request.query.get("recent") == "1" else None
    return web.json_response(
        db.usage_history(int(spool_id) if spool_id else None, since=since)
    )


async def api_reassign_usage(request):
    data = await request.json()
    row = db.reassign_usage(int(request.match_info["id"]), int(data["spool_id"]))
    await push_state(request.app)
    return web.json_response(row if row else {"error": "not found"},
                             status=200 if row else 404)


async def api_update_usage(request):
    data = await request.json()
    row = db.get_usage(int(request.match_info["id"]))
    if not row:
        return web.json_response({"error": "not found"}, status=404)
    if "spool_id" in data and int(data["spool_id"]) != row["spool_id"]:
        row = db.reassign_usage(row["id"], int(data["spool_id"]))
    if "grams" in data and float(data["grams"]) != row["grams"]:
        row = db.update_usage_grams(row["id"], float(data["grams"]))
    await push_state(request.app)
    return web.json_response(row)


async def _confirm_spool(app, spool_id):
    detected = DETECTION["detected"]
    candidates = [db.get_spool(c["id"]) for c in DETECTION["candidates"]]
    candidates = [c for c in candidates if c]
    if detected and candidates:
        db.set_meta("confirmed_detection", {
            "signature": matching.detection_signature(detected, candidates),
            "spool_id": spool_id,
        })
    DETECTION.update(ambiguous=False, candidates=[], spool_id=spool_id,
                     note="verified by user")
    await app["sync"].dismiss_notification("pmft_verify")


async def api_confirm_detection(request):
    data = await request.json()
    spool_id = int(data["spool_id"])
    if not db.get_spool(spool_id):
        return web.json_response({"error": "not found"}, status=404)
    active = db.active_spool()
    if not active or active["id"] != spool_id:
        db.set_active(spool_id)
    await _confirm_spool(request.app, spool_id)
    await push_state(request.app)
    return web.json_response({"ok": True})


# ---------- wiring ----------

async def start_services(app):
    ha_sync.configure(OPTIONS.get("ha_url"), OPTIONS.get("ha_token"))
    sync = HASync(db.active_spool)
    app["sync"] = sync

    async def on_ha_select(spool_id):
        spool = db.set_active(spool_id)
        if not spool:  # deleted since the selector options were built
            return
        DETECTION["note"] = "loaded from Home Assistant"
        log.info("Loaded '%s %s' (selected in HA)", spool["brand"], spool["name"])
        await sync.push_sensor()

    control = HAControl(on_ha_select)
    app["control"] = control

    tracker = PrintTracker(
        OPTIONS, on_job_finished,
        on_filament_detected=make_detection_handler(app),
        on_job_deducted=sync.push_sensor,
    )
    app["tracker"] = tracker

    direct = all(OPTIONS.get(k) for k in
                 ("printer_host", "printer_serial", "printer_access_code"))
    app["direct_mode"] = direct
    if direct:
        from source_bambu import BambuSource
        source = BambuSource(OPTIONS, tracker)
        log.info("Using direct printer connection to %s (HA-independent)",
                 OPTIONS["printer_host"])
    else:
        from source_ha import HASource
        source = HASource(OPTIONS, tracker, sync.session())
        log.info("Using ha-bambulab sensors via Home Assistant")
    app["source"] = source

    app["tasks"] = [
        asyncio.create_task(source.run()),
        asyncio.create_task(sync.run()),
        asyncio.create_task(control.run()),
    ]


async def stop_services(app):
    for task in app["tasks"]:
        task.cancel()


@web.middleware
async def no_cache_middleware(request, handler):
    """The UI iterates often; without this, browsers heuristically cache
    index.html/app.js/style.css (aiohttp's static handler sends no explicit
    Cache-Control) and a long-lived tab — e.g. an HA dashboard iframe —
    can keep serving a stale build long after a redeploy."""
    response = await handler(request)
    if request.path == "/" or request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


def make_app():
    app = web.Application(middlewares=[no_cache_middleware])
    app.on_startup.append(start_services)
    app.on_cleanup.append(stop_services)
    app.router.add_get("/", index)
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/catalog", api_catalog)
    app.router.add_get("/api/spools", api_list_spools)
    app.router.add_post("/api/spools", api_create_spool)
    app.router.add_put("/api/spools/{id}", api_update_spool)
    app.router.add_delete("/api/spools/{id}", api_delete_spool)
    app.router.add_post("/api/spools/{id}/activate", api_activate_spool)
    app.router.add_post("/api/spools/{id}/archive", api_archive_spool)
    app.router.add_post("/api/spools/{id}/use", api_use_spool)
    app.router.add_get("/api/usage", api_usage)
    app.router.add_post("/api/usage/{id}/reassign", api_reassign_usage)
    app.router.add_put("/api/usage/{id}", api_update_usage)
    app.router.add_post("/api/detection/confirm", api_confirm_detection)
    app.router.add_post("/api/settings/currency", api_set_currency)
    app.router.add_static("/static", WEB_DIR)
    return app


if __name__ == "__main__":
    db.init()
    web.run_app(make_app(), port=PORT)
