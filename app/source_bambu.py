"""Print data source: direct connection to the Bambu Lab printer.

Works without Home Assistant, so prints are tracked even while HA is down:
- MQTT (TLS :8883, user bblp + LAN access code) for print status, progress,
  task name, and the external spool's filament setting (vt_tray).
- FTPS (implicit TLS :990) to fetch the sliced 3MF and read the print's
  filament weight from Metadata/slice_info.config (the MQTT stream does
  not carry weight). If the fetch fails the print is recorded with 0 g
  and can be corrected from the history panel.

The printer's MQTT payloads are partial updates; vt_tray fields are merged
into a cache before being interpreted.
"""
import asyncio
import ftplib
import io
import json
import logging
import socket
import ssl
import xml.etree.ElementTree as ET
import zipfile

log = logging.getLogger("source.bambu")


class ImplicitFTPTLS(ftplib.FTP_TLS):
    """ftplib only speaks explicit FTPS; Bambu printers use implicit TLS."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sock = None

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value)
        self._sock = value


def _ssl_context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # printer uses a self-signed cert
    return ctx


def parse_slice_weight(threemf_bytes):
    """Sum used_g over all filaments in the 3MF's slice_info.config."""
    with zipfile.ZipFile(io.BytesIO(threemf_bytes)) as zf:
        with zf.open("Metadata/slice_info.config") as f:
            root = ET.parse(f).getroot()
    grams = 0.0
    for fil in root.iter("filament"):
        try:
            grams += float(fil.get("used_g", 0))
        except (TypeError, ValueError):
            pass
    return round(grams, 2)


def fetch_print_weight(host, access_code, task_name, gcode_file):
    """Blocking: download the job's 3MF over FTPS and return its weight."""
    candidates = []
    if task_name:
        candidates.append(f"/cache/{task_name}.3mf")
    if gcode_file:
        candidates += [f"/cache/{gcode_file}", f"/{gcode_file}", f"/model/{gcode_file}"]
    ftp = ImplicitFTPTLS(context=_ssl_context(), timeout=20)
    try:
        ftp.connect(host, 990)
        ftp.login("bblp", access_code)
        ftp.prot_p()
        for path in candidates:
            buf = io.BytesIO()
            try:
                ftp.retrbinary(f"RETR {path}", buf.write)
            except ftplib.all_errors:
                continue
            try:
                return parse_slice_weight(buf.getvalue())
            except Exception as exc:
                log.warning("Fetched %s but could not parse weight: %s", path, exc)
        log.warning("No readable 3MF found on printer (tried %s)", candidates)
        return 0.0
    finally:
        try:
            ftp.quit()
        except (ftplib.all_errors, OSError, EOFError):
            pass


class BambuSource:
    name = "Direct printer connection"

    def __init__(self, options, tracker):
        self.options = options
        self.tracker = tracker
        self.host = options["printer_host"]
        self.serial = options["printer_serial"]
        self.access_code = str(options["printer_access_code"])
        self.connected = False
        self.entities = {"direct": f"mqtts://{self.host}:8883"}
        self._vt = {}          # merged vt_tray cache (payloads are partial)
        self._gcode_file = ""
        self._queue = asyncio.Queue()
        self._weight_task = None

    async def run(self):
        import paho.mqtt.client as mqtt  # lazy: not needed in HA mode

        loop = asyncio.get_running_loop()

        def on_connect(client, userdata, flags, rc, *args):
            client.subscribe(f"device/{self.serial}/report")
            # ask for a full state dump; subsequent reports are deltas
            client.publish(
                f"device/{self.serial}/request",
                json.dumps({"pushing": {"sequence_id": "1", "command": "pushall"}}),
            )
            loop.call_soon_threadsafe(self._queue.put_nowait, {"_connected": True})
            log.info("Connected to printer MQTT at %s", self.host)

        def on_disconnect(client, userdata, *args):
            loop.call_soon_threadsafe(self._queue.put_nowait, {"_connected": False})

        def on_message(client, userdata, msg):
            try:
                payload = json.loads(msg.payload)
            except ValueError:
                return
            loop.call_soon_threadsafe(self._queue.put_nowait, payload)

        try:  # paho 2.x renamed the constructor signature
            client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION1, protocol=mqtt.MQTTv311
            )
        except AttributeError:
            client = mqtt.Client(protocol=mqtt.MQTTv311)
        client.username_pw_set("bblp", self.access_code)
        client.tls_set_context(_ssl_context())
        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_message = on_message
        client.reconnect_delay_set(min_delay=5, max_delay=60)
        client.connect_async(self.host, 8883, keepalive=30)
        client.loop_start()

        try:
            while True:
                payload = await self._queue.get()
                if "_connected" in payload:
                    self.connected = payload["_connected"]
                    continue
                try:
                    await self._handle(payload.get("print") or {})
                except Exception:
                    log.exception("Failed to handle printer report")
        finally:
            client.loop_stop()
            client.disconnect()

    async def _handle(self, p):
        if "gcode_file" in p:
            self._gcode_file = p["gcode_file"] or self._gcode_file
        if "subtask_name" in p:
            await self.tracker.apply("task", p["subtask_name"])
        if "mc_percent" in p:
            await self.tracker.apply("progress", p["mc_percent"])
        if "vt_tray" in p:
            self._vt.update(p["vt_tray"] or {})
            await self._apply_vt()
        if "gcode_state" in p:
            state = (p["gcode_state"] or "").lower()
            was_open = self.tracker.job_open
            await self.tracker.apply("status", state)
            if self.tracker.job_open and not was_open:
                self._start_weight_fetch()

    async def _apply_vt(self):
        tray_type = self._vt.get("tray_type") or ""
        if not tray_type:
            await self.tracker.apply("external", None)
            return
        name = self._vt.get("tray_sub_brands") or tray_type
        color = self._vt.get("tray_color") or ""
        await self.tracker.apply("external", name, {
            "type": tray_type,
            "color": f"#{color}" if color else "",
            "active": True,
        })

    def _start_weight_fetch(self):
        # Called once per job start. A fetch still running belongs to the
        # previous job — cancel it so its weight can't land on this one.
        if self._weight_task and not self._weight_task.done():
            self._weight_task.cancel()
        task_name = self.tracker.printer["task"]
        gcode_file = self._gcode_file

        async def fetch():
            grams = await asyncio.to_thread(
                fetch_print_weight, self.host, self.access_code,
                task_name, gcode_file,
            )
            if grams > 0:
                log.info("Print weight from 3MF: %.1f g", grams)
                await self.tracker.apply("weight", grams)

        self._weight_task = asyncio.create_task(fetch())
