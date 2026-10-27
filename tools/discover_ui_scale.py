#!/usr/bin/env python3
"""Discover the serialized UICanvasScaleData record in a Polytopia app bundle.

The locator recognizes an aligned Unity string followed by the expected
global multiplier and screen-limit record layout. It does not decode a full
SerializedFile type tree, so the emitted bundle hash and game version bind a
profile to the exact inspected build.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import plistlib
import struct
import sys
from pathlib import Path

from unityfs_inspect import Node, parse_bundle


OBJECT_NAME = b"UICanvasScaleData"
PRESET_KEY_PREFIX = "settings.scale."


def align4(value: int) -> int:
    return (value + 3) & ~3


def resolve_app_files(source_app: Path) -> tuple[Path, Path]:
    layouts = (
        (source_app / "Contents" / "Info.plist",
         source_app / "Contents" / "Resources" / "Data" / "data.unity3d"),
        (source_app / "Info.plist", source_app / "Data" / "data.unity3d"),
    )
    for info_path, bundle_path in layouts:
        if info_path.is_file() and bundle_path.is_file():
            return info_path, bundle_path
    expected = " or ".join(
        f"({info}, {bundle})" for info, bundle in layouts
    )
    raise ValueError(f"could not find app Info.plist and data.unity3d under {source_app}; expected {expected}")


def parse_scale_record(data: bytes, node: Node, name_prefix: int) -> dict:
    node_start = node.offset
    node_end = node.offset + node.size
    name_start = name_prefix + 4
    if name_prefix < node_start or name_start + len(OBJECT_NAME) > node_end:
        raise ValueError("Unity object name extends outside its node")
    if (name_prefix - node_start) % 4:
        raise ValueError("Unity object name is not 4-byte aligned")
    if data[name_prefix:name_start] != struct.pack("<I", len(OBJECT_NAME)):
        raise ValueError("Unity object name length does not match")
    if data[name_start:name_start + len(OBJECT_NAME)] != OBJECT_NAME:
        raise ValueError("Unity object name does not match")

    # Unity aligns the next serialized field after the length-prefixed name.
    global_offset = node_start + align4(name_start + len(OBJECT_NAME) - node_start)
    if global_offset + 8 > node_end:
        raise ValueError("global multiplier and screen-limit count are truncated")
    (global_multiplier,) = struct.unpack_from("<f", data, global_offset)
    (preset_count,) = struct.unpack_from("<i", data, global_offset + 4)
    if not math.isfinite(global_multiplier) or global_multiplier <= 0:
        raise ValueError(f"anomalous global multiplier: {global_multiplier!r}")
    records_start = global_offset + 8
    if preset_count <= 0:
        raise ValueError(f"screen-limit count must be positive, found {preset_count}")
    if preset_count > (node_end - records_start) // 12:
        raise ValueError(f"screen-limit count {preset_count} cannot fit inside node bounds")

    presets = []
    seen_keys = set()
    cursor = records_start
    previous_limit = None
    for index in range(preset_count):
        if cursor + 12 > node_end:
            raise ValueError(f"screen-limit record {index} is truncated")
        limit, multiplier, key_length = struct.unpack_from("<ffi", data, cursor)
        if not math.isfinite(limit):
            raise ValueError(f"screen-limit record {index} has a non-finite limit")
        if not math.isfinite(multiplier) or multiplier <= 0:
            raise ValueError(f"screen-limit record {index} has an anomalous multiplier {multiplier!r}")
        if key_length <= 0:
            raise ValueError(f"screen-limit record {index} has invalid key length {key_length}")
        key_start = cursor + 12
        key_end = key_start + key_length
        if key_end > node_end:
            raise ValueError(f"screen-limit record {index} key extends outside node bounds")
        key_bytes = data[key_start:key_end]
        try:
            key = key_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError(f"screen-limit record {index} key is not valid UTF-8") from exc
        if not key.startswith(PRESET_KEY_PREFIX) or not key[len(PRESET_KEY_PREFIX):]:
            raise ValueError(f"screen-limit record {index} has an unexpected localization key {key!r}")
        if key in seen_keys:
            raise ValueError(f"duplicate screen-limit localization key {key!r}")
        if previous_limit is not None and limit <= previous_limit:
            raise ValueError("screen-limit values are not strictly increasing")
        padding_end = node_start + align4(key_end - node_start)
        if padding_end > node_end:
            raise ValueError(f"screen-limit record {index} alignment extends outside node bounds")
        if any(data[key_end:padding_end]):
            raise ValueError(f"screen-limit record {index} has nonzero string padding")
        presets.append({"limit": limit, "multiplier": multiplier, "key": key})
        seen_keys.add(key)
        previous_limit = limit
        cursor = padding_end

    return {
        "logical_field_offset": global_offset,
        "original_global_ui_multiplier": global_multiplier,
        "presets": presets,
    }


def discover_scale_record(data: bytes, nodes: list[Node]) -> tuple[Node, dict]:
    marker = struct.pack("<I", len(OBJECT_NAME)) + OBJECT_NAME
    valid = []
    rejected = []
    for node in nodes:
        node_start = node.offset
        node_end = node.offset + node.size
        if node_start < 0 or node_end > len(data) or node_end < node_start:
            raise ValueError(f"invalid bounds for node {node.name!r}")
        search_from = node_start
        while True:
            name_prefix = data.find(marker, search_from, node_end)
            if name_prefix < 0:
                break
            search_from = name_prefix + 1
            if (name_prefix - node_start) % 4:
                continue
            try:
                record = parse_scale_record(data, node, name_prefix)
            except (ValueError, struct.error) as exc:
                rejected.append(f"{node.name}@{name_prefix:#x}: {exc}")
            else:
                valid.append((node, record))
    if len(valid) != 1:
        details = "; ".join(rejected)
        if len(valid) > 1:
            locations = ", ".join(f"{node.name}@{record['logical_field_offset']:#x}" for node, record in valid)
            raise ValueError(f"found {len(valid)} valid UICanvasScaleData records: {locations}")
        suffix = f"; rejected candidates: {details}" if details else ""
        raise ValueError(f"found no valid UICanvasScaleData record{suffix}")
    return valid[0]


def make_profile(source_app: Path) -> dict:
    info_path, bundle_path = resolve_app_files(source_app)
    with info_path.open("rb") as stream:
        app_info = plistlib.load(stream)
    game_version = app_info.get("CFBundleShortVersionString")
    if not isinstance(game_version, str) or not game_version.strip():
        raise ValueError(f"{info_path} has no CFBundleShortVersionString")
    game_version = game_version.strip()
    build_version = app_info.get("CFBundleVersion")
    if build_version is not None:
        build_version = str(build_version).strip()
        if (build_version and build_version != "0" and game_version != build_version
                and not game_version.endswith("." + build_version)):
            game_version = f"{game_version}.{build_version}"

    bundle = parse_bundle(bundle_path)
    node, record = discover_scale_record(bundle.data, bundle.nodes)
    digest = hashlib.sha256()
    with bundle_path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "schema_version": 1,
        "game_version": game_version,
        "source_asset_sha256": digest.hexdigest(),
        "unity_revision": bundle.unity_revision,
        "node_name": node.name,
        "logical_field_offset": f"0x{record['logical_field_offset']:x}",
        "original_global_ui_multiplier": record["original_global_ui_multiplier"],
        "presets": record["presets"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-app", type=Path, required=True,
                        help="path to the original Polytopia.app")
    parser.add_argument("--output-profile", type=Path,
                        help="write the discovered profile to this new JSON file; refuses overwrite")
    args = parser.parse_args()
    profile = make_profile(args.source_app)
    if args.output_profile is not None:
        profile_path = args.output_profile
        if profile_path.exists() or profile_path.is_symlink():
            raise FileExistsError(f"refusing to overwrite existing profile: {profile_path}")
        if profile_path.resolve() == resolve_app_files(args.source_app)[1].resolve():
            raise ValueError("profile output path must differ from the source bundle")
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        with profile_path.open("x", encoding="utf-8") as stream:
            json.dump(profile, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
    print(json.dumps(profile, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, struct.error, plistlib.InvalidFileException) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
