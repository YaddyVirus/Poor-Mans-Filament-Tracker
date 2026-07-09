"""Source-agnostic print state machine.

Fed by either the HA websocket source or the direct printer MQTT source
with the same normalized roles: status, weight, progress, task, external.
Fires on_job_finished when a job completes and on_filament_detected when
the external-spool filament setting changes.
"""
import logging

log = logging.getLogger("tracker")

# print status values that mean "a job is underway" (ha-bambulab strings;
# the direct source lowercases the printer's gcode_state to match)
ACTIVE_STATES = {"prepare", "running", "pause", "slicing", "init"}
DONE_STATES = {"finish", "failed"}
EMPTY_STATES = (None, "", "unknown", "unavailable", "Empty", "empty")


def _f(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class PrintTracker:
    def __init__(self, options, on_job_finished, on_filament_detected=None,
                 on_job_deducted=None):
        self.options = options
        self.on_job_finished = on_job_finished          # (grams, job_name, status)
        self.on_filament_detected = on_filament_detected  # async (detected, job_open)
        self.on_job_deducted = on_job_deducted          # async (), after a deduction
        self.printer = {
            "status": None, "weight": 0.0, "progress": 0.0, "task": "",
            "detected": None,
        }
        self.job_open = False
        self._last_weight = 0.0
        self._last_progress = 0.0
        self._detection_dirty = False

    async def apply(self, role, value, attrs=None):
        old_status = self.printer["status"]
        self._set(role, value, attrs)
        if role == "status" and self.printer["status"] != old_status:
            await self._on_status_change(self.printer["status"])
        await self._maybe_notify_detection()

    def _set(self, role, value, attrs):
        if role == "status":
            self.printer["status"] = value
        elif role == "weight":
            self.printer["weight"] = _f(value)
            if self.printer["weight"] > 0:
                self._last_weight = self.printer["weight"]
        elif role == "progress":
            self.printer["progress"] = _f(value)
            if self.job_open and self.printer["progress"] > 0:
                self._last_progress = self.printer["progress"]
        elif role == "task":
            if value and value not in EMPTY_STATES:
                self.printer["task"] = value
        elif role == "external":
            attrs = attrs or {}
            if value in EMPTY_STATES:
                detected = None
            else:
                detected = {
                    "name": value,
                    "type": attrs.get("type") or "",
                    "color": attrs.get("color") or "",
                    "active": bool(attrs.get("active")),
                }
            if detected != self.printer["detected"]:
                self.printer["detected"] = detected
                self._detection_dirty = True

    def arm_if_printing(self):
        """Call after initial state load: if we started mid-print, arm the
        job so the eventual finish still counts."""
        if self.printer["status"] in ACTIVE_STATES:
            self.job_open = True

    async def _maybe_notify_detection(self):
        if not self._detection_dirty:
            return
        self._detection_dirty = False
        log.info("External spool reported by printer: %s", self.printer["detected"])
        if self.on_filament_detected:
            try:
                await self.on_filament_detected(self.printer["detected"], self.job_open)
            except Exception:
                log.exception("Filament auto-match failed")

    async def _on_status_change(self, status):
        if status in ACTIVE_STATES:
            if not self.job_open:
                self.job_open = True
                self.printer["weight"] = 0.0
                self._last_weight = 0.0
                self._last_progress = 0.0
                log.info("Print started: %s", self.printer["task"] or "(unnamed)")
            return

        if status in DONE_STATES and self.job_open:
            self.job_open = False
            grams = self.printer["weight"] or self._last_weight
            if status == "failed":
                if not self.options.get("deduct_on_failed", True):
                    log.info("Print failed; deduct_on_failed is off, skipping")
                    return
                grams *= max(0.0, min(self._last_progress, 100.0)) / 100.0
            job = self.printer["task"] or "(unnamed print)"
            if grams <= 0:
                log.warning(
                    "Job '%s' ended (%s) with no weight available — recording "
                    "0 g; correct it from the history panel.", job, status,
                )
            else:
                log.info("Print %s: deducting %.1f g for '%s'", status, grams, job)
            self.on_job_finished(round(grams, 2), job, status)
            if self.on_job_deducted:
                await self.on_job_deducted()
        elif status == "idle":
            self.job_open = False
