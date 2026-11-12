#!/usr/bin/env python3
"""
Phase 1 hardware validation harness — ESP8684 Wi-Fi Network Analyzer.

This is the tool that answers "is the USB-serial link actually reliable?" with
numbers instead of vibes. It talks the real JSON-lines protocol to a real board
over a real USB-to-UART bridge and produces a pass/fail report.

Subcommands
-----------
  list       Enumerate serial ports, identify the bridge chip (CH34x / CP210x /
             FTDI), and flag likely analyzer boards.
  probe      Full Phase 1 acceptance suite against a connected board:
             handshake, ping latency + seq-gap accounting, payload integrity,
             malformed-input resilience, honest-unsupported check, config
             round-trip, throughput, and reconnect survival.
  self-test  No hardware: exercises the frame parser and seq-gap logic with
             synthetic byte streams (chunk splits, bootloader spew, truncated
             frames, garbage). This tests the HOST parser only — it never
             fabricates scan results.

Usage
-----
  python serial_probe.py list
  python serial_probe.py probe --port COM5
  python serial_probe.py probe --port /dev/ttyUSB0 --baud 115200 --report out.json
  python serial_probe.py self-test

Exit codes: 0 = all checks passed, 1 = one or more failed, 2 = usage/hardware error.
"""

from __future__ import annotations

import argparse
import csv
import pathlib
import hashlib
import json
import statistics
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

try:
    import serial  # pyserial
    from serial.tools import list_ports
except ImportError:  # pragma: no cover
    sys.exit("pyserial is required:  pip install -r requirements.txt")

PROTO_VERSION = 1

# USB VID/PID -> bridge name. The ESP8684 has no native USB, so whatever shows
# up here is the board's USB-to-UART bridge.
KNOWN_BRIDGES: dict[tuple[int, int], str] = {
    (0x1A86, 0x7523): "CH340",
    (0x1A86, 0x5523): "CH341",
    (0x1A86, 0x7522): "CH340K",
    (0x1A86, 0x55D4): "CH9102/CH343",
    (0x10C4, 0xEA60): "CP2102/CP2102N",
    (0x10C4, 0xEA70): "CP2105",
    (0x10C4, 0xEA71): "CP2108",
    (0x0403, 0x6001): "FT232R",
    (0x0403, 0x6015): "FT231X",
    (0x303A, 0x1001): "Espressif USB-JTAG/Serial (not present on ESP8684)",
}

# Bridges known to auto-reset cleanly via DTR/RTS toggling.
AUTORESET_BRIDGES = {"CP2102/CP2102N", "CH9102/CH343", "FT232R", "FT231X"}


# ---------------------------------------------------------------------------
# Frame parsing (pure, testable without hardware)
# ---------------------------------------------------------------------------
class FrameParser:
    """Splits a byte stream into JSON frames, tolerating everything the ESP
    throws at us: ROM bootloader spew on reset, partial reads, CRLF vs LF,
    stray NULs, and frames split across USB packets.

    Anything that is not a complete JSON object is counted, never silently
    dropped, so link quality is measurable rather than assumed.
    """

    def __init__(self, max_buffer: int = 64 * 1024) -> None:
        self._buf = bytearray()
        self._max_buffer = max_buffer
        self.non_json_lines = 0
        self.malformed_json_lines = 0
        self.frames_parsed = 0
        self.last_garbage = ""
        self.seq_last: Optional[int] = None
        self.seq_gaps = 0
        self.seq_missing = 0

    def feed(self, chunk: bytes) -> list[dict[str, Any]]:
        self._buf.extend(chunk)
        out: list[dict[str, Any]] = []
        while b"\n" in self._buf:
            line, _, rest = self._buf.partition(b"\n")
            self._buf = bytearray(rest)
            frame = self._parse_line(line)
            if frame is not None:
                out.append(frame)
        # Guard against a device that never sends a newline (or a huge lie).
        if len(self._buf) > self._max_buffer:
            self.non_json_lines += 1
            self.last_garbage = bytes(self._buf[:120]).decode("utf-8", "replace")
            self._buf.clear()
        return out

    def _parse_line(self, raw: bytes) -> Optional[dict[str, Any]]:
        line = raw.strip(b"\r\n\t \x00")
        if not line:
            return None
        if not line.startswith(b"{"):
            self.non_json_lines += 1
            self.last_garbage = line[:120].decode("utf-8", "replace")
            return None
        try:
            frame = json.loads(line.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            self.malformed_json_lines += 1
            self.last_garbage = line[:120].decode("utf-8", "replace")
            return None
        if not isinstance(frame, dict):
            self.malformed_json_lines += 1
            return None
        self.frames_parsed += 1
        self._track_seq(frame)
        return frame

    def _track_seq(self, frame: dict[str, Any]) -> None:
        seq = frame.get("seq")
        if not isinstance(seq, int):
            return
        if self.seq_last is not None:
            expected = self.seq_last + 1
            if seq != expected:
                if seq > expected:
                    self.seq_gaps += 1
                    self.seq_missing += seq - expected
                else:
                    self.seq_gaps += 1  # went backwards: device reset or reorder
        self.seq_last = seq


# ---------------------------------------------------------------------------
# Live link
# ---------------------------------------------------------------------------
class LinkError(RuntimeError):
    pass


class Link:
    def __init__(self, port: str, baud: int = 115200, verbose: bool = False) -> None:
        self.port = port
        self.baud = baud
        self.verbose = verbose
        try:
            self.ser = serial.Serial(port, baudrate=baud, timeout=0.05, write_timeout=3.0)
        except Exception as exc:  # SerialException, PermissionError, OSError ...
            # An unopenable port is an expected outcome (wrong name, port held by
            # another program, board unplugged), not a crash. Callers catch
            # LinkError and print a clean message with a documented exit code.
            raise LinkError(f"cannot open {port}: {exc}") from exc

        # Deassert DTR/RTS immediately. pyserial asserts both on open, and on
        # boards where DTR is wired to EN this holds the ESP in RESET for the
        # whole session - which looks exactly like a dead board (zero bytes).
        # We are a reader, not a flasher: we never want to touch the chip.
        try:
            self.ser.dtr = False
            self.ser.rts = False
            self.ser.reset_input_buffer()
        except Exception:
            pass

        self.parser = FrameParser()
        self._frames: list[dict[str, Any]] = []
        self._cursor = 0  # persistent read position for streaming readers
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._next_id = 1
        self._id_lock = threading.Lock()
        # Counters must exist BEFORE the reader thread runs: on a live port the
        # first bytes can arrive within microseconds of start(), and the reader
        # would otherwise die with AttributeError on a half-initialised object.
        self.bytes_rx = 0
        self.bytes_tx = 0
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        try:
            self.ser.close()
        except Exception:
            pass

    def __enter__(self) -> "Link":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- io ----------------------------------------------------------------
    def _reader(self) -> None:
        while not self._stop.is_set():
            try:
                # read(1) blocks until a byte arrives. The previous read(512)
                # waited out the whole port timeout whenever a reply was
                # shorter than 512 bytes, which added ~50 ms to EVERY measured
                # round trip and made the firmware look 10x slower than it is.
                first = self.ser.read(1)
                if not first:
                    continue
                pending = self.ser.in_waiting
                chunk = first + (self.ser.read(pending) if pending else b"")
            except Exception:
                break
            if not chunk:
                continue
            self.bytes_rx += len(chunk)
            frames = self.parser.feed(chunk)
            if not frames:
                continue
            with self._cond:
                self._frames.extend(frames)
                self._cond.notify_all()
            if self.verbose:
                for f in frames:
                    print(f"  << {json.dumps(f, ensure_ascii=False)[:200]}")

    def send_raw(self, data: bytes) -> None:
        self.ser.write(data)
        self.ser.flush()
        self.bytes_tx += len(data)

    def send(self, cmd: str, params: Optional[dict[str, Any]] = None, rid: Optional[int] = None) -> int:
        if rid is None:
            with self._id_lock:
                rid = self._next_id
                self._next_id += 1
        frame: dict[str, Any] = {"v": PROTO_VERSION, "id": rid, "cmd": cmd}
        if params is not None:
            frame["params"] = params
        line = json.dumps(frame, separators=(",", ":"), ensure_ascii=False) + "\n"
        if self.verbose:
            print(f"  >> {line.strip()}")
        self.send_raw(line.encode("utf-8"))
        return rid

    # -- receive helpers ---------------------------------------------------
    def wait_for(
        self, pred: Callable[[dict[str, Any]], bool], timeout: float = 2.0
    ) -> Optional[dict[str, Any]]:
        deadline = time.monotonic() + timeout
        seen = 0
        with self._cond:
            while True:
                while seen < len(self._frames):
                    frame = self._frames[seen]
                    seen += 1
                    if pred(frame):
                        return frame
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)

    def next_frame(
        self, pred: Callable[[dict[str, Any]], bool], timeout: float = 2.0
    ) -> Optional[dict[str, Any]]:
        """Like wait_for, but ADVANCES across calls.

        wait_for() keeps its scan position in a local, so calling it in a loop
        returns the same first matching frame every time - which is what a
        paged scan capture did: page 1 was collected three times and pages 2-3
        were never seen. Streaming needs a cursor that survives the call.
        """
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                while self._cursor < len(self._frames):
                    frame = self._frames[self._cursor]
                    self._cursor += 1
                    if pred(frame):
                        return frame
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)

    def request(
        self, cmd: str, params: Optional[dict[str, Any]] = None, timeout: float = 2.0
    ) -> dict[str, Any]:
        rid = self.send(cmd, params)
        frame = self.wait_for(lambda f: f.get("id") == rid, timeout)
        if frame is None:
            raise LinkError(f"timeout waiting for response to '{cmd}' (id={rid})")
        return frame

    def drain(self, seconds: float = 0.2) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            time.sleep(0.02)

    def flush_input(self) -> None:
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass
        with self._cond:
            self._frames.clear()


def diagnose_link(parser: FrameParser, baud: int) -> str:
    """Turn the parser's observations into an actionable next step.

    Written after a real field report: the board was still running its factory
    firmware and spewing colourised ESP-IDF log text. The old message blamed
    "baud rate / TX-RX swapped", which sent the user down the wrong path.
    """
    garb = parser.last_garbage or ""
    raw_log_markers = ("esp_image:", "rst:", "boot:", "I (", "CPU freq")
    looks_like_raw_log = any(m in garb for m in raw_log_markers) or garb.startswith("\x1b")

    if parser.non_json_lines > 10 and looks_like_raw_log:
        return (
            "The port is emitting RAW ESP-IDF LOG TEXT, not JSON frames.\n"
            "    -> This board is running DIFFERENT firmware (factory image / ESP-AT / another sketch).\n"
            "    -> Flash the analyzer firmware (factory image at offset 0x0), then re-run.\n"
            f"    Observed: {garb[:110]}"
        )
    if parser.frames_parsed == 0 and parser.non_json_lines == 0:
        return (
            "Total silence: the port opened but zero bytes arrived.\n"
            "    -> The chip is most likely sitting in the ROM stub flasher: esptool's\n"
            "       'Hard resetting via RTS pin' does nothing unless RTS->EN is wired,\n"
            "       which is common on CH340 clone boards. It is silent on UART0.\n"
            "    -> FIX: press the EN / RST button on the board (or unplug and replug\n"
            "       the USB cable), THEN re-run. Nothing else will help.\n"
            "    -> If it is still silent after a manual reset: charge-only cable,\n"
            "       wrong COM port, or a missing CH34x driver."
        )
    if parser.frames_parsed == 0 and parser.malformed_json_lines > 0:
        return (
            f"Bytes arrived but none parsed as JSON ({parser.malformed_json_lines} bad line(s)).\n"
            f"    -> almost always a BAUD MISMATCH: the device may not be at {baud} baud.\n"
        f"       On ESP32-C2 the UART clock comes from the crystal: firmware built for a\n"
        f"       40 MHz crystal on a 26 MHz board transmits at baud * 26 / 40\n"
        f"       (115200 -> 74880). Try --baud 74880 before suspecting the hardware."
        )
    if parser.frames_parsed > 0:
        return (
            f"{parser.frames_parsed} JSON frame(s) parsed but no 'hello' answer.\n"
            "    -> firmware speaks a different/older protocol, or lacks the 'hello' command."
        )
    return (
        "No automatic diagnosis. Capture the raw port output for ~10 s and inspect it.\n"
        f"    lines_seen={parser.frames_parsed + parser.non_json_lines + parser.malformed_json_lines}"
    )


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return ordered[k]


# ---------------------------------------------------------------------------
# Result bookkeeping
# ---------------------------------------------------------------------------
@dataclass
class Check:
    name: str
    passed: bool
    detail: str
    data: dict[str, Any] = field(default_factory=dict)


class Suite:
    def __init__(self) -> None:
        self.checks: list[Check] = []
        self.started = time.time()

    def record(self, name: str, passed: bool, detail: str, **data: Any) -> Check:
        chk = Check(name=name, passed=passed, detail=detail, data=data)
        self.checks.append(chk)
        mark = "PASS" if passed else "FAIL"
        print(f"[{mark}] {name}: {detail}")
        return chk

    @property
    def ok(self) -> bool:
        return all(c.passed for c in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "duration_s": round(time.time() - self.started, 2),
            "passed": self.ok,
            "checks": [
                {"name": c.name, "passed": c.passed, "detail": c.detail, "data": c.data}
                for c in self.checks
            ],
        }


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------
def cmd_list(_args: argparse.Namespace) -> int:
    ports = sorted(list_ports.comports(), key=lambda p: p.device)
    if not ports:
        print("No serial ports found.")
        print("  - Is the board plugged in with a DATA cable (not charge-only)?")
        return 2

    print(f"{'PORT':<22} {'VID:PID':<12} {'BRIDGE':<24} {'AUTO-RESET':<11} DESCRIPTION")
    print("-" * 110)
    candidates: list[str] = []
    for p in ports:
        vid, pid = p.vid, p.pid
        bridge = KNOWN_BRIDGES.get((vid, pid), "unknown") if vid is not None else "unknown"
        autoreset = "yes" if bridge in AUTORESET_BRIDGES else "verify"
        vp = f"{vid:04X}:{pid:04X}" if vid is not None else "-"
        if bridge != "unknown":
            candidates.append(p.device)
        print(f"{p.device:<22} {vp:<12} {bridge:<24} {autoreset:<11} {p.description}")

    print()
    if candidates:
        print("Likely analyzer board(s): " + ", ".join(candidates))
    else:
        print("No known USB-to-UART bridge detected. If a port is listed above,")
        print("pass it explicitly:  probe --port <PORT>")
    print("\nNext:  python serial_probe.py probe --port " + (candidates[0] if candidates else "<PORT>"))
    return 0


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------
def cmd_probe(args: argparse.Namespace) -> int:
    suite = Suite()
    print(f"Probing {args.port} at {args.baud} baud...\n")

    try:
        link = Link(args.port, args.baud, verbose=args.verbose)
    except Exception as exc:
        print(f"ERROR: cannot open {args.port}: {exc}", file=sys.stderr)
        return 2

    pings: list[float] = []
    try:
        # --- 1. handshake -------------------------------------------------
        # Opening a CH34x port asserts DTR/RTS, which can reset the board.
        # Give it time to boot, then retry hello a few times.
        link.flush_input()
        hello: Optional[dict[str, Any]] = None
        attempts = 0
        t0 = time.monotonic()
        while attempts < 6 and hello is None:
            attempts += 1
            try:
                frame = link.request("hello", timeout=1.0)
                if frame.get("type") == "hello":
                    hello = frame
            except LinkError:
                time.sleep(0.35)
        handshake_s = time.monotonic() - t0

        if hello is None:
            diagnosis = diagnose_link(link.parser, args.baud)
            print()
            print("=" * 70)
            print(" DIAGNOSIS")
            print("=" * 70)
            print(diagnosis)
            print("=" * 70)
            suite.record(
                "handshake",
                False,
                f"no valid 'hello' after {attempts} attempts. {diagnosis}",
                non_json_lines=link.parser.non_json_lines,
                malformed_lines=link.parser.malformed_json_lines,
                frames_parsed=link.parser.frames_parsed,
                last_garbage=link.parser.last_garbage,
                diagnosis=diagnosis,
            )
            _write_report(suite, args)
            return 1

        data = hello.get("data", {})
        suite.record(
            "handshake",
            True,
            f"device={data.get('device')} fw={data.get('fw')} proto={data.get('proto')} "
            f"({handshake_s:.2f}s, {attempts} attempt(s))",
            hello=data,
        )

        proto = data.get("proto")
        suite.record(
            "protocol_version",
            proto == PROTO_VERSION,
            f"device proto v{proto} vs host v{PROTO_VERSION}",
            device=proto,
            host=PROTO_VERSION,
        )

        # --- 2. info / identity -------------------------------------------
        info = link.request("info", timeout=2.0)
        idata = info.get("data", {})
        has_mac = bool(idata.get("mac_sta"))
        has_flash = isinstance(idata.get("flash_bytes"), int) and idata["flash_bytes"] > 0
        suite.record(
            "device_info",
            info.get("type") == "info" and has_mac and has_flash,
            f"chip={idata.get('chip')} rev={idata.get('chip_rev')} "
            f"flash={idata.get('flash_bytes')}B mac={idata.get('mac_sta')} "
            f"reset={idata.get('reset_reason')}",
            info=idata,
        )

        # --- 3. ping latency + seq accounting ------------------------------
        n_pings = args.pings
        for i in range(n_pings):
            sent = time.perf_counter()
            link.request("ping", {"t": i}, timeout=2.0)
            pings.append((time.perf_counter() - sent) * 1000.0)
            time.sleep(0.01)

        avg = statistics.fmean(pings)
        p95 = _percentile(pings, 95)
        worst = max(pings)
        suite.record(
            "ping_latency",
            worst < 250,
            f"n={n_pings} min={min(pings):.1f}ms avg={avg:.1f}ms "
            f"p95={p95:.1f}ms max={worst:.1f}ms",
            min_ms=round(min(pings), 2),
            avg_ms=round(avg, 2),
            p95_ms=round(p95, 2),
            max_ms=round(worst, 2),
        )

        suite.record(
            "frame_sequence_integrity",
            link.parser.seq_missing == 0,
            f"frames_parsed={link.parser.frames_parsed} gaps={link.parser.seq_gaps} "
            f"missing={link.parser.seq_missing}",
            gaps=link.parser.seq_gaps,
            missing=link.parser.seq_missing,
            non_json_lines=link.parser.non_json_lines,
            malformed_lines=link.parser.malformed_json_lines,
        )

        # Baseline the link counters *before* the deliberate abuse below:
        # rejecting an oversize line is correct behaviour, and it must bump
        # `overflow` by exactly one per rejected line. Comparing an absolute
        # zero here would penalise the device for doing the right thing.
        try:
            _pre = link.request("stats", timeout=2.0).get("data", {})
            overflow_baseline = int(_pre.get("link", {}).get("overflow", 0))
        except LinkError:
            overflow_baseline = 0

        # --- 4. payload integrity -----------------------------------------
        payload_ok = True
        sizes = [16, 64, 256, 1024]
        for size in sizes:
            payload = "".join(chr(0x41 + (i % 26)) for i in range(size))
            try:
                frame = link.request("echo", {"payload": payload}, timeout=3.0)
            except LinkError as exc:
                payload_ok = False
                suite.record(f"echo_{size}B", False, str(exc))
                continue
            d = frame.get("data", {})
            same = d.get("payload") == payload and d.get("len") == size
            payload_ok = payload_ok and same
            suite.record(
                f"echo_{size}B",
                same,
                "byte-identical round-trip" if same else "payload mismatch or truncation",
                sha256=hashlib.sha256(payload.encode()).hexdigest()[:16],
            )

        oversize = "A" * 2000
        frame = link.request("echo", {"payload": oversize}, timeout=3.0)
        suite.record(
            "oversize_rejected",
            frame.get("type") == "error" and frame.get("code") == "E_TOO_LARGE",
            f"2000-byte echo -> {frame.get('code')} (expected E_TOO_LARGE)",
            code=frame.get("code"),
        )

        # --- 5. malformed input resilience ---------------------------------
        link.send_raw(b"this is not json\n")
        f1 = link.wait_for(lambda f: f.get("code") == "E_BAD_JSON", 2.0)
        suite.record(
            "garbage_rejected",
            f1 is not None,
            "non-JSON line -> E_BAD_JSON" if f1 else "device did not report E_BAD_JSON",
        )

        link.send_raw(b'{"v":1,"id":9999,"cmd":"nope_nope"}\n')
        f2 = link.wait_for(
            lambda f: f.get("id") == 9999 and f.get("code") == "E_UNKNOWN_CMD", 2.0
        )
        suite.record(
            "unknown_cmd_rejected",
            f2 is not None,
            "unknown command -> E_UNKNOWN_CMD" if f2 else "no E_UNKNOWN_CMD",
        )

        link.send_raw(b'{"v":1,"id":10000,"cmd":\n')
        f3 = link.wait_for(lambda f: f.get("code") in {"E_BAD_JSON"}, 2.0)
        suite.record(
            "truncated_json_rejected",
            f3 is not None,
            "truncated JSON -> E_BAD_JSON" if f3 else "no error for truncated JSON",
        )

        link.send_raw(b'{"v":1,"id":10001,"cmd":"' + b"x" * 4000 + b'"}\n')
        f4 = link.wait_for(lambda f: f.get("code") == "E_TOO_LARGE", 3.0)
        suite.record(
            "inbound_overflow_rejected",
            f4 is not None,
            ">2048-byte inbound line -> E_TOO_LARGE" if f4 else "no E_TOO_LARGE for oversize inbound",
        )

        # Still alive after being abused?
        try:
            alive = link.request("ping", {"t": 31337}, timeout=2.0)
            survived = alive.get("type") == "pong"
        except LinkError:
            survived = False
        suite.record(
            "survives_bad_input",
            survived,
            "device still answers ping after malformed input" if survived else "device unresponsive",
        )

        # --- 6. scan command contract -------------------------------------
        # Phase 1 firmware must REFUSE scan.* (E_UNSUPPORTED) rather than fake it.
        # Phase 2+ must ACKNOWLEDGE with a scan_id and deliver results later as
        # unsolicited scan_result frames. Either is honest; silence is not.
        # Whether the delivered networks are truthful is the job of the separate
        # `scan` subcommand, which validates every field against the contract.
        phase = data.get("phase", 1)
        frame = link.request("scan.start", {"continuous": False}, timeout=3.0)
        if phase >= 2:
            ack_ok = (frame.get("type") == "scan_started"
                      and isinstance(frame.get("data", {}).get("scan_id"), int))
            detail = (f"phase {phase}: scan.start -> {frame.get('type')} "
                      f"scan_id={frame.get('data', {}).get('scan_id')}")
            try:
                link.request("scan.stop", timeout=2.0)
            except LinkError:
                pass
        else:
            ack_ok = frame.get("type") == "error" and frame.get("code") == "E_UNSUPPORTED"
            detail = (f"phase {phase}: scan.start -> {frame.get('type')}/{frame.get('code')} "
                      f"(must be E_UNSUPPORTED when scanning is not implemented)")
        suite.record("scan_command_contract", ack_ok, detail, response=frame)

        # --- 6b. scan parameter handling ----------------------------------
        # Omitting optional params must be accepted; out-of-range values must
        # be refused. Both directions have been real bugs, so both are pinned.
        if phase >= 2:
            cases = []
            for label, params, expect in (
                ("no params", {}, "ok"),
                ("interval 2000", {"continuous": True, "interval_ms": 2000}, "ok"),
                ("interval 10 (too small)", {"interval_ms": 10}, "E_BAD_PARAM"),
                ("interval 'abc' (wrong type)", {"interval_ms": "abc"}, "E_BAD_PARAM"),
            ):
                if params:
                    try:
                        link.request("scan.stop", timeout=1.5)
                    except LinkError:
                        pass
                resp = link.request("scan.start", params or None, timeout=3.0)
                if expect == "ok":
                    good = resp.get("type") in ("scan_started", "error") and \
                        (resp.get("type") == "scan_started" or resp.get("code") == "E_BUSY")
                else:
                    good = resp.get("type") == "error" and resp.get("code") == expect
                cases.append((label, good, f"{label} -> {resp.get('type')}/{resp.get('code')}"))
                try:
                    link.request("scan.stop", timeout=1.5)
                except LinkError:
                    pass
            suite.record(
                "scan_param_handling",
                all(c[1] for c in cases),
                "; ".join(c[2] for c in cases),
                cases=[{"case": c[0], "passed": c[1], "detail": c[2]} for c in cases],
            )

        # --- 7. config round-trip ------------------------------------------
        original = link.request("config.get", timeout=2.0).get("data", {})
        before_hb = original.get("heartbeat_ms", 5000)
        new_hb = 7000 if before_hb != 7000 else 6000
        setframe = link.request("config.set", {"heartbeat_ms": new_hb}, timeout=2.0)
        stored = setframe.get("data", {}).get("heartbeat_ms")
        restored = link.request("config.set", {"heartbeat_ms": before_hb}, timeout=2.0)
        back = restored.get("data", {}).get("heartbeat_ms")
        suite.record(
            "config_persistence",
            stored == new_hb and back == before_hb,
            f"heartbeat {before_hb} -> {stored} -> {back} ms",
            original=original,
        )

        bad = link.request("config.set", {"scan_interval_ms": 10}, timeout=2.0)
        suite.record(
            "config_validation",
            bad.get("type") == "error" and bad.get("code") == "E_BAD_PARAM",
            f"out-of-range scan_interval_ms -> {bad.get('code')} (expected E_BAD_PARAM)",
        )

        # --- 8. heartbeat ---------------------------------------------------
        hb = link.wait_for(lambda f: f.get("type") == "event.heartbeat", timeout=max(8.0, before_hb / 1000.0 + 4))
        suite.record(
            "heartbeat",
            hb is not None,
            f"unsolicited event.heartbeat received (uptime_ms={hb.get('data', {}).get('uptime_ms')})"
            if hb
            else "no heartbeat within timeout",
        )

        # --- 9. throughput --------------------------------------------------
        n = args.throughput_frames
        payload = "B" * 512
        t0 = time.perf_counter()
        for _ in range(n):
            link.request("echo", {"payload": payload}, timeout=3.0)
        elapsed = time.perf_counter() - t0
        frames_per_s = n / elapsed if elapsed > 0 else 0
        bulk = n * (512 + 40) * 2 / elapsed if elapsed > 0 else 0

        # A frame cannot cross the wire faster than the line rate allows:
        # 512 B payload + ~48 B envelope, sent and echoed back = ~1120 B per
        # round trip. At 115200 baud that caps out near 10 frames/s, so the old
        # flat "> 20 frames/s" bar was unreachable on this hardware by physics,
        # not by firmware fault. Judge against 50% of the theoretical ceiling.
        wire_bps = args.baud / 10.0
        theoretical_fps = wire_bps / (2 * (512 + 48))
        threshold = max(2.0, 0.5 * theoretical_fps)
        suite.record(
            "throughput",
            frames_per_s >= threshold,
            f"{n} x 512B echoes in {elapsed:.2f}s = {frames_per_s:.1f} frames/s "
            f"(~{bulk/1024:.1f} KB/s payload; {args.baud} baud ceiling "
            f"~{args.baud/10/1024:.1f} KB/s = ~{theoretical_fps:.1f} frames/s, "
            f"pass bar {threshold:.1f})",
            frames_per_s=round(frames_per_s, 2),
            theoretical_fps=round(theoretical_fps, 2),
            threshold=round(threshold, 2),
        )

        stats = link.request("stats", timeout=2.0).get("data", {})
        link_stats = stats.get("link", {})
        overflow_delta = int(link_stats.get("overflow", 0)) - overflow_baseline
        # Two deliberately oversize lines were sent above (2000-byte echo
        # payload, 4000-byte line). Exactly two rejections are expected; more
        # would mean the device is tearing frames it should have accepted.
        suite.record(
            "device_counters_clean",
            overflow_delta <= 2 and link_stats.get("uart_err", 0) == 0,
            f"overflow +{overflow_delta} from baseline (2 deliberate oversize probes), "
            f"uart_err={link_stats.get('uart_err')} "
            f"heap_free={stats.get('mem', {}).get('heap_free')}",
            stats=stats,
            overflow_baseline=overflow_baseline,
        )

        # --- 10. reconnect ---------------------------------------------------
        device_mac = idata.get("mac_sta")
        port, baud = args.port, args.baud
        link.close()
        time.sleep(0.5)
        link2 = Link(port, baud, verbose=args.verbose)
        try:
            h2 = None
            for _ in range(6):
                try:
                    f = link2.request("hello", timeout=1.0)
                    if f.get("type") == "hello":
                        h2 = f
                        break
                except LinkError:
                    time.sleep(0.35)
            same_device = h2 is not None and h2.get("data", {}).get("device") == data.get("device")
            suite.record(
                "reconnect",
                same_device,
                f"reopened {port} and re-handshaked successfully"
                if same_device
                else "could not re-handshake after reconnect",
                mac_before=device_mac,
            )
        finally:
            link2.close()
        link = None

    finally:
        if link is not None:
            link.close()

    _write_report(suite, args, pings)
    print()
    total = len(suite.checks)
    passed = sum(1 for c in suite.checks if c.passed)
    print(f"{passed}/{total} checks passed  —  {'PHASE 1 LINK OK' if suite.ok else 'ACTION NEEDED'}")
    return 0 if suite.ok else 1


def _write_report(suite: Suite, args: argparse.Namespace, pings: Optional[list[float]] = None) -> None:
    if args.report:
        payload = suite.to_dict()
        payload["port"] = args.port
        payload["baud"] = args.baud
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        print(f"\nJSON report written to {args.report}")
    if args.csv and pings:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["sample", "rtt_ms"])
            for i, p in enumerate(pings):
                writer.writerow([i, round(p, 3)])
        print(f"Ping CSV written to {args.csv}")


# ---------------------------------------------------------------------------
# self-test (no hardware: host parser only)
# ---------------------------------------------------------------------------
def cmd_self_test(_args: argparse.Namespace) -> int:
    print("Host parser self-test (no hardware, no scan data involved)\n")
    failures = 0

    def check(name: str, cond: bool, detail: str) -> None:
        nonlocal failures
        if not cond:
            failures += 1
        print(f"[{'PASS' if cond else 'FAIL'}] {name}: {detail}")

    # 1. Frame split across arbitrary chunk boundaries (real USB behaviour).
    frame = {"v": 1, "id": 3, "seq": 10, "type": "pong", "ts": 1234, "data": {"t": 1}}
    blob = (json.dumps(frame) + "\n").encode()
    parser = FrameParser()
    got: list[dict[str, Any]] = []
    for i in range(0, len(blob), 7):
        got.extend(parser.feed(blob[i : i + 7]))
    check("chunked_frame_assembly", len(got) == 1 and got[0]["type"] == "pong",
          f"{len(blob)} bytes in 7-byte chunks -> {len(got)} frame")

    # 2. Bootloader spew before the first real frame must be tolerated.
    parser = FrameParser()
    got = parser.feed(b"\x1b[0;32mESP-ROM:esp32c2-20220104\r\nBuild:Jan  4 2022\r\n" + blob)
    check("bootloader_noise_skipped", len(got) == 1 and parser.non_json_lines >= 1,
          f"parsed {len(got)} frame, ignored {parser.non_json_lines} noise line(s)")

    # 3. CRLF terminators.
    parser = FrameParser()
    got = parser.feed(blob.replace(b"\n", b"\r\n"))
    check("crlf_terminator", len(got) == 1, "CRLF-terminated frame parsed")

    # 4. Truncated / malformed JSON is counted, not crashed on.
    parser = FrameParser()
    got = parser.feed(b'{"v":1,"id":5,"cmd":\n')
    check("malformed_json_counted", not got and parser.malformed_json_lines == 1,
          f"malformed={parser.malformed_json_lines}, frames={len(got)}")

    # 5. UTF-8 SSIDs survive; invalid UTF-8 bytes do not raise.
    ssid_frame = {"v": 1, "id": 1, "seq": 1, "type": "scan_result",
                  "data": {"networks": [{"ssid": "مقهى_الواي_فاي", "rssi": -52}]}}
    parser = FrameParser()
    got = parser.feed((json.dumps(ssid_frame, ensure_ascii=False) + "\n").encode())
    check("utf8_ssid", bool(got) and got[0]["data"]["networks"][0]["ssid"] == "مقهى_الواي_فاي",
          "Arabic SSID round-tripped through the parser")
    parser = FrameParser()
    got = parser.feed(b'{"v":1,"type":"event.log","data":{"msg":"bad \xff\xfe bytes"}}\n')
    check("invalid_utf8_tolerated", bool(got), "invalid UTF-8 replaced, frame still parsed")

    # 6. seq-gap detection: the mechanism that proves a clean link.
    parser = FrameParser()
    for seq in (1, 2, 3, 7, 8):
        parser.feed((json.dumps({"v": 1, "seq": seq, "type": "x"}) + "\n").encode())
    check("seq_gap_detection", parser.seq_gaps == 1 and parser.seq_missing == 3,
          f"gap detected: gaps={parser.seq_gaps}, missing={parser.seq_missing}")

    # 7. Oversized unterminated garbage must not grow memory forever.
    parser = FrameParser(max_buffer=2048)
    parser.feed(b"X" * 5000)
    check("buffer_bounded", len(parser._buf) == 0 and parser.non_json_lines >= 1,
          "runaway input bounded and counted")

    print()
    print("Self-test OK — host parser is sound." if failures == 0 else f"{failures} self-test failure(s).")
    return 0 if failures == 0 else 1


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Scan capture (Phase 2)
# ---------------------------------------------------------------------------
# --- classification flags (PROTOCOL.md 7) -----------------------------------
#
# `strong` means "WPA3-capable", not "WPA3 enforced". A wpa2/wpa3 access point
# is in transition mode: it still admits WPA2-PSK clients, so the flag cannot
# promise that a connection uses WPA3. The contract says so explicitly, because
# a UI that prints "strong" next to a transition-mode AP is overstating what
# the radio measured - the same mistake the `weak` rule exists to avoid.
WPA3_CAPABLE = {
    "wpa3",                 # WPA3_PSK, WPA3_EXT_PSK, WPA3_EXT_PSK_MIXED_MODE
    "wpa2/wpa3",            # transition mode - WPA3 offered, WPA2 still accepted
    "wpa3-192",             # WPA3-Enterprise 192-bit
    "wpa3-enterprise",      # WPA3-Enterprise
    "wpa2/wpa3-enterprise",
    "owe",                  # Opportunistic Wireless Encryption
}

# Firmware v0.2.1 does not set `strong` for the two WPA3-enterprise strings:
# net_classify()'s `modern` set omits WIFI_AUTH_WPA3_ENTERPRISE and
# WIFI_AUTH_WPA2_WPA3_ENTERPRISE. That is a defect in the device, but fixing it
# needs a rebuild and a re-flash, so it is recorded as a known deviation and
# reported to the user instead of being silently accepted or silently failed.
KNOWN_V021_STRONG_GAP = {"wpa3-enterprise", "wpa2/wpa3-enterprise"}


SECURITY_VALUES = {
    "open", "wep", "wpa", "wpa2", "wpa/wpa2", "wpa2/wpa3", "wpa3", "wpa3-192",
    "owe", "wapi", "unknown",
    # enterprise variants are reported rather than collapsed into "unknown"
    "wpa2-enterprise", "wpa3-enterprise", "wpa2/wpa3-enterprise", "wpa-enterprise",
}


def quality_from_rssi(rssi: int) -> int:
    """The device computes this; the host checks it agrees. 2*(rssi+100) clamped."""
    return max(0, min(100, 2 * (rssi + 100)))


def validate_network(n: dict[str, Any]) -> list[str]:
    """Return a list of contract violations for one network object.

    This is deliberately strict: it is the test that catches a device inventing
    data. Every field must be consistent with what a radio could have measured.
    """
    bad: list[str] = []
    bssid = n.get("bssid", "")
    if not isinstance(bssid, str) or len(bssid) != 17 or bssid.count(":") != 5:
        bad.append(f"bssid malformed: {bssid!r}")
    else:
        for part in bssid.split(":"):
            if len(part) != 2 or any(c not in "0123456789ABCDEFabcdef" for c in part):
                bad.append(f"bssid not hex: {bssid!r}")
                break

    ch = n.get("channel")
    if not isinstance(ch, int) or not (1 <= ch <= 14):
        bad.append(f"channel out of range: {ch!r}")
    else:
        expect_freq = 2484 if ch == 14 else 2407 + 5 * ch
        if n.get("freq_mhz") != expect_freq:
            bad.append(f"freq_mhz {n.get('freq_mhz')!r} != {expect_freq} for channel {ch}")

    rssi = n.get("rssi")
    if not isinstance(rssi, int) or not (-110 <= rssi <= 10):
        bad.append(f"rssi implausible: {rssi!r}")
    elif n.get("quality") != quality_from_rssi(rssi):
        bad.append(f"quality {n.get('quality')!r} != {quality_from_rssi(rssi)} for rssi {rssi}")

    if n.get("band") != "2.4ghz":
        bad.append(f"band must be '2.4ghz' on this chip, got {n.get('band')!r}")
    if n.get("width_mhz") not in (20, 40):
        bad.append(f"width_mhz not 20/40: {n.get('width_mhz')!r}")

    sec = n.get("security")
    if sec not in SECURITY_VALUES:
        bad.append(f"security not in contract: {sec!r}")

    hidden = n.get("hidden")
    ssid = n.get("ssid")
    ssid_hex = n.get("ssid_hex")
    if hidden is True:
        # The honesty rule: a hidden network must never acquire a name.
        if ssid not in ("", None):
            bad.append(f"hidden network carries an SSID {ssid!r} - invented data")
        if ssid_hex:
            bad.append("hidden network carries ssid_hex")
    else:
        if not isinstance(ssid, str) and not ssid_hex:
            bad.append("visible network has neither ssid nor ssid_hex")
        if isinstance(ssid, str) and any(ord(c) < 0x20 for c in ssid):
            bad.append("ssid contains control characters")

    wps = n.get("wps")
    flags = n.get("flags", [])
    if not isinstance(flags, list):
        bad.append("flags must be an array")
    else:
        expect_weak = (sec == "open") or (wps is True)
        if ("weak" in flags) != expect_weak:
            bad.append(f"weak flag {('weak' in flags)} inconsistent with open={sec == 'open'} wps={wps}")
        if ("wps" in flags) != (wps is True):
            bad.append("wps flag inconsistent with wps field")
        if ("hidden" in flags) != (hidden is True):
            bad.append("hidden flag inconsistent with hidden field")
        if ("open" in flags) != (sec == "open"):
            bad.append("open flag inconsistent with security field")

        wants_strong = sec in WPA3_CAPABLE
        has_strong = "strong" in flags
        if wants_strong != has_strong:
            if wants_strong and sec in KNOWN_V021_STRONG_GAP:
                pass  # reported as a known deviation by cmd_scan, not a failure
            else:
                bad.append(
                    f"strong flag {has_strong} inconsistent with security {sec!r} "
                    f"(WPA3-capable={wants_strong})")
    return bad


def known_deviation(n: dict[str, Any]) -> str | None:
    """Describe a deviation we already understand, or None.

    Kept separate from validate_network so a real contract violation can never
    be excused by accident: only the exact, documented case returns a string.
    """
    sec = n.get("security")
    if sec in KNOWN_V021_STRONG_GAP and "strong" not in (n.get("flags") or []):
        return (f"{n.get('bssid', '?')}: wpa3-enterprise without the strong flag "
                f"is a known firmware v0.2.1 deviation (net_classify omits the "
                f"enterprise auth modes from its `modern` set)")
    return None


def _connect_and_handshake(port: str, baud: int, verbose: bool) -> tuple["Link", dict[str, Any]]:
    link = Link(port, baud, verbose=verbose)
    for _ in range(8):
        try:
            frame = link.request("hello", timeout=1.0)
            if frame.get("type") == "hello":
                return link, frame.get("data", {})
        except LinkError:
            time.sleep(0.35)
    link.close()
    raise LinkError("no hello from device - run the probe subcommand first")


def collect_scan_pages(
    link: "Link", deadline: float, per_frame_timeout: float = 1.5
) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
    """Gather every page of one sweep, keyed by page number.

    Uses next_frame() (cursor-based) rather than wait_for(): wait_for restarts
    at the oldest buffered frame on every call, so a loop over it collects page
    1 repeatedly and silently drops the rest of the sweep.
    """
    pages: dict[int, dict[str, Any]] = {}
    meta: dict[str, Any] = {}
    while time.monotonic() < deadline:
        frame = link.next_frame(lambda f: f.get("type") == "scan_result", per_frame_timeout)
        if frame is None:
            continue
        data = frame.get("data", {})
        meta = data
        pages[int(data.get("page", 1))] = data
        expected = data.get("pages")
        if expected and len(pages) >= int(expected):
            break
    return pages, meta


def cmd_scan(args: argparse.Namespace) -> int:
    print(f"Scanning via {args.port} at {args.baud} baud...\n")
    try:
        link, hello = _connect_and_handshake(args.port, args.baud, args.verbose)
    except LinkError as exc:
        print(f"[FAIL] {exc}")
        return 1

    try:
        print(f"device={hello.get('device')} fw={hello.get('fw')} phase={hello.get('phase')}")
        try:
            ack = link.request("scan.start", {"continuous": False}, timeout=3.0)
        except LinkError as exc:
            print(f"[FAIL] scan.start: {exc}")
            return 1
        if ack.get("type") != "scan_started":
            print(f"[FAIL] scan.start returned {ack.get('type')}/{ack.get('code')}: {ack.get('msg')}")
            return 1
        print(f"scan_id={ack.get('data', {}).get('scan_id')} acknowledged; "
              f"waiting up to {args.seconds:.0f}s for results...\n")

        pages, meta = collect_scan_pages(link, time.monotonic() + args.seconds)

        networks: list[dict[str, Any]] = []
        for idx in sorted(pages):
            networks.extend(pages[idx].get("networks", []))

        if not pages:
            print("[NO DATA] No scan_result frame arrived. The device did not report "
                  "any networks. Nothing has been invented to fill the gap.")
            return 2

        violations: list[str] = []
        notes: list[str] = []
        seen_bssid: dict[str, int] = {}
        for n in networks:
            for problem in validate_network(n):
                violations.append(f"{n.get('bssid', '?')}: {problem}")
            note = known_deviation(n)
            if note:
                notes.append(note)
            b = n.get("bssid")
            seen_bssid[b] = seen_bssid.get(b, 0) + 1
        for b, count in seen_bssid.items():
            if count > 1:
                violations.append(f"duplicate BSSID {b} appears {count} times")

        expected_pages = int(meta.get("pages") or 1)
        if len(pages) < expected_pages:
            missing = [p for p in range(1, expected_pages + 1) if p not in pages]
            violations.append(
                f"only {len(pages)} of {expected_pages} page(s) arrived (missing {missing}); "
                f"{len(networks)} of {meta.get('total')} networks were captured")

        total = meta.get("total")
        if isinstance(total, int) and total != len(networks):
            violations.append(f"total={total} but {len(networks)} network objects arrived")

        print(f"scan_id={meta.get('scan_id')} pages={meta.get('pages')} total={total} "
              f"scan_ms={meta.get('scan_ms')} truncated={meta.get('truncated')}")
        if meta.get("dropped"):
            print(f"WARNING: radio saw {meta.get('dropped')} more networks than the "
                  f"device could hold; results are incomplete by design")
        print()

        if networks:
            networks.sort(key=lambda x: x.get("rssi", -999), reverse=True)
            hdr = f"{'SSID':<24} {'BSSID':<18} {'CH':>3} {'MHz':>5} {'RSSI':>5} {'Q':>4} {'SECURITY':<12} FLAGS"
            print(hdr)
            print("-" * len(hdr))
            for n in networks:
                name = n.get("ssid") or ("<hidden>" if n.get("hidden") else n.get("ssid_hex", "?"))
                print(f"{str(name)[:24]:<24} {n.get('bssid', ''):<18} "
                      f"{n.get('channel', 0):>3} {n.get('freq_mhz', 0):>5} "
                      f"{n.get('rssi', 0):>5} {n.get('quality', 0):>4} "
                      f"{str(n.get('security', '')):<12} {','.join(n.get('flags', []))}")
        else:
            print("(no networks in range - a real, empty sweep)")

        weak = [n for n in networks if "weak" in n.get("flags", [])]
        hidden = [n for n in networks if n.get("hidden")]
        print(f"\n{len(networks)} network(s): {len(weak)} potentially weak "
              f"(open or WPS advertised), {len(hidden)} hidden")

        if args.json:
            payload = {
                "captured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "port": args.port, "baud": args.baud,
                "device": hello.get("device"), "fw": hello.get("fw"),
                "scan_id": meta.get("scan_id"), "pages": meta.get("pages"),
                "total": total, "truncated": meta.get("truncated"),
                "scan_ms": meta.get("scan_ms"),
                "networks": networks,
                "contract_violations": violations,
            }
            pathlib.Path(args.json).parent.mkdir(parents=True, exist_ok=True)
            pathlib.Path(args.json).write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                                               encoding="utf-8")
            print(f"JSON written to {args.json}")

        if args.csv:
            pathlib.Path(args.csv).parent.mkdir(parents=True, exist_ok=True)
            with open(args.csv, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["timestamp_utc", "bssid", "ssid", "hidden", "channel", "freq_mhz",
                            "rssi", "quality", "security", "wps", "flags"])
                stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                for n in networks:
                    w.writerow([stamp, n.get("bssid", ""), n.get("ssid", ""),
                                n.get("hidden"), n.get("channel"), n.get("freq_mhz"),
                                n.get("rssi"), n.get("quality"), n.get("security"),
                                n.get("wps"), "|".join(n.get("flags", []))])
            print(f"CSV written to {args.csv}")

        for note in notes:
            print(f"[NOTE] {note}")

        if violations:
            print(f"\n[FAIL] {len(violations)} contract violation(s):")
            for v in violations[:20]:
                print(f"  - {v}")
            return 1
        if notes:
            print(f"\n[PASS] no violations; {len(notes)} known deviation(s) noted above")
        else:
            print("\n[PASS] every field is consistent with the contract (no invented data)")
        return 0
    finally:
        try:
            link.request("scan.stop", timeout=1.0)
        except LinkError:
            pass
        link.close()


def main(argv: Optional[Iterable[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Phase 1 validation harness for the ESP8684 Wi-Fi Analyzer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = ap.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="enumerate serial ports and identify the bridge chip")
    p_list.set_defaults(func=cmd_list)

    p_probe = sub.add_parser("probe", help="run the Phase 1 acceptance suite against a board")
    p_probe.add_argument("--port", required=True, help="e.g. COM5 or /dev/ttyUSB0")
    p_probe.add_argument("--baud", type=int, default=115200, help="line rate (default 115200)")
    p_probe.add_argument("--pings", type=int, default=20, help="ping samples (default 20)")
    p_probe.add_argument("--throughput-frames", type=int, default=100,
                         help="echo frames for the throughput test (default 100)")
    p_probe.add_argument("--report", help="write a JSON report to this path")
    p_probe.add_argument("--csv", help="write ping RTT samples to this CSV path")
    p_probe.add_argument("--verbose", action="store_true", help="echo every frame")
    p_probe.set_defaults(func=cmd_probe)

    p_scan = sub.add_parser("scan", help="run one real scan and validate every field")
    p_scan.add_argument("--port", required=True, help="e.g. COM9 or /dev/ttyUSB0")
    p_scan.add_argument("--baud", type=int, default=115200)
    p_scan.add_argument("--seconds", type=float, default=12.0,
                        help="how long to wait for scan_result frames (default 12)")
    p_scan.add_argument("--json", help="write the results to this JSON path")
    p_scan.add_argument("--csv", help="write the results to this CSV path")
    p_scan.add_argument("--verbose", action="store_true")
    p_scan.set_defaults(func=cmd_scan)

    p_st = sub.add_parser("self-test", help="validate the host parser without hardware")
    p_st.set_defaults(func=cmd_self_test)

    args = ap.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
