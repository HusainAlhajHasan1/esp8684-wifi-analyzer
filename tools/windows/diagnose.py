#!/usr/bin/env python3
"""
"the board is silent" — decisive diagnostic, v2.

v1 bug this fixes: captured bytes were printed to the console but never written
into diagnose_report.txt, so the single most important piece of evidence was
lost. In v2 EVERY line goes through say(), and every capture is also saved as a
raw .bin next to the report.

v2's real upgrade: the port stays open while we reset the chip ourselves by
pulsing RTS (EN) with DTR held de-asserted (GPIO9 / IO0 high = boot from flash).
v1 reset through esptool, which needs the port, so the first 2-4 seconds of boot
output were lost every time - exactly the window that matters.

Measurements, in order:
  A  verify-flash      Are the shipped bytes really in flash?
  B  --before no-reset Is the chip idling in ROM download mode right now?
  C  read-flash        Is there a valid ESP32-C2 app header at 0x10000?
  D  reset + capture   Reset into the app with the port OPEN; capture the whole
                       boot log at 115200, print it as hex AND ASCII, save it.
  D1 baud sweep        Same reset-and-capture at 74880 / 230400 / 460800 / 9600,
                       to detect output that is present but at the wrong rate.
  E  diag image        Optional: flash the breadcrumb firmware and re-capture.

Nothing writes to the board except E, which you must confirm.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ART = ROOT / "firmware" / "artifacts"
FACTORY_NAME = "esp8684_analyzer_phase2_v0.2.1_FACTORY.bin"
DIAG_NAME = "esp8684_analyzer_phase1_DIAG_FACTORY.bin"
OUT = ROOT / "diagnose_report.txt"

CHIP = "esp32c2"
ESP_APP_MAGIC = 0xE9
CHIP_ID_ESP32C2 = 0x000C

lines: list[str] = []


def say(msg: str = "") -> None:
    """Single output sink: everything printed also lands in the report."""
    print(msg)
    lines.append(msg)


def hr(title: str) -> None:
    say(f"\n{'=' * 70}\n {title}\n{'=' * 70}")


def esptool(port: str, *args: str, baud: str | None = None):
    import subprocess
    import sys

    cmd = [sys.executable, "-m", "esptool", "--chip", CHIP, "-p", port]
    if baud:
        cmd += ["-b", baud]
    cmd += list(args)
    say("$ " + " ".join(cmd[2:]))
    return subprocess.run(cmd, capture_output=True, text=True, timeout=240, check=False)


def find_artifact(name: str) -> Path | None:
    exact = ART / name
    if exact.exists():
        return exact
    for cand in ROOT.rglob(Path(name).name):
        return cand
    for cand in ROOT.rglob("*.bin"):
        if "DIAG" in cand.name.upper() and Path(name).name == DIAG_NAME:
            return cand
    return None


def decode_app_header(blob: bytes) -> dict:
    if len(blob) < 24:
        return {"error": "short header"}
    return {
        "magic": blob[0],
        "segments": blob[1],
        "spi_mode": blob[2],
        "entry_addr": int.from_bytes(blob[4:8], "little"),
        "chip_id": int.from_bytes(blob[12:14], "little"),
        "min_chip_rev": blob[14],
        "min_chip_rev_full": int.from_bytes(blob[15:17], "little"),
        "max_chip_rev_full": int.from_bytes(blob[17:19], "little"),
    }


def hexdump(data: bytes, limit: int = 512) -> str:
    out = []
    for i in range(0, min(len(data), limit), 16):
        chunk = data[i:i + 16]
        hexs = " ".join(f"{b:02x}" for b in chunk)
        ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"  {i:04x}  {hexs:<47}  {ascii_}")
    if len(data) > limit:
        out.append(f"  ... ({len(data)} bytes total, full copy saved to disk)")
    return "\n".join(out)


def analyse(data: bytes) -> dict:
    """Distinguish real UART text from line noise and from baud-rate garble."""
    if not data:
        return {"n": 0, "verdict": "empty"}
    printable = sum(1 for b in data if 32 <= b < 127 or b in (9, 10, 13))
    pct = 100.0 * printable / len(data)
    distinct = len(set(data))
    if b"{\"" in data:
        verdict = "json"
    elif b"WFA-DIAG" in data:
        verdict = "diag"
    elif b"ets Jun" in data or b"ESP-ROM" in data or b"rst:0x" in data or b"boot:0x" in data:
        verdict = "rom-banner"
    elif pct > 90 and distinct > 8:
        verdict = "text"
    elif distinct <= 6 or pct < 45:
        verdict = "noise-or-garble"
    else:
        verdict = "unclear"
    return {"n": len(data), "printable_pct": round(pct, 1), "distinct": distinct, "verdict": verdict}


VERDICT_WORDS = {
    "empty": "no bytes at all",
    "json": "JSON protocol frames",
    "diag": "diagnostic breadcrumbs",
    "rom-banner": "ROM / bootloader banner",
    "text": "human-readable text",
    "noise-or-garble": "noise or wrong-baud garble",
    "unclear": "unrecognised bytes",
}


def reset_and_capture(port: str, baud: int, seconds: float, do_reset: bool = True) -> bytes:
    """Open the port, reset the chip into the APPLICATION, capture everything.

    DTR is kept de-asserted (GPIO9 high -> boot from flash) and RTS is pulsed
    low (EN) then released. Because we hold the port open across the reset, the
    boot log is captured from its first byte instead of being missed while
    esptool owns the port.
    """
    import serial

    ser = serial.Serial(port, baud, timeout=0.05)
    try:
        ser.dtr = False          # GPIO9 / IO0 high  -> normal boot
        ser.rts = False          # EN high
        time.sleep(0.05)
        try:
            ser.reset_input_buffer()
        except Exception:
            pass
        if do_reset:
            ser.rts = True       # EN low  -> chip held in reset
            time.sleep(0.15)
            ser.rts = False      # EN high -> chip runs
        deadline = time.time() + seconds
        buf = bytearray()
        while time.time() < deadline:
            try:
                chunk = ser.read(4096)
            except Exception:
                break
            if chunk:
                buf.extend(chunk)
    finally:
        try:
            ser.close()
        except Exception:
            pass
    return bytes(buf)


def report_capture(tag: str, data: bytes) -> str:
    info = analyse(data)
    raw = ROOT / f"capture_{tag}.bin"
    raw.write_bytes(data)
    say(f"  bytes={info['n']}  printable={info.get('printable_pct')}%  "
        f"distinct={info.get('distinct')}  -> {VERDICT_WORDS[info['verdict']]}")
    say(f"  raw copy: {raw.name}")
    if data:
        say(hexdump(data, 512))
    return info["verdict"]


def step_a_verify(port: str) -> None:
    hr("A. Is the shipped image actually in flash?  (verify-flash)")
    factory = find_artifact(FACTORY_NAME)
    if not factory:
        say(f"[SKIP] {FACTORY_NAME} not found under {ROOT} - re-extract the zip.")
        return
    r = esptool(port, "verify-flash", "0x0", str(factory))
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    say(out[-700:] if out else "(no output)")
    if r.returncode == 0:
        say("[PASS] Flash content matches the shipped image exactly.")
        say("       -> The write landed. Silence is therefore NOT a write problem.")
    elif "differ" in out.lower() or "mismatch" in out.lower():
        say("[FAIL] Flash content differs from the shipped image.")
    else:
        say("[WARN] verify-flash could not complete. This is not evidence either way.")


def step_b_noreset(port: str) -> bool:
    hr("B. Is the chip idling in ROM download mode?  (connect with --before no-reset)")
    r = esptool(port, "--before", "no-reset", "--after", "no-reset", "flash-id")
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    say(out[-500:] if out else "(no output)")
    if r.returncode == 0:
        say("[RESULT] Chip answered with NO reset -> IDLING IN DOWNLOAD MODE.")
        say("         -> Silence is then expected; the boot strap (GPIO9) is holding")
        say("            it there. Nothing is wrong with the firmware.")
        return True
    say("[RESULT] No answer without a reset -> NOT in download mode.")
    return False


def step_c_header(port: str) -> None:
    hr("C. Is there a valid app header at 0x10000?")
    hdr = ROOT / "app_header_dump.bin"
    r = esptool(port, "read-flash", "0x10000", "0x20", str(hdr))
    if r.returncode != 0 or not hdr.exists():
        say(((r.stdout or "") + (r.stderr or "")).strip()[-400:])
        say("[SKIP] could not read flash.")
        return
    blob = hdr.read_bytes()
    info = decode_app_header(blob)
    say(hexdump(blob, 32))
    say(f"  decoded: {info}")
    if info.get("magic") == ESP_APP_MAGIC and info.get("chip_id") == CHIP_ID_ESP32C2:
        say(f"[PASS] Valid ESP32-C2 app header, entry 0x{info['entry_addr']:08X}, "
            f"rev window v{info['min_chip_rev_full'] // 100}.{info['min_chip_rev_full'] % 100}"
            f"..v{info['max_chip_rev_full'] // 100}.{info['max_chip_rev_full'] % 100}")
    elif info.get("magic") == 0xFF:
        say("[FAIL] 0x10000 is erased (0xFF): there is NO app in flash.")
    else:
        say(f"[FAIL] Unexpected header: {info}")


def step_d_capture(port: str, seconds: float) -> str:
    hr(f"D. Reset into the app with the port OPEN — capture {seconds:.0f}s at 115200")
    say("   (DTR held high-impedance / GPIO9 high, RTS pulsed to reset, then read)")
    data = reset_and_capture(port, 115200, seconds)
    verdict = report_capture("D_115200", data)
    if verdict == "empty":
        say("[RESULT] ZERO bytes, captured from the first byte after a reset.")
        say("         -> Whatever the chip is doing, it is not transmitting on UART0")
        say("            at 115200. Step D1 checks other rates.")
    elif verdict in ("json", "diag", "text"):
        say("[RESULT] The chip IS talking at 115200 — output is shown in full above.")
        say("         -> Send me this file; the text names the failing stage.")
    elif verdict == "rom-banner":
        say("[RESULT] ROM/bootloader banner seen -> the chip resets and starts the")
        say("         bootloader, but does not reach our application.")
    else:
        say("[RESULT] Bytes present but not valid text at 115200 -> almost certainly")
        say("         a baud mismatch. Step D1 sweeps the likely rates.")
    return verdict


def step_d1_sweep(port: str, seconds: float = 3.5) -> dict:
    hr("D1. Same reset-and-capture at other baud rates")
    say("   A chip printing at a different rate looks like noise at 115200.")
    say("   74880 is the classic ROM-banner rate; 230400/460800 catch an app that")
    say("   booted with a different console speed.\n")
    found: dict[int, str] = {}
    for baud in (74880, 230400, 460800, 9600):
        say(f"-- {baud} baud --")
        data = reset_and_capture(port, baud, seconds)
        found[baud] = report_capture(f"D1_{baud}", data)
    good = {b: v for b, v in found.items() if v in ("json", "diag", "text", "rom-banner")}
    if good:
        say(f"\n[RESULT] Readable output at {sorted(good)} baud -> the link is fine,")
        say("         the firmware just talks at a speed we did not expect.")
    else:
        say("\n[RESULT] Nothing readable at any swept rate.")
        say("         -> Combined with step D, the chip is not transmitting UART0")
        say("            output at all. Step E (breadcrumb firmware) is then decisive.")
    return found


def step_e_diag(port: str, seconds: float) -> None:
    hr("E. Flash the BREADCRUMB firmware and re-capture")
    diag = find_artifact(DIAG_NAME)
    if not diag:
        say(f"[SKIP] {DIAG_NAME} not found anywhere under {ROOT}.")
        say("       Files present in the artifacts folder:")
        if ART.exists():
            for f in sorted(ART.glob("*.bin")):
                say(f"         - {f.name} ({f.stat().st_size} B)")
        else:
            say(f"         (folder {ART} does not exist)")
        say("       -> Re-extract the whole zip over C:\\esp8684-analyzer.")
        return
    say(f"   Using {diag}  ({diag.stat().st_size} B)")
    say("   It prints [WFA-DIAG] markers via esp_rom_printf from app_main: that")
    say("   call needs no driver, no logging system and no allocation, so our own")
    say("   code cannot suppress it. Where the markers stop is the failing stage.")
    ans = input("   Flash it now? [y/N]: ").strip().lower()
    if ans != "y":
        say("[SKIP] diagnostic flash declined.")
        return
    r = esptool(port, "write-flash", "--flash-mode", "dio", "--flash-freq", "60m",
                "--flash-size", "4MB", "0x0", str(diag), baud="460800")
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    say(out[-500:])
    if r.returncode != 0:
        say("[FAIL] diagnostic flash failed.")
        return
    say("[OK] written + verified path complete. Capturing the breadcrumbs now.")
    data = reset_and_capture(port, 115200, max(seconds, 8))
    verdict = report_capture("E_diag", data)
    if b"WFA-DIAG" in data:
        say("\n[RESULT] BREADCRUMB TRAIL — this localises the fault exactly:")
        for ln in data.split(b"\r\n"):
            if b"WFA-DIAG" in ln:
                say("    " + ln.decode("utf-8", "replace").strip())
        say("         -> Send me this list and I will fix the stage it stops at.")
    elif verdict == "empty":
        say("[RESULT] Still ZERO bytes with firmware that cannot be silenced by our")
        say("         own code. The chip is not executing this image at all, and is")
        say("         not even printing its own ROM log -> boot-mode / hardware.")
    else:
        say(f"[RESULT] Output but no breadcrumbs ({VERDICT_WORDS[verdict]}) -> execution")
        say("         stops before app_main, i.e. in the ROM/bootloader stage.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Diagnose a silent ESP8684")
    ap.add_argument("--port", required=True, help="e.g. COM9")
    ap.add_argument("--seconds", type=float, default=8.0, help="capture window per reset")
    ap.add_argument("--skip-verify", action="store_true", help="skip step A (slow)")
    ap.add_argument("--check-assets", action="store_true", help="only list artifact files")
    args = ap.parse_args(argv)

    if args.check_assets:
        say(f"artifacts folder: {ART}")
        if ART.exists():
            for f in sorted(ART.iterdir()):
                say(f"  {f.name}  {f.stat().st_size} B")
        else:
            say("  MISSING")
        return 0

    say(f"ESP8684 diagnostic v2 — port {args.port} — {time.strftime('%Y-%m-%d %H:%M:%S')}")

    if not args.skip_verify:
        step_a_verify(args.port)
    in_download_mode = step_b_noreset(args.port)
    step_c_header(args.port)
    d = step_d_capture(args.port, args.seconds)
    d1 = {} if d in ("json", "diag", "text") else step_d1_sweep(args.port)
    if d not in ("json", "diag", "text"):
        step_e_diag(args.port, args.seconds)

    hr("SUMMARY")
    say(f"  chip in download mode (no reset needed): {in_download_mode}")
    say(f"  capture at 115200: {VERDICT_WORDS[d]}")
    for baud, v in d1.items():
        say(f"  capture at {baud}: {VERDICT_WORDS[v]}")
    say("")
    say("Send me diagnose_report.txt plus any capture_*.bin files.")
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nFull log: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
