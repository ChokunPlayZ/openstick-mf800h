"""Synthetic fixtures only: these tests never enumerate USB or run EDL."""

import contextlib
import copy
import io
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from flasher import cli, device, images, prepare


def fixture(root):
    """Small, internally valid dual-GPT disk with the same boot/TZ gap policy."""
    backup = root / "stock"
    backup.mkdir()
    calibration = backup / "calibration"
    calibration.mkdir()
    sectors = 196608
    parts = [
        ("fsc", 40, 2), ("fsg", 64, 32), ("sec", 128, 32),
        ("modemst1", 512, 32), ("modemst2", 544, 32), ("persist", 1024, 64),
        ("modem", 4096, 128), ("sbl1", 8192, 1024), ("aboot", 10240, 2048),
        ("rpm", 12288, 1024), ("tz", 13312, 1024), ("hyp", 14336, 1024),
        ("boot", 67584, 65536), ("userdata", 133120, 32768), ("system", 165888, 1024),
    ]
    entries = bytearray(4096)
    for index, (name, start, count) in enumerate(parts):
        off = index * 128
        entries[off:off + 16] = b"t" * 16
        entries[off + 16:off + 32] = (index + 1).to_bytes(16, "little")
        struct.pack_into("<QQ", entries, off + 32, start, start + count - 1)
        encoded = name.encode("utf-16le")
        entries[off + 56:off + 56 + len(encoded)] = encoded
    def make_header(current, other, entry_lba):
        raw = bytearray(512)
        raw[:8] = b"EFI PART"
        struct.pack_into("<IIIIQQQQ", raw, 8, 0x10000, 92, 0, 0, current, other, 34, sectors - 34)
        raw[56:72] = b"g" * 16
        struct.pack_into("<QIII", raw, 72, entry_lba, 32, 128, zlib.crc32(entries))
        struct.pack_into("<I", raw, 16, zlib.crc32(raw[:92]))
        return raw
    raw = backup / "full_emmc.bin"
    with raw.open("wb") as stream:
        stream.truncate(sectors * 512)
        mbr = bytearray(512)
        mbr[450] = 0xEE
        mbr[510:] = b"\x55\xaa"
        stream.write(mbr)
        stream.write(make_header(1, sectors - 1, 2))
        stream.write(entries)
        stream.seek((sectors - 33) * 512)
        stream.write(entries)
        stream.seek((sectors - 1) * 512)
        stream.write(make_header(sectors - 1, 1, sectors - 33))
        for name, start, count in parts:
            if name in images.CALIBRATION:
                data = (name.encode() * (count * 512 // len(name) + 1))[:count * 512]
                if name == "fsc":
                    data = bytes(count * 512)
                stream.seek(start * 512)
                stream.write(data)
                (calibration / (name + ".bin")).write_bytes(data)
    gpt = images.parse_gpt(raw)
    info = {"model_label": "MF800H", "adb_serial": "SYNTHETIC", "emmc_bytes": raw.stat().st_size,
            "backup_complete": True, "gpt_crc_verified": True, "full_emmc_sha256": images.digest(raw)}
    images.write_json(backup / "device_info.json", info)
    output = root / "images"
    output.mkdir()
    plan = {}
    for name in ("userdata", "boot", "tz", "rpm", "hyp", "aboot", "sbl1"):
        size = 512 * 1024 if name == "userdata" else 1024**2 if name == "tz" else gpt[name]["byte_size"]
        path = output / (name + ".bin")
        with path.open("wb") as stream:
            stream.truncate(size)
        plan[name] = {"name": name, "file": path.name, "start_sector": 65536 if name == "tz" else gpt[name]["start_sector"],
                      "bytes": size, "sha256": images.digest(path)}
    for name, (start, data) in images.gpt_segments(raw, gpt).items():
        path = output / (name + ".bin")
        path.write_bytes(data)
        plan[name] = {"name": name, "file": path.name, "start_sector": start, "bytes": len(data), "sha256": images.digest(path)}
    manifest = {"model": "MF800H MIFID", "kernel": "6.12.1-msm8916", "target_adb_serial": "SYNTHETIC",
                "stock_sha256": info["full_emmc_sha256"], "write_plan": [plan[n] for n in images.WRITE_ORDER]}
    images.write_json(output / "manifest.json", manifest)
    return backup, output, info, gpt, manifest


class ImageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="mf800h-test-")
        cls.root = Path(cls.temporary.name)
        cls.backup, cls.output, cls.info, cls.gpt, cls.manifest = fixture(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        images.write_json(self.output / "manifest.json", self.manifest)

    def validate(self):
        # Only the test disk's capacity differs; all CRC/region checks remain real.
        with patch.object(images, "SUPPORTED_SECTORS", 196608):
            return images.validate_plan(self.backup, self.output)

    def mutate_manifest(self, change):
        manifest = copy.deepcopy(self.manifest)
        change(manifest)
        images.write_json(self.output / "manifest.json", manifest)

    def test_valid_backup_and_plan(self):
        self.assertEqual(images.validate_backup(self.backup)[0]["adb_serial"], "SYNTHETIC")
        self.assertEqual(len(self.validate()[2]["write_plan"]), 11)

    def test_different_target(self):
        self.mutate_manifest(lambda m: m.update(target_adb_serial="OTHER"))
        with self.assertRaisesRegex(images.SafetyError, "different backup"):
            self.validate()

    def test_different_stock_hash(self):
        self.mutate_manifest(lambda m: m.update(stock_sha256="0" * 64))
        with self.assertRaises(images.SafetyError):
            self.validate()

    def test_sbl1_must_be_last(self):
        self.mutate_manifest(lambda m: m["write_plan"].reverse())
        with self.assertRaisesRegex(images.SafetyError, "write set/order"):
            self.validate()

    def test_calibration_overlap(self):
        self.mutate_manifest(lambda m: m["write_plan"][0].update(start_sector=self.gpt["modem"]["start_sector"]))
        with self.assertRaises(images.SafetyError):
            self.validate()

    def test_unaligned_image(self):
        self.mutate_manifest(lambda m: m["write_plan"][0].update(bytes=513))
        with self.assertRaisesRegex(images.SafetyError, "Invalid region"):
            self.validate()

    def test_boolean_lba_not_accepted(self):
        self.mutate_manifest(lambda m: m["write_plan"][0].update(start_sector=True))
        with self.assertRaises(images.SafetyError):
            self.validate()

    def test_image_hash_mismatch(self):
        self.mutate_manifest(lambda m: m["write_plan"][0].update(sha256="0" * 64))
        with self.assertRaisesRegex(images.SafetyError, "size/hash"):
            self.validate()

    def test_path_traversal(self):
        for name in ("../stock/full_emmc.bin", "..\\stock\\full_emmc.bin", "/etc/passwd"):
            with self.subTest(name=name), self.assertRaises(images.SafetyError):
                images.local_file(self.output, name)

    def test_symlink_rejected(self):
        link = self.output / "link.bin"
        try:
            link.symlink_to(self.output / "boot.bin")
            with self.assertRaises(images.SafetyError):
                images.local_file(self.output, "link.bin")
        finally:
            link.unlink(missing_ok=True)

    def test_corrupt_gpt_header_crc(self):
        with (self.backup / "full_emmc.bin").open("rb") as stream:
            raw = bytearray(stream.read(1024)[512:])
        raw[56] ^= 1
        with self.assertRaisesRegex(images.SafetyError, "CRC"):
            images.header(raw, 1, 196608)

    def test_gpt_patch_only_moves_tz(self):
        patched = images.gpt_segments(self.backup / "full_emmc.bin", self.gpt)
        data = patched["gpt_primary_entries"][1]
        with (self.backup / "full_emmc.bin").open("rb") as stream:
            stream.seek(1024)
            original = stream.read(len(data))
        differences = []
        for off in range(0, len(data), 128):
            if original[off:off + 128] != data[off:off + 128]:
                differences.append(data[off + 56:off + 128].decode("utf-16le").split("\0")[0])
        self.assertEqual(differences, ["tz"])

    def test_rehashed_arbitrary_gpt_is_rejected(self):
        path = self.output / "gpt_primary_header.bin"
        original = path.read_bytes()
        try:
            data = bytearray(original)
            data[56] ^= 1
            path.write_bytes(data)
            def change(manifest):
                item = next(i for i in manifest["write_plan"] if i["name"] == "gpt_primary_header")
                item["sha256"] = images.digest(path)
            self.mutate_manifest(change)
            with self.assertRaisesRegex(images.SafetyError, "Unexpected GPT"):
                self.validate()
        finally:
            path.write_bytes(original)

    def test_changed_calibration_sidecar(self):
        path = self.backup / "calibration/sec.bin"
        original = path.read_bytes()
        try:
            path.write_bytes(bytes(len(original)))
            with self.assertRaisesRegex(images.SafetyError, "sidecar mismatch"):
                images.validate_backup(self.backup)
        finally:
            path.write_bytes(original)

    def test_incomplete_backup(self):
        info = dict(self.info, backup_complete=False)
        with patch.object(images, "read_json", return_value=info), self.assertRaises(images.SafetyError):
            images.validate_backup(self.backup)

    def test_non_object_manifest(self):
        (self.output / "manifest.json").write_text("[]")
        with self.assertRaisesRegex(images.SafetyError, "JSON object"):
            self.validate()

    def test_non_object_plan_entry(self):
        self.mutate_manifest(lambda manifest: manifest["write_plan"].__setitem__(0, "not-a-region"))
        with self.assertRaisesRegex(images.SafetyError, "write set/order"):
            self.validate()


class SparseTests(unittest.TestCase):
    def expand(self, chunks, blocks=3, checksum=0):
        temporary = tempfile.TemporaryDirectory(prefix="mf800h-sparse-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source, output = root / "sparse.bin", root / "raw.bin"
        source.write_bytes(struct.pack("<IHHHHIIII", 0xED26FF3A, 1, 0, 28, 12, 4096, blocks, len(chunks), checksum) + b"".join(chunks))
        prepare.expand_sparse(source, output)
        return output.read_bytes()

    def test_raw_fill_hole_and_crc(self):
        expected = b"r" * 4096 + b"abcd" * 1024 + bytes(4096)
        chunks = [struct.pack("<HHII", 0xCAC1, 0, 1, 4108) + b"r" * 4096,
                  struct.pack("<HHII", 0xCAC2, 0, 1, 16) + b"abcd",
                  struct.pack("<HHII", 0xCAC3, 0, 1, 12),
                  struct.pack("<HHII", 0xCAC4, 0, 0, 16) + struct.pack("<I", zlib.crc32(expected))]
        self.assertEqual(self.expand(chunks, checksum=zlib.crc32(expected)), expected)

    def test_truncated_raw(self):
        with self.assertRaisesRegex(images.SafetyError, "Truncated"):
            self.expand([struct.pack("<HHII", 0xCAC1, 0, 1, 4108) + b"x"], blocks=1)

    def test_overflow(self):
        with self.assertRaisesRegex(images.SafetyError, "exceeds"):
            self.expand([struct.pack("<HHII", 0xCAC3, 0, 2, 12)], blocks=1)

    def test_unknown_chunk(self):
        with self.assertRaisesRegex(images.SafetyError, "Unknown"):
            self.expand([struct.pack("<HHII", 0x1234, 0, 1, 12)], blocks=1)

    def test_bad_checksum(self):
        with self.assertRaisesRegex(images.SafetyError, "checksum"):
            self.expand([struct.pack("<HHII", 0xCAC3, 0, 1, 12)], blocks=1, checksum=1)


class FakeEDL:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    def identity(self, *args):
        self.calls.append(("identity",))
        if self.fail == "identity":
            raise images.SafetyError("Injected identity mismatch")

    def verify(self, label, *args):
        self.calls.append(("verify", label))
        if label == self.fail:
            raise images.SafetyError("Injected read-back mismatch")

    def run(self, label, *args):
        self.calls.append(("run", *args))


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mf800h-workflow-")
        self.addCleanup(self.temp.cleanup)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.temp.name)
        self.output = self.root / "images"
        self.output.mkdir()
        (self.output / "manifest.json").write_text("{}")
        self.raw = self.root / "full_emmc.bin"
        with self.raw.open("wb") as stream:
            stream.truncate(34 * 1024**2)
        self.info = {"adb_serial": "TEST", "full_emmc_sha256": "a" * 64, "emmc_bytes": self.raw.stat().st_size}
        self.gpt = {name: {"start_sector": 40, "byte_size": 512} for name in images.CALIBRATION}
        self.gpt["__metadata__"] = {"total_sectors": self.raw.stat().st_size // 512}
        (self.root / "calibration").mkdir()
        for name in images.CALIBRATION:
            (self.root / "calibration" / (name + ".bin")).write_bytes(b"c" * 512)
        (self.output / "boot.bin").write_bytes(b"b" * 512)
        self.manifest = {"write_plan": [{"name": "boot", "file": "boot.bin", "bytes": 512, "start_sector": 100,
                                        "sha256": images.digest(self.output / "boot.bin")} ]}
        self.stack.enter_context(patch.object(cli, "validate_plan", return_value=(self.info, self.gpt, self.manifest)))
        self.stack.enter_context(patch.object(cli, "validate_backup", return_value=(self.info, self.gpt)))
        self.stack.enter_context(patch.object(cli, "space"))
        self.stack.enter_context(patch.object(device, "device_lock", return_value=contextlib.nullcontext()))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(contextlib.redirect_stderr(io.StringIO()))

    def args(self, execute=True, confirmation=True):
        args = ["flash", "--backup", str(self.root), "--images", str(self.output), "--run-dir", str(self.root / "run")]
        if execute:
            args.append("--execute")
        if confirmation:
            args += ["--confirm", cli.token("flash", self.info)]
        return args

    def test_offline_default_never_constructs_edl(self):
        with patch.object(cli, "make_edl", side_effect=AssertionError("USB must remain untouched")):
            self.assertEqual(cli.main(self.args(execute=False)), 0)
        self.assertFalse((self.root / "run").exists())

    def test_confirmation_blocks_all_hardware(self):
        with patch.object(cli, "make_edl", side_effect=AssertionError("Must not contact USB")), patch("sys.stdin.isatty", return_value=False):
            self.assertEqual(cli.main(self.args(confirmation=False)), 1)

    def test_failed_readback_stops_without_reset(self):
        fake = FakeEDL(fail="verify-boot")
        with patch.object(cli, "make_edl", return_value=fake):
            self.assertEqual(cli.main(self.args()), 1)
        state = images.read_json(self.root / "run/state.json")
        self.assertFalse(state["completed"])
        self.assertFalse(state["reboot_issued"])
        self.assertEqual(state["verified"], [])
        self.assertTrue((self.output / ".flash-started.json").exists())
        self.assertFalse(any(call[0] == "run" and call[1] == "reset" for call in fake.calls))

    def test_success_never_resets(self):
        fake = FakeEDL()
        with patch.object(cli, "make_edl", return_value=fake):
            self.assertEqual(cli.main(self.args()), 0)
        state = images.read_json(self.root / "run/state.json")
        self.assertTrue(state["completed"])
        self.assertEqual(state["verified"], ["boot"])
        self.assertEqual(state["factory_partitions_unchanged"], list(images.CALIBRATION))
        self.assertFalse(any(call[0] == "run" and call[1] == "reset" for call in fake.calls))

    def test_preflight_failure_prevents_write_and_marker(self):
        fake = FakeEDL(fail="preflight-modemst1")
        with patch.object(cli, "make_edl", return_value=fake):
            self.assertEqual(cli.main(self.args()), 1)
        self.assertFalse((self.output / ".flash-started.json").exists())
        self.assertFalse(any(call[0] == "run" for call in fake.calls))

    def test_started_images_cannot_be_retried(self):
        (self.output / ".flash-started.json").write_text("{}")
        with patch.object(cli, "make_edl", side_effect=AssertionError("Must not contact USB")):
            self.assertEqual(cli.main(self.args()), 1)

    def test_historical_flash_record_is_preserved(self):
        record = self.output / "flash-state.json"
        record.write_text('{"completed":true}')
        self.assertEqual(cli.main(self.args()), 1)
        self.assertEqual(record.read_text(), '{"completed":true}')

    def test_restore_default_is_offline(self):
        with patch.object(cli, "make_edl", side_effect=AssertionError("Must not contact USB")):
            self.assertEqual(cli.main(["restore", "--backup", str(self.root)]), 0)

    def test_failed_state_cannot_reboot(self):
        state = self.root / "failed.json"
        images.write_json(state, {"operation": "flash", "completed": False})
        with patch.object(cli, "make_edl", side_effect=AssertionError("Must not contact USB")):
            self.assertEqual(cli.main(["reboot", "--state", str(state), "--run-dir", str(self.root / "reset"), "--execute"]), 1)

    def test_restore_identity_failure_prevents_writes(self):
        fake = FakeEDL(fail="identity")
        with patch.object(cli, "make_edl", return_value=fake):
            result = cli.main(["restore", "--backup", str(self.root), "--run-dir", str(self.root / "restore"),
                               "--execute", "--confirm", cli.token("restore", self.info)])
        self.assertEqual(result, 1)
        self.assertFalse(any(call[0] == "run" for call in fake.calls))
        self.assertFalse(images.read_json(self.root / "restore/state.json")["completed"])

    def test_restore_failed_full_readback_never_resets(self):
        fake = FakeEDL(fail="verify-full-stock")
        with patch.object(cli, "make_edl", return_value=fake):
            result = cli.main(["restore", "--backup", str(self.root), "--run-dir", str(self.root / "restore"),
                               "--execute", "--confirm", cli.token("restore", self.info)])
        self.assertEqual(result, 1)
        self.assertTrue(any(call[:2] == ("run", "wf") for call in fake.calls))
        self.assertFalse(any(call[:2] == ("run", "reset") for call in fake.calls))
        self.assertFalse(images.read_json(self.root / "restore/state.json")["completed"])

    def test_reboot_rechecks_device_before_reset(self):
        state = self.root / "completed.json"
        images.write_json(state, {"operation": "restore", "completed": True, "backup": str(self.root),
                                 "stock_sha256": self.info["full_emmc_sha256"], "reboot_issued": False})
        fake = FakeEDL(fail="before-reboot-stock")
        with patch.object(cli, "make_edl", return_value=fake):
            result = cli.main(["reboot", "--state", str(state), "--run-dir", str(self.root / "reset"),
                               "--execute", "--confirm", cli.token("reboot", self.info)])
        self.assertEqual(result, 1)
        self.assertFalse(any(call[:2] == ("run", "reset") for call in fake.calls))
        self.assertFalse(images.read_json(state)["reboot_issued"])

    def test_reboot_is_explicit_and_recorded(self):
        state = self.root / "completed.json"
        images.write_json(state, {"operation": "restore", "completed": True, "backup": str(self.root),
                                 "stock_sha256": self.info["full_emmc_sha256"], "reboot_issued": False})
        fake = FakeEDL()
        with patch.object(cli, "make_edl", return_value=fake):
            result = cli.main(["reboot", "--state", str(state), "--run-dir", str(self.root / "reset"),
                               "--execute", "--confirm", cli.token("reboot", self.info)])
        self.assertEqual(result, 0)
        self.assertEqual(fake.calls[-1], ("run", "reset"))
        self.assertTrue(images.read_json(state)["reboot_issued"])

    def test_flash_identity_failure_prevents_any_write(self):
        fake = FakeEDL(fail="identity")
        with patch.object(cli, "make_edl", return_value=fake):
            self.assertEqual(cli.main(self.args()), 1)
        self.assertFalse(any(call[0] == "run" for call in fake.calls))
        self.assertFalse((self.output / ".flash-started.json").exists())

    def test_existing_run_directory_never_reused(self):
        (self.root / "run").mkdir()
        (self.root / "run/user-data.txt").write_text("preserve me")
        with patch.object(cli, "make_edl", side_effect=AssertionError("Must not contact USB")):
            self.assertEqual(cli.main(self.args()), 1)
        self.assertEqual((self.root / "run/user-data.txt").read_text(), "preserve me")

    def test_fresh_directory_never_overwrites(self):
        with self.assertRaises(images.SafetyError):
            cli.fresh_directory(self.output)


class DeviceTests(unittest.TestCase):
    def test_multiple_or_missing_devices_prevent_subprocess(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            script = path / "edl.py"
            loader = path / "loader.mbn"
            script.touch()
            loader.touch()
            edl = device.EDL(path, script, loader)
            for count in (0, 2):
                with patch.object(device, "edl_count", return_value=count), patch.object(device.subprocess, "run") as run:
                    with self.assertRaises(images.SafetyError):
                        edl.run("not-allowed", "ws", 0, "anything")
                    run.assert_not_called()

    def test_lock_is_exclusive_and_released(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "lock"
            with device.device_lock(path):
                with self.assertRaises(RuntimeError):
                    with device.device_lock(path):
                        self.fail("Second process entered lock")
            self.assertFalse(path.exists())

    def test_zero_exit_code_with_fatal_edl_output_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script, loader = root / "edl.py", root / "loader.mbn"
            script.touch()
            loader.touch()
            edl = device.EDL(root, script, loader)
            def fake_run(*args, **kwargs):
                kwargs["stdout"].write("Error writing sector\n")
                return device.subprocess.CompletedProcess(args, 0)
            with contextlib.redirect_stdout(io.StringIO()), patch.object(device, "edl_count", return_value=1), patch.object(device.subprocess, "run", side_effect=fake_run):
                with self.assertRaisesRegex(images.SafetyError, "EDL failed"):
                    edl.run("failure", "ws", 10, "image.bin")

    def test_short_readback_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script, loader = root / "edl.py", root / "loader.mbn"
            script.touch()
            loader.touch()
            edl = device.EDL(root, script, loader)
            def fake_run(label, operation, start, count, path):
                path.write_bytes(b"short")
            with patch.object(edl, "run", side_effect=fake_run), self.assertRaisesRegex(images.SafetyError, "Short/absent"):
                edl.read("short", 0, 512)

    def test_digest_rejects_truncated_region(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "short.bin"
            path.write_bytes(b"short")
            with self.assertRaises(images.SafetyError):
                images.digest(path, size=512)


if __name__ == "__main__":
    unittest.main()
