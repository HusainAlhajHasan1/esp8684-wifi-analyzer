#!/usr/bin/env python3
"""Protocol regression harness — NOT firmware, NOT a data source.

Replays the 22-check acceptance suite against a virtual serial port so host
tooling can be tested without a board attached.

Guard rails, deliberately built in:
  * it answers only control commands (hello/ping/echo/info/stats/config/hb);
  * scan commands are acknowledged with an EMPTY result set (total 0) - it has
    no radio, so "no networks" is the only truthful answer; SoftAP commands
    return E_UNSUPPORTED. It therefore CANNOT emit network records;
  * it is never imported by the Android app and never flashed to hardware.

It exists so a change to serial_probe.py can be validated in seconds instead of
by re-flashing a board - it is a test double for the LINK, not for the radio.
"""
import json
import os
import pathlib
import pty
import subprocess
import sys
import threading
import time

PROBE = str(pathlib.Path(__file__).with_name("serial_probe.py"))
PORT = None
seq = 0
overflow = 0
uart_err = 0
cfg = {"heartbeat_ms": 5000, "scan_interval_ms": 15000, "max_networks": 32,
       "baud": 115200, "log_level": 2}
MAX_INBOUND = 2048
MAX_ECHO = 1024


def frame(type_, rid, data=None):
    global seq
    seq += 1
    f = {"v": 1, "id": rid, "seq": seq, "type": type_, "ts": int(time.time() * 1000) % 100000}
    if data is not None:
        f["data"] = data
    return json.dumps(f, separators=(",", ":")) + "\n"


def err(rid, code, msg):
    return json.dumps({"v": 1, "id": rid, "seq": 0, "type": "error", "code": code,
                       "msg": msg}, separators=(",", ":")) + "\n"


def peek_id(line: str) -> int:
    i = line.find('"id"')
    if i < 0:
        return 0
    j = line.find(":", i)
    if j < 0:
        return 0
    j += 1
    while j < len(line) and line[j] in " \t":
        j += 1
    neg = line[j:j + 1] == "-"
    if neg:
        j += 1
    k = j
    while k < len(line) and line[k].isdigit():
        k += 1
    if k == j:
        return 0
    v = min(int(line[j:k]), 2**31 - 1)
    return -v if neg else v


def handle(line: str) -> str | None:
    global overflow, cfg
    if len(line) > MAX_INBOUND:
        overflow += 1
        return err(peek_id(line), "E_TOO_LARGE", f"inbound frame exceeds {MAX_INBOUND} bytes")
    try:
        req = json.loads(line)
        if not isinstance(req, dict):
            raise ValueError
    except Exception:
        return err(0, "E_BAD_JSON", "line is not a JSON object")

    rid = req.get("id", 0)
    cmd = req.get("cmd")
    p = req.get("params") or {}

    if cmd == "hello":
        return frame("hello", rid, {
            "device": "ESP8684-DevKitM-1", "chip": "ESP32-C2", "fw": "0.2.0",
            "build_tag": "phase2", "proto": 1, "phase": 2, "baud": cfg["baud"],
            "ts_source": "uptime_ms", "encoding": "json-lines/utf-8",
            "commands": ["hello", "ping", "echo", "info", "stats", "config.get",
                         "config.set", "hb.set", "reboot",
                         "scan.start", "scan.stop", "scan.get"],
            "planned": ["ap.start", "ap.stop"],
            "limits": {"max_inbound_bytes": MAX_INBOUND, "max_echo_bytes": MAX_ECHO,
                       "max_networks": cfg["max_networks"]}})
    if cmd == "info":
        return frame("info", rid, {
            "device": "ESP8684-DevKitM-1", "chip": "ESP32-C2", "chip_rev": "v2.0",
            "cores": 1, "flash_bytes": 4194304, "mac_sta": "B8:7B:4D:C8:AA:0C",
            "xtal_mhz": 26,
            "fw": {"version": "0.2.0", "project": "esp8684-wifi-analyzer",
                   "idf": "v5.5.5", "build_tag": "phase1"},
            "uptime_ms": int(time.time() * 1000) % 1000000,
            "heap_free": 198432, "heap_min_free": 191880,
            "reset_reason": "power_on"})
    if cmd == "ping":
        return frame("pong", rid, {"t": p.get("t"), "pong": "ok"})
    if cmd == "echo":
        payload = p.get("payload")
        if not isinstance(payload, str):
            return err(rid, "E_BAD_PARAM", "payload must be a string")
        if len(payload) > MAX_ECHO:
            return err(rid, "E_TOO_LARGE", f"payload exceeds {MAX_ECHO} bytes")
        return frame("echo", rid, {"payload": payload, "len": len(payload)})
    if cmd == "stats":
        return frame("stats", rid, {
            "link": {"bytes_rx": 1000, "bytes_tx": 900, "lines_rx": 40,
                     "overflow": overflow, "uart_err": uart_err, "frames": 40},
            "mem": {"heap_free": 198432, "heap_min_free": 191880}})
    if cmd == "config.get":
        return frame("config", rid, dict(cfg))
    if cmd == "config.set":
        for k, v in p.items():
            if k == "scan_interval_ms" and (not isinstance(v, int) or v < 1000):
                return err(rid, "E_BAD_PARAM", "scan_interval_ms out of range")
            if k in cfg or k in ("heartbeat_ms", "scan_interval_ms"):
                cfg[k] = v
        return frame("config", rid, dict(cfg))
    if cmd == "hb.set":
        cfg["heartbeat_ms"] = int(p.get("heartbeat_ms", cfg["heartbeat_ms"]))
        return frame("config", rid, dict(cfg))
    if cmd == "reboot":
        return frame("ack", rid, {"reboot": "scheduled"})
    if cmd == "scan.start":
        iv = p.get("interval_ms")
        if iv is not None and (not isinstance(iv, int) or isinstance(iv, bool) or iv < 1000 or iv > 60000):
            return err(rid, "E_BAD_PARAM", "interval_ms out of range")
        # Acknowledge like real firmware, then deliver an EMPTY sweep. This
        # harness must never fabricate network records: an empty result is the
        # only truthful thing a device with no radio can report.
        threading.Thread(target=emit_empty_scan, args=(master_fd_ref[0],), daemon=True).start()
        return frame("scan_started", rid, {"scan_id": 1, "continuous": False})
    if cmd == "scan.stop":
        return frame("scan_stopped", rid, {"scan_id": 1})
    if cmd == "scan.get":
        return frame("scan_status", rid, {"scan_id": 1, "scanning": False, "continuous": False,
                                          "networks": 0, "scans": 1, "max_networks": 32})
    if cmd in ("ap.start", "ap.stop"):
        return err(rid, "E_UNSUPPORTED", f"{cmd} is not implemented yet")
    return err(rid, "E_UNKNOWN_CMD", f"unknown command '{cmd}'")


master_fd_ref = [None]


def emit_empty_scan(fd: int) -> None:
    time.sleep(0.3)
    try:
        os.write(fd, frame("scan_result", 0, {
            "scan_id": 1, "page": 1, "pages": 1, "total": 0,
            "truncated": False, "scan_ms": 300, "networks": []}).encode())
    except OSError:
        pass


def device_loop(master_fd: int) -> None:
    buf = b""
    dropping = False
    while True:
        try:
            chunk = os.read(master_fd, 4096)
        except OSError:
            return
        if not chunk:
            continue
        buf += chunk
        while b"\n" in buf:
            raw, buf = buf.split(b"\n", 1)
            line = raw.rstrip(b"\r").decode("utf-8", "replace")
            if not line.strip():
                continue
            out = handle(line)
            if out:
                os.write(master_fd, out.encode())


def heartbeat_loop(master_fd: int) -> None:
    while True:
        time.sleep(1.2)
        try:
            os.write(master_fd, frame("event.heartbeat", 0,
                                      {"uptime_ms": int(time.time() * 1000) % 1000000,
                                       "heap_free": 198432}).encode())
        except OSError:
            return


def main() -> int:
    master, slave = pty.openpty()
    master_fd_ref[0] = master
    port = os.ttyname(slave)
    print(f"virtual device on {port}")
    threading.Thread(target=device_loop, args=(master,), daemon=True).start()
    threading.Thread(target=heartbeat_loop, args=(master,), daemon=True).start()

    cmd = [sys.executable, PROBE, "probe", "--port", port, "--pings", "5",
           "--throughput-frames", "8", "--report", "/tmp/sim_report.json",
           "--csv", "/tmp/sim_pings.csv"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    print(r.stdout[-6000:])
    if r.stderr.strip():
        print("STDERR:", r.stderr[-2000:])
    try:
        rep = json.load(open("/tmp/sim_report.json"))
        passed = sum(1 for c in rep["checks"] if c["passed"])
        print(f"\n==> {passed}/{len(rep['checks'])} checks passed")
        for c in rep["checks"]:
            if not c["passed"]:
                print(f"   FAIL {c['name']}: {c['detail']}")
    except Exception as exc:
        print("no report:", exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
