#!/usr/bin/env python3
"""Read UnityFS bundles with uncompressed or LZ4/LZ4HC block compression."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path


def u16(data: bytes, pos: int, endian: str = ">") -> tuple[int, int]:
    return struct.unpack_from(endian + "H", data, pos)[0], pos + 2


def u32(data: bytes, pos: int, endian: str = ">") -> tuple[int, int]:
    return struct.unpack_from(endian + "I", data, pos)[0], pos + 4


def u64(data: bytes, pos: int, endian: str = ">") -> tuple[int, int]:
    return struct.unpack_from(endian + "Q", data, pos)[0], pos + 8


def read_cstring(data: bytes, pos: int) -> tuple[str, int]:
    end = data.find(b"\0", pos)
    if end < 0:
        raise ValueError("unterminated UnityFS string")
    return data[pos:end].decode("utf-8", errors="replace"), end + 1


def read_cstring_bytes(data: bytes, pos: int) -> tuple[str, bytes, int]:
    end = data.find(b"\0", pos)
    if end < 0:
        raise ValueError("unterminated UnityFS string")
    raw = data[pos:end]
    return raw.decode("utf-8", errors="replace"), raw, end + 1


def lz4_block(src: bytes, expected_size: int) -> bytes:
    """Decode a raw LZ4 block without external dependencies."""
    out = bytearray()
    ip = 0
    while ip < len(src):
        token = src[ip]
        ip += 1
        literal_length = token >> 4
        if literal_length == 15:
            while True:
                extra = src[ip]
                ip += 1
                literal_length += extra
                if extra != 255:
                    break
        end = ip + literal_length
        if end > len(src):
            raise ValueError("LZ4 literal exceeds compressed block")
        out.extend(src[ip:end])
        ip = end
        if ip == len(src):
            break
        if ip + 2 > len(src):
            raise ValueError("truncated LZ4 match offset")
        match_offset = src[ip] | (src[ip + 1] << 8)
        ip += 2
        if not match_offset or match_offset > len(out):
            raise ValueError(f"invalid LZ4 match offset {match_offset}")
        match_length = token & 0x0F
        if match_length == 15:
            while True:
                extra = src[ip]
                ip += 1
                match_length += extra
                if extra != 255:
                    break
        match_length += 4
        for _ in range(match_length):
            out.append(out[-match_offset])
    if len(out) != expected_size:
        raise ValueError(f"LZ4 size mismatch: got {len(out)}, expected {expected_size}")
    return bytes(out)


@dataclass
class Block:
    uncompressed_size: int
    compressed_size: int
    flags: int


@dataclass
class Node:
    offset: int
    size: int
    flags: int
    name: str
    raw_name: bytes = b""


@dataclass
class UnityFS:
    data: bytes
    version: int
    unity_version: str
    unity_revision: str
    flags: int
    blocks: list[Block]
    nodes: list[Node]
    content_hash: bytes
    raw_blocks: list[bytes]
    header_fields_offset: int
    header_end: int


def decompress_block(data: bytes, expected_size: int, compression: int) -> bytes:
    if compression == 0:
        if len(data) != expected_size:
            raise ValueError("uncompressed block size mismatch")
        return data
    if compression in (2, 3):
        return lz4_block(data, expected_size)
    raise ValueError(f"unsupported UnityFS compression type {compression}")


def align16(value: int) -> int:
    return (value + 15) & ~15


def parse_bundle(path: Path) -> UnityFS:
    raw = path.read_bytes()
    if not raw.startswith(b"UnityFS\0"):
        raise ValueError("not a UnityFS bundle")
    pos = 8
    version, pos = u32(raw, pos)
    unity_version, pos = read_cstring(raw, pos)
    unity_revision, pos = read_cstring(raw, pos)
    header_fields_offset = pos
    declared_size, pos = u64(raw, pos)
    compressed_info_size, pos = u32(raw, pos)
    uncompressed_info_size, pos = u32(raw, pos)
    flags, pos = u32(raw, pos)
    header_end = pos
    if declared_size != len(raw):
        raise ValueError(f"bundle size mismatch: header {declared_size}, actual {len(raw)}")
    aligned_header_end = align16(header_end) if version >= 7 else header_end
    if flags & 0x80:
        info_start = len(raw) - compressed_info_size
        info_compressed = raw[info_start:]
        blocks_start = aligned_header_end
    else:
        info_start = aligned_header_end
        info_compressed = raw[info_start:info_start + compressed_info_size]
        blocks_start = info_start + compressed_info_size
    if flags & 0x200:
        blocks_start = align16(blocks_start)
    info = decompress_block(info_compressed, uncompressed_info_size, flags & 0x3F)
    p = 0
    content_hash = info[p:p + 16]
    if len(content_hash) != 16:
        raise ValueError("truncated UnityFS content hash")
    p += 16
    block_count, p = u32(info, p)
    blocks = []
    for _ in range(block_count):
        uncompressed, p = u32(info, p)
        compressed, p = u32(info, p)
        block_flags, p = u16(info, p)
        blocks.append(Block(uncompressed, compressed, block_flags))
    node_count, p = u32(info, p)
    nodes = []
    for _ in range(node_count):
        offset, p = u64(info, p)
        size, p = u64(info, p)
        node_flags, p = u32(info, p)
        name, raw_name, p = read_cstring_bytes(info, p)
        nodes.append(Node(offset, size, node_flags, name, raw_name))
    if p != len(info):
        raise ValueError(f"{len(info) - p} unexpected bytes after UnityFS block metadata")
    stream = bytearray()
    block_pos = blocks_start
    raw_blocks = []
    for block in blocks:
        compressed_data = raw[block_pos:block_pos + block.compressed_size]
        if len(compressed_data) != block.compressed_size:
            raise ValueError("UnityFS block is truncated")
        raw_blocks.append(compressed_data)
        stream.extend(decompress_block(compressed_data, block.uncompressed_size, block.flags & 0x3F))
        block_pos += block.compressed_size
    expected_block_end = info_start if flags & 0x80 else len(raw)
    if block_pos != expected_block_end:
        raise ValueError(f"unexpected UnityFS payload tail: blocks end at {block_pos:#x}, expected {expected_block_end:#x}")
    if not nodes:
        raise ValueError(f"bundle has no nodes; hash={content_hash.hex()}")
    if any(node.offset + node.size > len(stream) for node in nodes):
        raise ValueError("UnityFS node points outside decompressed block stream")
    return UnityFS(bytes(stream), version, unity_version, unity_revision, flags, blocks, nodes,
                   content_hash, raw_blocks, header_fields_offset, header_end)


def _build_blocks_info(content_hash: bytes, blocks: list[Block], nodes: list[Node]) -> bytes:
    out = bytearray(content_hash)
    out.extend(struct.pack(">I", len(blocks)))
    for block in blocks:
        out.extend(struct.pack(">IIH", block.uncompressed_size, block.compressed_size, block.flags))
    out.extend(struct.pack(">I", len(nodes)))
    for node in nodes:
        name = node.raw_name or node.name.encode("utf-8")
        if b"\0" in name:
            raise ValueError(f"node name contains a null byte: {node.name!r}")
        if name.decode("utf-8", errors="replace") != node.name:
            raise ValueError(f"node name is not round-trip UTF-8: {node.name!r}")
        out.extend(struct.pack(">QQI", node.offset, node.size, node.flags))
        out.extend(name)
        out.append(0)
    return bytes(out)


def write_bundle_patch(input_path: Path, output_path: Path, logical_offset: int,
                       expected_bytes: bytes, replacement_bytes: bytes) -> dict:
    """Write a same-length patch while preserving untouched compressed blocks.

    The source is never overwritten. Only UnityFS v8 bundles from the current
    6000.3.17f1 build are accepted. The rebuilt block-info hash is zeroed, as
    in UnityPy's UnityFS writer, rather than retaining a stale source hash.
    """
    input_path = Path(input_path)
    output_path = Path(output_path)
    if not input_path.is_file():
        raise ValueError(f"input is not a regular file: {input_path}")
    if output_path.exists() or output_path.is_symlink():
        raise FileExistsError(f"refusing to overwrite existing output: {output_path}")
    if input_path.resolve() == output_path.resolve():
        raise ValueError("input and output paths must differ")
    if not isinstance(expected_bytes, bytes) or not isinstance(replacement_bytes, bytes):
        raise TypeError("expected_bytes and replacement_bytes must be bytes")
    if not expected_bytes or len(expected_bytes) != len(replacement_bytes):
        raise ValueError("patch bytes must be non-empty and exactly the same length")
    if logical_offset < 0:
        raise ValueError("logical_offset must be non-negative")

    raw = input_path.read_bytes()
    bundle = parse_bundle(input_path)
    if bundle.version != 8 or bundle.unity_revision != "6000.3.17f1":
        raise ValueError(f"unsupported bundle build: UnityFS v{bundle.version} {bundle.unity_revision!r}")
    known_archive_flags = 0x3F | 0x40 | 0x80 | 0x100 | 0x200
    if bundle.flags & ~known_archive_flags:
        raise ValueError(f"unknown UnityFS archive flags: {bundle.flags:#x}")
    if bundle.flags & 0x80:
        raise ValueError("block-info-at-end bundles are not supported by the patch writer")
    if not bundle.flags & 0x40 or not bundle.flags & 0x200:
        raise ValueError(f"expected combined v8 info and 16-byte aligned data flags: {bundle.flags:#x}")
    if bundle.flags & 0x100:
        raise ValueError("old web-plugin compatibility flag is not supported")
    if bundle.flags & 0x3F not in (0, 2, 3):
        raise ValueError(f"unsupported block-info compression: {bundle.flags & 0x3F}")
    for i, block in enumerate(bundle.blocks):
        if block.flags & ~0x7F or block.flags & 0x3F not in (0, 2, 3):
            raise ValueError(f"unsupported flags on storage block {i}: {block.flags:#x}")
    if len(bundle.raw_blocks) != len(bundle.blocks):
        raise ValueError("parsed storage block count mismatch")

    patch_end = logical_offset + len(expected_bytes)
    if patch_end > len(bundle.data):
        raise ValueError("patch range extends beyond logical bundle data")
    covered_nodes = [node for node in bundle.nodes
                     if logical_offset >= node.offset and patch_end <= node.offset + node.size]
    if len(covered_nodes) != 1:
        raise ValueError(f"patch range must be inside exactly one node; found {len(covered_nodes)}")
    actual = bundle.data[logical_offset:patch_end]
    if actual != expected_bytes:
        raise ValueError(f"expected bytes mismatch at logical offset {logical_offset:#x}: found {actual.hex()}")

    patched_data = bytearray(bundle.data)
    patched_data[logical_offset:patch_end] = replacement_bytes
    data_changed = expected_bytes != replacement_bytes
    new_blocks = []
    output_payloads = []
    changed_blocks = []
    logical_cursor = 0
    for index, block in enumerate(bundle.blocks):
        block_end = logical_cursor + block.uncompressed_size
        block_data = bytes(patched_data[logical_cursor:block_end])
        touched = data_changed and logical_offset < block_end and patch_end > logical_cursor
        if touched:
            new_flags = block.flags & ~0x3F
            output_payloads.append(block_data)
            changed_blocks.append(index)
            new_blocks.append(Block(block.uncompressed_size, len(block_data), new_flags))
        else:
            payload = bundle.raw_blocks[index]
            output_payloads.append(payload)
            new_blocks.append(Block(block.uncompressed_size, block.compressed_size, block.flags))
        logical_cursor = block_end
    if logical_cursor != len(bundle.data):
        raise ValueError("storage blocks do not cover the complete logical stream")

    # UnityFS's 16-byte field is named uncompressedDataHash by common readers.
    # UnityPy's writer zero-initializes it when rebuilding a bundle:
    # https://github.com/K0lb3/UnityPy/blob/master/UnityPy/files/BundleFile.py
    new_info = _build_blocks_info(bytes(16), new_blocks, bundle.nodes)
    new_archive_flags = bundle.flags & ~0x3F  # info block is stored uncompressed
    header_prefix = raw[:bundle.header_fields_offset]
    output = bytearray(header_prefix)
    output.extend(struct.pack(">QIII", 0, len(new_info), len(new_info), new_archive_flags))
    while len(output) % 16:
        output.append(0)
    output.extend(new_info)
    output.extend(bytes(align16(len(output)) - len(output)))
    for payload in output_payloads:
        output.extend(payload)
    struct.pack_into(">QIII", output, bundle.header_fields_offset,
                     len(output), len(new_info), len(new_info), new_archive_flags)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("xb") as destination:
        destination.write(output)
    return {
        "output": str(output_path),
        "logical_offset": logical_offset,
        "length": len(expected_bytes),
        "changed_blocks": changed_blocks,
        "block_count": len(new_blocks),
        "archive_flags": new_archive_flags,
        "content_hash": bytes(16).hex(),
        "file_size": len(output),
    }
