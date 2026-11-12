# Known Limitations

Written to be honest about what this hardware and this design can and cannot do.
Numbers are from the actual build, not estimates.

---

## 1. Memory (the binding constraint)

| Resource | Value | Consequence |
|---|---|---|
| SRAM | 272 KB total; ~278,528 B offered to the linker | Phase 1 uses 4.2% static. Peak heap use during a Phase 2 scan is the real test |
| Flash | **4 MB confirmed** (ESP8684H, GD flash) | App partition `0x3F0000` (3.9 MB); Phase 1 image is 194,526 B (4.6%) |
| Largest free block | reported by `stats.mem.heap_largest_block` (~110 KB at boot) | Heap **fragmentation**, not total free heap, is what breaks long sessions |

Practical rules this imposes:

- Scan results are **paged**, never emitted as one giant frame (§7 of PROTOCOL.md).
- `max_networks` is capped at 64 and defaults to 32. A dense environment with
  60+ visible BSSIDs is stored as *the strongest 32 by RSSI*, and the frame is
  flagged `"truncated":true`. Silent truncation is never acceptable.
- No JSON document is built larger than ~1.4 KB at a time.
- Scan history is **not** stored on the device. The device is a sensor; history
  lives in the Android Room database (and optionally Supabase).

## 2. Radio

- **2.4 GHz only.** ESP32-C2 has no 5 GHz radio and no 6 GHz. Any 5 GHz network
  is structurally invisible — the app must say so rather than imply a complete
  survey.
- **Single radio.** Scanning and SoftAP (laboratory mode) cannot run
  simultaneously. Time-slicing is required and will degrade both; the firmware
  refuses concurrent modes with `E_BUSY` rather than pretending.
- **Scan is disruptive.** A full channel sweep takes ~1.5–3 s. If the device is
  ever in station mode, a scan interrupts its connection.
- **802.11ax/HE is not supported** by this chip. `phy` reports `b`/`g`/`n` only.
- **RSSI is relative.** Values are what the ESP32-C2's radio reports; they are
  not calibrated to an absolute dBm reference and vary by board, antenna and
  orientation. Use them for comparison and trends, not for absolute claims.
- **Channel width** is reported from the secondary-channel offset (20/40 MHz).
  It cannot detect every real-world misconfiguration.

## 3. Security fields — what they do and do not mean

- **`wps` is an advertisement, not a vulnerability.** It comes from the WPS bit
  in the beacon/probe-response capability information (`wifi_ap_record_t.wps`).
  An AP can advertise WPS and still have it disabled, or vice versa. The UI must
  never label such a network "vulnerable".
- **Hidden networks stay hidden.** A hidden SSID is reported as `ssid: ""` with
  its real BSSID, channel and RSSI. The device will **never** invent a
  placeholder name like `"<hidden>"` and pass it off as data. Occasional
  hidden networks may reveal their SSID in a probe response; the device reports
  whatever the driver honestly provides.
- **`security` is what the beacon claims.** "WPA2" is the advertised
  authentication mode, not proof of a correctly configured network.
- **`weak` means "open or WPS-advertised"** — a configuration observation.
  Phrase it as such everywhere in the UI, including the AI assistant's prompt.
- **`strong` means WPA3-capable, not WPA3-enforced.** `wpa2/wpa3` is transition
  mode: the AP offers WPA3 but still admits WPA2-PSK clients, so a device can
  associate with WPA2 alone. `strong` also does **not** imply the connection is
  encrypted with WPA3, and it never means "secure". Label it "WPA3 capable".
- **There is no `pmf` field.** An earlier draft specified one, sourced from
  `wifi_ap_record_t.pmf_cfg`. That member does not exist — ESP-IDF's scan record
  carries no PMF capability information (it lives in the RSN capabilities
  element of the beacon, which the driver does not expose). Reporting it would
  mean inventing data, so it was removed from the contract entirely.
- **Known deviation: WPA3-Enterprise is not flagged `strong` in v0.2.1.**
  `net_classify()` omits `WIFI_AUTH_WPA3_ENTERPRISE` and
  `WIFI_AUTH_WPA2_WPA3_ENTERPRISE` from its `modern` set, so such an AP arrives
  with `security: "wpa3-enterprise"` and no `strong` flag — even though it is
  the strongest mode on the air. The host reports this as a `[NOTE]` and the run
  still passes. Formally accepted for v0.2.1 and tracked in **HSE-11** for the
  Phase 3 firmware line; it is not a blocker for Phase 2.

## 4. Explicit non-goals (by design, not by omission)

Not implemented, and not planned:

- Deauthentication or disassociation frames
- Jamming, flooding, or channel interference
- Handshake capture, PMKID capture, or any credential extraction
- WPS PIN brute force
- Any feature that interferes with networks the user does not own

Laboratory mode (SoftAP, Phase 4/5) exists only to let the user raise a test
access point **on their own hardware** and observe how the analyzer reports it.

## 5. Transport

- **115200 baud by default** — ~11.5 KB/s raw. Sufficient (a full scan is ~8 KB),
  but it bounds how fast continuous scanning can be reported. 460800 is available
  but bridge-dependent, especially on CH340 clones.
- **UART0 is shared with the ROM bootloader and the IDF console.** Mitigated by
  routing all logs into JSON `event.log` frames, but boot-time noise before the
  app runs is unavoidable — hosts must resync (PROTOCOL.md §1.3).
- **No hardware flow control.** If the host stops reading while the device
  emits, the device's 8 KB TX ring eventually blocks a task instead of dropping
  data. Reading continuously is mandatory on the host side.
- **`ts` is uptime, not wall-clock.** There is no RTC and no network time.
- **A blocked writer in the worst case** (host stops reading entirely for
  thousands of frames) can stall the emitting task until the watchdog fires; the
  device then resets. This is a deliberate trade — losing the link is better than
  silently losing data.

## 6. Android side

- **USB Host + OTG only.** No Bluetooth, no network transport, no Wi-Fi-based
  pairing. Phones without OTG cannot use the app.
- **Opening the port can reset the board** (DTR/RTS), so every connect costs
  ~1.5 s and must be treated as a possible device reboot (`seq` restarts at 1).
- **Latency** of the USB-serial path is typically 5–40 ms per round-trip; live
  graphs should be drawn at 1–2 Hz, not per frame.
- **Android kills USB access** if the app is backgrounded on some OEM builds.
  Reconnect handling must be the default path, not an edge case.

## 7. Phase status

| Phase | State |
|---|---|
| 1 — Hardware, USB serial, basic commands | **Implemented, builds, binary shipped.** Runtime acceptance pending on your board |
| 2 — Wi-Fi scanning (`scan_result`) | Protocol specified; firmware returns `E_UNSUPPORTED` |
| 3 — Android UI | Not started (Android project scaffold is Phase 3) |
| 4 — Signal monitor, channel overview | Not started |
| 5 — History, export, AI | Supabase schema drafted; not applied |
| 6 — Arabic/i18n, polish, docs | Not started |

The firmware deliberately refuses unimplemented commands. If you ever see a
`scan_result` from a Phase 1 build, that is a bug and should be reported.

## The mask-ROM banner is always at 74880 baud

On a board with a 26 MHz crystal, the ROM code inside the chip prints its first
lines (`ESP-ROM:esp32c2-eco4-...`, `rst:0x1 (POWERON)...`) at 74880 baud, before
any firmware configuration exists. It cannot be changed. Everything from the
2nd-stage bootloader onwards - including this project's protocol - is 115200.

Host tools must therefore treat pre-handshake bytes as noise, and must never
conclude "the board is dead" from an unreadable first burst: check whether the
board answers at 74880 first (`--baud 74880`).
