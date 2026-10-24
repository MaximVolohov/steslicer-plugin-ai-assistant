"""Generate small test models (binary STL) for the smoke tests.

    python make_samples.py            -> dev/samples/*.stl

tube_side.stl   hollow cylinder Ø30/Ø12 x 40 mm lying on its side (axis along X):
                a body of revolution -> the analyzer should suggest 5D and an upright axis.
bracket.stl     L-shaped bracket 50 x 30 x 40 mm, 5 mm walls -> a classic 3D part.
"""
import math
import os
import struct

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "samples")


def write_stl(path, triangles):
    with open(path, "wb") as stream:
        stream.write(b"STE AI Assistant sample".ljust(80, b" "))
        stream.write(struct.pack("<I", len(triangles)))
        for a, b, c in triangles:
            ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
            vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
            n = (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)
            length = math.sqrt(sum(x * x for x in n)) or 1.0
            stream.write(struct.pack("<3f", *(x / length for x in n)))
            for vertex in (a, b, c):
                stream.write(struct.pack("<3f", *vertex))
            stream.write(b"\0\0")


def tube(outer=15.0, inner=6.0, length=40.0, segments=64):
    """Axis along Z from 0 to length; returned triangles are CCW seen from outside."""
    tris = []
    for i in range(segments):
        a0 = 2 * math.pi * i / segments
        a1 = 2 * math.pi * (i + 1) / segments
        o0 = (outer * math.cos(a0), outer * math.sin(a0))
        o1 = (outer * math.cos(a1), outer * math.sin(a1))
        i0 = (inner * math.cos(a0), inner * math.sin(a0))
        i1 = (inner * math.cos(a1), inner * math.sin(a1))
        # outer wall
        tris.append(((o0[0], o0[1], 0), (o1[0], o1[1], 0), (o1[0], o1[1], length)))
        tris.append(((o0[0], o0[1], 0), (o1[0], o1[1], length), (o0[0], o0[1], length)))
        # inner wall (normals point to the axis)
        tris.append(((i0[0], i0[1], 0), (i1[0], i1[1], length), (i1[0], i1[1], 0)))
        tris.append(((i0[0], i0[1], 0), (i0[0], i0[1], length), (i1[0], i1[1], length)))
        # top ring
        tris.append(((o0[0], o0[1], length), (o1[0], o1[1], length), (i1[0], i1[1], length)))
        tris.append(((o0[0], o0[1], length), (i1[0], i1[1], length), (i0[0], i0[1], length)))
        # bottom ring
        tris.append(((o0[0], o0[1], 0), (i1[0], i1[1], 0), (o1[0], o1[1], 0)))
        tris.append(((o0[0], o0[1], 0), (i0[0], i0[1], 0), (i1[0], i1[1], 0)))
    return tris


def lay_on_side(tris):
    """Rotate so the Z axis becomes the X axis: (x, y, z) -> (z, y, -x), then shift to z >= 0."""
    rotated = [tuple((v[2], v[1], -v[0]) for v in t) for t in tris]
    min_z = min(v[2] for t in rotated for v in t)
    return [tuple((v[0], v[1], v[2] - min_z) for v in t) for t in rotated]


def extrude(polygon, cap_triangles, depth):
    """Prism: polygon (CCW in XZ, as (x, z)) extruded along +Y by depth."""
    tris = []
    for (a, b, c) in cap_triangles:
        pa, pb, pc = polygon[a], polygon[b], polygon[c]
        # front cap at y=0 (outward normal -Y) and back cap at y=depth (+Y)
        tris.append(((pa[0], 0, pa[1]), (pb[0], 0, pb[1]), (pc[0], 0, pc[1])))
        tris.append(((pa[0], depth, pa[1]), (pc[0], depth, pc[1]), (pb[0], depth, pb[1])))
    count = len(polygon)
    for i in range(count):
        p, q = polygon[i], polygon[(i + 1) % count]
        tris.append(((p[0], 0, p[1]), (q[0], depth, q[1]), (q[0], 0, q[1])))
        tris.append(((p[0], 0, p[1]), (p[0], depth, p[1]), (q[0], depth, q[1])))
    return tris


def bracket():
    polygon = [(0, 0), (50, 0), (50, 5), (5, 5), (5, 40), (0, 40)]
    caps = [(0, 1, 2), (0, 2, 3), (0, 3, 4), (0, 4, 5)]
    return extrude(polygon, caps, 30.0)


def main():
    os.makedirs(OUT, exist_ok=True)
    write_stl(os.path.join(OUT, "tube_side.stl"), lay_on_side(tube()))
    write_stl(os.path.join(OUT, "bracket.stl"), bracket())
    for name in sorted(os.listdir(OUT)):
        print(os.path.join(OUT, name))


if __name__ == "__main__":
    main()
