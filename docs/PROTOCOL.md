# USB-Serial Protocol — v1 (normative)

**Device:** ESP8684 / ESP32-C2 running `esp8684-wifi-analyzer` firmware
**Transport:** UART0 via the board's USB-to-UART bridge
**Status:** Phase 1 implemented and buildable. Phase 2 fields are specified here
now so the Android client can be written against a stable contract.

---

## 1. Transport layer

| Parameter | Value | Notes |
|---|---|---|
| Physical | UART0, GPIO20 = TX (U0TXD), GPIO19 = RX (U0RXD) | ESP8684 has **no native USB**; always through a bridge chip |
| Frame format | 8N1, no parity, no flow control | RTS/CTS is not wired on DevKitM-1 headers used here |
| Default baud | 115200 | The only rate that is dependable on every CH34x/CP210x bridge |
| Optional baud | 230400, 460800 | Opt-in, **reboot required**, see `config.set` |
| Encoding | UTF-8 JSON, one object per line, `\n` terminated | `\r\n` is tolerated on input |
| Max inbound line | 2048 bytes (excluding CR/LF) | Longer lines are discarded and answered with `E_TOO_LARGE` |
| Max outbound line | bounded by paging (see §7) | The device never emits an unbounded frame |

### Framing rules

1. Every frame is exactly one line. No frame contains a literal newline.
2. The device writes each frame under a single UART write lock. Frames never
   interleave, even when a heartbeat, an error and a command response are
   emitted from different tasks.
3. The host **must** tolerate non-JSON lines. On reset the ROM bootloader prints
   plain text at the open baud rate, and the first ~1.5 s after the port opens
   (DTR/RTS toggle) can contain garbage. Resynchronise by discarding any line
   that does not start with `{` and that does not parse as JSON.
4. The device **never** prints raw log text into the stream: `log_router.c`
   wraps every ESP-IDF log line into an `event.log` frame. This is a hard
   invariant, not a convention.

---

## 2. Common envelope

Every frame, in either direction, carries these fields:

| Field | Type | Meaning |
|---|---|---|
| `v` | int | Protocol version. Current: `1` |
| `id` | int | Request identifier. Host picks any int ≥ 1; the device echoes it. `0` means "unsolicited device frame" |
| `seq` | int | Device frame counter, monotonic from 1 since boot. **Host uses it to detect dropped/corrupted frames** |
| `type` | string | Frame type (see §3, §4) |
| `ts` | int | Device **uptime** in milliseconds. Not wall-clock: the ESP8684 has no RTC and no network time in Phase 1 |

> **`ts` semantics matter.** It is `esp_timer` uptime, so it resets to 0 on
> reboot and cannot be converted to a date. The Android app must timestamp
> received data with its own clock and may use `ts` only for deltas. `seq` also
> restarts at 1 after a reboot; a backwards jump in `seq` is a reliable reboot
> indicator.

### Request (host → device)

```json
{"v":1,"id":42,"cmd":"ping","params":{"t":1731412800}}
```

`params` is optional and may be omitted for commands that take none.

### Response (device → host)

```json
{"v":1,"id":42,"seq":17,"type":"pong","ts":84213,"data":{"t":1731412800,"pong":"ok"}}
```

### Error (device → host)

```json
{"v":1,"id":42,"seq":18,"type":"error","ts":84220,"code":"E_BAD_PARAM","msg":"config.set failed: E_BAD_PARAM"}
```

An error frame **replaces** the normal response for that `id`. It is never sent
in addition to a success frame.

---

## 3. Error codes

| Code | Meaning | Typical cause |
|---|---|---|
| `E_BAD_JSON` | Frame is not parseable JSON | Truncated write, wrong baud rate, non-JSON noise |
| `E_BAD_FRAME` | Valid JSON but missing a required field (e.g. `cmd`) | Client bug |
| `E_UNKNOWN_CMD` | `cmd` not in the command table | Typo, or client newer than firmware |
| `E_BAD_PARAM` | Parameter present but out of range / wrong type | e.g. `scan_interval_ms: 10` |
| `E_TOO_LARGE` | Payload or line exceeds the limit | `echo` > 1024 B, line > 2048 B |
| `E_BUSY` | Resource in use (reserved; used by Phase 2 scans) | Concurrent scan request |
| `E_NO_MEM` | Allocation failed | Heap fragmentation — check `stats.mem` |
| `E_UNSUPPORTED` | Command exists in the protocol but not in this firmware build | `scan.start` on Phase 1 firmware |
| `E_NO_DATA` | asked for cached scan results before any scan completed |
| `E_TIMEOUT` | Operation timed out (reserved for Phase 2 radio ops) | Scan aborted by driver |
| `E_INTERNAL` | Unexpected internal failure | NVS write failure, UART overrun |

`E_UNSUPPORTED` is deliberately used instead of fake data. If a feature is not
implemented, the device says so. **The device never fabricates scan results.**

---

## 4. Command reference (Phase 1)

### `hello`
Handshake. Use as the first frame after opening the port.

Response `type: "hello"`:
```json
{"v":1,"id":1,"seq":4,"type":"hello","ts":8123,"data":{
  "device":"ESP8684-DevKitM-1","chip":"ESP32-C2","fw":"0.1.0","build_tag":"phase1",
  "proto":1,"phase":2,"baud":115200,"ts_source":"uptime_ms","encoding":"json-lines/utf-8",
  "commands":["hello","ping","echo","info","stats","config.get","config.set","hb.set","reboot",
              "scan.start","scan.stop","scan.get"],
  "planned":["ap.start","ap.stop"],
  "limits":{"max_inbound_bytes":2048,"max_echo_bytes":1024,"max_networks":32}}}
```

`planned` lists commands the protocol reserves but this build refuses with
`E_UNSUPPORTED`. The Android app should grey these out rather than hide them.

### Oversize input

A line longer than `limits.max_inbound_bytes` (2048) is discarded and answered with

```json
{"v":1,"id":28,"seq":7,"type":"error","ts":1200,"code":"E_TOO_LARGE",
 "msg":"inbound frame exceeds 2048 bytes"}
```

The firmware recovers the requester's `id` from the retained beginning of the
line (id-recovery is best-effort: the first `"id":<digits>` in the prefix wins).
This matters: a rejection addressed to id `0` would be ignored by a host that
matches replies to requests, so a *correctly rejected* frame would look like a
timeout - indistinguishable from a dead link.

`echo` with a payload above `limits.max_echo_bytes` (1024) returns the same
error code, with the exact request id, before the line limit is reached.

Rejecting an oversize line increments the device's `overflow` counter by one.
Hosts performing a deliberate oversize test must therefore baseline that counter
first, not assert it is zero.

### `ping`
Liveness + RTT. Echoes `params.t` verbatim.

```json
→ {"v":1,"id":2,"cmd":"ping","params":{"t":7}}
← {"v":1,"id":2,"seq":5,"type":"pong","ts":9120,"data":{"t":7,"pong":"ok"}}
```

### `echo`
Payload integrity check. `payload` must be a string of ≤ 1024 bytes.

```json
→ {"v":1,"id":3,"cmd":"echo","params":{"payload":"AAAA"}}
← {"v":1,"id":3,"seq":6,"type":"echo","ts":9210,"data":{"payload":"AAAA","len":4}}
```

Larger payloads return `E_TOO_LARGE`. Use this to validate the link end-to-end
before trusting scan data.

### `info`
Real hardware identity. No WiFi init required — the STA MAC comes from eFuse.

```json
← {"v":1,"id":4,"seq":7,"type":"info","ts":9300,"data":{
  "model":"ESP8684-DevKitM-1","chip":"ESP32-C2","chip_rev":"v0.0","cores":1,
  "flash_bytes":2097152,"mac_sta":"AA:BB:CC:DD:EE:FF",
  "fw":{"version":"0.1.0","project":"esp8684-wifi-analyzer","idf":"v5.5.5",
        "build_tag":"phase1","built":"Nov 12 2026","time":"12:11:00"},
  "uptime_ms":9300,"heap_free":198432,"heap_min_free":191880,
  "reset_reason":"power_on"}}
```

`reset_reason` values: `power_on`, `external_pin`, `software`, `panic`,
`int_wdt`, `task_wdt`, `other_wdt`, `deep_sleep`, `brownout`, `sdio`, `unknown`.
A `brownout` here means the USB supply sagged during TX — see `LIMITATIONS.md`.

### `stats`
Link and memory counters. Use it to *prove* link quality over a long session.

```json
← {"v":1,"id":5,"seq":8,"type":"stats","ts":12000,"data":{
  "link":{"bytes_rx":512,"bytes_tx":4411,"lines_rx":17,"overflow":0,"uart_err":0},
  "protocol":{"rx_frames":17,"bad_json":0,"bad_frame":0,"unknown_cmd":0,
              "unsupported":1,"bad_param":0,"max_frame_len":46},
  "mem":{"heap_free":198112,"heap_min_free":191880,"heap_largest_block":110580},
  "uptime_ms":12000,"reset_reason":"power_on"}}
```

### `config.get` / `config.set`

```json
← {"v":1,"id":6,"seq":9,"type":"config","ts":13000,"data":{
  "scan_interval_ms":5000,"heartbeat_ms":5000,"baud":115200,"running_baud":115200,
  "baud_pending":false,"max_networks":32,"log_level":2,"auto_scan":false}}
```

`config.set` accepts any subset; each field is validated and an out-of-range
value returns `E_BAD_PARAM` **without changing anything** (all-or-nothing).

| Field | Range | Effect |
|---|---|---|
| `scan_interval_ms` | 1000–60000 | Phase 2 auto-scan period |
| `heartbeat_ms` | 1000–60000 | Heartbeat period, applied immediately |
| `baud` | 115200, 230400, 460800 | **Persisted, applied on next boot only** |
| `max_networks` | 1–64 | Scan result cap (RAM bound) |
| `log_level` | 0–4 (none/err/warn/info/debug) | Stored; takes effect at boot |
| `auto_scan` | bool | Reserved for Phase 2 |

The response states what happened:
`"apply":"applied"` or `"apply":"reboot_required_for_baud"`.

### `hb.set`
Shorthand for `config.set {"heartbeat_ms": N}`, persists and restarts the timer.

### `reboot`
```json
→ {"v":1,"id":9,"cmd":"reboot"}
← {"v":1,"id":9,"seq":20,"type":"rebooting","ts":20000,"data":{"reboot_in_ms":300}}
```
The device waits for the TX buffer to drain, then restarts. Expect ~1.5 s of
silence plus boot-time noise; re-handshake with `hello` after about 2 s.

---

## 5. Unsolicited device frames (`id: 0`)

| `type` | When | Payload |
|---|---|---|
| `event.boot` | Once, right after boot | Full `info` object + `"ready":"phase1"` |
| `event.heartbeat` | Every `heartbeat_ms` | `{"uptime_ms":…,"heap_free":…}` |
| `event.log` | Whenever the firmware logs | `{"msg":"…"}` |

Clients must ignore unknown `type` values (forward compatibility) and must not
treat `event.log` as data.

**Liveness rule:** consider the link dead after 3 consecutive missed
heartbeats (i.e. `3 × heartbeat_ms`), or immediately on a UART/USB detach
broadcast from Android.

---

## 6. Protocol versioning

- `v` is a single integer, currently `1`.
- The device rejects a request whose `v` is **greater** than its own with
  `E_UNSUPPORTED` and a message naming both versions.
- Additive changes (new `type`, new optional `params`, new response fields) do
  **not** bump `v`. Clients must ignore unknown fields.
- Removing or repurposing a field **does** bump `v`.

---

## 7. Phase 2 contract: scan results (implemented in firmware v0.2.0)

Firmware v0.2.0 serves `scan.start` / `scan.stop` / `scan.get`. Phase 1 builds
answer them with `E_UNSUPPORTED`; a client must handle both, because a board may
still be running an older image.

### Command flow

```json
→ {"v":1,"id":20,"cmd":"scan.start","params":{"continuous":false,"interval_ms":5000}}
← {"v":1,"id":20,"seq":40,"type":"scan_started","ts":41200,"data":{"scan_id":7}}

← {"v":1,"id":0,"seq":41,"type":"scan_result","ts":43900,"data":{
    "scan_id":7,"page":1,"pages":1,"total":23,"truncated":false,
    "scan_ms":1840,"networks":[ … ]}}
```

Scans are **asynchronous**: `scan.start` acknowledges immediately, results
arrive later as unsolicited `scan_result` frames. A 2.4 GHz full scan takes
roughly 1.5–3 s on ESP32-C2 (this firmware uses 90–120 ms per channel, active).

If the radio sees more networks than the device can hold (32), the excess is
reported in `dropped` and `truncated` is set — the page is never padded with
guesses.

### `scan.get`

Answers with current state, and optionally replays the cache:

```json
→ {"v":1,"id":21,"cmd":"scan.get","params":{"replay":true}}
← {"v":1,"id":21,"seq":44,"type":"scan_status","ts":44100,"data":{
     "scan_id":7,"scanning":false,"continuous":false,"interval_ms":5000,
     "networks":23,"last_scan_ms":1840,"scans":3,"failed":0,"dropped":0,
     "max_networks":32}}
```

With `params.replay == true` the device also re-emits the cached `scan_result`
pages (same `scan_id`, `"cached":true`) so a client that lost a page can recover
it **without** triggering new RF activity. If nothing has been scanned yet the
reply is `E_NO_DATA` - never an empty-but-pretend-success result.

`scan.start` while a scan is already running returns `E_BUSY`.
`scan.stop` cancels continuous scanning and aborts an in-flight scan.

### Paging

When the result set does not fit in one line, the device splits it into
`pages` frames sharing one `scan_id`, each with `page` 1..N. Each frame stays
under ~1400 bytes so it cannot be truncated by any host buffer. The host
reassembles by `scan_id`.

### Network object

| Field | Type | Meaning |
|---|---|---|
| `ssid` | string | SSID, JSON-escaped. For hidden networks: `""` (never invented) |
| `ssid_hex` | string | Present **only** when the SSID is not valid UTF-8. Hex of the raw bytes. Client should prefer this when present |
| `bssid` | string | `"AA:BB:CC:DD:EE:FF"` |
| `rssi` | int | dBm, negative (rarely slightly positive at very close range) |
| `quality` | int | 0–100, derived: `2*(rssi+100)` clamped — computed on device for consistency |
| `channel` | int | Primary channel, 1–14 |
| `freq_mhz` | int | Derived: 2407 + 5×channel (ch 14 = 2484) |
| `band` | string | `"2.4ghz"` (this chip has no 5 GHz radio) |
| `width_mhz` | int | 20 or 40 (from secondary channel) |
| `security` | string | `"open"`, `"wep"`, `"wpa"`, `"wpa2"`, `"wpa/wpa2"`, `"wpa2/wpa3"`, `"wpa3"`, `"wpa3-192"`, `"owe"`, `"wapi"`, `"unknown"` |
| `hidden` | bool | `true` when SSID length is 0 |
| `wps` | bool | WPS advertised in the beacon/probe-response capability info |
| `phy` | array | Subset of `["b","g","n"]` actually advertised |
| `flags` | array | Derived tags, see classification below |

### Classification tags (`flags`)

Computed on the device so every client agrees:

| Tag | Rule |
|---|---|
| `open` | `security == "open"` |
| `weak` | `open` **or** `wps == true` |
| `wps` | `wps == true` |
| `hidden` | SSID length 0 |
| `legacy` | WEP, or 802.11b-only PHY |
| `strong` | `security` is **WPA3-capable**: `wpa3`, `wpa2/wpa3`, `wpa3-192`, `wpa3-enterprise`, `wpa2/wpa3-enterprise`, `owe` |
| `crowded` | assigned by the client from channel occupancy, not sent by device |

`weak` is an *observation about configuration*, not an accusation of
vulnerability, and must be presented that way in the UI. WPS being advertised
does not mean WPS is enabled or exploitable.

### What `strong` does and does not promise

`strong` means **the AP is capable of WPA3**, not that a connection will use it.
`wpa2/wpa3` is transition mode: the AP advertises WPA3 but still admits WPA2-PSK
clients, so a phone can associate with WPA2 alone. This distinction was settled
by real hardware data — an early scan flagged three transition-mode networks
`strong` while an earlier draft of this table excluded them, and the host
validator did not check the flag at all, so nothing failed.

Therefore the client **must not** render `strong` as "WPA3" or as a security
guarantee. Acceptable labels: *"WPA3 capable"*, *"supports WPA3"*. Unacceptable:
*"WPA3"*, *"secure"*, *"protected by WPA3"*. Show the `security` string itself
whenever the user needs to know whether transition mode is in play.

A WPA3-only AP and a transition-mode AP are not equivalent, and this contract
does not pretend they are: `security` carries the distinction, `strong` does not.

### Known deviation in firmware v0.2.1 (enterprise WPA3)

`net_classify()` in v0.2.1 builds its `modern` set from `WIFI_AUTH_WPA3_PSK`,
`WPA2_WPA3_PSK`, `WPA3_ENT_192`, the WPA3-Ext-PSK modes and `OWE`, but **omits
`WIFI_AUTH_WPA3_ENTERPRISE` and `WIFI_AUTH_WPA2_WPA3_ENTERPRISE`**. Those two
networks therefore arrive without `strong`, even though they are WPA3-capable
and, in the 192-bit case, the strongest thing on the air.

This is **formally accepted for firmware v0.2.1** and deferred to the Phase 3
firmware line under `HSE-11`, rather than forcing a rebuild and re-flash for an
authentication mode that did not appear in the acceptance sweep.

Hosts must treat it as a **known deviation, not a contract violation**: report
it and keep going. `tools/host/serial_probe.py` does exactly that — `known_deviation()`
prints a `[NOTE]` and the run still passes — while any *other* `strong`
inconsistency remains a hard failure. Fixing it requires a firmware build that
sets `strong` for the enterprise auth modes; it was deferred rather than bundled
into a host-only release, so the image on the board and this document differ in
exactly one documented place.

**No `pmf` field.** An earlier draft of this contract specified one, sourced
from `wifi_ap_record_t.pmf_cfg`. That member does not exist: ESP-IDF's scan
record carries no PMF capability information (it lives in the RSN capabilities
element of the beacon, which the driver does not expose). Reporting it would
mean inventing data, so the field is removed and `strong` no longer depends on
it.

### Security values actually emitted

`open`, `wep`, `wpa`, `wpa2`, `wpa/wpa2`, `wpa2/wpa3`, `wpa3`, `wpa3-192`,
`owe`, `wapi`, `unknown`, plus the enterprise variants `wpa2-enterprise`,
`wpa3-enterprise`, `wpa2/wpa3-enterprise`, `wpa-enterprise`. Enterprise modes
are reported distinctly rather than collapsed into `unknown`.

---

## 8. Host implementation checklist (Android)

1. Open port → **wait ~1.5 s** before parsing. Opening asserts DTR/RTS on most
   bridges and resets the ESP. Discard everything until the first `{`.
2. Send `hello` with a 1 s timeout, retry up to 5 times. Only then enable UI.
3. Maintain a `seq` gap counter from day one and show link quality in the UI.
4. Never block the main thread: run the reader on `Dispatchers.IO`, expose
   frames as a `Flow`, and keep the last N frames only.
5. Timestamp every frame with the phone clock on arrival; store `ts` alongside
   for delta calculations.
6. On `event.boot` with a lower `seq`, treat it as a reboot and re-handshake.
7. On USB detach, stop reading, close the connection, and surface a reconnect
   state — the device keeps running, the link does not.
8. Never send passwords, keys, SSIDs from user input, or any credential to a
   remote AI service. Only structured scan metadata ever leaves the device.
9. **Reassemble paged sweeps — never re-read the stream head.** A sweep arrives as
   `pages` separate `scan_result` frames. Read them with an *advancing* cursor that
   survives across calls, keyed on `page`, and keep collecting until
   `len(pages_collected) == data.pages`. A reader that re-scans from the oldest
   buffered frame each call returns page 1 forever and silently drops the rest:
   this happened on hardware (13 networks, 3 pages, only 5 recorded), so treat a
   page-count mismatch as a hard error and report it, never as an empty result.
   Deduplicate defensively: a repeated `page` must overwrite, not append.

---

## 9. Change log

| Version | Date | Change |
|---|---|---|
| 1 | 2026-11-12 | Initial specification. Phase 1 commands implemented; Phase 2 shapes reserved |
| 2 | 2026-11-12 | Phase 2 scan contract implemented (v0.2.1): paged `scan_result`, `quality`/`weak`/`hidden` rules, `ssid_hex`; `pmf` dropped. Host checklist item 9 added after a real paging-collection bug |
| 3 | 2026-11-12 | `strong` redefined as **WPA3-capable** (adds `wpa2/wpa3` and the enterprise WPA3 modes), after a real 13-network sweep exposed the old rule contradicting the firmware. Client wording constraints added; firmware v0.2.1 enterprise gap documented as a known deviation. Host validator now enforces `strong` (it never did) |
