"""Offline image validation. This module never imports USB or opens a device."""

import hashlib
import json
import os
import re
import struct
import zlib
from pathlib import Path

SECTOR = 512
SUPPORTED_SECTORS = 7634944
CALIBRATION = ("fsc", "fsg", "modem", "modemst1", "modemst2", "persist", "sec")
FIRMWARE_SHA256 = "2dbbb357ea9e426ce610ba1eaf76869809331ac58ad4072e6493f62dd85a63be"
WRITE_ORDER = (
    "userdata", "boot", "tz", "rpm", "hyp", "aboot", "gpt_backup_entries",
    "gpt_backup_header", "gpt_primary_entries", "gpt_primary_header", "sbl1",
)


class SafetyError(RuntimeError):
    """A failed safety check; do not issue further hardware commands."""


def require(condition, message):
    # Never use assert for safety checks: python -O must not disable them.
    if not condition:
        raise SafetyError(message)


def digest(path, offset=0, size=None):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        stream.seek(offset)
        remaining = size
        while remaining is None or remaining > 0:
            block = stream.read(8 * 1024 * 1024 if remaining is None else min(remaining, 8 * 1024 * 1024))
            if not block:
                require(remaining in (None, 0), f"Truncated image: {path}")
                break
            result.update(block)
            if remaining is not None:
                remaining -= len(block)
    return result.hexdigest()


def read_json(path):
    with Path(path).open(encoding="utf-8") as stream:
        value = json.load(stream)
    require(isinstance(value, dict), f"Expected a JSON object: {path}")
    return value


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def local_file(directory, name):
    require(isinstance(name, str) and name and Path(name).name == name and name not in (".", "..")
            and "/" not in name and "\\" not in name, "Image names must be plain filenames")
    candidate = Path(directory) / name
    require(not candidate.is_symlink() and candidate.is_file(), f"Missing file or symlink: {candidate}")
    return candidate


def header(raw, lba, total_sectors):
    require(len(raw) == SECTOR and raw[:8] == b"EFI PART", "Invalid GPT signature/sector")
    revision, size, crc, reserved = struct.unpack_from("<IIII", raw, 8)
    require(revision == 0x10000 and 92 <= size <= SECTOR and reserved == 0, "Unsupported GPT header")
    check = bytearray(raw[:size])
    struct.pack_into("<I", check, 16, 0)
    require(zlib.crc32(check) == crc, "GPT header CRC mismatch")
    current, other, first, last = struct.unpack_from("<QQQQ", raw, 24)
    entries_lba, count, entry_size, entries_crc = struct.unpack_from("<QIII", raw, 72)
    require(current == lba and other == (total_sectors - 1 if lba == 1 else 1), "GPT disk geometry mismatch")
    require(34 <= first <= last < total_sectors - 1, "Invalid GPT usable range")
    require(1 <= count <= 128 and entry_size == 128, "Unsupported GPT entry format")
    require(entries_lba > 1 and entries_lba + (count * entry_size + 511) // 512 <= total_sectors,
            "GPT entries outside disk")
    return {"entries_lba": entries_lba, "count": count, "entry_size": entry_size,
            "entries_crc": entries_crc, "first": first, "last": last}


def parse_gpt(path):
    """Validate both complete GPTs, CRCs, partition bounds, duplicates and overlap."""
    path = Path(path)
    size = path.stat().st_size
    require(size >= 68 * SECTOR and size % SECTOR == 0, "Invalid full eMMC image size")
    sectors = size // SECTOR
    tables = []
    headers = []
    with path.open("rb") as stream:
        mbr = stream.read(SECTOR)
        require(mbr[510:] == b"\x55\xaa" and mbr[450] == 0xEE, "Missing protective MBR")
        for lba in (1, sectors - 1):
            stream.seek(lba * SECTOR)
            raw = stream.read(SECTOR)
            meta = header(raw, lba, sectors)
            stream.seek(meta["entries_lba"] * SECTOR)
            entries = stream.read(meta["count"] * meta["entry_size"])
            require(len(entries) == meta["count"] * meta["entry_size"]
                    and zlib.crc32(entries) == meta["entries_crc"], "GPT entry CRC mismatch")
            headers.append((raw, meta))
            tables.append(entries)
    require(tables[0] == tables[1] and headers[0][0][56:72] == headers[1][0][56:72]
            and headers[0][1]["first"] == headers[1][1]["first"]
            and headers[0][1]["last"] == headers[1][1]["last"], "Primary and backup GPT disagree")
    meta = headers[0][1]
    require(meta["entries_lba"] == 2 and 2 + (len(tables[0]) + 511) // 512 <= meta["first"],
            "Unsupported primary GPT placement")
    require(headers[1][1]["entries_lba"] > meta["last"]
            and headers[1][1]["entries_lba"] + (len(tables[1]) + 511) // 512 <= sectors - 1,
            "Backup GPT overlaps usable space/header")
    parts = {}
    guids = set()
    for offset in range(0, len(tables[0]), 128):
        entry = tables[0][offset:offset + 128]
        if entry[:16] == bytes(16):
            continue
        start, end = struct.unpack_from("<QQ", entry, 32)
        name = entry[56:128].decode("utf-16le").split("\0")[0]
        require(re.fullmatch(r"[A-Za-z0-9_-]+", name) and name not in parts, "Invalid/duplicate GPT name")
        require(entry[16:32] != bytes(16) and entry[16:32] not in guids, "Invalid/duplicate partition GUID")
        guids.add(entry[16:32])
        require(meta["first"] <= start <= end <= meta["last"], f"Partition outside disk: {name}")
        for existing in parts.values():
            require(end < existing["start_sector"] or start > existing["end_sector"], "Overlapping GPT partitions")
        parts[name] = {"start_sector": start, "end_sector": end, "byte_offset": start * SECTOR,
                       "byte_size": (end - start + 1) * SECTOR, "unique_guid": entry[16:32].hex()}
    parts["__metadata__"] = {"total_bytes": size, "total_sectors": sectors, "backup_lba": sectors - 1,
                             "primary_entries_lba": meta["entries_lba"],
                             "backup_entries_lba": headers[1][1]["entries_lba"], "entries_bytes": len(tables[0])}
    return parts


def validate_backup(directory):
    directory = Path(directory)
    info = read_json(directory / "device_info.json")
    require(info.get("backup_complete") is True and info.get("gpt_crc_verified") is True,
            "Backup was not completed and GPT-verified")
    require(info.get("model_label") == "MF800H" and isinstance(info.get("adb_serial"), str)
            and re.fullmatch(r"[A-Za-z0-9_-]+", info["adb_serial"]), "Not an identified MF800H backup")
    raw = local_file(directory, "full_emmc.bin")
    gpt = parse_gpt(raw)
    require(digest(raw) == info.get("full_emmc_sha256"), "Full backup SHA-256 mismatch")
    require(info.get("emmc_bytes") == raw.stat().st_size, "Backup size does not match metadata")
    for name in CALIBRATION:
        require(name in gpt, f"Missing factory partition: {name}")
        sidecar = local_file(directory / "calibration", name + ".bin")
        part = gpt[name]
        expected = digest(raw, part["byte_offset"], part["byte_size"])
        require(sidecar.stat().st_size == part["byte_size"] and digest(sidecar) == expected,
                f"Calibration sidecar mismatch: {name}")
        if name != "fsc":
            require(expected not in (hashlib.sha256(bytes(part["byte_size"])).hexdigest(),
                                     hashlib.sha256(b"\xff" * part["byte_size"]).hexdigest()),
                    f"Empty/sanitized factory partition: {name}")
    return info, gpt


def gpt_segments(raw, gpt, tz_start=65536, tz_sectors=2048):
    """Relocate only TZ; preserve all GUIDs, attributes and other entries."""
    segments = {}
    with Path(raw).open("rb") as stream:
        for label, lba in (("primary", 1), ("backup", gpt["__metadata__"]["backup_lba"])):
            stream.seek(lba * SECTOR)
            data = bytearray(stream.read(SECTOR))
            entries_lba, count, entry_size = struct.unpack_from("<QII", data, 72)
            stream.seek(entries_lba * SECTOR)
            entries = bytearray(stream.read(count * entry_size))
            found = 0
            for offset in range(0, len(entries), entry_size):
                if entries[offset + 56:offset + 128].decode("utf-16le").split("\0")[0] == "tz":
                    struct.pack_into("<QQ", entries, offset + 32, tz_start, tz_start + tz_sectors - 1)
                    found += 1
            require(found == 1, "Missing/duplicate TZ entry")
            struct.pack_into("<I", data, 88, zlib.crc32(entries))
            struct.pack_into("<I", data, 16, 0)
            struct.pack_into("<I", data, 16, zlib.crc32(data[:struct.unpack_from('<I', data, 12)[0]]))
            require(len(entries) % SECTOR == 0, "Unaligned GPT table")
            segments[f"gpt_{label}_entries"] = (entries_lba, bytes(entries))
            segments[f"gpt_{label}_header"] = (lba, bytes(data))
    return segments


def validate_plan(backup, images):
    info, gpt = validate_backup(backup)
    manifest = read_json(Path(images) / "manifest.json")
    require(manifest.get("target_adb_serial") == info["adb_serial"]
            and manifest.get("stock_sha256") == info["full_emmc_sha256"], "Manifest belongs to a different backup/device")
    require(manifest.get("model") == "MF800H MIFID" and manifest.get("kernel") == "6.12.1-msm8916",
            "Unsupported model/kernel manifest")
    plan = manifest.get("write_plan", [])
    require(isinstance(plan, list) and all(isinstance(item, dict) for item in plan)
            and [item.get("name") for item in plan] == list(WRITE_ORDER),
            "Unsupported write set/order (SBL1 must be last)")
    require(gpt["__metadata__"]["total_sectors"] == SUPPORTED_SECTORS and gpt["boot"]["byte_size"] == 32 * 1024**2
            and gpt["tz"]["byte_size"] == 512 * 1024, "Unsupported stock layout; stop for board research")
    tz_start, tz_size = 65536, 1024**2
    for name, part in gpt.items():
        if name != "__metadata__":
            require(tz_start * SECTOR + tz_size <= part["byte_offset"]
                    or tz_start * SECTOR >= part["byte_offset"] + part["byte_size"], "TZ relocation overlaps stock partition")
    raw = Path(backup) / "full_emmc.bin"
    require(digest(raw, tz_start * SECTOR, tz_size) == hashlib.sha256(bytes(tz_size)).hexdigest(),
            "TZ relocation space is not empty in stock backup")
    segments = gpt_segments(raw, gpt)
    ranges = []
    for item in plan:
        name = item["name"]
        start, size = item.get("start_sector"), item.get("bytes")
        require(type(start) is int and type(size) is int and start >= 0 and size > 0 and size % SECTOR == 0,
                f"Invalid region: {name}")
        require(start * SECTOR + size <= raw.stat().st_size, f"Region outside disk: {name}")
        path = local_file(images, item.get("file"))
        require(path.stat().st_size == size and digest(path) == item.get("sha256"), f"Image size/hash mismatch: {name}")
        if name in segments:
            expected_start, expected_data = segments[name]
            require(start == expected_start and path.read_bytes() == expected_data, f"Unexpected GPT changes: {name}")
        elif name == "tz":
            require(start == tz_start and size == tz_size, "Invalid relocated TZ region")
        else:
            part = gpt[name]
            require(start == part["start_sector"] and size <= part["byte_size"], f"Image exceeds its partition: {name}")
            require(name == "userdata" or size == part["byte_size"], f"Incomplete boot firmware region: {name}")
        begin, end = start * SECTOR, start * SECTOR + size
        for other_begin, other_end in ranges:
            require(end <= other_begin or begin >= other_end, "Overlapping writes")
        ranges.append((begin, end))
        for protected in CALIBRATION:
            part = gpt[protected]
            require(end <= part["byte_offset"] or begin >= part["byte_offset"] + part["byte_size"],
                    f"Write overlaps factory {protected}")
    return info, gpt, manifest
