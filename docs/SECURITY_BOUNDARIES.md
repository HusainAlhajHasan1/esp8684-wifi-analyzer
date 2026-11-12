# Security boundaries

This project measures Wi-Fi. It does not attack it. This page states the line
explicitly, because the request to cross it has already been made once, and a
boundary that is only remembered is a boundary that moves.

## Absolutely refused, and why

| Requested | Status | Reason |
|---|---|---|
| Start an AP that **clones** a nearby WPA2 network's SSID so that people connect to it | **refused** | This is an *evil twin*. Its only purpose is deception: the victim believes they joined the network they trust. |
| **Capture the passphrase** users type into that AP | **refused** | Credential harvesting. In Germany this is §202a / §263a StGB territory; equivalents exist across the EU and most other jurisdictions. |
| WPS PIN brute force, handshake capture, deauthentication floods | **refused** | Attacking third-party infrastructure. Not implemented in any phase, and not planned. |
| Any network penetration or "audit" feature aimed at networks the owner has not authorised | **refused** | Same as above, regardless of good intentions. |

A tool that impersonates an AP and records what people type has no benign
configuration. Building it "for testing" produces the same artefact as building
it for theft, and the artefact is what leaves the repository.

## What this project does instead

Everything is **passive observation of what the radio advertises**:

- SSID, BSSID, channel, RSSI, PHY, width — read from beacons and probe responses.
- `security` — the authentication mode the AP advertises.
- `wps` — whether WPS is *advertised*. Not whether it is enabled, not whether it
  is exploitable, and this project never tries to use it.
- `hidden` — whether the SSID field is empty. The device will not reveal a hidden
  name, and the app will not invent one. A hidden SSID only becomes visible when
  a client probes for it.
- `weak` — open security or WPS advertised. A configuration observation, phrased
  as such everywhere, including in any AI assistance.
- `strong` — WPA3 capable. **Not** a guarantee that the connection uses WPA3.

## SoftAP lab mode (Phase 4) — scope, if it is ever built

Permitted: start an AP with a name, channel and security mode the operator
chooses, on hardware they control, to observe how **their own** devices behave
when joining, and to measure coverage.

Still refused inside that feature:
- copying a third party's SSID, or any SSID the operator does not own;
- any page that asks for a Wi-Fi passphrase;
- logging, storing or transmitting credentials;
- running it where other people's devices could join it without their consent.

If those four conditions cannot be enforced in code, the feature does not ship.

## Data handling

- Scan results contain third-party SSIDs and BSSIDs: they describe the physical
  neighbourhood of whoever ran the scan.
- They stay on the device that measured them. `scan*.json` is git-ignored;
  nothing is uploaded, and the Android app declares no internet permission.
- Only structured metadata may be shared for diagnostics, never a raw capture.
