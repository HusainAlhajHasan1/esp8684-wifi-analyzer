# Hardware: identification, wiring, flashing

## 1. What the board is

| Item | Value |
|---|---|
| Board (your selection) | **ESP8684-DevKitM-1** |
| Module | ESP8684-WROOM-02C class module |
| Chip | **ESP8684H (revision v2.0)** — marketed as **ESP32-C2**, same silicon |
| Flash | **4 MB, GigaDevice (GD)**, confirmed by `esptool flash-id` |
| Board MAC | `b8:7b:4d:c8:aa:0c` (STA) |
| Core | Single RISC-V @ up to 120 MHz |
| SRAM | 272 KB usable (~278,528 B reported to the linker) |
| Radio | 2.4 GHz Wi-Fi 4 (802.11 b/g/n), Bluetooth LE 5.0 |
| Native USB | **None.** All host communication goes through the on-board USB-to-UART bridge |
| UART0 | TX = GPIO20 (U0TXD), RX = GPIO19 (U0RXD) |

### ✅ Confirmed on the actual board (2026-11-12)

```
Chip type:          ESP8684H (revision v2.0)
Features:           Wi-Fi, BT 5 (LE), Single Core, 120MHz, Embedded Flash 4MB (GD)
Crystal frequency:  26MHz
MAC:                b8:7b:4d:c8:aa:0c
Flash Manufacturer: c8  (GigaDevice)   Device: 4016
Detected flash size: 4MB
```

So: **4 MB flash**, which is what the partition table is built for (app
partition `0x3F0000` = 3.9 MB). Note the chip reports as **ESP8684H**, a variant
of the ESP8684 family — same ESP32-C2 target, just a different package/flash
option, so the firmware target is unchanged.

You also reported a **CH34x** bridge, where the genuine Espressif DevKitM-1
ships a **CP2102N** (`10C4:EA60`). A CH340/CH343 (`1A86:xxxx`) means a clone or
re-bridged variant. Consequences that still apply:

1. **Auto-reset may not be wired.** Many clones do not connect DTR/RTS to EN/BOOT.
   If flashing cannot sync, enter download mode by hand (hold **BOOT**, tap
   **EN**, release **BOOT**).
2. **Baud ceiling is lower.** CH340 clones are less reliable at 460800; the
   firmware defaults to a conservative 115200, and flashing falls back to
   115200 if 460800 fails.

### Confirm it in 60 seconds

```bash
# 1. Which bridge is actually on the board?
lsusb                                   # Linux
#   → "QinHeng Electronics CH340 serial converter" = CH34x
#   → "Silicon Labs CP210x UART Bridge"            = CP2102N
# macOS: system_profiler SPUSBDataType | grep -A6 -i "serial\|ch34\|cp210"
# Windows: Device Manager → Ports (COM & LPT) → Properties → Details → Hardware Ids

# 2. Which chip and how much flash? (authoritative — reads the silicon)
python -m esptool --chip esp32c2 -p <PORT> flash-id     # esptool v5+ uses hyphens
#   → Chip type: ESP8684H (revision v2.0)
#   → Embedded Flash 4MB (GD)
#   → Detected flash size: 4MB
#   → MAC: b8:7b:4d:c8:aa:0c
```

Send me those two outputs and I will pin `platformio.ini` to the exact flash
size instead of the safe default.

---

## 2. Wiring (only if you use an external bridge)

If the board's bridge is dead or you are wiring a bare module:

| Bridge pin | ESP8684 pin | Note |
|---|---|---|
| TX | GPIO19 (U0RXD) | bridge TX → chip RX, crossed |
| RX | GPIO20 (U0TXD) | chip TX → bridge RX |
| GND | GND | common ground is mandatory |
| 3V3 | 3V3 | **3.3 V logic only.** 5 V on a UART pin will damage the chip |
| DTR | EN (via 2× NPN or RC) | optional auto-reset |
| RTS | GPIO9 / BOOT (via NPN) | optional auto-download |

Do **not** power the module from a 5 V USB rail directly. If you power from the
phone's OTG port, keep in mind the ~100 mA budget and that Wi-Fi TX bursts draw
~240 mA peaks — see `LIMITATIONS.md` for the brownout symptom.

---

## 3. Drivers

| OS | CH34x | CP210x |
|---|---|---|
| Windows 10/11 | WCH CH343SER/CH341SER driver (wch.cn). Windows may bind a generic driver that looks fine but drops bytes — install the vendor driver | Silicon Labs CP210x Universal Windows Driver |
| Linux | In-tree `ch341` module since forever; add yourself to `dialout`: `sudo usermod -aG dialout $USER` then re-login | In-tree `cp210x` |
| macOS | WCH CH34x VCP driver for Apple Silicon (signed, requires kext approval on recent macOS) | Silicon Labs VCP driver, or the in-tree `AppleUSBVCP` on newer releases |

A missing/wrong driver shows up as "port opens but no data" — which the probe
harness reports as a handshake failure with non-JSON noise.

---

## 4. Flashing

### Option A — PlatformIO (recommended)

```bash
cd firmware
pio run                        # build
pio run -t upload              # flash over the bridge
pio device monitor -b 115200   # watch raw frames (Ctrl+C to exit)
```

Requires the pioarduino platform pinned in `platformio.ini` (stock
`platformio/espressif32` cannot build for ESP32-C2 at all — see the comment in
that file).

### Option B — esptool, using the prebuilt factory image

The factory image contains bootloader + partition table + app at the correct
offsets, so one command flashes a complete working device:

```bash
python -m esptool --chip esp32c2 -p <PORT> -b 460800 \
  --before default-reset --after hard-reset \
  write-flash --flash-mode dio --flash-freq 60m --flash-size 4MB \
  0x0 firmware/artifacts/esp8684_analyzer_phase2_v0.2.1_FACTORY.bin
```

Or write the three parts individually (offsets are chip-specific and correct
for ESP8684):

```bash
python -m esptool --chip esp32c2 -p <PORT> -b 460800 write-flash \
  0x0     firmware/artifacts/bootloader.bin \
  0x8000  firmware/artifacts/partition-table.bin \
  0x10000 firmware/artifacts/esp8684_analyzer_phase2_v0.2.1_app.bin
```

Full erase first if the device misbehaves after a partition change:

```bash
python -m esptool --chip esp32c2 -p <PORT> erase-flash
```

### Manual download mode (clones without auto-reset)

1. Hold **BOOT** (GPIO9 low).
2. Tap and release **EN** (reset).
3. Release **BOOT**.
4. Start the flash command within a few seconds.

### Upload / connection error table

| Symptom | Cause | Fix |
|---|---|---|
| `Failed to connect to ESP32-C2: No serial data received` | Not in download mode; auto-reset absent; wrong port | Manual BOOT+EN sequence; check `list` output |
| `Timed out waiting for packet header` | Baud too high for the bridge | Drop to `-b 115200` |
| Flash "succeeds" but device reboots forever | flash size declared larger than physical | This board is 4 MB; keep `--flash-size 4MB` |
| JSON never appears, raw `I (188) esp_image:` text instead | **Different firmware still on the device** | `erase-flash`, then write the factory image at `0x0` |
| Port opens, handshake never succeeds | Charge-only USB cable | Use a data cable |
| Works on PC, not on phone | Phone OTG current limit or cable | Powered hub; check `brownout` in `reset_reason` |
| Random `event.log` "uart rx overflow" | Host writing while not reading, or 460800 on a weak bridge | Return to 115200 |

---

## 5. Baud rate policy

Default **115200**. It is the only rate proven stable across every bridge in the
field, and it is fast enough: a full Phase 2 scan of ~25 networks is roughly
8 KB, i.e. ~0.7 s of transfer — comparable to the scan itself.

Changing it is a two-step operation, by design:

```json
→ {"v":1,"id":1,"cmd":"config.set","params":{"baud":460800}}
← {"type":"config","data":{"baud":460800,"running_baud":115200,"baud_pending":true,
                            "apply":"reboot_required_for_baud"}}
→ {"v":1,"id":2,"cmd":"reboot"}
   … device restarts at 460800. Reopen the port at 460800. …
```

The device never changes the line rate underneath a running host.

---

## 6. What the Android app must know about this hardware

- **OTG required.** Android needs a USB-OTG-capable port and a suitable cable.
  The app uses the USB Host API plus a USB-serial library; the ESP8684 has no
  native USB, so the app always talks to a bridge chip.
- **VID/PID allow-list for auto-detection:** `1A86:7523` (CH340),
  `1A86:55D4` (CH9102/CH343), `10C4:EA60` (CP2102/N), `0403:6001` (FT232).
  An unknown-but-plausible bridge must still be selectable manually — never
  hide it, clones are common.
- **Opening the port resets the board** on bridges with DTR/RTS wired: expect
  ~1.5 s of silence and possible boot noise. Flush and wait before the
  handshake, or the first `hello` will be lost.
- **Never assert DTR/RTS** while a scan is running (Phase 2): it would reset the
  device mid-scan.

## Clock and baud rate (why 26 MHz matters)

The ESP32-C2 derives every peripheral clock from the crystal, and **this board
has a 26 MHz crystal** (esptool prints `Crystal frequency: 26MHz`). ESP-IDF's
Kconfig default for the C2 is `XTAL_FREQ_40`, so a default build scales every
PLL-derived clock by 26/40:

| Effect | Wrong config (40 MHz assumed) | Correct config (26 MHz) |
|---|---|---|
| CPU | ~78 MHz instead of 120 MHz | 120 MHz |
| UART asked for 115200 | transmits at **74880** | transmits at 115200 |
| Wi-Fi/BT PHY timing | off by the same ratio | correct |

A UART that answers at 74880 instead of 115200 is **not** a dead board, a bad
cable or a stuck bootloader: it is this ratio, and it mimics silence exactly
because nothing parses on a 115200 host.

This project therefore pins, in `firmware/sdkconfig.defaults`:

```
CONFIG_XTAL_FREQ_26=y
CONFIG_ESP_CONSOLE_UART_CUSTOM=y      # only to make the baud prompt visible
CONFIG_ESP_CONSOLE_UART_BAUDRATE=115200
```

(`ESP_CONSOLE_UART_DEFAULT` hides the baud prompt, and IDF then forces its own
default of 74880 for a 26 MHz C2.)

The very first lines of a cold boot - the **mask-ROM banner**
(`ESP-ROM:esp32c2-eco4-...`) - are printed by hardware firmware that runs before
any of this configuration, so they always appear at 74880. Everything from the
2nd-stage bootloader onwards is 115200.

`info` reports `xtal_mhz` so this can never be guessed at again.

