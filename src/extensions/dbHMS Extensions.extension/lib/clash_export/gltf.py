# -*- coding: utf-8 -*-
"""Minimal glTF 2.0 binary (.glb) writer.

Pure data: standard library only (json, struct). Python-2-safe so it runs
in IronPython 2.7 inside Revit, and unit-tested under CPython 3.

Each Mesh becomes one node -> one mesh -> one primitive -> one material,
all sharing a single binary buffer (the .glb BIN chunk). Positions are
FLOAT VEC3; indices, when present, are UNSIGNED_INT SCALAR. Per-element
metadata rides along in each node's 'extras'. The result loads natively in
three.js (GLTFLoader) and in Windows 3D Viewer.
"""

import json
import struct


GENERATOR = "dbHMS 3D Viewer"

# glTF / WebGL constants
_FLOAT = 5126
_UNSIGNED_INT = 5125
_ARRAY_BUFFER = 34962
_ELEMENT_ARRAY_BUFFER = 34963
_TRIANGLES = 4

# .glb container constants
_GLB_MAGIC = 0x46546C67   # "glTF"
_GLB_VERSION = 2
_CHUNK_JSON = 0x4E4F534A  # "JSON"
_CHUNK_BIN = 0x004E4942   # "BIN\0"

# Pack big arrays in bounded chunks so we never blow the argument limit on
# struct.pack(fmt, *values) for large meshes.
_PACK_CHUNK = 4096


def _pad4(n):
    """Bytes needed to round `n` up to a multiple of 4."""
    return (4 - (n % 4)) % 4


def _pack_floats(values):
    out = bytearray()
    n = len(values)
    i = 0
    while i < n:
        chunk = values[i:i + _PACK_CHUNK]
        out.extend(struct.pack('<%df' % len(chunk), *chunk))
        i += _PACK_CHUNK
    return out


def _pack_uints(values):
    out = bytearray()
    n = len(values)
    i = 0
    while i < n:
        chunk = values[i:i + _PACK_CHUNK]
        out.extend(struct.pack('<%dI' % len(chunk), *chunk))
        i += _PACK_CHUNK
    return out


def build_glb(meshes, asset_extras=None):
    """Build a complete .glb as a bytearray from a list of Mesh objects.

    Meshes with no positions are skipped. Returns a valid (possibly empty-
    scene) glb either way.
    """
    bin_buf = bytearray()
    accessors = []
    buffer_views = []
    materials = []
    gltf_meshes = []
    nodes = []
    scene_nodes = []

    for mesh in meshes:
        if not mesh.positions:
            continue

        # --- positions ---
        pos_offset = len(bin_buf)
        bin_buf.extend(_pack_floats(mesh.positions))
        pos_len = len(bin_buf) - pos_offset
        bin_buf.extend(b'\x00' * _pad4(len(bin_buf)))

        pos_view = len(buffer_views)
        buffer_views.append({
            "buffer": 0,
            "byteOffset": pos_offset,
            "byteLength": pos_len,
            "target": _ARRAY_BUFFER,
        })
        mn, mx = mesh.bounds()
        pos_accessor = len(accessors)
        accessors.append({
            "bufferView": pos_view,
            "componentType": _FLOAT,
            "count": mesh.vertex_count,
            "type": "VEC3",
            "min": mn,
            "max": mx,
        })

        primitive = {
            "attributes": {"POSITION": pos_accessor},
            "mode": _TRIANGLES,
        }

        # --- indices (optional) ---
        if mesh.indices:
            idx_offset = len(bin_buf)
            bin_buf.extend(_pack_uints(mesh.indices))
            idx_len = len(bin_buf) - idx_offset
            bin_buf.extend(b'\x00' * _pad4(len(bin_buf)))

            idx_view = len(buffer_views)
            buffer_views.append({
                "buffer": 0,
                "byteOffset": idx_offset,
                "byteLength": idx_len,
                "target": _ELEMENT_ARRAY_BUFFER,
            })
            idx_accessor = len(accessors)
            accessors.append({
                "bufferView": idx_view,
                "componentType": _UNSIGNED_INT,
                "count": len(mesh.indices),
                "type": "SCALAR",
            })
            primitive["indices"] = idx_accessor

        # --- material ---
        r, g, b = mesh.color
        mat_index = len(materials)
        materials.append({
            "pbrMetallicRoughness": {
                "baseColorFactor": [r, g, b, 1.0],
                "metallicFactor": 0.0,
                "roughnessFactor": 0.85,
            },
            "doubleSided": True,
        })
        primitive["material"] = mat_index

        mesh_index = len(gltf_meshes)
        gltf_meshes.append({"primitives": [primitive]})

        node = {"mesh": mesh_index}
        if mesh.metadata:
            node["extras"] = mesh.metadata
        scene_nodes.append(len(nodes))
        nodes.append(node)

    asset = {"version": "2.0", "generator": GENERATOR}
    if asset_extras:
        asset["extras"] = asset_extras

    gltf = {
        "asset": asset,
        "scene": 0,
        "scenes": [{"nodes": scene_nodes}],
        "nodes": nodes,
        "meshes": gltf_meshes,
        "materials": materials,
        "accessors": accessors,
        "bufferViews": buffer_views,
        "buffers": [{"byteLength": len(bin_buf)}],
    }

    # glTF JSON chunk must be UTF-8. ensure_ascii=False avoids an IronPython
    # quirk with non-ASCII escaping and gives us real UTF-8 once encoded.
    json_text = json.dumps(gltf, ensure_ascii=False,
                           separators=(',', ':'), sort_keys=True)
    json_bytes = bytearray(json_text.encode('utf-8'))
    json_bytes.extend(b' ' * _pad4(len(json_bytes)))   # JSON pads with spaces

    bin_bytes = bytearray(bin_buf)
    bin_bytes.extend(b'\x00' * _pad4(len(bin_bytes)))   # BIN pads with zeros

    total = 12 + 8 + len(json_bytes) + 8 + len(bin_bytes)
    out = bytearray()
    out.extend(struct.pack('<III', _GLB_MAGIC, _GLB_VERSION, total))
    out.extend(struct.pack('<II', len(json_bytes), _CHUNK_JSON))
    out.extend(json_bytes)
    out.extend(struct.pack('<II', len(bin_bytes), _CHUNK_BIN))
    out.extend(bin_bytes)
    return out


def write_glb(path, meshes, asset_extras=None):
    """Build and write a .glb to `path`. Returns the number of bytes written."""
    data = build_glb(meshes, asset_extras=asset_extras)
    with open(path, 'wb') as f:
        f.write(bytes(data))
    return len(data)
