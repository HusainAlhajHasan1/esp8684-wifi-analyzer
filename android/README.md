# Wi-Fi Analyzer — Android client (Phase 3)

Native Android client for the ESP8684 analyzer board. It talks to the board over
USB-serial and shows exactly what the radio measured.

**No internet permission is declared.** The app cannot upload a scan even if it
wanted to; scan data never leaves the phone.

---

## What it does

| Feature | Detail |
|---|---|
| USB-serial link | CH340 / CP210x / FTDI / Espressif bridges, 115200 8N1, auto-launch on plug-in |
| Handshake | Retries `hello` up to 10×, because opening the port resets the ESP |
| Paged sweeps | Reassembles `pages` frames keyed by `(scan_id, page)`; a missing page is an error, never a smaller result |
| Network list | SSID, BSSID, channel, RSSI, quality bar, security, flags |
| Filters | All · Weak · WPS advertised · Open · Hidden · WPA3 capable |
| Sort | Signal · Channel · Name |
| Details sheet | Every field the device sent, plus what each tag does and does not mean |
| Cache replay | `scan.get {replay:true}` re-reads the device's last sweep without new RF activity |
| Contract validation | Mirrors `tools/host/serial_probe.py`; violations are shown prominently instead of hidden |

## What it deliberately does NOT do

- **No invented names.** A hidden network shows a `‹hidden network›` label. The
  app never guesses an SSID.
- **No vulnerability claims.** `weak` means "open or WPS advertised" —
  configuration, not exploitability. `strong` means "WPA3 capable", not
  "protected by WPA3": a `wpa2/wpa3` AP still admits WPA2 clients.
- **No attacking.** No WPS PIN/brute force, no handshake capture, no deauth, no
  evil-twin AP, no credential collection. See [`docs/SECURITY_BOUNDARIES.md`](../docs/SECURITY_BOUNDARIES.md).

---

## Building

### In CI (easiest, nothing to install)
Push to `main` and pick up the APK: **Actions → Android APK → latest run → Artifacts
→ `wifi-analyzer-debug-apk`**. The workflow is [`.github/workflows/android.yml`](../.github/workflows/android.yml).

### In Android Studio
Open the `android/` folder as a project. Android Studio 2024.1+ with JDK 17
bundled. `Run` installs it on a connected phone.

### From the command line
```bash
export ANDROID_HOME=$HOME/Android/Sdk      # or your SDK path
echo "sdk.dir=$ANDROID_HOME" > android/local.properties
cd android
gradle assembleDebug
# -> app/build/outputs/apk/debug/app-debug.apk
```

A debug APK is unsigned for release but installs fine:
```bash
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

### Note on memory
`gradle.properties` is deliberately constrained (`-Xmx640m`, no parallel workers,
in-process Kotlin). A 2 GB machine OOM-kills the default configuration during
dexing — that is a measured fact from building this project, not a precaution.

---

## Using it

1. Flash firmware **v0.2.1** (see the top-level README) and plug in the board.
2. Android asks to allow the USB device — allow it, and tick "always".
3. Press **Scan**. A full 2.4 GHz sweep takes ~2 s.
4. Tap any row for the full record.

If the status says *"Board is not answering hello"*, the firmware is older than
v0.2.x or the CH340 driver is missing.

---

## Layout

```
app/src/main/java/com/hseen/wifianalyzer/
  MainActivity.kt           Compose host, USB attach intent
  usb/UsbSerialLink.kt      transport only: lines in, lines out
  protocol/Frames.kt        Network / ScanPage / Sweep / Frame
  protocol/Contract.kt      the spec, executable (mirror of the host validator)
  protocol/ScanCollector.kt paged reassembly, missing-page detection
  ui/ScanViewModel.kt       state + handshake + scan lifecycle
  ui/ScanScreen.kt          list, filters, details sheet
  ui/Theme.kt               colours, including flag semantics
```
