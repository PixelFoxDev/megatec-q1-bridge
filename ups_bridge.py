#!/usr/bin/env python3
r"""
ups_bridge.py v6 - Fox Hardware Substrate UPS Bridge (direct USB)

Reads the MEC0003 UPS (VID_0001/PID_0000) DIRECTLY over USB using its native
MegaTec "Q1" protocol, and serves the result as JSON over HTTP for
hardware_substrate.py on the NAS to poll. No UPSilon, no RupsMon, no trial.

Requires (on this laptop):
    - the UPS bound to WinUSB (via Zadig)
    - pyusb installed
    - libusb-1.0.dll (64-bit) in the same folder as this script

Protocol (established empirically 2026-07-13):
    "Q1" is issued as a USB GET_DESCRIPTOR request for string index 3,
    langid 0x0000. The UPS answers as a UTF-16 string descriptor:
        (MMM.M NNN.N PPP.P QQQ RR.R SS.S TT.T bbbbbbbb<CR>
        input  fault  output load freq battV temp status-bits
    This unit reports temperature as "--.-" (no sensor) -> temperature_c=None.

JSON contract preserved for hardware_substrate.py:
    input_voltage_v, output_voltage_v, battery_percent, load_percent,
    on_battery, ups_timestamp
Added: battery_voltage_v, frequency_hz, fault_voltage_v, temperature_c,
    status_raw, and decoded status flags.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread, Lock

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
VID = 0x0001
PID = 0x0000
Q1_STRING_INDEX = 0x03
Q1_LANGID = 0x0000

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 5570
POLL_INTERVAL = 5.0
STALE_AFTER = 90.0             # HTTP: how old a reading may be before flagged stale

HEARTBEAT_INTERVAL = 300.0     # log a healthy status line every N seconds
READ_FAIL_WARN = 3             # consecutive USB failures before PIPELINE warning

# Battery voltage -> percent map (24V / 2-pack system; tune to your battery).
BATT_V_FULL = 27.4             # ~100%
BATT_V_EMPTY = 21.0            # ~0%

# Health thresholds
VOLTAGE_LOW_V = 207.0          # 230V -10% (only checked while on mains)
VOLTAGE_HIGH_V = 253.0         # 230V +10%
LOW_BATTERY_PCT = 50
CRIT_BATTERY_PCT = 20
HIGH_LOAD_PCT = 80

# Megatec Q1 status bit positions (left to right in the 8-char field).
STATUS_BITS = [
    ("on_battery",       "utility/mains fail - running on battery"),
    ("battery_low",      "battery low"),
    ("bypass_boost",     "bypass / boost / buck active"),
    ("ups_failed",       "UPS fault"),
    ("offline_type",     "UPS is offline/line-interactive type"),
    ("test_in_progress", "battery test in progress"),
    ("shutdown_active",  "shutdown active"),
    ("beeper_on",        "beeper enabled"),
]

_state_lock = Lock()
_latest: dict | None = None
_last_read_ok: float = 0.0
_last_error: str | None = None
_read_count: int = 0
_error_count: int = 0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ups_bridge")


# ---------------------------------------------------------------------------
# USB / Q1
# ---------------------------------------------------------------------------
def _load_backend():
    import usb.backend.libusb1
    here = os.path.dirname(os.path.abspath(__file__))
    dll = os.path.join(here, "libusb-1.0.dll")
    if os.path.exists(dll):
        return usb.backend.libusb1.get_backend(find_library=lambda _: dll)
    return usb.backend.libusb1.get_backend()


def open_device():
    """Find and configure the UPS. Returns the device or None if absent."""
    import usb.core
    dev = usb.core.find(idVendor=VID, idProduct=PID, backend=_load_backend())
    if dev is None:
        return None
    try:
        dev.set_configuration()
    except Exception:
        pass
    # one throwaway read to flush any stale buffer (the 'ÿÿQ1' first-read quirk)
    try:
        _raw_q1(dev)
    except Exception:
        pass
    return dev


def _raw_q1(dev):
    """One raw Q1 exchange -> decoded ASCII reply (may be garbage; caller validates)."""
    raw = bytes(dev.ctrl_transfer(
        0x80, 0x06, (0x03 << 8) | Q1_STRING_INDEX, Q1_LANGID, 128, timeout=2000))
    if len(raw) < 4:
        return ""
    text = raw[2:].decode("utf-16-le", errors="ignore")
    for term in ("\r", "\x00"):
        i = text.find(term)
        if i != -1:
            text = text[:i]
    return text.strip()


def read_q1(dev, retries=4):
    """Read a VALID Q1 reply, retrying past the occasional garbage first-read."""
    last = None
    for _ in range(retries):
        text = _raw_q1(dev)   # USBError (device gone) propagates out
        if text.startswith("(") and len(text[1:].split()) >= 8:
            return text
        last = text
        time.sleep(0.05)
    raise ValueError(f"no valid Q1 after {retries} tries (last: {last!r})")


def parse_q1(reply):
    """Parse a Megatec Q1 reply string into the bridge's JSON dict."""
    fields = reply[1:].split()

    def fnum(s):
        return None if set(s) <= set("-.") else float(s)

    input_v = float(fields[0])
    fault_v = float(fields[1])
    output_v = float(fields[2])
    load_pct = int(fields[3])
    freq_hz = float(fields[4])
    batt_v = float(fields[5])
    temp_c = fnum(fields[6])
    status = fields[7]

    flags = {name: (len(status) > i and status[i] == "1")
             for i, (name, _d) in enumerate(STATUS_BITS)}

    pct = 100.0 * (batt_v - BATT_V_EMPTY) / (BATT_V_FULL - BATT_V_EMPTY)
    batt_pct = int(round(max(0.0, min(100.0, pct))))

    now = time.time()
    d = {
        "input_voltage_v": round(input_v, 1),
        "fault_voltage_v": round(fault_v, 1),
        "output_voltage_v": round(output_v, 1),
        "load_percent": load_pct,
        "frequency_hz": round(freq_hz, 1),
        "battery_voltage_v": round(batt_v, 2),
        "battery_percent": batt_pct,
        "temperature_c": temp_c,
        "on_battery": flags["on_battery"],
        "status_raw": status,
        "ups_timestamp": int(now),
        "ups_timestamp_age_seconds": 0.0,
        "q1_raw": reply,
        "bridge_read_at": now,
    }
    d.update(flags)
    return d


# ---------------------------------------------------------------------------
# Health evaluation + status line
# ---------------------------------------------------------------------------
def _status_line(d):
    st = "OB" if d["on_battery"] else "OL"
    return (f"in={d['input_voltage_v']}V out={d['output_voltage_v']}V "
            f"load={d['load_percent']}% "
            f"batt={d['battery_percent']}%({d['battery_voltage_v']}V) {st}")


def evaluate_health(d):
    """Return (status_word, [(category, message), ...]) for edge-triggered logging."""
    warnings = []

    if d["on_battery"]:
        warnings.append(("POWER",
                         "ON BATTERY - utility/mains has failed. UPS is carrying the load."))

    if d.get("ups_failed"):
        warnings.append(("FAULT", "UPS reports an internal fault (status bit set)."))

    if d.get("shutdown_active"):
        warnings.append(("SHUTDOWN", "UPS reports shutdown active."))

    batt = d["battery_percent"]
    if d.get("battery_low"):
        warnings.append(("BATTERY", f"UPS signals BATTERY LOW ({batt}%, {d['battery_voltage_v']}V)."))
    elif batt <= CRIT_BATTERY_PCT:
        warnings.append(("BATTERY", f"battery critically low: {batt}% ({d['battery_voltage_v']}V)."))
    elif batt <= LOW_BATTERY_PCT:
        warnings.append(("BATTERY", f"battery low: {batt}% ({d['battery_voltage_v']}V)."))

    if d["load_percent"] >= HIGH_LOAD_PCT:
        warnings.append(("LOAD", f"UPS load high: {d['load_percent']}%."))

    # Input-voltage band only makes sense on mains; on battery it reads ~0 by design.
    if not d["on_battery"]:
        v = d["input_voltage_v"]
        if v < VOLTAGE_LOW_V:
            warnings.append(("VOLTAGE", f"input voltage low: {v}V (brownout?)."))
        elif v > VOLTAGE_HIGH_V:
            warnings.append(("VOLTAGE", f"input voltage high: {v}V (surge?)."))

    return ("WARN" if warnings else "OK"), warnings


# ---------------------------------------------------------------------------
# Reader loop
# ---------------------------------------------------------------------------
def reader_loop():
    global _latest, _last_read_ok, _last_error, _read_count, _error_count

    log.info("Reader thread starting. Talking to UPS at VID_%04X/PID_%04X over USB.",
             VID, PID)

    try:
        import usb.core
        import usb.util
    except Exception as e:
        log.error("pyusb not available: %s - install pyusb and place libusb-1.0.dll "
                  "beside this script.", e)
        return

    dev = None
    consecutive_fail = 0
    fail_warned = False
    first_ok_logged = False
    active_warnings: set[str] = set()
    last_heartbeat = 0.0
    on_battery_since = None
    on_battery_min_v = None
    on_battery_min_pct = None

    while True:
        try:
            if dev is None:
                dev = open_device()
                if dev is None:
                    raise usb.core.USBError("UPS not found on USB")

            reply = read_q1(dev)
            d = parse_q1(reply)
            now = time.time()

            with _state_lock:
                _latest = d
                _last_read_ok = now
                _last_error = None
                _read_count += 1

            if fail_warned:
                log.info("[PIPELINE] UPS readable again after %d failed attempts.",
                         consecutive_fail)
                fail_warned = False
            consecutive_fail = 0

            if not first_ok_logged:
                log.info("PIPELINE ALIVE - first good reading: %s", _status_line(d))
                first_ok_logged = True
                last_heartbeat = now

            status_word, warnings = evaluate_health(d)
            current = {c for c, _ in warnings}
            for cat, msg in warnings:
                if cat not in active_warnings:
                    log.warning("[%s] %s", cat, msg)
            for cat in active_warnings - current:
                if cat == "POWER":
                    continue   # POWER restore is reported by the episode summary below
                log.info("[%s] cleared - back to normal.", cat)
            active_warnings = current

            # On-battery episode tracking: one summary line per mains-failure event.
            if d["on_battery"]:
                if on_battery_since is None:
                    on_battery_since = now
                    on_battery_min_v = d["battery_voltage_v"]
                    on_battery_min_pct = d["battery_percent"]
                else:
                    on_battery_min_v = min(on_battery_min_v, d["battery_voltage_v"])
                    on_battery_min_pct = min(on_battery_min_pct, d["battery_percent"])
            elif on_battery_since is not None:
                log.warning("[POWER] mains restored after %.0fs on battery; "
                            "battery dipped to %.2fV / %d%%.",
                            now - on_battery_since, on_battery_min_v, on_battery_min_pct)
                on_battery_since = None
                on_battery_min_v = None
                on_battery_min_pct = None

            if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                log.info("heartbeat %s - %s", status_word, _status_line(d))
                last_heartbeat = now

        except usb.core.USBError as e:
            consecutive_fail += 1
            with _state_lock:
                _error_count += 1
                _last_error = str(e)
            if dev is not None:
                try:
                    usb.util.dispose_resources(dev)
                except Exception:
                    pass
                dev = None
            if consecutive_fail >= READ_FAIL_WARN and not fail_warned:
                log.warning("[PIPELINE] cannot read the UPS (%s). USB unplugged, or "
                            "the device lost WinUSB? Retrying...", e)
                fail_warned = True

        except ValueError as e:
            # a valid reply didn't parse - transient; keep last good, don't drop device
            with _state_lock:
                _error_count += 1
                _last_error = str(e)
            log.debug("transient parse issue: %s", e)

        except Exception as e:
            log.exception("reader_loop unexpected: %s", e)
            dev = None

        time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def _send_json(self, code, payload):
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/ups", "/ups/"):
            with _state_lock:
                if _latest is None:
                    self._send_json(503, {"ok": False, "error": _last_error or "no reading yet"})
                    return
                age = time.time() - _last_read_ok
                payload = dict(_latest)
                payload["ok"] = True
                payload["age_seconds"] = round(age, 1)
                payload["stale"] = age > STALE_AFTER
                self._send_json(200, payload)
            return

        if self.path in ("/health", "/health/"):
            with _state_lock:
                age = time.time() - _last_read_ok if _last_read_ok else None
                self._send_json(200, {
                    "ok": True,
                    "bridge": "ups_bridge_v6_usb",
                    "reads_ok": _read_count,
                    "reads_failed": _error_count,
                    "last_read_age_seconds": round(age, 1) if age else None,
                    "last_error": _last_error,
                    "stale": (age is not None and age > STALE_AFTER),
                })
            return

        self._send_json(404, {"ok": False, "error": "unknown path"})

    def log_message(self, format, *args):
        msg = format % args
        if " 200 " not in msg:
            log.info("HTTP %s - %s", self.client_address[0], msg)


def serve():
    server = HTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    log.info("HTTP server listening on http://%s:%d", LISTEN_HOST, LISTEN_PORT)
    log.info("Endpoints: /ups  /health")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down (Ctrl+C)")
        server.server_close()


def main():
    reader = Thread(target=reader_loop, daemon=True, name="ups-reader")
    reader.start()
    time.sleep(POLL_INTERVAL + 0.5)
    serve()


if __name__ == "__main__":
    main()