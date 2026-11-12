#!/usr/bin/env python3
"""Local-first internet route failover for NetworkManager-managed Linux hosts."""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import time
from urllib.parse import urlparse

LOG = logging.getLogger("network-checker")
BACKUP_TYPES = {"wifi", "gsm", "cdma", "wwan"}


def command(*args: str, timeout: int = 10) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, check=False, timeout=timeout)


def network_snapshot() -> dict:
    """Collect read-only network facts; no passwords or Wi-Fi profiles."""
    nm = command("nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device", "status")
    if nm.returncode:
        raise RuntimeError(f"nmcli gagal: {nm.stderr.strip()}")
    routes_result = command("ip", "-j", "-4", "route", "show", "default")
    if routes_result.returncode:
        raise RuntimeError(f"ip route gagal: {routes_result.stderr.strip()}")
    routes = json.loads(routes_result.stdout or "[]")
    devices = []
    for line in nm.stdout.splitlines():
        fields = line.split(":", 2)
        if len(fields) != 3 or not fields[0]:
            continue
        name, kind, state = fields
        # NetworkManager escapes ':' in some names; interface names themselves cannot contain ':'.
        if name.startswith("\\") or not Path("/sys/class/net", name).exists():
            continue
        device_path = Path(os.path.realpath(f"/sys/class/net/{name}/device"))
        usb = "/usb" in str(device_path) and (Path(f"/sys/class/net/{name}/device").exists())
        device_routes = [
            {"gateway": route.get("gateway"), "metric": route.get("metric", 0)}
            for route in routes if route.get("dev") == name
        ]
        devices.append({"name": name, "type": kind, "state": state,
                        "usb_backed": usb, "default_routes": device_routes})
    result = {"devices": devices, "default_routes": routes}
    for tool, args, key in (("mmcli", ("-L",), "modem_manager"),
                            ("lsusb", (), "usb_devices")):
        if shutil.which(tool):
            try:
                r = command(tool, *args)
                result[key] = r.stdout.strip() if r.returncode == 0 else f"unavailable: {r.stderr.strip()}"
            except (OSError, subprocess.TimeoutExpired):
                result[key] = "unavailable"
    return result


def discover(snapshot: dict, config: dict) -> tuple[str, str]:
    devices = snapshot["devices"]
    eligible = {d["name"]: d for d in devices if d["type"] not in ("loopback", "bridge", "tun")}
    primary = config.get("primary_interface")
    backup = config.get("backup_interface")
    if primary is not None and primary not in eligible:
        raise ValueError(f"primary_interface={primary} tidak ditemukan sebagai perangkat fisik")
    if backup is not None and backup not in eligible:
        raise ValueError(f"backup_interface={backup} tidak ditemukan sebagai perangkat fisik")
    # No silent guessing when there are multiple possibilities.
    primary_candidates = [d["name"] for d in eligible.values() if d["type"] == "ethernet" and not d["usb_backed"]]
    backup_candidates = [d["name"] for d in eligible.values() if d["type"] in BACKUP_TYPES or
                         (d["type"] == "ethernet" and d["usb_backed"])]
    if primary is None:
        options = [name for name in primary_candidates if name != backup]
        if len(options) != 1:
            raise ValueError(f"LAN ambigu: kandidat={options}; isi primary_interface di config")
        primary = options[0]
    if backup is None:
        options = [name for name in backup_candidates if name != primary]
        if len(options) != 1:
            raise ValueError(f"Modem/backup ambigu: kandidat={options}; isi backup_interface di config")
        backup = options[0]
    if primary == backup:
        raise ValueError("Primary dan backup harus berbeda")
    return primary, backup


def probe(interface: str, urls: list[str], timeout: int) -> bool:
    for url in urls:
        try:
            result = command("curl", "--silent", "--show-error", "--noproxy", "*",
                             "--interface", interface, "--output", "/dev/null", "--max-time", str(timeout),
                             "--write-out", "%{http_code}", url, timeout=timeout + 2)
            if result.returncode == 0 and result.stdout.strip() == "204":
                return True
        except (subprocess.TimeoutExpired, OSError):
            continue
    return False


class Failover:
    def __init__(self, failures: int, recoveries: int):
        self.failures = failures
        self.recoveries = recoveries
        self.failure_count = 0
        self.recovery_count = 0
        self.selected: str | None = None

    def decide(self, primary_ok: bool, backup_ok: bool) -> str | None:
        self.failure_count = 0 if primary_ok else min(self.failure_count + 1, self.failures)
        self.recovery_count = min(self.recovery_count + 1, self.recoveries) if primary_ok else 0
        if self.selected is None:
            if primary_ok:
                return "primary"
            return "backup" if self.failure_count >= self.failures and backup_ok else None
        if self.selected == "primary":
            return "backup" if self.failure_count >= self.failures and backup_ok else "primary"
        return "primary" if self.recovery_count >= self.recoveries else "backup"


def set_route_metric(interface: str, metric: int, apply: bool) -> bool:
    if not apply:
        LOG.warning("[DRY-RUN] set metric %s=%s", interface, metric)
        return True
    # Do not disconnect interfaces or change persistent connection profiles.
    for args in (("nmcli", "device", "modify", interface, "ipv4.route-metric", str(metric)),
                 ("nmcli", "device", "reapply", interface)):
        try:
            result = command(*args)
        except (OSError, subprocess.TimeoutExpired) as exc:
            LOG.error("Perintah gagal %s: %s", interface, exc)
            return False
        if result.returncode:
            LOG.error("Perintah gagal %s: %s", " ".join(args), result.stderr.strip())
            return False
    return True


def route_is_preferred(primary: str, backup: str) -> bool:
    try:
        result = command("ip", "-j", "-4", "route", "show", "default")
        if result.returncode:
            return False
        routes = json.loads(result.stdout or "[]")
        a = [r.get("metric", 0) for r in routes if r.get("dev") == primary]
        b = [r.get("metric", 0) for r in routes if r.get("dev") == backup]
        return bool(a and (not b or min(a) < min(b)))
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False


def validate_config(config: dict) -> None:
    for key in ("interval_seconds", "timeout_seconds", "fail_threshold", "recover_threshold",
                "preferred_metric", "standby_metric"):
        if type(config.get(key)) is not int or config[key] < 1:
            raise ValueError(f"{key} harus bilangan bulat positif")
    if config["preferred_metric"] >= config["standby_metric"]:
        raise ValueError("preferred_metric harus lebih kecil dari standby_metric")
    urls = config.get("probe_urls")
    if not isinstance(urls, list) or not urls or not all(
        isinstance(url, str) and urlparse(url).scheme == "https" and urlparse(url).hostname
        for url in urls
    ):
        raise ValueError("probe_urls wajib daftar URL HTTPS")
    for key in ("primary_interface", "backup_interface"):
        if config.get(key) is not None and (not isinstance(config[key], str) or not config[key]):
            raise ValueError(f"{key} harus null atau nama interface")


def main() -> int:
    p = argparse.ArgumentParser(description="Orange Pi network scan & safe Internet failover")
    p.add_argument("--config", default="/etc/orangepi-network-checker.json")
    p.add_argument("--scan", action="store_true", help="Hanya baca daftar interface/modem dan kemungkinan jalur")
    p.add_argument("--once", action="store_true", help="Satu siklus pengecekan")
    p.add_argument("--apply", action="store_true", help="Izinkan perubahan route metric aktif (default dry-run)")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        snapshot = network_snapshot()
        if args.scan:
            try:
                config = json.loads(Path(args.config).read_text())
                snapshot["selection"] = dict(zip(("primary", "backup"), discover(snapshot, config)))
            except (OSError, ValueError, KeyError) as exc:
                snapshot["selection_warning"] = str(exc)
            print(json.dumps(snapshot, indent=2, ensure_ascii=False))
            return 0
        config = json.loads(Path(args.config).read_text())
        validate_config(config)
        primary, backup = discover(snapshot, config)
        LOG.info("Primary=%s, backup=%s, mode=%s", primary, backup, "APPLY" if args.apply else "DRY-RUN")
        machine = Failover(config["fail_threshold"], config["recover_threshold"])
        while True:
            # Keep monitoring even if the LAN cable or modem link is disconnected.
            snapshot = network_snapshot()
            routed = {d["name"] for d in snapshot["devices"]
                      if d["state"].startswith("connected") and d["default_routes"]}
            primary_ok = primary in routed and probe(primary, config["probe_urls"], config["timeout_seconds"])
            backup_ok = backup in routed and probe(backup, config["probe_urls"], config["timeout_seconds"])
            target = machine.decide(primary_ok, backup_ok)
            LOG.info("LAN=%s BACKUP=%s fail=%d recovery=%d selected=%s target=%s",
                     primary_ok, backup_ok, machine.failure_count, machine.recovery_count,
                     machine.selected, target)
            if target is not None:
                preferred, standby = (primary, backup) if target == "primary" else (backup, primary)
                # Re-assert priority when NetworkManager replaces a route after reconnection.
                needs_change = target != machine.selected or (args.apply and not route_is_preferred(preferred, standby))
                if needs_change:
                    success = preferred in routed and set_route_metric(preferred, config["preferred_metric"], args.apply)
                    if success and standby in routed:
                        success = set_route_metric(standby, config["standby_metric"], args.apply)
                    if success and (not args.apply or route_is_preferred(preferred, standby)):
                        machine.selected = target
                        LOG.warning("Preferensi internet -> %s%s", target, " [SIMULASI]" if not args.apply else "")
                    else:
                        LOG.error("Failover gagal/verifikasi route gagal; akan dicoba ulang")
            if args.once:
                break
            time.sleep(config["interval_seconds"])
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired) as exc:
        LOG.error("Checker berhenti: %s", exc)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
