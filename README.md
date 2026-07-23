# megatec-q1-bridge
Reads a generic MegaTec/Q1 UPS directly over USB and serves the readings as JSON over HTTP

Reads a generic MegaTec / Q1 protocol UPS directly over USB and serves the
readings as JSON over HTTP. No vendor software, no daemon, no trial licence.

Written because the bundled Windows software gave readings I could not rely on,
and then its trial expired — and registering it required an internet connection
the machine did not have. I needed trustworthy power data for a long-running
process on another box, so I went at the UPS directly instead.

```
$ curl http://192.168.1.50:5570/ups
{
  "input_voltage_v": 241.3,
  "output_voltage_v": 241.3,
  "battery_voltage_v": 27.1,
  "battery_percent": 95,
  "load_percent": 18,
  "frequency_hz": 50.0,
  "temperature_c": null,
  "on_battery": false,
  "battery_low": false,
  "ups_failed": false,
  "status_raw": "00001001",
  "ok": true,
  "age_seconds": 2.4,
  "stale": false
}
```

---

## Should you use this instead of NUT?

Probably not, and I would rather say so up front.

[Network UPS Tools](https://networkupstools.org/) is the mature, well-tested
answer for UPS monitoring, and its `nutdrv_qx` driver covers the MegaTec/Q1
family including many USB variants. **Try NUT first.**

This exists for the narrower case where that has not worked out: an unbranded
UPS presenting as `VID_0001 / PID_0000`, vendor software that reports figures
you do not trust, and a need for a single readable Python file you can audit and
change rather than a driver stack to configure.

The part that may be useful to you regardless of which tool you end up with is
the transport note below.

---

## The transport quirk

On this device, the `Q1` status query is not carried over a bulk or HID
interrupt endpoint. It is issued as a **USB `GET_DESCRIPTOR` request for string
index 3, langid `0x0000`**, and the UPS answers as a UTF-16LE string descriptor:

```
(MMM.M NNN.N PPP.P QQQ RR.R SS.S TT.T bbbbbbbb<CR>
 input fault output load freq battV temp status-bits
```

In code:

```python
raw = dev.ctrl_transfer(0x80, 0x06, (0x03 << 8) | 0x03, 0x0000, 128, timeout=2000)
text = bytes(raw)[2:].decode("utf-16-le", errors="ignore")
```

Established empirically, not from a datasheet. Two practical consequences:

- **The first read after opening the device usually returns garbage** (often
  `ÿÿQ1`). The bridge does one throwaway read on open and validates every reply,
  retrying up to four times before raising.
- A reply is only accepted if it starts with `(` and splits into at least eight
  whitespace-separated fields. Anything else is treated as transient.

The eight status characters are decoded as: on battery, battery low, bypass /
boost / buck, UPS fault, offline type, test in progress, shutdown active, beeper
enabled.

---

## Requirements

- Python 3.9+
- [`pyusb`](https://pypi.org/project/pyusb/)
- **Windows:** `libusb-1.0.dll` (64-bit) beside the script, and the UPS bound to
  WinUSB using [Zadig](https://zadig.akeo.ie/)
- **Linux:** untested, but should work with `pyusb` and a udev rule granting
  access to the device

---

## Setup (Windows)

1. Install pyusb: `pip install pyusb`
2. Download `libusb-1.0.dll` (64-bit) and place it next to `ups_bridge.py`
3. Run Zadig, enable **Options → List All Devices**, select the UPS, and install
   the **WinUSB** driver for it

   > This replaces the vendor driver for that device. Vendor monitoring software
   > will stop working. That is usually the point, but know it before you do it.
4. `python ups_bridge.py`

You should see a reader thread start, then a status line every five minutes:

```
2026-07-20 14:02:11 INFO in=241.3V out=241.3V load=18% batt=95%(27.1V) OL
```

---

## Endpoints

| Path | Returns |
|---|---|
| `/ups` | Latest reading, with `age_seconds` and a `stale` flag. `503` if no successful read yet. |
| `/health` | Bridge status: reads succeeded, reads failed, age of last good read, last error. |

`stale` is set once a reading is older than `STALE_AFTER` (default 90s), so a
consumer can tell a fresh reading from a frozen one without guessing.

---

## Tuning

Edit the constants at the top of the file.

| Constant | Meaning |
|---|---|
| `VID` / `PID` | USB identifiers. Change if your device differs. |
| `BATT_V_FULL` / `BATT_V_EMPTY` | Battery voltage → percent map. **Defaults are for a 24V two-pack system. Set these for your battery or the percentage will be meaningless.** |
| `POLL_INTERVAL` | Seconds between reads (default 5) |
| `STALE_AFTER` | Age at which a reading is flagged stale (default 90) |
| `VOLTAGE_LOW_V` / `VOLTAGE_HIGH_V` | Mains band for brownout / surge warnings (defaults are 230V ±10%) |
| `LOW_BATTERY_PCT` / `CRIT_BATTERY_PCT` / `HIGH_LOAD_PCT` | Warning thresholds |

Some units — including mine — have no temperature sensor and report `--.-`.
That is parsed as `null` rather than zero, so a missing sensor is not mistaken
for a cold one.

---

## Behaviour worth knowing

- **Warnings are edge-triggered.** A condition logs when it starts and when it
  clears, rather than on every poll, so a long outage does not bury the log.
- **Device loss is recoverable.** If the UPS is unplugged or loses its WinUSB
  binding, the reader drops the handle and retries; the last good reading stays
  served with a rising `age_seconds` and the `stale` flag set.
- **Input voltage is only range-checked on mains.** On battery it reads near
  zero by design, which would otherwise trip a false brownout warning.
- The reader runs in a daemon thread; the HTTP server runs in the main thread.
  Shared state is behind a lock.

---

## Security

The HTTP server has **no authentication and binds to `0.0.0.0`**. It is
intended for a trusted local network. Do not expose it to the internet. If you
only need it locally, change `LISTEN_HOST` to `127.0.0.1`.

---

## Status

Working and in continuous use, but this is one file tested against one UPS. The
Q1 field layout is standard across the MegaTec family; the string-descriptor
transport may not be. If it works on your device — or does not — an issue saying
which model would be genuinely useful.

## Licence

MIT.

## Attribution

I'm not a Python programmer. The protocol behaviour documented here I
established by trial and observation on my own hardware, and the design,
testing and tuning are mine; the code itself was written with AI assistance.
Worth saying plainly — and it means that if you file an issue, my answers may
be slower than you'd expect from someone who wrote every line.
