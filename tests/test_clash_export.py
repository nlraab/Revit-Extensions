"""Tests for lib/clash_export (mesh.py + gltf.py).

Pure data, no Revit. Validates the Mesh counts/bounds and that the .glb
writer produces a structurally valid glTF 2.0 binary: correct container
header, parseable JSON chunk, and a BIN chunk whose bytes round-trip back
to the input positions/indices through the declared accessors.
"""

import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIB_ROOT = ROOT / "src" / "extensions" / "dbHMS Extensions.extension" / "lib"
sys.path.insert(0, str(LIB_ROOT))


from clash_export.mesh import Mesh          # noqa: E402
from clash_export import gltf               # noqa: E402
from clash_export import revit_geometry     # noqa: E402


_GLB_MAGIC = 0x46546C67
_CHUNK_JSON = 0x4E4F534A
_CHUNK_BIN = 0x004E4942


def _parse_glb(data):
    """Parse a .glb byte string into (gltf_dict, bin_bytes)."""
    data = bytes(data)
    magic, version, total = struct.unpack_from('<III', data, 0)
    assert magic == _GLB_MAGIC, "bad magic"
    assert version == 2, "bad version"
    assert total == len(data), "length field mismatch"
    off = 12
    gltf_obj = None
    bin_bytes = b''
    while off < len(data):
        clen, ctype = struct.unpack_from('<II', data, off)
        off += 8
        chunk = data[off:off + clen]
        off += clen
        if ctype == _CHUNK_JSON:
            gltf_obj = json.loads(chunk.decode('utf-8'))
        elif ctype == _CHUNK_BIN:
            bin_bytes = chunk
    return gltf_obj, bin_bytes


def _read_accessor_floats(gltf_obj, bin_bytes, accessor_index):
    acc = gltf_obj["accessors"][accessor_index]
    bv = gltf_obj["bufferViews"][acc["bufferView"]]
    offset = bv.get("byteOffset", 0)
    n = acc["count"] * 3  # VEC3
    return list(struct.unpack_from('<%df' % n, bin_bytes, offset))


def _read_accessor_uints(gltf_obj, bin_bytes, accessor_index):
    acc = gltf_obj["accessors"][accessor_index]
    bv = gltf_obj["bufferViews"][acc["bufferView"]]
    offset = bv.get("byteOffset", 0)
    n = acc["count"]
    return list(struct.unpack_from('<%dI' % n, bin_bytes, offset))


class MeshTests(unittest.TestCase):
    def test_counts_non_indexed(self):
        m = Mesh(positions=[0, 0, 0, 1, 0, 0, 0, 1, 0])  # 3 verts
        self.assertEqual(m.vertex_count, 3)
        self.assertEqual(m.triangle_count, 1)

    def test_counts_indexed(self):
        m = Mesh(positions=[0, 0, 0, 1, 0, 0, 0, 1, 0, 1, 1, 0],
                 indices=[0, 1, 2, 0, 2, 3])
        self.assertEqual(m.vertex_count, 4)
        self.assertEqual(m.triangle_count, 2)

    def test_bounds(self):
        m = Mesh(positions=[-1, -2, -3, 4, 5, 6])
        mn, mx = m.bounds()
        self.assertEqual(mn, [-1, -2, -3])
        self.assertEqual(mx, [4, 5, 6])

    def test_empty_bounds(self):
        m = Mesh(positions=[])
        self.assertEqual(m.bounds(), (None, None))


class GlbStructureTests(unittest.TestCase):
    def setUp(self):
        # One non-indexed triangle and one indexed quad, distinct colors.
        self.m1 = Mesh(
            positions=[0, 0, 0, 1, 0, 0, 0, 1, 0],
            color=(1.0, 0.0, 0.0),
            metadata={"element_id": 111, "category": "Pipe Curves"},
        )
        self.m2 = Mesh(
            positions=[0, 0, 0, 2, 0, 0, 2, 2, 0, 0, 2, 0],
            indices=[0, 1, 2, 0, 2, 3],
            color=(0.0, 1.0, 0.0),
            metadata={"element_id": 222, "discipline": "Mechanical"},
        )
        self.data = gltf.build_glb([self.m1, self.m2],
                                   asset_extras={"units": "meters"})
        self.gltf, self.bin = _parse_glb(self.data)

    def test_header_and_length(self):
        # _parse_glb asserts magic/version/length already; re-confirm length.
        self.assertEqual(struct.unpack_from('<I', bytes(self.data), 8)[0],
                         len(self.data))

    def test_node_mesh_material_counts(self):
        self.assertEqual(len(self.gltf["nodes"]), 2)
        self.assertEqual(len(self.gltf["meshes"]), 2)
        self.assertEqual(len(self.gltf["materials"]), 2)
        self.assertEqual(self.gltf["scenes"][0]["nodes"], [0, 1])

    def test_asset_extras_passthrough(self):
        self.assertEqual(self.gltf["asset"]["extras"]["units"], "meters")
        self.assertEqual(self.gltf["asset"]["version"], "2.0")

    def test_node_extras_metadata(self):
        self.assertEqual(self.gltf["nodes"][0]["extras"]["element_id"], 111)
        self.assertEqual(self.gltf["nodes"][1]["extras"]["discipline"],
                         "Mechanical")

    def test_material_colors(self):
        c0 = self.gltf["materials"][0]["pbrMetallicRoughness"]["baseColorFactor"]
        c1 = self.gltf["materials"][1]["pbrMetallicRoughness"]["baseColorFactor"]
        self.assertEqual(c0, [1.0, 0.0, 0.0, 1.0])
        self.assertEqual(c1, [0.0, 1.0, 0.0, 1.0])

    def test_primitive_indices_presence(self):
        prim0 = self.gltf["meshes"][0]["primitives"][0]
        prim1 = self.gltf["meshes"][1]["primitives"][0]
        self.assertNotIn("indices", prim0)   # m1 is non-indexed
        self.assertIn("indices", prim1)       # m2 is indexed

    def test_position_accessor_minmax(self):
        prim1 = self.gltf["meshes"][1]["primitives"][0]
        acc = self.gltf["accessors"][prim1["attributes"]["POSITION"]]
        self.assertEqual(acc["type"], "VEC3")
        self.assertEqual(acc["count"], 4)
        self.assertEqual(acc["min"], [0, 0, 0])
        self.assertEqual(acc["max"], [2, 2, 0])

    def test_positions_roundtrip(self):
        prim0 = self.gltf["meshes"][0]["primitives"][0]
        floats = _read_accessor_floats(
            self.gltf, self.bin, prim0["attributes"]["POSITION"])
        for got, want in zip(floats, self.m1.positions):
            self.assertAlmostEqual(got, want, places=5)

    def test_indices_roundtrip(self):
        prim1 = self.gltf["meshes"][1]["primitives"][0]
        idx = _read_accessor_uints(self.gltf, self.bin, prim1["indices"])
        self.assertEqual(idx, [0, 1, 2, 0, 2, 3])

    def test_buffer_length_matches(self):
        # buffers[0].byteLength is the unpadded content length; the BIN chunk
        # may be padded up to a 4-byte boundary, so chunk >= declared.
        declared = self.gltf["buffers"][0]["byteLength"]
        self.assertGreaterEqual(len(self.bin), declared)
        self.assertEqual(len(self.bin) % 4, 0)


class GlbEdgeCaseTests(unittest.TestCase):
    def test_skips_empty_mesh(self):
        good = Mesh(positions=[0, 0, 0, 1, 0, 0, 0, 1, 0])
        empty = Mesh(positions=[])
        data = gltf.build_glb([empty, good, empty])
        gltf_obj, _ = _parse_glb(data)
        self.assertEqual(len(gltf_obj["nodes"]), 1)

    def test_empty_scene(self):
        data = gltf.build_glb([])
        gltf_obj, bin_bytes = _parse_glb(data)
        self.assertEqual(gltf_obj["nodes"], [])
        self.assertEqual(gltf_obj["buffers"][0]["byteLength"], 0)

    def test_write_glb_file(self):
        m = Mesh(positions=[0, 0, 0, 1, 0, 0, 0, 1, 0])
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "out.glb")
        try:
            n = gltf.write_glb(path, [m])
            self.assertTrue(os.path.isfile(path))
            self.assertEqual(os.path.getsize(path), n)
            with open(path, 'rb') as f:
                gltf_obj, _ = _parse_glb(f.read())
            self.assertEqual(len(gltf_obj["nodes"]), 1)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
            os.rmdir(tmp)


class RoundRobinTests(unittest.TestCase):
    """_round_robin is pure logic (no Revit), so we can exercise the
    interleaving directly with stand-in 'elements'."""

    def test_interleaves_sources(self):
        host = (None, ["h1", "h2", "h3"])
        link = ("LINK", ["a1", "a2"])
        out = revit_geometry._round_robin([host, link])
        # One from each per round: h1, a1, h2, a2, h3
        self.assertEqual(out, [
            ("h1", None), ("a1", "LINK"),
            ("h2", None), ("a2", "LINK"),
            ("h3", None),
        ])

    def test_single_source(self):
        out = revit_geometry._round_robin([(None, ["x", "y"])])
        self.assertEqual(out, [("x", None), ("y", None)])

    def test_empty(self):
        self.assertEqual(revit_geometry._round_robin([]), [])
        self.assertEqual(revit_geometry._round_robin([(None, [])]), [])


if __name__ == "__main__":
    unittest.main()
