"""Contract tests for safe provider asset normalization and transport."""

from __future__ import annotations

import base64
import json
import struct
from io import BytesIO

import pytest
from PIL import Image

from orchestrator.adapters.assets.gltf import pack_gltf


def _png_fixture() -> bytes:
    stream = BytesIO()
    Image.new("RGBA", (2, 2), (20, 40, 60, 255)).save(stream, format="PNG")
    return stream.getvalue()


def _parse_glb(data: bytes) -> tuple[dict, bytes]:
    magic, version, total = struct.unpack_from("<4sII", data)
    assert magic == b"glTF" and version == 2 and total == len(data)
    offset = 12
    json_size, json_type = struct.unpack_from("<II", data, offset)
    assert json_type == 0x4E4F534A
    document = json.loads(data[offset + 8 : offset + 8 + json_size].rstrip(b" \0"))
    offset += 8 + json_size
    binary = b""
    if offset < len(data):
        binary_size, binary_type = struct.unpack_from("<II", data, offset)
        assert binary_type == 0x004E4942
        binary = data[offset + 8 : offset + 8 + binary_size]
        assert len(binary) == binary_size
        offset += 8 + binary_size
    assert offset == len(data)
    return document, binary


def test_packer_embeds_multiple_buffers_images_and_preserves_document_metadata() -> None:
    document = {
        "asset": {"version": "2.0", "generator": "synthetic fixture"},
        "buffers": [{"uri": "mesh.bin", "byteLength": 3}, {"uri": "extra.bin", "byteLength": 2}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": 3},
            {"buffer": 1, "byteOffset": 0, "byteLength": 2},
        ],
        "images": [{"uri": "textures/albedo.png", "mimeType": "image/png"}],
        "materials": [{"name": "paint", "pbrMetallicRoughness": {"metallicFactor": 0.2}}],
        "animations": [{"name": "idle", "channels": [], "samplers": []}],
        "meshes": [{"primitives": [{"attributes": {}, "material": 0}]}],
    }
    png_bytes = _png_fixture()
    packed = pack_gltf(
        document,
        {"mesh.bin": b"abc", "extra.bin": b"de", "textures/albedo.png": png_bytes},
        max_bytes=4096,
    )
    result, binary = _parse_glb(packed)
    assert result["asset"] == document["asset"]
    assert result["materials"] == document["materials"]
    assert result["animations"] == document["animations"]
    assert result["meshes"] == document["meshes"]
    declared_length = result["buffers"][0]["byteLength"]
    assert len(result["buffers"]) == 1
    assert [view["byteOffset"] for view in result["bufferViews"][:2]] == [0, 4]
    assert all(
        view["byteOffset"] + view["byteLength"] <= declared_length
        for view in result["bufferViews"]
    ), "every bufferView, including embedded images, must fit the declared BIN length"
    assert declared_length <= len(binary) <= declared_length + 3
    image_view = result["bufferViews"][result["images"][0]["bufferView"]]
    assert binary[image_view["byteOffset"]:image_view["byteOffset"] + image_view["byteLength"]] == png_bytes
    assert binary[:3] == b"abc" and binary[4:6] == b"de"
    assert "uri" not in result["images"][0]
    assert result["images"][0]["mimeType"] == "image/png"
    assert result["images"][0]["bufferView"] < len(result["bufferViews"])


def test_packer_supports_bounded_data_uris() -> None:
    png_bytes = _png_fixture()
    doc = {
        "asset": {"version": "2.0"},
        "buffers": [{"uri": "data:application/octet-stream;base64,YWJj", "byteLength": 3}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": 3}],
        "images": [{"uri": "data:image/png;base64," + base64.b64encode(png_bytes).decode()}],
    }
    packed = pack_gltf(doc, {}, max_bytes=1024)
    normalized, binary = _parse_glb(packed)
    assert binary.startswith(b"abc")
    assert "uri" not in normalized["images"][0]
    assert normalized["images"][0]["mimeType"] == "image/png"
    image_view = normalized["bufferViews"][normalized["images"][0]["bufferView"]]
    assert binary[image_view["byteOffset"]:image_view["byteOffset"] + image_view["byteLength"]] == png_bytes


def test_identical_source_buffers_keep_distinct_views_and_offsets() -> None:
    doc = {
        "asset": {"version": "2.0"},
        "buffers": [
            {"uri": "same.bin", "byteLength": 2},
            {"uri": "same.bin", "byteLength": 2},
        ],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": 2},
            {"buffer": 1, "byteOffset": 0, "byteLength": 2},
        ],
    }
    normalized, binary = _parse_glb(pack_gltf(doc, {"same.bin": b"ab"}, max_bytes=1024))
    views = normalized["bufferViews"]
    assert [view["byteOffset"] for view in views] == [0, 4]
    assert binary[:2] == binary[4:6] == b"ab"


@pytest.mark.parametrize(
    "uri",
    ["missing.bin", "../outside.bin", r"..\outside.bin", "/absolute.bin", "C:/drive.bin", "%2e%2e/outside.bin", "https://example.invalid/a"],
)
def test_packer_refuses_unresolved_or_escaping_references(uri: str) -> None:
    doc = {"asset": {"version": "2.0"}, "buffers": [{"uri": uri, "byteLength": 1}]}
    with pytest.raises(ValueError):
        pack_gltf(doc, {}, max_bytes=1024)


def test_packer_enforces_output_cap() -> None:
    doc = {"asset": {"version": "2.0"}, "buffers": [{"uri": "mesh.bin", "byteLength": 8}]}
    with pytest.raises(ValueError):
        pack_gltf(doc, {"mesh.bin": b"12345678"}, max_bytes=24)


@pytest.mark.parametrize("image", [
    {"bufferView": 99, "mimeType": "image/png"},
    {"bufferView": -1, "mimeType": "image/png"},
])
def test_packer_rejects_out_of_range_image_buffer_view(image: dict) -> None:
    doc = {
        "asset": {"version": "2.0"},
        "buffers": [{"uri": "mesh.bin", "byteLength": 1}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": 1}],
        "images": [image],
    }
    with pytest.raises(ValueError):
        pack_gltf(doc, {"mesh.bin": b"x"}, max_bytes=1024)


def test_packer_checks_cap_while_combining_repeated_resources() -> None:
    doc = {
        "asset": {"version": "2.0"},
        "buffers": [
            {"uri": "same.bin", "byteLength": 8},
            {"uri": "same.bin", "byteLength": 8},
        ],
    }
    with pytest.raises(ValueError):
        pack_gltf(doc, {"same.bin": b"12345678"}, max_bytes=64)


def test_packer_does_not_modify_input_document() -> None:
    doc = {"asset": {"version": "2.0"}, "buffers": [{"uri": "b.bin", "byteLength": 1}]}
    before = json.dumps(doc, sort_keys=True)
    pack_gltf(doc, {"b.bin": b"x"}, max_bytes=1024)
    assert json.dumps(doc, sort_keys=True) == before

