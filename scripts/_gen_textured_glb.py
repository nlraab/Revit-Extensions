# -*- coding: utf-8 -*-
"""Throwaway: build a textured .glb (one box + UVs + an embedded checker PNG data URI)
using the REAL GlbWriter/Mesh, to prove the texture pipeline end-to-end in viewer3.html
via the preview. Writes web/test_tex.glb. Not shipped; delete after."""
import base64
import os
import struct
import sys
import zlib

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(REPO, "src", "extensions", "dbHMS Extensions.extension", "lib")
sys.path.insert(0, LIB)
from clash_export.mesh import Mesh
from clash_export.gltf import GlbWriter

WEB = os.path.join(REPO, "src", "extensions", "dbHMS Extensions.extension",
                   "dbHMS Tools.tab", "Clash Detection.panel", "3D Viewer.pushbutton", "web")


def checker_png_datauri(n=64, cells=8):
    """An n x n RGB checker PNG as a data: URI (no external file needed)."""
    rows = []
    for y in range(n):
        row = bytearray()
        for x in range(n):
            on = ((x * cells // n) + (y * cells // n)) % 2 == 0
            row += (b'\xcc\x55\x33' if on else b'\xee\xe6\xda')  # brick-red / mortar
        rows.append(bytes(row))
    raw = b''.join(b'\x00' + r for r in rows)

    def chunk(typ, data):
        return (struct.pack('>I', len(data)) + typ + data +
                struct.pack('>I', zlib.crc32(typ + data) & 0xffffffff))
    sig = b'\x89PNG\r\n\x1a\n'
    ihdr = struct.pack('>IIBBBBB', n, n, 8, 2, 0, 0, 0)   # 8-bit RGB
    png = sig + chunk(b'IHDR', ihdr) + chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b'')
    return 'data:image/png;base64,' + base64.b64encode(png).decode('ascii')


def main():
    s = 1.0
    c = [(-s, -s, -s), (s, -s, -s), (s, s, -s), (-s, s, -s),
         (-s, -s, s), (s, -s, s), (s, s, s), (-s, s, s)]
    pos, nrm, uv = [], [], []
    import math
    for (x, y, z) in c:
        pos += [x, y, z]
        l = math.sqrt(x*x + y*y + z*z)
        nrm += [x/l, y/l, z/l]
        uv += [(x + s) / (2*s), (z + s) / (2*s)]   # simple planar UVs (enough to show mapping)
    faces = [(0,1,2),(0,2,3),(4,6,5),(4,7,6),(0,4,5),(0,5,1),
             (1,5,6),(1,6,2),(2,6,7),(2,7,3),(3,7,4),(3,4,0)]
    idx = []
    for f in faces:
        idx += list(f)
    w = GlbWriter(os.path.join(WEB, "test_tex.glb"))
    w.add(Mesh(positions=pos, indices=idx, normals=nrm, uvs=uv,
               texture=checker_png_datauri(), color=(1, 1, 1),
               metadata={"element_id": 1, "category": "Walls", "model": "ARCH.rvt", "name": "Brick wall"}))
    size = w.finalize()
    print("wrote test_tex.glb:", size, "bytes (1 textured box)")


if __name__ == "__main__":
    main()
