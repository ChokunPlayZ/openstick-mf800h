"""Command line workflows; offline commands do not touch USB."""

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .images import (CALIBRATION, SECTOR, SafetyError, digest, parse_gpt, read_json,
                     require, validate_backup, validate_plan, write_json)

ROOT = Path(__file__).resolve().parents[1]


def now():
    return datetime.now(timezone.utc).isoformat()


def token(operation, info):
    return f"{operation.upper()} {info['adb_serial']} {info['full_emmc_sha256'][:12]}"


def confirm(args, operation, info):
    expected = token(operation, info)
    if args.confirm is None and sys.stdin.isatty():
        print("This can erase data or render the device unbootable. Keep the original backup offline.")
        answer = input(f"Type exactly: {expected}\n> ")
    else:
        answer = args.confirm
    require(answer == expected, f"Confirmation required: --confirm {expected!r}")


def fresh_directory(path):
    path = Path(path).resolve()
    require(not path.exists(), f"Refusing to overwrite existing directory: {path}")
    require(path.parent.is_dir(), f"Create parent directory first: {path.parent}")
    path.mkdir(mode=0o700)
    return path


def space(path, size):
    require(shutil.disk_usage(path).free > size + 512 * 1024**2, "Insufficient free disk space for verified read-back")


def make_edl(args, directory):
    from .device import EDL
    return EDL(directory, args.edl_script, args.loader, args.timeout)


def doctor(args):
    from .device import INSTALLER, edl_count
    from .prepare import tool
    failures = []
    for module in ("usb.core", "serial", "docopt", "Crypto", "Cryptodome", "lxml", "capstone", "colorama"):
        try:
            available = importlib.util.find_spec(module) is not None
        except ModuleNotFoundError:
            available = False
        print(f"{'OK' if available else 'MISSING'} Python {module}")
        if not available:
            failures.append(module)
    for name in ("dtc", "debugfs", "mke2fs", "e2fsck"):
        try:
            print(f"OK {name}: {tool(name)}")
        except RuntimeError as exc:
            print(f"MISSING {exc}")
            failures.append(name)
    for path in (Path(args.edl_script or INSTALLER / "edl/edl.py"),
                 Path(args.loader or INSTALLER / "loaders/prog_emmc_firehose_8916.mbn")):
        print(f"{'OK' if path.is_file() else 'MISSING'} {path}")
        if not path.is_file():
            failures.append(str(path))
    try:
        print(f"Qualcomm 05c6:9008 devices: {edl_count()} (enumeration only; no programmer uploaded)")
    except Exception as exc:
        print(f"USB unavailable: {exc}")
        failures.append("USB backend")
    print("Preparation currently requires macOS/Linux; Windows can validate/flash prepared images with working libusb.")
    return 1 if failures else 0


def backup(args):
    require(args.execute, "Backup requires --execute: it uploads a RAM programmer and reads eMMC")
    require(args.confirm_board == "MF800H-MSM8916-512M", "Confirm the physical MF800H/MSM8916/512 MB board")
    require(re.fullmatch(r"[A-Za-z0-9_-]+", args.adb_serial), "Invalid ADB serial")
    from .device import device_lock
    with device_lock():
        output = fresh_directory(args.output)
        space(output, 3909091328 + 128 * 1024**2)
        edl = make_edl(args, output)
        info = {"model_label": "MF800H", "adb_serial": args.adb_serial, "created_at": now(),
                "backup_complete": False, "gpt_crc_verified": False}
        write_json(output / "device_info.json", info)
        try:
            # A raw full-device read, never a generic donor image or calibration injection.
            raw = output / "full_emmc.bin"
            edl.run("read-full-emmc", "rf", raw)
            require(raw.is_file(), "EDL did not produce a full dump")
            gpt = parse_gpt(raw)
            require(gpt["__metadata__"]["total_sectors"] == 7634944 and "system" in gpt
                    and "userdata" in gpt and "boot" in gpt and "tz" in gpt
                    and gpt["boot"]["byte_size"] == 32 * 1024**2 and gpt["tz"]["byte_size"] == 512 * 1024,
                    "Not the supported stock MF800H layout (do not back up an already converted device as stock)")
            calibration = output / "calibration"
            calibration.mkdir(mode=0o700)
            with raw.open("rb") as stream:
                for name in CALIBRATION:
                    require(name in gpt, f"Missing factory partition: {name}")
                    part = gpt[name]
                    stream.seek(part["byte_offset"])
                    data = stream.read(part["byte_size"])
                    require(len(data) == part["byte_size"], f"Truncated factory partition: {name}")
                    (calibration / (name + ".bin")).write_bytes(data)
            # Independently reread all seven factory partitions and boot.
            for name in (*CALIBRATION, "boot"):
                part = gpt[name]
                edl.verify("independent-" + name, part["start_sector"], part["byte_size"],
                           digest(raw, part["byte_offset"], part["byte_size"]))
            info.update({"backup_complete": True, "gpt_crc_verified": True, "emmc_bytes": raw.stat().st_size,
                         "full_emmc_sha256": digest(raw), "backup_completed_at": now(),
                         "independent_boot_read_verified": True})
            write_json(output / "gpt.json", gpt)
            write_json(output / "device_info.json", info)
            validate_backup(output)
        except BaseException as exc:
            info.update({"backup_complete": False, "error": repr(exc)})
            write_json(output / "device_info.json", info)
            raise
    print(f"Verified private stock backup: {output}. Copy it to a second drive before flashing.")
    return 0


def show_plan(info, manifest):
    print(f"MF800H target: {info['adb_serial']} | stock SHA-256: {info['full_emmc_sha256']}")
    for item in manifest["write_plan"]:
        print(f"  {item['name']:22} LBA {item['start_sector']:8}  {item['bytes']:12,} bytes  {item['file']}")
    print("All seven factory radio/calibration partitions are excluded. SBL1 is last.")


def flash(args):
    info, gpt, manifest = validate_plan(args.backup, args.images)
    show_plan(info, manifest)
    print("Confirmation:", token("flash", info))
    if not args.execute:
        print("OFFLINE PLAN ONLY. Nothing contacted or written. Add --execute and --run-dir for the real flash.")
        return 0
    confirm(args, "flash", info)
    require(args.run_dir, "--run-dir must name a NEW directory for logs/state")
    # A started manifest cannot be retried using a new run directory.
    # Historical flash-state.json is also honored and never modified.
    images = Path(args.images).resolve()
    require(not (images / "flash-state.json").exists() and not (images / ".flash-started.json").exists(),
            "These images already have a flash record. Do not blindly repeat; use recovery docs.")
    from .device import device_lock
    with device_lock():
        output = fresh_directory(args.run_dir)
        space(output, max(item["bytes"] for item in manifest["write_plan"]))
        edl = make_edl(args, output)
        state = {"operation": "flash", "started_at": now(), "backup": str(Path(args.backup).resolve()),
                 "images": str(images), "stock_sha256": info["full_emmc_sha256"],
                 "manifest_sha256": digest(images / "manifest.json"), "verified": [], "completed": False,
                 "reboot_issued": False, "writes_started": False}
        write_json(output / "state.json", state)
        try:
            raw = Path(args.backup) / "full_emmc.bin"
            edl.identity(args.backup, gpt)
            # Compare BOTH GPTs including unused primary sectors and the exact relocated TZ gap.
            edl.verify("preflight-primary-gpt", 0, 34 * SECTOR, digest(raw, 0, 34 * SECTOR))
            sectors = gpt["__metadata__"]["total_sectors"]
            edl.verify("preflight-backup-gpt", sectors - 33, 33 * SECTOR,
                       digest(raw, (sectors - 33) * SECTOR, 33 * SECTOR))
            edl.verify("preflight-tz-gap", 65536, 1024**2, digest(raw, 65536 * SECTOR, 1024**2))
            for name in CALIBRATION:
                part = gpt[name]
                edl.verify("preflight-" + name, part["start_sector"], part["byte_size"],
                           digest(Path(args.backup) / "calibration" / (name + ".bin")))
            # Marker is exclusive and stays even on error. Inputs and old records remain intact.
            with (images / ".flash-started.json").open("x") as marker:
                json.dump({"run_dir": str(output), "started_at": now()}, marker)
                marker.flush()
                os.fsync(marker.fileno())
            state["writes_started"] = True
            write_json(output / "state.json", state)
            for item in manifest["write_plan"]:
                edl.identity(args.backup, gpt)
                path = images / item["file"]
                require(digest(path) == item["sha256"], f"Image changed during flash: {path}")
                state["current_region"] = item["name"]
                write_json(output / "state.json", state)
                edl.run("write-" + item["name"], "ws", item["start_sector"], path)
                edl.verify("verify-" + item["name"], item["start_sector"], item["bytes"], item["sha256"])
                state["verified"].append(item["name"])
                write_json(output / "state.json", state)
            for name in CALIBRATION:
                part = gpt[name]
                edl.verify("preserved-" + name, part["start_sector"], part["byte_size"],
                           digest(Path(args.backup) / "calibration" / (name + ".bin")))
            state.update({"completed": True, "completed_at": now(), "factory_partitions_unchanged": list(CALIBRATION)})
            write_json(output / "state.json", state)
        except BaseException as exc:
            state["error"] = repr(exc)
            write_json(output / "state.json", state)
            raise
    print(f"ALL WRITES VERIFIED. Device remains in EDL. Use reboot --state {output / 'state.json'} after review.")
    return 0


def restore(args):
    info, gpt = validate_backup(args.backup)
    print(f"Full stock restore: {info['emmc_bytes']:,} bytes; erases Linux and all subsequent data.")
    print("Confirmation:", token("restore", info))
    if not args.execute:
        print("OFFLINE VALIDATION ONLY. No USB access. Add --execute and --run-dir for a real restore.")
        return 0
    confirm(args, "restore", info)
    require(args.run_dir, "--run-dir must name a NEW directory")
    from .device import device_lock
    with device_lock():
        output = fresh_directory(args.run_dir)
        space(output, info["emmc_bytes"])
        edl = make_edl(args, output)
        state = {"operation": "restore", "backup": str(Path(args.backup).resolve()), "started_at": now(),
                 "stock_sha256": info["full_emmc_sha256"], "completed": False, "reboot_issued": False}
        write_json(output / "state.json", state)
        try:
            edl.identity(args.backup, gpt)
            # The factory FSG/SEC/PERSIST fingerprint is required even with a damaged GPT.
            raw = Path(args.backup).resolve() / "full_emmc.bin"
            edl.run("restore-full-emmc", "wf", raw)
            edl.verify("verify-full-stock", 0, info["emmc_bytes"], info["full_emmc_sha256"])
            state.update({"completed": True, "completed_at": now()})
            write_json(output / "state.json", state)
        except BaseException as exc:
            state["error"] = repr(exc)
            write_json(output / "state.json", state)
            raise
    print(f"FULL STOCK RESTORE VERIFIED. Still in EDL. Review {output / 'state.json'}, then use reboot.")
    return 0


def reboot(args):
    state_path = Path(args.state).resolve()
    state = read_json(state_path)
    require(state.get("completed") is True and state.get("operation") in ("flash", "restore")
            and not state.get("error") and not state.get("reboot_issued"), "Refusing reboot: incomplete/failed/already rebooted state")
    info, gpt = validate_backup(state["backup"])
    require(state.get("stock_sha256") == info["full_emmc_sha256"], "State/backup mismatch")
    require(args.execute, "Reboot is separate and requires --execute")
    confirm(args, "reboot", info)
    from .device import device_lock
    with device_lock():
        output = fresh_directory(args.run_dir)
        edl = make_edl(args, output)
        edl.identity(state["backup"], gpt)
        # Recheck what is on this device, not just a host-side completed flag.
        if state["operation"] == "flash":
            _, _, manifest = validate_plan(state["backup"], state["images"])
            require(digest(Path(state["images"]) / "manifest.json") == state["manifest_sha256"], "Manifest changed since flash")
            space(output, max(item["bytes"] for item in manifest["write_plan"]))
            for item in manifest["write_plan"]:
                edl.verify("before-reboot-" + item["name"], item["start_sector"], item["bytes"], item["sha256"])
        else:
            space(output, info["emmc_bytes"])
            edl.verify("before-reboot-stock", 0, info["emmc_bytes"], info["full_emmc_sha256"])
        # Persist the attempted reset before sending it: timeout is not proof of failure.
        state.update({"reboot_issued": True, "reboot_requested_at": now(), "reboot_logs": str(output)})
        write_json(state_path, state)
        edl.run("explicit-reset", "reset")
    print("Reset sent. Allow several minutes for first boot; verify USB/SSH, not just the LCD.")
    return 0


def parser():
    result = argparse.ArgumentParser(description="Backup-first MF800H experimental flashing assistant. No automatic reboot.")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--edl-script", type=Path, help="bkerler edl.py (defaults to tools/edl/edl.py)")
    common.add_argument("--loader", type=Path, help="known-compatible MSM8916 eMMC firehose programmer")
    common.add_argument("--timeout", type=int, default=3600, help="per-operation EDL deadline, seconds")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", parents=[common], help="check host tools and enumerate USB only").set_defaults(action=doctor)
    command = commands.add_parser("backup", parents=[common], help="read and verify a NEW private stock backup")
    command.add_argument("--output", type=Path, required=True)
    command.add_argument("--adb-serial", required=True, help="serial recorded on stock Android; not an EDL serial")
    command.add_argument("--confirm-board", required=True, choices=["MF800H-MSM8916-512M"])
    command.add_argument("--execute", action="store_true")
    command.set_defaults(action=backup)
    command = commands.add_parser("prepare", help="prepare headless per-device images OFFLINE (macOS/Linux)")
    command.add_argument("--backup", type=Path, required=True)
    command.add_argument("--firmware", type=Path, required=True, help="pinned LongQT Debian v1.2 zip")
    command.add_argument("--output", type=Path, required=True)
    command.add_argument("--profile-dir", type=Path, default=ROOT / "profiles")
    command.add_argument("--confirm-board", required=True, choices=["MF800H-MSM8916-512M"])
    command.set_defaults(action=prepare_command)
    for name, action in (("plan", flash), ("flash", flash), ("restore", restore)):
        command = commands.add_parser(name, parents=[common], help={"plan": "validate images OFFLINE",
                      "flash": "offline plan by default; --execute enables verified writes",
                      "restore": "offline validation by default; --execute enables full stock restore"}[name])
        command.add_argument("--backup", type=Path, required=True)
        if name != "restore":
            command.add_argument("--images", type=Path, required=True)
        if name != "plan":
            command.add_argument("--execute", action="store_true")
            command.add_argument("--run-dir", type=Path)
            command.add_argument("--confirm", help="exact printed confirmation phrase (otherwise interactive prompt)")
        else:
            command.set_defaults(execute=False)
        command.set_defaults(action=action)
    command = commands.add_parser("reboot", parents=[common], help="reverify a completed run and explicitly reboot")
    command.add_argument("--state", type=Path, required=True)
    command.add_argument("--run-dir", type=Path, required=True)
    command.add_argument("--execute", action="store_true")
    command.add_argument("--confirm")
    command.set_defaults(action=reboot)
    return result


def prepare_command(args):
    from .prepare import prepare
    prepare(args.backup, args.firmware, args.output, args.profile_dir)
    return 0


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if hasattr(args, "timeout"):
            require(args.timeout > 0, "Timeout must be positive")
        return args.action(args)
    except KeyboardInterrupt:
        print("STOPPED. No reboot issued automatically. Keep power/USB stable and review the run log.", file=sys.stderr)
        return 130
    except (SafetyError, OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"STOP: {exc}\nNo automatic reboot or retry. See docs/FLASHING.md for recovery.", file=sys.stderr)
        return 1
