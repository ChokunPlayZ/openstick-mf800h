"""Reproducible, per-device headless image preparation without USB access."""

import shutil
import struct
import subprocess
import uuid
import zipfile
import zlib
from pathlib import Path

from .images import (FIRMWARE_SHA256, SECTOR, WRITE_ORDER, digest, gpt_segments,
                     read_json, require, validate_backup, validate_plan, write_json)


def expand_sparse(source, destination):
    """Expand Android sparse v1 RAW/FILL/HOLE/CRC chunks, with strict bounds."""
    def take(stream, size):
        data = stream.read(size)
        require(len(data) == size, "Truncated Android sparse image")
        return data

    with Path(source).open("rb") as stream, Path(destination).open("xb") as out:
        values = struct.unpack("<IHHHHIIII", take(stream, 28))
        magic, major, minor, file_size, chunk_size, block_size, blocks, chunks, checksum = values
        require(magic == 0xED26FF3A and major == 1 and minor == 0, "Unsupported Android sparse header")
        require(28 <= file_size <= 4096 and 12 <= chunk_size <= 4096 and block_size == 4096,
                "Unsupported sparse geometry")
        require(0 < blocks * block_size <= 2 * 1024**3 and chunks <= 1_000_000, "Sparse expansion exceeds limit")
        take(stream, file_size - 28)
        written, crc = 0, 0
        for _ in range(chunks):
            kind, reserved, count, total = struct.unpack("<HHII", take(stream, 12))
            take(stream, chunk_size - 12)
            length = count * block_size
            require(written + length <= blocks * block_size, "Sparse chunk exceeds image")
            payload = total - chunk_size
            if kind == 0xCAC1:
                require(payload == length, "Invalid RAW sparse chunk")
                remaining = length
                while remaining:
                    data = take(stream, min(remaining, 1024**2))
                    out.write(data)
                    crc = zlib.crc32(data, crc)
                    remaining -= len(data)
            elif kind in (0xCAC2, 0xCAC3):
                require(payload == (4 if kind == 0xCAC2 else 0), "Invalid fill/hole sparse chunk")
                pattern = take(stream, 4) if kind == 0xCAC2 else bytes(4)
                remaining = length
                block = pattern * (1024**2 // 4)
                while remaining:
                    data = block[:min(remaining, len(block))]
                    out.write(data)
                    crc = zlib.crc32(data, crc)
                    remaining -= len(data)
            elif kind == 0xCAC4:
                require(count == 0 and payload == 4, "Invalid CRC sparse chunk")
                require(struct.unpack("<I", take(stream, 4))[0] == crc, "Sparse CRC mismatch")
            else:
                require(False, f"Unknown sparse chunk: {kind:#x}")
            written += length
        require(written == blocks * block_size and not stream.read(1), "Sparse length/trailing-data mismatch")
        require(checksum == 0 or checksum == crc, "Sparse image checksum mismatch")


def tool(name):
    candidates = [shutil.which(name)]
    for directory in ("/opt/homebrew/opt/e2fsprogs/sbin", "/usr/local/opt/e2fsprogs/sbin", "/usr/sbin", "/sbin"):
        candidates.append(str(Path(directory) / name))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise RuntimeError(f"Missing {name}; install dtc and e2fsprogs (see docs/FLASHING.md)")


def run(*args):
    result = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=600)
    require(result.returncode == 0, f"Command failed: {args}\n{result.stdout}\n{result.stderr}")
    return result.stdout


def quote(value):
    value = str(value)
    require(not any(c in value for c in '\n\r\x00'), "Unsupported control character in path")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def fs_write(debugfs, image, output, destination, content, mode):
    local = output / (destination.strip("/").replace("/", "_") + ".txt")
    local.write_text(content)
    commands = (f"rm {destination}\nwrite {quote(local)} {destination}\n"
                f"set_inode_field {destination} mode {mode}\n")
    result = subprocess.run([debugfs, "-w", str(image)], input=commands, capture_output=True, text=True, timeout=60)
    require(result.returncode == 0 and "Allocated inode" in result.stdout,
            f"Filesystem write failed: {destination}\n{result.stdout}\n{result.stderr}")
    require(run(debugfs, "-R", f"cat {destination}", image) == content, f"Filesystem verification failed: {destination}")


def prepare(backup, archive, output, profile_dir):
    info, gpt = validate_backup(backup)
    backup, archive, output, profile_dir = map(lambda p: Path(p).resolve(), (backup, archive, output, profile_dir))
    require(digest(archive) == FIRMWARE_SHA256, "Not the pinned LongQT Debian v1.2 archive (SHA-256 mismatch)")
    require(not output.exists(), "Output already exists; use a fresh directory, never overwrite historical images")
    require(gpt["__metadata__"]["total_sectors"] == 7634944 and gpt["boot"]["byte_size"] == 32 * 1024**2
            and gpt["tz"]["byte_size"] == 512 * 1024 and "system" in gpt, "Unsupported factory layout")
    for name in ("userdata", "rpm", "hyp", "aboot", "sbl1"):
        require(name in gpt, f"Missing partition: {name}")
    require(shutil.disk_usage(output.parent).free >= 6 * 1024**3, "Preparation needs at least 6 GiB free")
    debugfs, mke2fs, e2fsck, dtc = [tool(n) for n in ("debugfs", "mke2fs", "e2fsck", "dtc")]
    output.mkdir(mode=0o700)
    source = output / "source"
    source.mkdir()
    expected = {"boot.bin", "rootfs.bin", "sbl1.mbn", "rpm.mbn", "hyp.mbn", "aboot.mbn", "tz.mbn"}
    with zipfile.ZipFile(archive) as zipped:
        for name in expected:
            matches = [item for item in zipped.infolist() if item.filename == name]
            require(len(matches) == 1 and matches[0].file_size < 2 * 1024**3, f"Invalid archive member: {name}")
            with zipped.open(matches[0]) as src, (source / name).open("xb") as dst:
                shutil.copyfileobj(src, dst)
    boot, rootfs = output / "boot.ext2", output / "rootfs.ext4"
    print("Expanding pinned Linux filesystems (offline)...", flush=True)
    expand_sparse(source / "rootfs.bin", rootfs)
    expand_sparse(source / "boot.bin", source / "boot.raw")
    require(rootfs.stat().st_size <= gpt["userdata"]["byte_size"], "Rootfs exceeds factory userdata")
    for name in ("upstream-mf800.dts", "mf800h.dts"):
        shutil.copyfile(profile_dir / name, output / name)
    profile = (output / "mf800h.dts").read_text()
    # The original stock 25 C curve is retained only as unvalidated metadata.
    props = read_json(profile_dir / "stock_3_properties.json")
    prefix = "/qcom,battery-data/qcom,qrd-4v2-2000mah-data/qcom,pc-temp-ocv-lut/"
    temps = struct.unpack(">5i", bytes.fromhex(props[prefix + "qcom,lut-col-legend"]))
    raw_percent = bytes.fromhex(props[prefix + "qcom,lut-row-legend"])
    percent = struct.unpack(">" + "I" * (len(raw_percent) // 4), raw_percent)
    raw_curve = bytes.fromhex(props[prefix + "qcom,lut-data"])
    curve = struct.unpack(">" + "I" * (len(raw_curve) // 4), raw_curve)
    ocv = ", ".join(f"<{curve[i * 5 + temps.index(25)] * 1000} {soc}>" for i, soc in enumerate(percent))
    marker = "/delete-property/ ocv-capacity-table-0;"
    require(profile.count(marker) == 1, "Unexpected headless profile source")
    (output / "mf800h.dts").write_text(profile.replace(marker, "ocv-capacity-table-0 = " + ocv + ";"))
    run(dtc, "-q", "-I", "dts", "-O", "dtb", "-o", output / "msm8916-mf800h.dtb", output / "mf800h.dts")
    run(debugfs, "-R", f"dump /vmlinuz {quote(output / 'vmlinuz')}", source / "boot.raw")
    require((output / "vmlinuz").stat().st_size > 8_000_000, "Missing/invalid kernel")
    with boot.open("xb") as stream:
        stream.truncate(gpt["boot"]["byte_size"])
    run(mke2fs, "-t", "ext2", "-b", "4096", "-I", "128", "-O", "none", "-m", "0", "-L", "boot", boot)
    for directory in ("/extlinux", "/dtbs", "/dtbs/qcom"):
        run(debugfs, "-w", "-R", f"mkdir {directory}", boot)
    for local, remote in (("vmlinuz", "/vmlinuz"), ("msm8916-mf800h.dtb", "/dtbs/qcom/msm8916-mf800h.dtb")):
        run(debugfs, "-w", "-R", f"write {quote(output / local)} {remote}", boot)
        extracted = output / ("check-" + local)
        run(debugfs, "-R", f"dump {remote} {quote(extracted)}", boot)
        require(digest(extracted) == digest(output / local), f"Boot file copy failed: {local}")
        extracted.unlink()
    def guid(name):
        return str(uuid.UUID(bytes_le=bytes.fromhex(gpt[name]["unique_guid"])))

    extlinux = ("linux /vmlinuz\nfdt /dtbs/qcom/msm8916-mf800h.dtb\n"
                f"append earlycon root=PARTUUID={guid('userdata')} console=ttyMSM0,115200 no_framebuffer=true rw rootwait\n")
    fs_write(debugfs, boot, output, "/extlinux/extlinux.conf", extlinux, "0100644")
    fs_write(debugfs, rootfs, output, "/etc/fstab", f"PARTUUID={guid('boot')}\t/boot\text2\tdefaults\t0 2\n", "0100644")
    fs_write(debugfs, rootfs, output, "/etc/hostname", "openstick-mf800h\n", "0100644")
    changes = {
        "/etc/hosts": (("openstick", "openstick-mf800h"),),
        "/etc/msm8916-usb-gadget.conf": (("ENABLE_RNDIS=1", "ENABLE_RNDIS=0"), ("ENABLE_NCM=0", "ENABLE_NCM=1"),
                                         ("ENABLE_UMS=1", "ENABLE_UMS=0"), ('USB_PRODUCT="USB Gadget"', 'USB_PRODUCT="OpenStick MF800H"')),
        "/etc/systemd/system/resize-rootfs.service": (("by-partlabel/rootfs", "by-partlabel/userdata"),),
        "/etc/NetworkManager/system-connections/hotspot.nmconnection": (("ssid=4G-UFI-XX", "ssid=OpenStick-MF800H"),
                                                                       ("psk=1234567890", "psk=openstick")),
        "/etc/NetworkManager/system-connections/lte.nmconnection": (("id=lte\n", "id=lte\nautoconnect=false\n"),),
    }
    for destination, replacements in changes.items():
        content = run(debugfs, "-R", f"cat {destination}", rootfs)
        for old, new in replacements:
            require(old in content, f"Unexpected upstream configuration: {destination}: {old}")
            content = content.replace(old, new)
        fs_write(debugfs, rootfs, output, destination, content, "0100600" if destination.endswith(".nmconnection") else "0100644")
    for image in (boot, rootfs):
        run(e2fsck, "-fn", image)
    plan = {}
    for name, path in (("userdata", rootfs), ("boot", boot)):
        plan[name] = {"name": name, "file": path.name, "start_sector": gpt[name]["start_sector"], "bytes": path.stat().st_size}
    for name in ("tz", "rpm", "hyp", "aboot", "sbl1"):
        data = (source / (name + ".mbn")).read_bytes()
        size = 1024**2 if name == "tz" else gpt[name]["byte_size"]
        require(len(data) <= size, f"Firmware exceeds slot: {name}")
        path = output / (name + ".bin")
        path.write_bytes(data + bytes(size - len(data)))
        plan[name] = {"name": name, "file": path.name, "start_sector": 65536 if name == "tz" else gpt[name]["start_sector"], "bytes": size}
    for name, (start, data) in gpt_segments(backup / "full_emmc.bin", gpt).items():
        path = output / (name + ".bin")
        path.write_bytes(data)
        plan[name] = {"name": name, "file": path.name, "start_sector": start, "bytes": len(data)}
    items = [plan[name] for name in WRITE_ORDER]
    for item in items:
        item["sha256"] = digest(output / item["file"])
    write_json(output / "manifest.json", {"model": "MF800H MIFID", "kernel": "6.12.1-msm8916",
               "target_adb_serial": info["adb_serial"], "stock_sha256": info["full_emmc_sha256"],
               "firmware_archive_sha256": FIRMWARE_SHA256, "root_guid": guid("userdata"), "boot_guid": guid("boot"),
               "charging": "Linux charger disabled; charging unverified", "lcd": "headless; install LCD support separately",
               "write_plan": items})
    validate_plan(backup, output)
    print(f"Prepared and validated: {output}/manifest.json. No USB access or writes to the device.")
