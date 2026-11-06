#!/usr/bin/env python3
"""Recover lost MF800H modem discovery, without changing any carrier settings."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import sys


BOOT_GRACE = 45
DISCOVERY_GRACE = 30
RESTART_COOLDOWN = 300
MODEMS = ["mmcli", "--list-modems", "--output-json"]
RADIO = ["nmcli", "--terse", "--fields", "WWAN", "radio"]
ACTIVE = ["systemctl", "is-active", "ModemManager.service"]
RESTART = ["systemctl", "restart", "ModemManager.service"]


@dataclass(frozen=True)
class Paths:
    model: Path = Path("/proc/device-tree/model")
    qmi: Path = Path("/dev/wwan0qmi0")
    uptime: Path = Path("/proc/uptime")
    state: Path = Path("/run/openstick-cellular-recovery/state.json")


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=30, check=False)


def checked(runner, args: list[str]) -> str:
    result = runner(args)
    if result.returncode:
        raise RuntimeError(f"{' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"missing_since": None, "last_restart": None}
    state = json.loads(path.read_text())
    if not isinstance(state, dict):
        raise ValueError("Invalid recovery state")
    for key in ("missing_since", "last_restart"):
        value = state.get(key)
        if value is not None and (type(value) not in (int, float) or value < 0):
            raise ValueError("Invalid recovery timestamp")
    return state


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(".new")
    temporary.write_text(json.dumps(state) + "\n")
    temporary.replace(path)


def check(paths: Paths = Paths(), runner=run) -> str:
    if "MF800H" not in paths.model.read_text() or not paths.qmi.exists():
        return "skipped"
    now = float(paths.uptime.read_text().split()[0])
    if now < BOOT_GRACE or runner(ACTIVE).returncode:
        return "skipped"
    # Respect an intentional WWAN disable or a manually stopped ModemManager.
    if checked(runner, RADIO) != "enabled":
        return "skipped"
    payload = json.loads(checked(runner, MODEMS))
    modems = payload.get("modem-list") if isinstance(payload, dict) else None
    if not isinstance(modems, list) or any(not isinstance(m, str) for m in modems):
        raise ValueError("Invalid ModemManager modem list; refusing recovery")
    state = load_state(paths.state)
    if modems:
        if state.get("missing_since") is not None:
            state["missing_since"] = None
            save_state(paths.state, state)
        return "healthy"
    if state.get("missing_since") is None:
        state["missing_since"] = now
        save_state(paths.state, state)
        return "waiting"
    if now - state["missing_since"] < DISCOVERY_GRACE:
        return "waiting"
    last_restart = state.get("last_restart")
    if last_restart is not None and now - last_restart < RESTART_COOLDOWN:
        return "cooldown"
    # Record *before* requesting the restart, so failed requests are rate-limited too.
    state.update(last_restart=now, missing_since=None)
    save_state(paths.state, state)
    checked(runner, RESTART)
    return "restarted"


def main() -> int:
    try:
        result = check()
        if result == "restarted":
            print("QMI port present but no modem exported: restarted ModemManager. "
                  "NetworkManager will retry eligible autoconnect profiles.")
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Cellular recovery check failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
