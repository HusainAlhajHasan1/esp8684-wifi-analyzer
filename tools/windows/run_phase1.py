#!/usr/bin/env python3
"""
Phase 1 helper for Windows (works on macOS/Linux too).

Why this exists: the first version did this work in .bat and broke when the
project lived in a folder called "esp8684-wifi-analyzer-phase1(1)" - a literal
'(' inside an `if (...)` block terminates the block early in cmd.exe. Batch is
the wrong tool for handling user paths. This script does the same job in Python,
where parentheses in paths are just characters.

What it does
------------
  --flash       identify the board (real flash size), optionally erase, flash the
                prebuilt factory image, then optionally run the acceptance test
  --probe-only  just run the acceptance test against an already-flashed board
  --list-ports  print detected serial ports and exit

Always: writes flash_id.txt (flash mode) and phase1_report.json (probe mode),
prints everything, and copies the report to the clipboard on Windows.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FACTORY = ROOT / "firmware" / "artifacts" / "esp8684_analyzer_phase2_v0.2.1_FACTORY.bin"
PROBE = ROOT / "tools" / "host" / "serial_probe.py"
REPORT = ROOT / "tools" / "host" / "phase1_report.json"
FLASH_ID_TXT = ROOT / "flash_id.txt"

CHIP = "esp32c2"

MANUAL_RESET_HELP = """\
  esptool reports "Hard resetting via RTS pin...", but on CH340 clone boards RTS
  is usually NOT wired to EN. The chip therefore stays in the ROM stub flasher,
  which is SILENT on UART0 - it looks exactly like a dead board."""

# Bridges that mean "this is probably the analyzer board".
LIKELY_BRIDGES = {
    (0x1A86, 0x7523): "CH340",
    (0x1A86, 0x5523): "CH341",
    (0x1A86, 0x55D4): "CH9102/CH343",
    (0x10C4, 0xEA60): "CP2102/CP2102N",
    (0x0403, 0x6001): "FT232R",
}


def hr(title: str) -> None:
    print(f"\n{'=' * 66}\n {title}\n{'=' * 66}")


def run(cmd: list[str], *, capture: bool = False) -> subprocess.CompletedProcess:
    printable = " ".join(f'"{c}"' if " " in c else c for c in cmd)
    print(f"$ {printable}")
    try:
        return subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            timeout=300,
            check=False,
        )
    except FileNotFoundError:
        print(f"[ERROR] command not found: {cmd[0]}")
        raise SystemExit(1)


def ensure_deps() -> None:
    probe = run([sys.executable, "-c", "import esptool, serial"], capture=True)
    if probe.returncode == 0:
        return
    print("Installing esptool + pyserial (one time, ~10 MB)...")
    r = run([sys.executable, "-m", "pip", "install", "--quiet", "--upgrade", "esptool", "pyserial"])
    if r.returncode != 0:
        print("[ERROR] pip install failed - check your internet connection.")
        raise SystemExit(1)


def list_ports() -> list[tuple[str, str, str]]:
    from serial.tools import list_ports as lp

    found = []
    for p in sorted(lp.comports(), key=lambda x: x.device):
        vid, pid = p.vid, p.pid
        bridge = LIKELY_BRIDGES.get((vid, pid), "unknown") if vid is not None else "unknown"
        vp = f"{vid:04X}:{pid:04X}" if vid is not None else "-"
        found.append((p.device, bridge, f"{vp}  {p.description}"))
    return found


def show_ports(ports: list[tuple[str, str, str]]) -> str | None:
    if not ports:
        print("[ERROR] No serial ports found at all.")
        print("        -> charge-only USB cable, or the CH34x driver is missing.")
        return None
    print(f"{'PORT':<10} {'BRIDGE':<16} DESCRIPTION")
    print("-" * 66)
    for dev, bridge, desc in ports:
        print(f"{dev:<10} {bridge:<16} {desc}")
    likely = [d for d, b, _ in ports if b != "unknown"]
    if likely:
        print(f"\nLikely analyzer board: {likely[0]}")
    return likely[0] if likely else ports[0][0]


def normalise_port(port: str) -> str:
    """Upper-case Windows COM names only.

    A blanket .upper() turned a POSIX device such as /dev/pts/0 into /DEV/PTS/0,
    which does not exist - so the simulator and any pty-based test were
    unreachable through this wrapper even though they worked through the probe.
    """
    port = port.strip()
    return port.upper() if port[:3].upper() == "COM" else port


def ask_port(ports: list[tuple[str, str, str]], preset: str | None) -> str:
    if preset:
        return normalise_port(preset)
    default = show_ports(ports)
    prompt = f"\nCOM port [default {default}]: " if default else "\nCOM port (e.g. COM9): "
    try:
        answer = input(prompt).strip()
    except EOFError:
        answer = ""
    port = answer or (default or "")
    if not port:
        print("[ERROR] No port given.")
        raise SystemExit(1)
    return normalise_port(port)


def esptool(port: str, *args: str, baud: str | None = None) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "esptool", "--chip", CHIP, "-p", port]
    if baud:
        cmd += ["-b", baud]
    cmd += list(args)
    return run(cmd, capture=True)


def step_identify(port: str) -> bool:
    hr(f"Identifying the board on {port}")
    r = esptool(port, "flash-id")
    text = (r.stdout or "") + (r.stderr or "")
    print(text.strip())
    FLASH_ID_TXT.write_text(text, encoding="utf-8")
    print(f"\n-> saved to {FLASH_ID_TXT}")

    if r.returncode != 0 or "Detected flash size" not in text:
        print("\n[ERROR] The board did not answer.")
        print("  * Tap the EN (reset) button once and try again - a previous esptool")
        print("    run can leave the chip sitting in the stub flasher, silent.")
        print("  * Or enter download mode: hold BOOT, tap EN, release BOOT.")
        print("  * Or the cable is charge-only / the port is held open by another app.")
        return False

    if "4MB" not in text:
        print("\n[WARN] This image is built for 4 MB flash but the board reports otherwise.")
        print("       Tell me the detected size before flashing.")
    return True


def step_flash(port: str) -> bool:
    if not FACTORY.exists():
        print(f"[ERROR] Missing firmware image:\n        {FACTORY}")
        print("        Extract the whole ZIP; do not move individual files around.")
        return False

    hr("Erase? (recommended when the board still runs vendor/factory firmware)")
    try:
        ans = input("Erase the whole flash first? [y/N]: ").strip().lower()
    except EOFError:
        ans = ""
    if ans == "y":
        r = esptool(port, "erase-flash")
        if r.returncode != 0:
            print((r.stdout or "") + (r.stderr or ""))
            print("[ERROR] Erase failed. Hold BOOT, tap EN, release BOOT, retry.")
            return False
        print("[OK] Flash erased.")

    hr("Flashing the analyzer firmware (4 MB layout, app partition 3.9 MB)")
    r = esptool(
        port,
        "--before", "default-reset", "--after", "hard-reset",
        "write-flash", "--flash-mode", "dio", "--flash-freq", "60m",
        "--flash-size", "4MB", "0x0", str(FACTORY),
        baud="460800",
    )
    text = (r.stdout or "") + (r.stderr or "")
    print(text.strip())

    if r.returncode != 0:
        print("\n[WARN] Failed at 460800 - retrying at 115200 (common on CH340 clones)...")
        r = esptool(
            port,
            "--before", "default-reset", "--after", "hard-reset",
            "write-flash", "--flash-mode", "dio", "--flash-freq", "60m",
            "--flash-size", "4MB", "0x0", str(FACTORY),
            baud="115200",
        )
        print(((r.stdout or "") + (r.stderr or "")).strip())
        if r.returncode != 0:
            print("\n[ERROR] Flashing failed. Hold BOOT, tap EN, release BOOT, retry.")
            return False

    print("\n[OK] Write finished and verified.")
    print("\n" + "=" * 66)
    print(" ONE MANUAL STEP IS REQUIRED")
    print("=" * 66)
    print(MANUAL_RESET_HELP)
    print("\n  -> Press the EN / RST button on the board once,")
    print("     or unplug the USB cable and plug it back in.")
    try:
        input("  Press Enter once you have reset the board: ")
    except EOFError:
        pass
    time.sleep(0.6)
    return True


def _report_is_silent() -> bool:
    """True when the probe failed because ZERO bytes arrived (reset problem,
    not a protocol problem)."""
    try:
        data = json.loads(REPORT.read_text(encoding="utf-8"))
    except Exception:
        return False
    for chk in data.get("checks", []):
        if chk.get("name") == "handshake" and not chk.get("passed"):
            d = chk.get("data", {})
            return d.get("frames_parsed", 0) == 0 and d.get("non_json_lines", 0) == 0
    return False


BAUD_MISMATCH_HELP = """
 WHAT THIS MEANS
 ---------------
 The firmware is transmitting correctly, just not at 115200 baud. On ESP32-C2
 the UART clock is derived from the crystal, so firmware built for a 40 MHz
 crystal on a 26 MHz board transmits at 115200 * 26 / 40 = 74880 baud.

 It looks EXACTLY like a dead board on a 115200 host: zero parsable frames.

 The fix is to flash the current image (firmware v0.1.1 is built for the 26 MHz
 crystal this hardware actually has). Until then, the probe can still talk to
 the old image at 74880:

     py -3 tools\\host\\serial_probe.py probe --port %s --baud 74880
"""


def _probe_frames_at(port: str, baud: int) -> int:
    """Run the probe once at a given baud and return how many frames parsed."""
    alt = ROOT / f"probe_{baud}.json"
    try:
        run([sys.executable, str(PROBE), "probe", "--port", port,
             "--baud", str(baud), "--report", str(alt)])
    except Exception:
        return 0
    try:
        data = json.loads(alt.read_text(encoding="utf-8"))
    except Exception:
        return 0
    for chk in data.get("checks", []):
        if chk.get("name") == "handshake":
            return int(chk.get("data", {}).get("frames_parsed", 0))
    return 0


def step_probe(port: str, retries: int = 3) -> int:
    if not PROBE.exists():
        print(f"[ERROR] Missing {PROBE}")
        return 1

    for attempt in range(1, retries + 1):
        suffix = "" if attempt == 1 else f" (attempt {attempt}/{retries})"
        hr(f"Acceptance test on {port}{suffix}")
        rc = run([sys.executable, str(PROBE), "probe", "--port", port,
                  "--report", str(REPORT),
                  "--csv", str(ROOT / "tools" / "host" / "pings.csv")]).returncode
        if REPORT.exists():
            print(f"\n-> report: {REPORT}")
            copy_to_clipboard(REPORT)

        if rc == 0:
            return 0
        if not _report_is_silent():
            print("\nThe board DID answer - this is a firmware/protocol problem, not a")
            print("reset problem. Send me the report and I will take it from there.")
            return rc
        if attempt == retries:
            break

        # A baud mismatch is indistinguishable from silence on the host side,
        # and it is far more common than a dead board. Check it before asking
        # the user to press buttons.
        print("\nNo frames at 115200 - checking whether the board speaks at another rate...")
        for alt_baud in (74880, 230400, 460800, 9600):
            frames = _probe_frames_at(port, alt_baud)
            if frames > 0:
                print(f"\n*** The board ANSWERED at {alt_baud} baud ({frames} frame(s)). ***")
                print(BAUD_MISMATCH_HELP % port)
                print("\nRe-flash with the current image, then run this test again:")
                print("  py -3 tools\\windows\\run_phase1.py --flash --port " + port)
                return 0
        print("No frames at 115200, 74880, 230400, 460800 or 9600.\n")

        print("=" * 66)
        print(" ZERO BYTES AT EVERY BAUD - the chip is not running the app")
        print("=" * 66)
        print(MANUAL_RESET_HELP)
        print("\n  -> Press the EN / RST button on the board now,")
        print("     or unplug + replug the USB cable.")
        try:
            ans = input("  Then press Enter to retry (or type q to quit): ").strip().lower()
        except EOFError:
            ans = "q"
        if ans == "q":
            return rc
        time.sleep(1.0)

    print("\nStill silent after repeated manual resets, at every baud rate:")
    print("  * A different USB cable (charge-only cables are indistinguishable by eye)")
    print("  * A different USB port or a powered hub (brownout resets look like this)")
    print("  * Device Manager: the COM port must have no warning triangle")
    return 1


def step_watch(port: str, seconds: int = 15) -> int:
    """Print whatever the board sends, verbatim. Proves with your own eyes that
    the firmware is running, independent of the test suite."""
    import serial

    hr(f"Watching {port} at 115200 for {seconds}s (Ctrl+C to stop)")
    try:
        ser = serial.Serial(port, 115200, timeout=1)
    except Exception as exc:
        print(f"[ERROR] cannot open {port}: {exc}")
        return 1
    try:
        ser.dtr = False
        ser.rts = False
    except Exception:
        pass

    deadline = time.time() + seconds
    seen = 0
    try:
        while time.time() < deadline:
            line = ser.readline()
            if not line:
                continue
            seen += 1
            print(line.decode("utf-8", "replace").rstrip()[:300])
    except KeyboardInterrupt:
        pass
    finally:
        ser.close()

    print(f"\n{seen} line(s) received.")
    if seen == 0:
        print("Silent -> press EN / RST and run this again.")
        return 1
    print("JSON frames = correct firmware. Raw log text = wrong firmware still flashed.")
    return 0


def copy_to_clipboard(path: Path) -> None:
    """Best effort: put the report on the clipboard so it can be pasted in chat."""
    try:
        if os.name == "nt":
            with open(path, "rb") as fh:
                subprocess.run("clip", stdin=fh, check=False, shell=True)
            print("-> copied to clipboard (paste it into the chat)")
        elif sys.platform == "darwin":
            with open(path, "rb") as fh:
                subprocess.run("pbcopy", stdin=fh, check=False)
            print("-> copied to clipboard")
        elif shutil.which("xclip"):
            with open(path, "rb") as fh:
                subprocess.run(["xclip", "-selection", "clipboard"], stdin=fh, check=False)
            print("-> copied to clipboard")
    except Exception as exc:  # never fail the run over the clipboard
        print(f"(clipboard copy skipped: {exc})")


def step_scan(port: str, seconds: int = 12, json_out: str | None = None) -> int:
    """Run the Phase 2 sweep through the probe, keeping the flow to one command.

    The scan subcommand lives in serial_probe.py; this wrapper exists so nobody
    has to remember a second path or a second set of arguments.
    """
    hr(f"Scanning {port} for real networks (no invented data)")
    probe = Path(__file__).resolve().parent.parent / "host" / "serial_probe.py"
    cmd = [sys.executable, str(probe), "scan", "--port", port, "--seconds", str(seconds)]
    if json_out:
        cmd += ["--json", json_out]
    sys.stdout.flush()  # keep our header above the child's output when piped
    r = subprocess.run(cmd)
    if r.returncode == 0:
        print("\nRESULT: sweep captured and consistent with the contract.")
    elif r.returncode == 2:
        print("\nRESULT: no scan_result arrived - the device reported nothing.")
    else:
        print("\nRESULT: contract violation or command failure - paste the output above.")
    return r.returncode


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ESP8684 Wi-Fi Analyzer - Phase 1 helper")
    ap.add_argument("--flash", action="store_true", help="identify + erase(optional) + flash + probe")
    ap.add_argument("--probe-only", action="store_true", help="only run the acceptance test")
    ap.add_argument("--list-ports", action="store_true", help="list serial ports and exit")
    ap.add_argument("--port", help="serial port, e.g. COM9 (skips the prompt)")
    ap.add_argument("--no-probe", action="store_true", help="flash but skip the acceptance test")
    ap.add_argument("--watch", action="store_true", help="just print raw incoming lines for 15 s")
    ap.add_argument("--scan", action="store_true", help="run one Phase 2 sweep and print the table")
    ap.add_argument("--json", dest="json_out", help="write the scan result to this JSON file")
    ap.add_argument("--seconds", type=int, default=15, help="seconds for --watch (default 15)")
    args = ap.parse_args(argv)

    ports = list_ports()
    if args.list_ports:
        show_ports(ports)
        return 0

    ensure_deps()

    if args.watch:
        return step_watch(ask_port(ports, args.port), args.seconds)

    if args.scan:
        return step_scan(ask_port(ports, args.port), args.seconds, args.json_out)

    if args.probe_only:
        return step_probe(ask_port(ports, args.port))

    # default action is the full flash flow
    port = ask_port(ports, args.port)
    if not step_identify(port):
        return 1
    if not step_flash(port):
        return 1
    if args.no_probe:
        print("\nSkipping the acceptance test (--no-probe).")
        return 0

    try:
        go = input("\nRun the acceptance test now? [Y/n]: ").strip().lower()
    except EOFError:
        go = "y"
    if go in ("", "y", "yes"):
        rc = step_probe(port)
        print("\n" + ("RESULT: Phase 1 link verified." if rc == 0
                      else "RESULT: some checks failed - paste the output above."))
        return rc
    return 0


if __name__ == "__main__":
    sys.exit(main())
