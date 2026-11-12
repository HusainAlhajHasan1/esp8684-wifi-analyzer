# ESP8684 Wi-Fi Network Analyzer

Real-hardware Wi-Fi discovery, monitoring and signal analysis on an **ESP8684
(ESP32-C2)**, driven from a **native Android app** over USB serial.

No simulated data anywhere: every network shown by this project comes from an
`esp_wifi_scan` on the physical radio. If a feature is not implemented, the
firmware says `E_UNSUPPORTED` — it never invents results.

---

## Status

| Phase | Scope | State |
|---|---|---|
| **1** | Hardware verification, USB-serial link, JSON protocol, basic commands | ✅ **Accepted on hardware** — 23/23 checks, `PHASE 1 LINK OK` (v0.2.1) |
| **2** | Wi-Fi scanner, classification, real `scan_result` frames | ✅ **Accepted on hardware** — real 13-network sweep, 3 pages reassembled, 0 violations (v0.2.1) |
| **3** | Android app (Kotlin/Compose, USB host, scanner UI) | 🚧 **Source complete** — scan/filter/details client in `android/`; APK built by CI (see [`.github/workflows/android.yml`](.github/workflows/android.yml)) |
| 4 | Signal monitor, channel overview, SoftAP lab mode | Not started |
| 5 | History (Room + optional Supabase), export, AI assistant | Schema drafted, unapplied |
| 6 | Arabic + English, polish, full docs | Not started |

The Android client is deliberately **passive**: it displays what the radio
advertises and nothing more. What this project will never implement — cloned APs,
WPS attack modes, credential capture — is written down in
[`docs/SECURITY_BOUNDARIES.md`](docs/SECURITY_BOUNDARIES.md).

Contract version: **v3** (`PROTOCOL.md` §7/§8). Firmware: **v0.2.1**. One documented
deviation is carried against v0.2.1 (`wpa3-enterprise` lacks the `strong` flag),
accepted and deferred — see `LIMITATIONS.md` §3 and issue HSE-11.

### Phase 1 build facts (measured, not estimated)

```
Target   : esp32c2  (ESP8684H v2.0, 4 MB GD flash) — confirmed on hardware
Toolchain: ESP-IDF v5.5.5, pioarduino platform 55.03.312
RAM      : 11.6%  — 32,292 B of 278,528 B   (heap_free ~72 KB after Wi-Fi init)
Flash    : 17.5%  — 732,580 B of 4,194,304 B (app partition 3.9 MB)
Warnings : none from firmware sources (-Wall -Wextra)
Images   : app 733,152 B · FACTORY 798,688 B · DIAG_FACTORY 798,816 B · chip_id 0x000C
```

---

## What Phase 1 delivers

**Firmware** (`firmware/`) — modular ESP-IDF C, non-blocking, watchdog-supervised:

| Module | Responsibility |
|---|---|
| `tools/host/protocol_simulator.py` | Replays the acceptance suite on a virtual port (no network data, ever) |
| `serial_manager.c` | Owns UART0 (GPIO20 TX / GPIO19 RX). Line assembly, overrun recovery, atomic frame writes |
| `protocol.c` | JSON-lines framer: `v`/`id`/`seq`/`type`/`ts` envelope, error frames |
| `command_processor.c` | Command table, validation, heartbeat timer |
| `app_config.c` | NVS-persisted config with range validation (all-or-nothing writes) |
| `device_info.c` | Chip, revision, flash size, eFuse MAC, IDF version, reset reason |
| `log_router.c` | Funnels **all** ESP-IDF logs into JSON `event.log` frames so the stream can never be corrupted |

**Commands:** `hello`, `ping`, `echo`, `info`, `stats`, `config.get`,
`config.set`, `hb.set`, `reboot`.

**Flashable artifacts** (`firmware/artifacts/`): factory image, app, bootloader,
partition table, `SHA256SUMS.txt`.

**Scanner** (`wifi_scanner.c`, `network_classifier.c`) — real `esp_wifi_scan` in
STA mode on its own task, results delivered as paged `scan_result` frames with
`quality`, `security`, `hidden`, `wps`, `legacy`, `weak`, `strong` flags computed
on the device so every client agrees. Paging uses measure-then-rebuild: peak
~6 KB, against ~72 KB free after Wi-Fi init.

**Validation harness** (`tools/host/serial_probe.py`) — two suites:
`probe` (23 checks over the real link: handshake, RTT distribution, `seq` gaps,
payload integrity, malformed-input resilience, config round-trip, throughput,
reconnect) and `scan` (one real sweep, printed as a table and validated field by
field against the contract).

**Offline regression tests** (`tools/host/test_scan_collect.py`, 5 checks) — paged
sweep reassembly plus the classification-flag contract. Synthetic fixture only;
it never substitutes for a real sweep.

**Docs:** [`PROTOCOL.md`](docs/PROTOCOL.md) ·
[`HARDWARE.md`](docs/HARDWARE.md) ·
[`PHASE1_TEST_PLAN.md`](docs/PHASE1_TEST_PLAN.md) ·
[`LIMITATIONS.md`](docs/LIMITATIONS.md)

---

## Quick start

```bash
# 0. identify the board (bridge chip + real flash size)
python -m esptool --chip esp32c2 -p <PORT> flash-id

# 1. build
cd firmware && pio run

# 2. flash the complete image
python -m esptool --chip esp32c2 -p <PORT> -b 460800 write-flash \
  --flash-mode dio --flash-freq 60m --flash-size 4MB \
  0x0 artifacts/esp8684_analyzer_phase2_v0.2.1_FACTORY.bin

# 3. run the acceptance suite
cd ../tools/host && pip install -r requirements.txt
python serial_probe.py list
python serial_probe.py probe --port <PORT> --report phase1_report.json

# 4. take a real sweep
python serial_probe.py scan  --port <PORT> --json scan1.json
```

Expected: `{"type":"event.boot",…}` within ~2 s of reset, then a heartbeat every
5 s, then **23/23 checks passed — PHASE 1 LINK OK**, and a scan table ending in
`[PASS] every field is consistent with the contract (no invented data)`.

> `scan1.json` contains real neighbour SSIDs and BSSIDs. It is git-ignored by
> default: measurement data stays on the machine that measured it.

---

## Repository layout

```
firmware/                 ESP-IDF firmware (PlatformIO)
  platformio.ini          pinned toolchain + why (stock espressif32 cannot build C2)
  boards/                 custom ESP8684-DevKitM-1 manifest
  src/                    modular firmware sources
  artifacts/              prebuilt flashable binaries + checksums
docs/
  PROTOCOL.md             normative USB-serial protocol v1
  HARDWARE.md             board identification, wiring, flashing, error table
  PHASE1_TEST_PLAN.md     step-by-step acceptance criteria
  LIMITATIONS.md          real hardware/design limits
  SECURITY_BOUNDARIES.md  what this project refuses to build, and why
  sql/                    Supabase schema for optional cloud history (Phase 5)
tools/host/
  serial_probe.py         link + scan harness (list / probe / scan / self-test)
  test_scan_collect.py    offline regression tests: paging + flag contract
  protocol_simulator.py   virtual device for offline suite runs (no network data)
tools/windows/
  1_identify_and_flash.bat  thin launcher: identify board + flash firmware (no toolchain needed)
  2_run_probe.bat           thin launcher: run the acceptance suite, write the report
  run_phase1.py             the real logic (ports, esptool, flash, probe, clipboard)
  README_WINDOWS.md         step-by-step Windows walkthrough + troubleshooting
android/                  Android client (Phase 3): Kotlin + Compose + USB host
  README.md               what it does, what it refuses to do, how to build
.github/workflows/        CI: builds the debug APK on every push
```

---

## Two build decisions worth knowing

1. **Stock PlatformIO cannot build for ESP32-C2.** `platformio/espressif32`
   ships no C2 board definition, and its ESP-IDF builder selects the RISC-V
   toolchain only for `esp32c3`/`esp32c6`, so `mcu=esp32c2` silently gets the
   Xtensa toolchain and fails. The project therefore pins
   **pioarduino 55.03.312** (ESP-IDF 5.5.5), which supports C2 properly. Pinned,
   not floating.

2. **Flash size is measured, not assumed: 4 MB.** The board reported
   `ESP8684H (revision v2.0), Embedded Flash 4MB (GD)` via `esptool flash-id`,
   so the partition table now gives the app 3.9 MB (`0x3F0000` at `0x10000`)
   instead of the earlier conservative 2 MB safety default.

---

## Hard rules this project follows

- Never fabricate scan results. Phase 1 firmware refuses `scan.*` explicitly.
- No deauthentication, jamming, or credential extraction — ever.
- Laboratory mode (SoftAP) is restricted to the user's own equipment.
- No passwords, keys or credentials are ever sent to any AI service; only
  structured scan metadata would be.
- Every hardware limitation is documented in `LIMITATIONS.md` rather than hidden.
