"""Explicit, logged EDL operations. All writes are gated by the CLI workflow."""

import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

from .images import SECTOR, digest, require

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "tools"


def edl_count():
    try:
        import usb.core
        return len(list(usb.core.find(find_all=True, idVendor=0x05C6, idProduct=0x9008)))
    except ImportError as exc:
        raise RuntimeError("PyUSB is missing. Use the EDL virtualenv; see docs/FLASHING.md") from exc


@contextmanager
def device_lock(path=ROOT / ".edl.lock"):
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RuntimeError(f"EDL lock exists: {path}. Check for another process; see recovery docs.") from exc
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(str(os.getpid()) + "\n")
        yield
    finally:
        Path(path).unlink()


class EDL:
    def __init__(self, directory, script=None, loader=None, timeout=3600):
        self.directory = Path(directory).resolve()
        self.script = Path(script or INSTALLER / "edl/edl.py").resolve()
        self.loader = Path(loader or INSTALLER / "loaders/prog_emmc_firehose_8916.mbn").resolve()
        self.timeout = timeout
        self.sequence = 0
        require(self.script.is_file() and self.loader.is_file(), "Missing bkerler EDL script or MSM8916 loader")

    def run(self, label, *args):
        require(edl_count() == 1, "Exactly one Qualcomm 05c6:9008 device must be connected")
        self.sequence += 1
        log = self.directory / f"{self.sequence:03d}-{label}.log"
        command = [sys.executable, str(self.script), *map(str, args), f"--loader={self.loader}"]
        if args[0] != "reset":
            command.append("--memory=eMMC")
        print(f"  {label} (log: {log.name})", flush=True)
        with log.open("x") as stream:
            result = subprocess.run(command, cwd=self.directory, stdout=stream, stderr=subprocess.STDOUT,
                                    timeout=20 if args[0] == "reset" else self.timeout)
        text = log.read_text(errors="replace")
        fatal = ("Error writing ", "Error on writing ", "Error on sahara handshake", "No suitable loader found",
                 "Error: Couldn't detect partition", "Traceback (most recent call last)", "Connection detected, quiting")
        require(result.returncode == 0 and not any(marker in text for marker in fatal),
                f"EDL failed: {log}. Stop; no automatic retry or reboot.")
        return text

    def read(self, label, start, size):
        require(type(start) is int and start >= 0 and size > 0 and size % SECTOR == 0, "Invalid read range")
        path = self.directory / f"{self.sequence + 1:03d}-{label}.bin"
        require(not path.exists(), f"Refusing to overwrite read-back: {path}")
        self.run(label, "rs", start, size // SECTOR, path)
        require(path.is_file() and path.stat().st_size == size, f"Short/absent read-back: {path}")
        return path

    def verify(self, label, start, size, expected):
        path = self.read(label, start, size)
        require(digest(path) == expected, f"DEVICE READ-BACK MISMATCH: {label}; do not reboot")
        path.unlink()  # Only this invocation's successful scratch read, never a source/backup.

    def identity(self, backup, gpt):
        # Factory FSG + SEC + PERSIST, unlike modemst*, normally stay stable after Linux boots.
        # Fail closed if either changed. No identity-bypass switch is provided.
        for name in ("fsg", "sec", "persist"):
            part = gpt[name]
            self.verify("identity-" + name, part["start_sector"], part["byte_size"],
                        digest(Path(backup) / "calibration" / (name + ".bin")))
