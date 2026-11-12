#!/usr/bin/env python3
"""Offline regression tests for the host-side scan path.

Two things are covered: reassembly of a paged sweep, and the classification-flag
contract (`strong` / `weak` / `wps` / `hidden`).

Run:  python3 tools/host/test_scan_collect.py

WHY THIS EXISTS
---------------
A hardware run on v0.2.1 returned a healthy 13-network sweep in 3 pages, but the
probe reported `total=13 but 5 network objects arrived`. The device was correct;
the host collector was not. SerialLink.wait_for() kept its scan position in a
local variable, so every call started again at the oldest buffered frame. The
collection loop therefore read page 1 three times and never saw pages 2-3.

TEST DATA IS SYNTHETIC AND SAYS SO
----------------------------------
The frames built below are a FIXTURE. They describe 13 invented networks
(Net0..Net12) with made-up BSSIDs and RSSIs. Nothing here is presented as a
measurement, no real SSID is used, and this file must never be read as a scan
result. Its only job is to prove that a 5/5/3 three-page sequence is reassembled
into 13 objects along the exact code path the hardware uses.

A live sweep can only be produced by the board. That is what
`serial_probe.py scan --port COM9` is for.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
import types
from pathlib import Path

SUITE_VERSION = "2 (collector + flag contract)"

HERE = Path(__file__).resolve().parent
PROBE = HERE / "serial_probe.py"

# How the real board split its 13 networks; kept identical on purpose.
PAGE_SPLIT = [5, 5, 3]
TOTAL = sum(PAGE_SPLIT)


class FakeSerial:
    """Stands in for the USB-UART bridge. Feeds pre-built lines, accepts writes."""

    queue: list[bytes] = []

    def __init__(self, port, baudrate=None, timeout=None, write_timeout=None):
        self.port = port

    dtr = False
    rts = False

    def reset_input_buffer(self) -> None:
        pass

    def flush(self) -> None:
        pass

    def write(self, data: bytes) -> int:
        return len(data)

    def read(self, size: int) -> bytes:
        if not FakeSerial.queue:
            time.sleep(0.005)
            return b""
        return FakeSerial.queue.pop(0)

    @property
    def in_waiting(self) -> int:
        return 0

    def close(self) -> None:
        pass


def load_probe():
    """Import serial_probe.py with a fake pyserial installed."""
    serial_mod = types.ModuleType("serial")
    serial_mod.Serial = FakeSerial
    tools_mod = types.ModuleType("serial.tools")
    ports_mod = types.ModuleType("serial.tools.list_ports")
    ports_mod.comports = lambda: []
    tools_mod.list_ports = ports_mod
    serial_mod.tools = tools_mod
    sys.modules.update(
        {"serial": serial_mod, "serial.tools": tools_mod, "serial.tools.list_ports": ports_mod}
    )

    spec = importlib.util.spec_from_file_location("serial_probe", PROBE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["serial_probe"] = module
    spec.loader.exec_module(module)
    return module


def synthetic_network(probe, index: int) -> dict:
    """One invented network. Not a measurement - see the module docstring."""
    channel = [1, 6, 11, 2, 9][index % 5]
    rssi = -40 - index
    return {
        "bssid": f"AA:BB:CC:DD:EE:{index:02X}",
        "ssid": f"Net{index}",
        "rssi": rssi,
        "quality": probe.quality_from_rssi(rssi),
        "channel": channel,
        "freq_mhz": 2484 if channel == 14 else 2407 + 5 * channel,
        "band": "2.4ghz",
        "width_mhz": 20,
        "security": "wpa2",
        "hidden": False,
        "wps": False,
        "flags": [],
    }


def build_sweep(probe) -> list[bytes]:
    """Three scan_result frames, 5/5/3 networks, exactly as the board emits them."""
    frames: list[bytes] = []
    first = 0
    for page, count in enumerate(PAGE_SPLIT, start=1):
        networks = [synthetic_network(probe, i) for i in range(first, first + count)]
        first += count
        frame = {
            "v": 1,
            "id": 0,
            "seq": page,
            "type": "scan_result",
            "ts": page,
            "data": {
                "scan_id": 1,
                "page": page,
                "pages": len(PAGE_SPLIT),
                "total": TOTAL,
                "truncated": False,
                "scan_ms": 2287,
                "networks": networks,
            },
        }
        frames.append(json.dumps(frame).encode() + b"\n")
    return frames


def test_paged_sweep_is_fully_reassembled(probe) -> bool:
    FakeSerial.queue = build_sweep(probe)
    link = probe.Link("FAKE", 115200)
    try:
        pages, meta = probe.collect_scan_pages(link, time.monotonic() + 5.0)
    finally:
        link.close()

    networks = [n for page in sorted(pages) for n in pages[page].get("networks", [])]
    problems = [v for n in networks for v in probe.validate_network(n)]
    duplicate_free = len({n["bssid"] for n in networks}) == len(networks)

    ok = (
        sorted(pages) == [1, 2, 3]
        and len(networks) == TOTAL == meta.get("total")
        and not problems
        and duplicate_free
    )
    print(f"  pages={sorted(pages)} networks={len(networks)}/{TOTAL} "
          f"total={meta.get('total')} violations={len(problems)} unique={duplicate_free}")
    return ok


def test_wait_for_would_repeat_page_one(probe) -> bool:
    """Documents the bug that was fixed: wait_for() cannot stream."""
    FakeSerial.queue = build_sweep(probe)
    link = probe.Link("FAKE", 115200)
    try:
        time.sleep(0.2)
        seen = [
            link.wait_for(lambda f: f.get("type") == "scan_result", 0.3)["data"]["page"]
            for _ in range(3)
        ]
    finally:
        link.close()
    print(f"  wait_for x3 -> pages={seen} (expected [1, 1, 1]: it never advances)")
    return seen == [1, 1, 1]


def test_short_sweep_is_reported_not_hidden(probe) -> bool:
    """If a page never arrives, the collector must expose the gap, not paper over it."""
    frames = build_sweep(probe)[:1]  # page 1 only
    FakeSerial.queue = frames
    link = probe.Link("FAKE", 115200)
    try:
        pages, meta = probe.collect_scan_pages(link, time.monotonic() + 1.0)
    finally:
        link.close()
    networks = [n for page in pages.values() for n in page.get("networks", [])]
    missing = [p for p in range(1, int(meta.get("pages", 1)) + 1) if p not in pages]
    print(f"  truncated feed -> pages={sorted(pages)} missing={missing} "
          f"networks={len(networks)} of total={meta.get('total')}")
    return sorted(pages) == [1] and missing == [2, 3] and len(networks) == 5


def test_flag_contract(probe) -> bool:
    """`strong` means WPA3-CAPABLE, not WPA3-enforced (PROTOCOL.md 7).

    This check exists because real hardware data exposed the gap: a wpa2/wpa3
    transition-mode AP was flagged `strong` while the spec excluded it, and the
    host validator never looked at `strong` at all, so nothing failed.
    """
    cases = [
        # security,                 flags,                      expect_violation
        ("wpa3",                    ["strong"],                 False),
        ("wpa2/wpa3",               ["strong"],                 False),   # transition mode
        ("wpa3-192",                ["strong"],                 False),
        ("owe",                     ["strong"],                 False),
        ("wpa3-enterprise",         ["strong"],                 False),
        ("wpa2/wpa3-enterprise",    ["strong"],                 False),
        ("wpa2",                    ["strong"],                 True),    # WPA2 is not WPA3
        ("wpa2",                    [],                         False),
        ("wpa",                     ["strong"],                 True),    # not even WPA2
        ("wep",                     ["strong"],                 True),
        ("open",                    ["weak", "open"],           False),
        ("open",                    ["weak"],                   True),    # open must carry open
        ("open",                    ["open"],                   True),    # open is always weak
        ("wpa2",                    ["weak"],                   True),    # closed network is not weak
        ("wpa3",                    [],                         True),    # WPA3 without strong
    ]
    failures = []
    for sec, flags, expect_bad in cases:
        n = synthetic_network(probe, 0)
        n["security"] = sec
        n["wps"] = "wps" in flags
        n["flags"] = list(flags)
        got = probe.validate_network(n)
        if bool(got) != expect_bad:
            failures.append(f"{sec}/{'|'.join(flags) or 'no flags'}: "
                            f"expected {'violation' if expect_bad else 'accepted'}, "
                            f"got {got or 'accepted'}")
    for f in failures:
        print(f"  {f}")
    print(f"  {len(cases) - len(failures)}/{len(cases)} flag cases behave as the contract says")
    return not failures


def test_enterprise_gap_is_reported_not_hidden(probe) -> bool:
    """Firmware v0.2.1 omits `strong` on WPA3-enterprise. That must surface as a
    note, not vanish and not fail the run."""
    n = synthetic_network(probe, 0)
    n["security"] = "wpa3-enterprise"
    n["flags"] = []
    note = probe.known_deviation(n)
    violations = probe.validate_network(n)
    ok = bool(note) and not violations
    print(f"  note={'yes' if note else 'NO'} violations={violations or 'none'}")
    return ok


def main() -> int:
    probe = load_probe()
    print(f"offline host-tool regression test suite v{SUITE_VERSION}")
    print(f"synthetic fixture: {TOTAL} invented networks - never a substitute "
          f"for a real sweep\n")
    checks = [
        ("3-page sweep reassembled to 13 objects", test_paged_sweep_is_fully_reassembled),
        ("wait_for() reproduces the old page-1 loop", test_wait_for_would_repeat_page_one),
        ("missing pages are surfaced, not filled in", test_short_sweep_is_reported_not_hidden),
        ("flag contract: strong = WPA3-capable, not WPA3-enforced", test_flag_contract),
        ("wpa3-enterprise gap is reported, not hidden", test_enterprise_gap_is_reported_not_hidden),
    ]
    results = []
    for label, fn in checks:
        try:
            passed = bool(fn(probe))
        except Exception as exc:  # noqa: BLE001 - a crash is a failed check, not a traceback
            print(f"  EXCEPTION {type(exc).__name__}: {exc}")
            passed = False
        print(f"  {'PASS' if passed else 'FAIL'}  {label}\n")
        results.append(passed)

    passed_count = sum(results)
    print(f"{passed_count}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
