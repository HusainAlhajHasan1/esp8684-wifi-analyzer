# Phase 1 — Acceptance Test Plan

Goal: prove the hardware, the toolchain and the USB-serial link are real and
reliable **before** any Wi-Fi code exists. Everything here is measured on your
bench; nothing is assumed.

Do these five steps in order. Each has a hard pass criterion. If a step fails,
stop there — later steps depend on it.

---

## Step 0 — Identify the silicon (2 min)

```bash
lsusb                                                  # or Device Manager / system_profiler
python -m esptool --chip esp32c2 -p <PORT> flash-id
```

**Pass when:** a bridge chip is identified and `flash-id` reports the chip plus an
explicit `Detected flash size`.

**Status: DONE** — `ESP8684H (revision v2.0)`, `Embedded Flash 4MB (GD)`,
MAC `b8:7b:4d:c8:aa:0c`. The partition table has been rebuilt for 4 MB
(app partition `0x3F0000`).

> Why it matters: you reported a CH34x bridge on a board that normally ships a
> CP2102N. That determines the flash size we can safely declare and whether
> auto-reset works. Getting this wrong produces mysterious boot loops later.

---

## Step 1 — Build from source (5 min, no hardware)

```bash
cd firmware
pio run
```

**Pass when:** `SUCCESS`, and the size report shows an image with headroom.
Reference values from the build used to produce the shipped binary:

```
RAM:   [          ]   4.2% (used 11,680 bytes from 278,528 bytes)
Flash: [          ]   4.7% (used 195,202 bytes from 4,194,304 bytes)
```

**Report back:** the whole build log if it fails, otherwise just the two size
lines.

---

## Step 2 — Flash and see the boot frame

```bash
# one-shot full image (bootloader + partitions + app)
python -m esptool --chip esp32c2 -p <PORT> -b 460800 \
  write-flash --flash-mode dio --flash-freq 60m --flash-size 4MB \
  0x0 firmware/artifacts/esp8684_analyzer_phase2_v0.2.1_FACTORY.bin

pio device monitor -b 115200
```

**Pass when:** within ~2 s of reset you see a single JSON line beginning:

```json
{"v":1,"id":0,"seq":1,"type":"event.boot","ts":…,"data":{"model":"ESP8684-DevKitM-1",…}}
```

and then one `event.heartbeat` every 5 s.

**Fail signatures and what they mean:**

| What you see | Meaning |
|---|---|
| Nothing at all | Wrong port, charge-only cable, or no driver |
| Human-readable log text with `I (123) esp_image:` / `rst:` | **Old/other firmware** still on the device — `erase-flash`, then re-flash |
| Boot loop / `rst:0x3 (RTC_SW_SYS_RST)` | Flash size declared too large |
| `event.boot` but no heartbeats | Timer task issue — send the full log |

---

## Step 3 — Automated link acceptance

```bash
cd tools/host
pip install -r requirements.txt
python serial_probe.py list
python serial_probe.py probe --port <PORT> --report phase1_report.json --csv pings.csv
```

**Pass when:** `PHASE 1 LINK OK`, all checks green. The suite verifies:

| Check | Criterion |
|---|---|
| `handshake` | `hello` answered within 6 attempts |
| `protocol_version` | device `proto` == host `1` |
| `device_info` | real MAC and non-zero flash size reported |
| `ping_latency` | max RTT < 250 ms (expect 5–40 ms) |
| `frame_sequence_integrity` | **zero missing `seq` values** across the run |
| `echo_16..1024B` | byte-identical round-trips |
| `oversize_rejected` | 2000-byte echo → `E_TOO_LARGE` |
| `garbage_rejected` | non-JSON → `E_BAD_JSON` |
| `unknown_cmd_rejected` | → `E_UNKNOWN_CMD` |
| `truncated_json_rejected` | → `E_BAD_JSON` |
| `inbound_overflow_rejected` | 4000-byte line → `E_TOO_LARGE` |
| `survives_bad_input` | still answers `ping` after being abused |
| `no_placeholder_data` | `scan.start` → `E_UNSUPPORTED` (**no fake networks, ever**) |
| `config_persistence` | set → read back → restore |
| `config_validation` | `scan_interval_ms: 10` → `E_BAD_PARAM` |
| `heartbeat` | unsolicited `event.heartbeat` observed |
| `throughput` | > 20 frames/s at 115200 |
| `device_counters_clean` | device-side `overflow == 0`, `uart_err == 0` |
| `reconnect` | close + reopen + re-handshake |

Sanity-check the harness without hardware first if you like:

```bash
python serial_probe.py self-test     # 8/8 expected
```

**Report back:** `phase1_report.json`. That file is the Phase 1 sign-off.

---

## Step 4 — Soak test (10–30 min)

```bash
python serial_probe.py probe --port <PORT> --pings 500 --throughput-frames 2000 \
  --report soak_report.json
```

Then, while it is still connected, let the monitor run idle for a few minutes.

**Pass when:**
- `frame_sequence_integrity`: `missing=0`
- `device_counters_clean`: `overflow=0`, `uart_err=0`
- `heap_free` in consecutive `stats` calls does not trend downward (watch for
  a leak — Phase 1 allocates per frame by design, so it must return to baseline)
- no `reset_reason` change from `power_on` mid-session (would indicate a
  brownout or watchdog reset)

---

## Step 5 — Sign-off

Phase 1 is complete when:

- [ ] `flash_id` captured, flash size confirmed
- [ ] `pio run` builds clean with no warnings from `src/`
- [ ] `event.boot` observed on a real port
- [ ] `serial_probe.py probe` reports `PHASE 1 LINK OK`
- [ ] 10+ minute soak with zero missing frames and zero device-side overruns
- [ ] `phase1_report.json` archived in the repo issue

Only then do we enable the radio in Phase 2 — because from that point on, every
bug is ambiguous (protocol vs. Wi-Fi), and a proven link removes half the
search space.

---

## What to send me

1. `phase1_report.json`
2. `flash_id` output + `lsusb` line
3. The module marking on the shield
4. Anything that failed, verbatim

With those I can pin the flash size, finalise the board manifest, and start
Phase 2 (`WiFiScanner` + real `scan_result` frames).
