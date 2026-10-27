#!/usr/bin/env python3
"""Create or adjust a patched Polytopia app with a larger global UI scale.

The unmodified source app is read-only. Output defaults to patched/Polytopia.app;
--check only validates the source. No game is launched or packages installed.
"""

import argparse
import hashlib
import json
import math
import plistlib
import shutil
import struct
import subprocess
import uuid
from pathlib import Path

from unityfs_inspect import parse_bundle, write_bundle_patch


DATA_PATH = Path("Contents/Resources/Data/data.unity3d")
DEFAULT_OUTPUT = Path("patched/Polytopia.app")
DEFAULT_PROFILE = Path(__file__).resolve().parent.parent / "profiles/steam-2.17.3.16375.json"
SIGNING_FILES = [DATA_PATH, Path("Contents/MacOS/Polytopia"),
                 Path("Contents/_CodeSignature/CodeResources")]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_profile(path=None):
    profile = json.loads((path or DEFAULT_PROFILE).read_text())
    required = {"schema_version", "game_version", "source_asset_sha256", "unity_revision",
                "node_name", "logical_field_offset", "original_global_ui_multiplier"}
    if required - profile.keys() or profile.get("schema_version") != 1:
        raise ValueError("Unsupported or incomplete build profile")
    checksum = profile["source_asset_sha256"]
    if (not isinstance(checksum, str) or len(checksum) != 64
            or any(char not in "0123456789abcdef" for char in checksum)):
        raise ValueError("Profile requires a full SHA-256")
    offset = int(profile["logical_field_offset"], 0)
    value = profile["original_global_ui_multiplier"]
    if offset < 20 or type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError("Anomalous field offset or original multiplier in profile")
    return profile


def check_source(source, profile=None):
    profile = profile or load_profile()
    info = plistlib.loads((source / "Contents/Info.plist").read_bytes())
    if info.get("CFBundleShortVersionString") != profile["game_version"]:
        raise ValueError("Unsupported game version; inspect this build before patching")
    data = source / DATA_PATH
    if digest(data) != profile["source_asset_sha256"]:
        raise ValueError("Source asset hash differs from the inspected original; refusing to guess")
    bundle = parse_bundle(data)
    if bundle.unity_revision != profile["unity_revision"]:
        raise ValueError("Unity revision differs from the build profile")
    # The current writer was validated on this engine revision. Discovery can
    # inspect another revision, but writing it requires a compatibility review.
    if bundle.version != 8 or bundle.unity_revision != "6000.3.17f1":
        raise ValueError("Bundle writer needs review for this Unity revision/format")
    offset = int(profile["logical_field_offset"], 0)
    marker = b"UICanvasScaleData"
    if bundle.data[offset - 20:offset - 20 + len(marker)] != marker:
        raise ValueError("Expected UI scale asset name is absent at the inspected position")
    matching_nodes = [node for node in bundle.nodes if node.name == profile["node_name"]
                      and node.offset <= offset and offset + 4 <= node.offset + node.size]
    if len(matching_nodes) != 1:
        raise ValueError("UI field is not inside the profiled bundle node")
    expected = struct.pack("<f", profile["original_global_ui_multiplier"])
    if bundle.data[offset:offset + 4] != expected:
        raise ValueError("Original global UI multiplier differs from the build profile")
    return bundle


def prepare(source, destination, factor, profile=None, update=False, game_closed_confirmed=False):
    profile = profile or load_profile()
    source = source.resolve(strict=True)
    destination = destination.resolve()
    if destination.exists() and not update:
        raise ValueError("Output exists; choose a new path to preserve it")
    if update and not destination.is_dir():
        raise ValueError("Patched app does not exist")
    if destination.suffix != ".app":
        raise ValueError("Output must be an .app bundle")
    if destination == source or source in destination.parents or destination in source.parents:
        raise ValueError("Source and destination must be separate app bundles")
    report_path = destination.with_suffix(".patch.json")
    if report_path.is_symlink():
        raise ValueError("Patch report must not be a symlink")
    if report_path.exists() and not update:
        raise ValueError("A report already exists for this output; choose a new path")
    previous = None
    if update and not game_closed_confirmed:
        running = subprocess.run(["pgrep", "-x", "Polytopia"], capture_output=True, text=True)
        if running.returncode not in (0, 1):
            raise ValueError("Could not check whether Polytopia is running: "
                             + running.stderr.strip()
                             + ". Check in your terminal; only after confirming it is closed, "
                             "use --game-closed-confirmed.")
        if running.returncode == 0:
            raise ValueError("Quit Polytopia before updating the patched app")
    if update:
        if game_closed_confirmed:
            print("Using explicit confirmation that Polytopia is closed.", flush=True)
        previous = json.loads(report_path.read_text())
        if (previous["source_asset_sha256"] != profile["source_asset_sha256"]
                or previous["game_version"] != profile["game_version"]):
            raise ValueError("Patched app came from a different official build; prepare a fresh patched app")
        if digest(destination / DATA_PATH) != previous["trial_asset_sha256"]:
            raise ValueError("Patched asset differs from its report; refusing to overwrite it")
    try:
        replacement = struct.pack("<f", factor)
    except (OverflowError, struct.error) as error:
        raise ValueError("Multiplier must fit a finite, positive float32") from error
    stored_factor = struct.unpack("<f", replacement)[0]
    if not math.isfinite(stored_factor) or stored_factor <= 0:
        raise ValueError("Multiplier must fit a finite, positive float32")
    original = check_source(source, profile)
    if not update:
        print(f"Copying original app to {destination}", flush=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination, symlinks=True)
    for name in SIGNING_FILES:
        candidate = destination / name
        if not candidate.is_file() or destination not in candidate.resolve().parents:
            raise ValueError(f"Expected a regular file inside the patched app: {name}")
    target = destination / DATA_PATH
    # The writer reads the unmodified source and changes the destination resource.
    temporary = target.with_name("data.ui-patch.unity3d")
    offset = int(profile["logical_field_offset"], 0)
    write_bundle_patch(source / DATA_PATH, temporary, offset,
                       struct.pack("<f", profile["original_global_ui_multiplier"]), replacement)
    changed = parse_bundle(temporary)
    expected_data = original.data[:offset] + replacement + original.data[offset + 4:]
    if changed.data != expected_data or changed.nodes != original.nodes:
        raise ValueError("Patched resource changed content beyond the requested UI field")
    print("Verified decompressed assets: only the requested float changed.", flush=True)
    backup = None
    if update:
        backup = destination.parent / "patch-backups" / str(uuid.uuid4())
        for name in SIGNING_FILES:
            saved = backup / name
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(destination / name, saved)
        shutil.copy2(report_path, backup / "patch-report.json")
        print(f"Saved the patched app's three affected files to {backup}", flush=True)
    # Retain the report keys used by previously generated patched apps.
    report = {
        "game_version": profile["game_version"],
        "source_app": str(source),
        "trial_app": str(destination),
        "source_asset_sha256": profile["source_asset_sha256"],
        "trial_asset_sha256": digest(temporary),
        "logical_field_offset": hex(offset),
        "original_global_ui_multiplier": profile["original_global_ui_multiplier"],
        "trial_global_ui_multiplier": stored_factor,
        "previous_trial_multiplier": previous["trial_global_ui_multiplier"] if previous else None,
        "previous_trial_backup": str(backup) if backup else None,
        "native_machine_code_patched": False,
        "decompressed_assets_verified": True,
        "code_signature_verified": True,
        "game_launched": False,
        "appearance_verified": False,
    }
    try:
        temporary.replace(target)
        subprocess.run(["codesign", "--force", "--sign", "-", str(destination)], check=True)
        subprocess.run(["codesign", "--verify", "--deep", "--strict", str(destination)], check=True)
        if digest(source / DATA_PATH) != profile["source_asset_sha256"]:
            raise ValueError("Source app changed during preparation")
        report_path.write_text(json.dumps(report, indent=2) + "\n")
    except BaseException:
        if backup:
            for name in SIGNING_FILES:
                shutil.copy2(backup / name, destination / name)
            shutil.copy2(backup / "patch-report.json", report_path)
            print("Restored the patched app after an unsuccessful update.", flush=True)
        raise
    print(f"Patched app prepared; global UI multiplier = {stored_factor:g}")
    print(f"Report: {report_path}")
    print("No game was launched. The source app is unchanged.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-app", type=Path, required=True,
                        help="path to the original Steam Polytopia.app")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--output-app", type=Path,
                      help="output app path (default: patched/Polytopia.app)")
    mode.add_argument("--update-app", "--update-trial", dest="update_app", type=Path,
                      help="adjust an existing patched app with its adjacent patch report")
    mode.add_argument("--check", action="store_true", help="validate the source without writing files")
    parser.add_argument("--profile", type=Path, help="reviewed JSON profile for an official build")
    parser.add_argument("--game-closed-confirmed", action="store_true",
                        help="use an explicit manual confirmation when the process query is unavailable")
    parser.add_argument("--multiplier", type=float, default=3.0,
                        help="absolute global UI multiplier; default: 3.0")
    args = parser.parse_args()
    if not math.isfinite(args.multiplier) or args.multiplier <= 0:
        parser.error("--multiplier must be finite and positive")
    if args.game_closed_confirmed and not args.update_app:
        parser.error("--game-closed-confirmed applies only to --update-app")
    profile = load_profile(args.profile)
    if args.check:
        check_source(args.source_app, profile)
        print(f"Source matches {profile['game_version']}; "
              f"global UI multiplier = {profile['original_global_ui_multiplier']:g}")
        print("No files changed.")
    else:
        prepare(args.source_app, args.update_app or args.output_app or DEFAULT_OUTPUT,
                args.multiplier, profile, update=bool(args.update_app),
                game_closed_confirmed=args.game_closed_confirmed)


if __name__ == "__main__":
    main()
