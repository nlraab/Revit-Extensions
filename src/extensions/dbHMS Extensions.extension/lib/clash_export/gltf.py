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
import os
import shutil
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
    """Build and write a .glb to `path`. Returns the number of bytes written.

    In-memory: fine for small/medium exports and the test suite. For a whole
    building, use GlbWriter, which streams geometry to disk."""
    data = build_glb(meshes, asset_extras=asset_extras)
    with open(path, 'wb') as f:
        f.write(bytes(data))
    return len(data)


class GlbWriter(object):
    """Streaming .glb writer for whole-model exports.

    Each Mesh's geometry bytes go straight to a temp binary file as it's
    added, so peak memory stays bounded by the largest single mesh plus the
    (comparatively small) JSON metadata - instead of holding every Mesh AND
    the full binary buffer in RAM at once (which would blow up on a large
    building). Produces byte-identical output to build_glb for the same
    meshes.

    Usage:
        w = GlbWriter(path, asset_extras=...)
        for mesh in meshes:
            w.add(mesh)
        size = w.finalize()
    """

    def __init__(self, path, asset_extras=None):
        self.path = path
        self.asset_extras = asset_extras
        self._tmp = path + ".bin.tmp"
        self._bin = open(self._tmp, 'wb')
        self._bin_len = 0
        self._accessors = []
        self._buffer_views = []
        self._materials = []
        self._meshes = []
        self._nodes = []
        self._scene_nodes = []
        self._finalized = False

    def _write_bin(self, b):
        self._bin.write(bytes(b))
        self._bin_len += len(b)

    def _pad_bin(self):
        pad = _pad4(self._bin_len)
        if pad:
            self._bin.write(b'\x00' * pad)
            self._bin_len += pad

    def add(self, mesh):
        """Append one Mesh (no-op for an empty mesh)."""
        if not mesh.positions:
            return

        pos_offset = self._bin_len
        self._write_bin(_pack_floats(mesh.positions))
        pos_len = self._bin_len - pos_offset
        self._pad_bin()
        pos_view = len(self._buffer_views)
        self._buffer_views.append({
            "buffer": 0, "byteOffset": pos_offset, "byteLength": pos_len,
            "target": _ARRAY_BUFFER,
        })
        mn, mx = mesh.bounds()
        pos_accessor = len(self._accessors)
        self._accessors.append({
            "bufferView": pos_view, "componentType": _FLOAT,
            "count": mesh.vertex_count, "type": "VEC3", "min": mn, "max": mx,
        })
        primitive = {"attributes": {"POSITION": pos_accessor}, "mode": _TRIANGLES}

        if mesh.indices:
            idx_offset = self._bin_len
            self._write_bin(_pack_uints(mesh.indices))
            idx_len = self._bin_len - idx_offset
            self._pad_bin()
            idx_view = len(self._buffer_views)
            self._buffer_views.append({
                "buffer": 0, "byteOffset": idx_offset, "byteLength": idx_len,
                "target": _ELEMENT_ARRAY_BUFFER,
            })
            idx_accessor = len(self._accessors)
            self._accessors.append({
                "bufferView": idx_view, "componentType": _UNSIGNED_INT,
                "count": len(mesh.indices), "type": "SCALAR",
            })
            primitive["indices"] = idx_accessor

        r, g, b = mesh.color
        mat_index = len(self._materials)
        self._materials.append({
            "pbrMetallicRoughness": {
                "baseColorFactor": [r, g, b, 1.0],
                "metallicFactor": 0.0, "roughnessFactor": 0.85,
            },
            "doubleSided": True,
        })
        primitive["material"] = mat_index

        mesh_index = len(self._meshes)
        self._meshes.append({"primitives": [primitive]})
        node = {"mesh": mesh_index}
        if mesh.metadata:
            node["extras"] = mesh.metadata
        self._scene_nodes.append(len(self._nodes))
        self._nodes.append(node)

    def finalize(self):
        """Assemble the final .glb from the JSON metadata + streamed binary.
        Returns the total byte length. Safe to call once."""
        if self._finalized:
            return None
        self._finalized = True
        final_pad = _pad4(self._bin_len)
        self._bin.close()

        asset = {"version": "2.0", "generator": GENERATOR}
        if self.asset_extras:
            asset["extras"] = self.asset_extras
        gltf = {
            "asset": asset, "scene": 0,
            "scenes": [{"nodes": self._scene_nodes}],
            "nodes": self._nodes, "meshes": self._meshes,
            "materials": self._materials, "accessors": self._accessors,
            "bufferViews": self._buffer_views,
            "buffers": [{"byteLength": self._bin_len}],
        }
        json_text = json.dumps(gltf, ensure_ascii=False,
                               separators=(',', ':'), sort_keys=True)
        json_bytes = bytearray(json_text.encode('utf-8'))
        json_bytes.extend(b' ' * _pad4(len(json_bytes)))

        bin_chunk_len = self._bin_len + final_pad
        total = 12 + 8 + len(json_bytes) + 8 + bin_chunk_len
        with open(self.path, 'wb') as out:
            out.write(struct.pack('<III', _GLB_MAGIC, _GLB_VERSION, total))
            out.write(struct.pack('<II', len(json_bytes), _CHUNK_JSON))
            out.write(bytes(json_bytes))
            out.write(struct.pack('<II', bin_chunk_len, _CHUNK_BIN))
            with open(self._tmp, 'rb') as b:
                shutil.copyfileobj(b, out)
            if final_pad:
                out.write(b'\x00' * final_pad)
        self._remove_tmp()
        return total

    def close(self):
        """Abort cleanup - close the temp stream and remove it. Call on error
        paths so a failed export doesn't leave a .bin.tmp behind."""
        try:
            if not self._bin.closed:
                self._bin.close()
        except Exception:
            pass
        self._remove_tmp()

    def _remove_tmp(self):
        try:
            if os.path.exists(self._tmp):
                os.remove(self._tmp)
        except OSError:
            pass
