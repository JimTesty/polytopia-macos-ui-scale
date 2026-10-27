"""Small synthetic fixtures; no game files or macOS signing required."""

import hashlib
import json
import os
import plistlib
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import prepare_scaled_app as app
from discover_ui_scale import discover_scale_record, make_profile
from unityfs_inspect import Node, parse_bundle


def fixture_bundle(payload):
    """One uncompressed UnityFS v8 node, built without the patch writer."""
    info = bytes(16) + struct.pack(">IIIH", 1, len(payload), len(payload), 0)
    info += struct.pack(">IQQI", 1, 0, len(payload), 0) + b"sharedassets0.assets\0"
    header = b"UnityFS\0" + struct.pack(">I", 8) + b"5.x.x\0" + b"6000.3.17f1\0"
    size_offset = len(header)
    result = bytearray(header + struct.pack(">QIII", 0, len(info), len(info), 0x240))
    result += bytes((-len(result)) % 16)
    result += info
    result += bytes((-len(result)) % 16)
    result += payload
    struct.pack_into(">Q", result, size_offset, len(result))
    return bytes(result)


class PatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "Original.app"
        self.trial = self.root / "Trial.app"
        name, key = b"UICanvasScaleData", b"settings.scale.small"
        record = struct.pack("<I", len(name)) + name + bytes(3)
        record += struct.pack("<fiffi", 1.0, 1, 0.0, 1.0, len(key)) + key
        self.payload = bytes(32) + record + bytes((-len(record)) % 4)
        self.original = fixture_bundle(self.payload)
        for name in app.SIGNING_FILES:
            target = self.source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.original if name == app.DATA_PATH else b"original signing file")
        (self.source / "Contents/Info.plist").write_bytes(
            plistlib.dumps({"CFBundleShortVersionString": "fixture"}))
        self.profile = make_profile(self.source)
        self.offset = int(self.profile["logical_field_offset"], 0)

    def create_trial(self):
        with patch.object(app.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
            app.prepare(self.source, self.trial, 2.0, self.profile)

    def test_cli_checks_creates_and_adjusts_patched_app(self):
        profile_path = self.root / "profile.json"
        profile_path.write_text(json.dumps(self.profile))
        arguments = ["prepare_scaled_app.py", "--source-app", str(self.source),
                     "--profile", str(profile_path)]
        destination = self.root / "patched/Polytopia.app"
        previous_directory = Path.cwd()
        try:
            os.chdir(self.root)
            with patch.object(sys, "argv", arguments + ["--check"]):
                app.main()
            self.assertFalse(destination.parent.exists())
            with patch.object(sys, "argv", arguments), patch.object(
                    app.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
                app.main()
            self.assertTrue(destination.is_dir())
            report = destination.with_suffix(".patch.json")
            self.assertEqual(json.loads(report.read_text())["trial_global_ui_multiplier"], 3.0)
            with patch.object(sys, "argv", arguments + [
                    "--update-app", str(destination), "--multiplier", "2.0",
                    "--game-closed-confirmed"]), patch.object(
                    app.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
                app.main()
            self.assertEqual(json.loads(report.read_text())["trial_global_ui_multiplier"], 2.0)
            self.assertEqual(struct.unpack_from("<f", parse_bundle(
                destination / app.DATA_PATH).data, self.offset)[0], 2.0)
            self.assertEqual((self.source / app.DATA_PATH).read_bytes(), self.original)
        finally:
            os.chdir(previous_directory)

    def test_only_requested_field_changes(self):
        self.create_trial()
        result = parse_bundle(self.trial / app.DATA_PATH)
        expected = (self.payload[:self.offset] + struct.pack("<f", 2.0)
                    + self.payload[self.offset + 4:])
        self.assertEqual(result.data, expected)
        self.assertEqual((self.source / app.DATA_PATH).read_bytes(), self.original)
        report = self.trial.with_suffix(".patch.json")
        self.assertTrue(report.is_file())

    def test_failed_signing_restores_trial_and_report(self):
        self.create_trial()
        files = [self.trial / name for name in app.SIGNING_FILES]
        files.append(self.trial.with_suffix(".patch.json"))
        before = {path: path.read_bytes() for path in files}

        def fail_signing(command, **kwargs):
            files[1].write_bytes(b"partially changed signature")
            files[2].write_bytes(b"partially changed resource seal")
            raise subprocess.CalledProcessError(1, command)

        with patch.object(app.subprocess, "run", side_effect=fail_signing):
            with self.assertRaises(subprocess.CalledProcessError):
                app.prepare(self.source, self.trial, 3.0, self.profile,
                            update=True, game_closed_confirmed=True)
        self.assertEqual({path: path.read_bytes() for path in files}, before)
        self.assertEqual((self.source / app.DATA_PATH).read_bytes(), self.original)

    def test_unknown_source_rejected_before_copy(self):
        profile = dict(self.profile, source_asset_sha256="0" * 64)
        with self.assertRaisesRegex(ValueError, "hash differs"):
            app.prepare(self.source, self.trial, 2.0, profile)
        self.assertFalse(self.trial.exists())
        self.assertEqual(hashlib.sha256(self.original).hexdigest(),
                         self.profile["source_asset_sha256"])

    def test_discovery_relocates_and_rejects_ambiguity_or_nan(self):
        moved = bytes(80) + self.payload
        _, found = discover_scale_record(moved, [Node(0, len(moved), 0, "fixture")])
        self.assertEqual(found["logical_field_offset"], self.offset + 80)
        both = self.payload * 2
        with self.assertRaisesRegex(ValueError, "2 valid"):
            discover_scale_record(both, [Node(0, len(both), 0, "fixture")])
        invalid = bytearray(self.payload)
        struct.pack_into("<f", invalid, self.offset, float("nan"))
        with self.assertRaisesRegex(ValueError, "anomalous global"):
            discover_scale_record(bytes(invalid), [Node(0, len(invalid), 0, "fixture")])


if __name__ == "__main__":
    unittest.main()
