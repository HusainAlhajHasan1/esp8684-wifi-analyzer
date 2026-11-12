# Running this on Windows — the short version

You need: **Windows 10/11**, **Python 3**, and a **USB data cable**.

> A charge-only cable is the most common reason this "doesn't work". Try another
> cable before anything else.

---

## 0. Setup (5 minutes)

1. **Install Python 3** — <https://www.python.org/downloads/>
   On the first screen, **tick “Add python.exe to PATH”**.
   Check with `python --version` → must print `Python 3.x`.

2. **Extract the ZIP to a simple path with no parentheses**, e.g.

   ```
   C:\esp8684-analyzer
   ```

   Why: a folder like `...\Downloads\esp8684-wifi-analyzer-phase1(1)\...`
   contains `( )`, which older `cmd.exe` batch logic mis-parses. The current
   scripts are Python-based and tolerate it, but a clean path removes a whole
   class of problems. Avoid OneDrive-synced folders too — they lock files
   mid-flash.

3. **Plug the board in** with a USB **data** cable.

---

## 1. Identify + flash

Double-click:

```
tools\windows\1_identify_and_flash.bat
```

It is a 3-line launcher for `run_phase1.py`, which does the real work:

| Step | What happens |
|---|---|
| Board seems silent after flashing | Firmware may be speaking **74880** (built for a 40 MHz crystal on a 26 MHz chip). `run_phase1.py` tests 115200 / 74880 / 230400 / 460800 / 9600 automatically; v0.1.1 fixes it at the source. |
| ports | lists COM ports, marks the likely board (CH340 / CP210x) |
| port | asks for it (e.g. `COM9`) |
| identify | runs `esptool flash-id`, prints chip + **real flash size**, saves `flash_id.txt` in the project folder |
| erase | asks `Erase the whole flash first? [y/N]` — **answer `y` if the board still runs vendor/factory firmware** |
| flash | writes the 4 MB factory image at `0x0` (falls back to 115200 baud automatically if 460800 fails) |
| **reset** | **asks you to press the EN / RST button** — see the warning below |
| probe | runs the acceptance test, retrying with a reset prompt if the board is silent |

### ⚠ Why the board goes silent after flashing

`esptool` prints `Hard resetting via RTS pin...`, but on CH340 clone boards the
RTS→EN line is usually **not wired**. The chip therefore stays inside the ROM
**stub flasher**, which sends nothing at all on UART0 — a "dead board" that is
perfectly healthy.

**After every esptool command, press the EN / RST button once** (or unplug and
replug the USB cable). The scripts now stop and ask you to do exactly that, and
the probe retries automatically if it sees zero bytes.

No PlatformIO, no compiler toolchain — only `esptool` (installed automatically).

**Send me `flash_id.txt`.** Expected for this board:

```
Chip type:          ESP8684H (revision v2.0)
Features:           Wi-Fi, BT 5 (LE), Single Core, 120MHz, Embedded Flash 4MB (GD)
Detected flash size: 4MB
MAC: b8:7b:4d:c8:aa:0c
```

> esptool v5 renamed commands to hyphens: `flash-id`, `erase-flash`,
> `write-flash`. Typing `flash_id` still works but prints a deprecation warning.

---

## 2. Run the acceptance test

Double-click:

```
tools\windows\2_run_probe.bat
```

Runs the 23-check suite (~30–60 s), writes `tools\host\phase1_report.json`,
and copies it to your clipboard. Expected ending:

```
23/23 checks passed  —  PHASE 1 LINK OK
```

**Send me `phase1_report.json`** — attach the file or paste it.

Want to see the raw stream yourself, without the test suite?

```
python tools\windows\run_phase1.py --watch --port COM9
```

One `event.boot` frame then a heartbeat every 5 s = correct firmware running.

### 2b. Take a real scan (Phase 2)

```bat
python tools\windows\run_phase1.py --scan --port COM9
```

One sweep, printed as a table and saved as JSON with `--json scan1.json`. The
figures are whatever is actually in the air around you — nothing is simulated.
A healthy run ends like:

```
scan_id=1 pages=3 total=13 scan_ms=2287 truncated=False
13 network(s): 1 potentially weak (open or WPS advertised), 2 hidden
[PASS] every field is consistent with the contract (no invented data)
```

Read the last line before trusting the table:

| Line | Meaning |
|---|---|
| `[PASS] every field ...` | sweep complete, all pages arrived, every field consistent |
| `only 2 of 3 page(s) arrived (missing [3])` | a page was lost — the numbers above are incomplete, re-run |
| `total=13 but 5 network objects arrived` | prefix arrived, rest lost — re-run before believing the counts |
| `(no networks in range - a real, empty sweep)` | the radio genuinely saw nothing; not an error |
| `[PASS] no violations; N known deviation(s) noted above` | sweep is valid; the `[NOTE]` lines explain a known firmware quirk (currently: WPA3-enterprise APs arrive without the `strong` flag) |

`0 potentially weak` is a statement about **configuration** (open security or WPS
advertised), not a claim that a network is exploitable. Likewise `strong` means
**WPA3-capable**, not WPA3-enforced: a `wpa2/wpa3` network still lets a client
connect with WPA2, so never read `strong` as "secure".

---

## 3. Troubleshooting (real cases, in order of likelihood)

| Symptom | Meaning | Fix |
|---|---|---|
| `Total silence: the port opened but zero bytes arrived` | The chip is left in the **ROM stub flasher** after any esptool run (RTS→EN not wired) | Press **EN / RST** (or replug USB), re-run. The tool now prompts for this and retries |
| Raw text like `I (188) esp_image: segment 4 ...` in the report | **Different firmware** is running (factory/vendor image) | Answer `y` to “Erase flash first?” and re-flash |
| `Failed to connect ... No serial data received` | Not in download mode; clone board without auto-reset | Hold **BOOT**, tap **EN**, release **BOOT**, retry |
| `Timed out waiting for packet header` | 460800 too fast for a CH340 clone | The script retries at 115200 automatically |
| `Access is denied` / port busy | Another program holds the port | Close Arduino IDE, PuTTY, Arduino Serial Monitor |
| Port missing / yellow triangle in Device Manager | Driver | Install WCH **CH341SER**: <https://www.wch-ic.com/downloads/CH341SER_EXE.html> |
| `No serial ports found at all` | Cable is charge-only, or driver missing | Different cable first, then the driver |

---

## 3b. If the board stays completely silent

Run the dedicated diagnostic — it replaces guesswork with measurements:

```
python tools\windows\diagnose.py --port COM9
```

It performs, in order:

| Step | Question it answers |
|---|---|
| **A** `verify-flash` | Are the shipped bytes really in flash? (independent of the write log) |
| **B** connect with `--before no-reset` | Is the chip idling in ROM **download mode** right now? |
| **C** `read-flash 0x10000` | Is a valid ESP32-C2 app header at the app offset? |
| **D** reset + capture @115200 | The port stays **open** while the tool pulses RTS (EN) itself, so the boot log is caught from its first byte — printed as hex *and* ASCII, and saved to `capture_D_115200.bin` |
| **D1** same at 74880 / 230400 / 460800 / 9600 | Is the board talking, but at a different baud rate? A wrong-rate stream looks like noise at 115200 |
| **E** flash the **breadcrumb** image | Same firmware plus `[WFA-DIAG]` markers written with `esp_rom_printf` from `app_main` — that call needs no driver, no logging system and no allocation, so **our own code cannot silence it**. Where the markers stop is the failing stage |

Every capture is written to disk as `capture_*.bin` and the whole session goes
into `diagnose_report.txt`. Send me **both**.

To check what your extraction actually contains (step E skipped for a missing
file last time):

```
python tools\windows\diagnose.py --check-assets
```

## 4. No Python allowed on this machine?

Use the browser flasher — Chrome or Edge only (Web Serial API):

1. Open <https://espressif.github.io/esptool-js/>
2. **Connect** → pick the port → it reports chip and flash size.
3. **Add file** → `firmware\artifacts\esp8684_analyzer_phase2_v0.2.1_FACTORY.bin`
   → flash offset **0x0**.
4. Open any serial terminal at **115200 8N1** to read the boot frame.

You should see exactly one line starting `{"v":1,"id":0,"seq":1,"type":"event.boot"`
and then a heartbeat every 5 s. **JSON, never raw log text.** That is the proof
the right firmware is running.

---

## What to send me

| Item | Where it comes from | Why |
|---|---|---|
| `flash_id.txt` | step 1 | Confirms chip + 4 MB flash (already known, but keep it on record) |
| `phase1_report.json` | step 2 | The Phase 1 sign-off: zero dropped frames on real hardware |
| One `event.boot` line | step 3 (optional) | Independent confirmation a real device is running the real firmware |

The report contains your board's MAC. Redact it if you prefer
(`"mac_sta": "AA:BB:CC:DD:EE:FF"`) — I don't need it.
