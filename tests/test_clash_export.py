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
    # indices may be narrowed to UNSIGNED_SHORT (5123) when the mesh fits in 16 bits
    fmt = 'H' if acc["componentType"] == 5123 else 'I'
    return list(struct.unpack_from('<%d%s' % (n, fmt), bin_bytes, offset))


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

    def test_small_mesh_indices_narrowed_to_ushort(self):
        # a 4-vertex mesh fits in 16 bits -> indices stored as UNSIGNED_SHORT (2 bytes)
        prim1 = self.gltf["meshes"][1]["primitives"][0]
        acc = self.gltf["accessors"][prim1["indices"]]
        self.assertEqual(acc["componentType"], 5123)  # UNSIGNED_SHORT

    def test_buffer_length_matches(self):
        # buffers[0].byteLength is the unpadded content length; the BIN chunk
        # may be padded up to a 4-byte boundary, so chunk >= declared.
        declared = self.gltf["buffers"][0]["byteLength"]
        self.assertGreaterEqual(len(self.bin), declared)
        self.assertEqual(len(self.bin) % 4, 0)


class TextureTests(unittest.TestCase):
    def _make(self, **kw):
        m = Mesh(positions=[0, 0, 0, 1, 0, 0, 0, 1, 0], **kw)
        return _parse_glb(gltf.build_glb([m]))

    def test_uvs_emit_texcoord0_accessor(self):
        g, b = self._make(uvs=[0, 0, 1, 0, 0, 1])
        prim = g["meshes"][0]["primitives"][0]
        self.assertIn("TEXCOORD_0", prim["attributes"])
        acc = g["accessors"][prim["attributes"]["TEXCOORD_0"]]
        self.assertEqual(acc["type"], "VEC2")
        self.assertEqual(acc["count"], 3)
        vals = list(struct.unpack_from('<6f', b, g["bufferViews"][acc["bufferView"]].get("byteOffset", 0)))
        self.assertEqual(vals, [0, 0, 1, 0, 0, 1])

    def test_texture_emits_image_sampler_and_basecolortexture(self):
        g, _ = self._make(uvs=[0, 0, 1, 0, 0, 1], texture="tex/brick.png")
        self.assertEqual(g["images"][0]["uri"], "tex/brick.png")
        self.assertEqual(len(g["samplers"]), 1)
        self.assertEqual(g["textures"][0]["source"], 0)
        prim = g["meshes"][0]["primitives"][0]
        pbr = g["materials"][prim["material"]]["pbrMetallicRoughness"]
        self.assertEqual(pbr["baseColorTexture"]["index"], 0)

    def test_untextured_mesh_has_no_texture_arrays(self):
        g, _ = self._make()
        self.assertNotIn("images", g)
        self.assertNotIn("textures", g)
        prim = g["meshes"][0]["primitives"][0]
        self.assertNotIn("baseColorTexture", g["materials"][prim["material"]]["pbrMetallicRoughness"])

    def test_shared_texture_is_deduped(self):
        a = Mesh(positions=[0, 0, 0, 1, 0, 0, 0, 1, 0], uvs=[0, 0, 1, 0, 0, 1], texture="t.png")
        b = Mesh(positions=[0, 0, 1, 1, 0, 1, 0, 1, 1], uvs=[0, 0, 1, 0, 0, 1], texture="t.png")
        g, _ = _parse_glb(gltf.build_glb([a, b]))
        self.assertEqual(len(g["images"]), 1)      # same uri -> one image
        self.assertEqual(len(g["textures"]), 1)

    def test_writer_matches_build_glb_with_textures(self):
        meshes = [Mesh(positions=[0, 0, 0, 1, 0, 0, 0, 1, 0], uvs=[0, 0, 1, 0, 0, 1],
                       texture="data:image/png;base64,AAAA", color=(0.2, 0.4, 0.6))]
        ref = bytes(gltf.build_glb(meshes))
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "t.glb")
            w = gltf.GlbWriter(path)
            for m in meshes:
                w.add(m)
            w.finalize()
            with open(path, 'rb') as f:
                self.assertEqual(f.read(), ref)   # streaming path byte-identical
        finally:
            try: os.remove(os.path.join(tmp, "t.glb"))
            except OSError: pass
            os.rmdir(tmp)


class TextureExtractionTests(unittest.TestCase):
    """The Revit-free parts of texture extraction: path handling, data-URI encoding,
    and the appearance-asset tree walk (mocked). The Revit API calls themselves are
    lazy-imported and only run inside Revit."""

    def test_looks_like_image(self):
        self.assertTrue(revit_geometry._looks_like_image("Brick.PNG"))
        self.assertTrue(revit_geometry._looks_like_image("a/b/c.jpg"))
        self.assertFalse(revit_geometry._looks_like_image("notes.txt"))
        self.assertFalse(revit_geometry._looks_like_image(None))

    def test_first_image_path_picks_first_image(self):
        self.assertEqual(revit_geometry._first_image_path("a.png|b.jpg"), "a.png")
        self.assertEqual(revit_geometry._first_image_path("x.txt|b.jpg"), "b.jpg")
        self.assertIsNone(revit_geometry._first_image_path("x.txt|y.dat"))

    def test_file_to_datauri_roundtrips(self):
        import base64
        tmp = tempfile.mkdtemp()
        try:
            p = os.path.join(tmp, "t.png")
            payload = b"\x89PNG\r\n\x1a\nsome-bytes"
            with open(p, "wb") as f:
                f.write(payload)
            uri = revit_geometry._file_to_datauri(p)
            self.assertTrue(uri.startswith("data:image/png;base64,"))
            self.assertEqual(base64.b64decode(uri.split(",", 1)[1]), payload)
        finally:
            try: os.remove(os.path.join(tmp, "t.png"))
            except OSError: pass
            os.rmdir(tmp)

    def test_file_to_datauri_skips_oversize(self):
        tmp = tempfile.mkdtemp()
        try:
            p = os.path.join(tmp, "big.png")
            with open(p, "wb") as f:
                f.write(b"\x00" * (revit_geometry._MAX_TEX_BYTES + 1))
            self.assertIsNone(revit_geometry._file_to_datauri(p))
        finally:
            try: os.remove(os.path.join(tmp, "big.png"))
            except OSError: pass
            os.rmdir(tmp)

    def test_resolve_absolute_existing_path(self):
        tmp = tempfile.mkdtemp()
        try:
            p = os.path.join(tmp, "wall.jpg")
            with open(p, "wb") as f:
                f.write(b"x")
            self.assertEqual(revit_geometry._resolve_texture_path(p), p)
            self.assertIsNone(revit_geometry._resolve_texture_path("nope/missing.jpg"))
        finally:
            try: os.remove(os.path.join(tmp, "wall.jpg"))
            except OSError: pass
            os.rmdir(tmp)

    def test_find_bitmap_path_walks_connected_asset(self):
        # mock the Revit tree: a diffuse slot -> connected UnifiedBitmap asset ->
        # a *bitmap* string property holding the file path
        class P(object):
            def __init__(self, name="", value=None, members=None, connected=None):
                self.Name = name; self._v = value
                self._members = members; self._c = connected or []
            @property
            def Value(self):
                if self._v is None: raise AttributeError("no value")
                return self._v
            @property
            def Size(self):
                if self._members is None: raise AttributeError("not a collection")
                return len(self._members)
            def Get(self, i): return self._members[i]
            @property
            def NumberOfConnectedProperties(self): return len(self._c)
            def GetConnectedProperty(self, j): return self._c[j]
        bitmap = P(name="unifiedbitmap_Bitmap", value="1\\Mats\\brick.png")
        ubasset = P(members=[bitmap])
        diffuse = P(name="generic_diffuse", connected=[ubasset])
        root = P(members=[diffuse])
        self.assertEqual(revit_geometry._find_bitmap_path(root), "1\\Mats\\brick.png")

    def test_find_bitmap_path_none_when_no_bitmap(self):
        class P(object):
            def __init__(self, name="", members=None):
                self.Name = name; self._members = members
            @property
            def Size(self):
                if self._members is None: raise AttributeError
                return len(self._members)
            def Get(self, i): return self._members[i]
            @property
            def NumberOfConnectedProperties(self): return 0
        root = P(members=[P(name="generic_diffuse"), P(name="common_Tint")])
        self.assertIsNone(revit_geometry._find_bitmap_path(root))

    def _slot_with_bitmap(self, slot_name, path):
        class P(object):
            def __init__(self, name="", value=None, members=None, connected=None):
                self.Name = name; self._v = value
                self._members = members; self._c = connected or []
            @property
            def Value(self):
                if self._v is None: raise AttributeError("no value")
                return self._v
            @property
            def Size(self):
                if self._members is None: raise AttributeError("not a collection")
                return len(self._members)
            def Get(self, i): return self._members[i]
            @property
            def NumberOfConnectedProperties(self): return len(self._c)
            def GetConnectedProperty(self, j): return self._c[j]
        bm = P(name="unifiedbitmap_Bitmap", value=path)
        return P(name=slot_name, connected=[P(members=[bm])]), P

    def test_find_bitmap_path_prefers_diffuse_over_bump(self):
        # bump slot listed FIRST, diffuse second -> must still pick the diffuse colour map
        bump, P = self._slot_with_bitmap("generic_bump_map", "bump.png")
        diffuse, _ = self._slot_with_bitmap("generic_diffuse", "cream.jpg")
        root = P(members=[bump, diffuse])
        self.assertEqual(revit_geometry._find_bitmap_path(root), "cream.jpg")

    def test_find_bitmap_path_skips_lone_bump_map(self):
        # only a bump map -> return None rather than paint a bump map on as base colour
        bump, P = self._slot_with_bitmap("generic_bump_map", "bump.png")
        root = P(members=[bump])
        self.assertIsNone(revit_geometry._find_bitmap_path(root))

    def test_find_bitmap_path_skips_cmu_pattern_map(self):
        # CMU: solid base colour + a relief/pattern map -> don't use the pattern as colour
        pat, P = self._slot_with_bitmap("masonrycmu_pattern_map", "blockgrid.png")
        root = P(members=[pat])
        self.assertIsNone(revit_geometry._find_bitmap_path(root))


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

    def test_large_mesh_indices_stay_uint(self):
        # a mesh with > 65536 vertices can't use 16-bit indices -> UNSIGNED_INT
        big = Mesh(positions=[0.0] * (3 * 65537), indices=[0, 65536, 1])
        data = gltf.build_glb([big])
        gltf_obj, _ = _parse_glb(data)
        prim = gltf_obj["meshes"][0]["primitives"][0]
        acc = gltf_obj["accessors"][prim["indices"]]
        self.assertEqual(acc["componentType"], 5125)  # UNSIGNED_INT

    def test_index_narrowing_roundtrips_values(self):
        # narrowed (ushort) indices must still decode to the exact input values
        m = Mesh(positions=[0, 0, 0, 1, 0, 0, 0, 1, 0, 1, 1, 0],
                 indices=[0, 1, 2, 0, 2, 3])
        gltf_obj, bin_bytes = _parse_glb(gltf.build_glb([m]))
        prim = gltf_obj["meshes"][0]["primitives"][0]
        self.assertEqual(_read_accessor_uints(gltf_obj, bin_bytes, prim["indices"]),
                         [0, 1, 2, 0, 2, 3])

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


class GlbWriterTests(unittest.TestCase):
    """The streaming writer must match the in-memory writer byte-for-byte."""

    def _meshes(self):
        return [
            Mesh(positions=[0, 0, 0, 1, 0, 0, 0, 1, 0],
                 color=(0.17, 0.42, 0.69), metadata={"element_id": 111}),
            Mesh(positions=[0, 0, 0, 2, 0, 0, 2, 2, 0, 0, 2, 0],
                 indices=[0, 1, 2, 0, 2, 3], color=(0.0, 1.0, 0.0),
                 metadata={"element_id": 222, "discipline": "Mechanical"}),
        ]

    def test_matches_build_glb_bytes(self):
        meshes = self._meshes()
        ref = bytes(gltf.build_glb(meshes, asset_extras={"units": "meters"}))
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "stream.glb")
        try:
            w = gltf.GlbWriter(path, asset_extras={"units": "meters"})
            for m in meshes:
                w.add(m)
            size = w.finalize()
            with open(path, 'rb') as f:
                data = f.read()
            self.assertEqual(size, len(data))
            self.assertEqual(data, ref)            # byte-identical
            self.assertFalse(os.path.exists(path + ".bin.tmp"))   # temp cleaned
        finally:
            for fn in (path, path + ".bin.tmp"):
                try:
                    os.remove(fn)
                except OSError:
                    pass
            os.rmdir(tmp)

    def test_skips_empty_mesh(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "s.glb")
        try:
            w = gltf.GlbWriter(path)
            w.add(Mesh(positions=[]))
            w.add(Mesh(positions=[0, 0, 0, 1, 0, 0, 0, 1, 0]))
            w.finalize()
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
