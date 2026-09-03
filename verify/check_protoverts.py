"""Checks the GLM exporter's protovert deduplication rewrite.

The original scanned every protovert created so far for each face corner (quadratic). The
replacement buckets by the two fields compared exactly - vertex index and UV - and only runs
the 0.05 normal tolerance test inside the bucket. This reproduces both algorithms on synthetic
mesh data and asserts they emit identical triangle indices, then times them.
"""
import random
import sys
import time


def original(corners):
    """Verbatim pre-patch logic."""
    protoverts = []
    triangles = []
    for v, u, n in corners:
        found = -1
        for j in range(len(protoverts)):
            proto = protoverts[j]
            if (proto[0] == v and proto[1] == u
                    and abs(proto[2][0] - n[0]) < 0.05
                    and abs(proto[2][1] - n[1]) < 0.05
                    and abs(proto[2][2] - n[2]) < 0.05):
                found = j
                break
        if found >= 0:
            triangles.append(found)
        else:
            protoverts.append((v, u, n))
            triangles.append(len(protoverts) - 1)
    return triangles, len(protoverts)


def bucketed(corners):
    """Post-patch logic."""
    protoverts = []
    buckets = {}
    triangles = []
    for v, u, n in corners:
        key = (v, u[0], u[1])
        found = -1
        for j in buckets.get(key, ()):
            proto = protoverts[j]
            if (abs(proto[2][0] - n[0]) < 0.05
                    and abs(proto[2][1] - n[1]) < 0.05
                    and abs(proto[2][2] - n[2]) < 0.05):
                found = j
                break
        if found >= 0:
            triangles.append(found)
        else:
            buckets.setdefault(key, []).append(len(protoverts))
            protoverts.append((v, u, n))
            triangles.append(len(protoverts) - 1)
    return triangles, len(protoverts)


def make_corners(rng, numVerts, numFaces, seamChance=0.15, normalSplitChance=0.1):
    """Corners resembling a real mesh: mostly shared verts, some UV seams, some split normals -
    including near-duplicate normals inside the 0.05 tolerance, which is where the two
    implementations could conceivably disagree."""
    uvs = {}
    corners = []
    for _ in range(numFaces * 3):
        v = rng.randrange(numVerts)
        if v not in uvs or rng.random() < seamChance:
            uvs.setdefault(v, []).append((rng.random(), rng.random()))
        u = rng.choice(uvs[v])
        base = (rng.uniform(-1, 1), rng.uniform(-1, 1), rng.uniform(-1, 1))
        if rng.random() < normalSplitChance:
            # deliberately within/near the 0.05 tolerance boundary
            jitter = rng.choice([0.0, 0.01, 0.049, 0.051, 0.2])
            base = (base[0] + jitter, base[1], base[2])
        corners.append((v, u, base))
    return corners


failures = []
rng = random.Random(815)

print("[1] identical output on randomised meshes")
worst = None
for trial in range(300):
    corners = make_corners(rng, rng.randint(5, 120), rng.randint(5, 200))
    a = original(corners)
    b = bucketed(corners)
    if a != b:
        worst = (trial, a, b)
        break
ok = worst is None
print(("  PASS  " if ok else "  FAIL  ")
      + ("300 meshes, triangle indices and vertex counts match exactly"
         if ok else f"diverged on trial {worst[0]}"))
if not ok:
    failures.append("identity")

print("\n[2] scaling")
print("  {:>8} {:>8} {:>12} {:>12} {:>8}".format("verts", "faces", "original s", "bucketed s", "speedup"))
for numVerts, numFaces in ((500, 1000), (2000, 4000), (6000, 12000)):
    corners = make_corners(rng, numVerts, numFaces)
    t0 = time.perf_counter()
    ra = original(corners)
    t1 = time.perf_counter()
    rb = bucketed(corners)
    t2 = time.perf_counter()
    if ra != rb:
        failures.append(f"identity at {numVerts}")
    old_t, new_t = t1 - t0, t2 - t1
    print("  {:>8} {:>8} {:>12.4f} {:>12.4f} {:>7.1f}x".format(
        numVerts, numFaces, old_t, new_t, old_t / new_t if new_t else float("inf")))

print("\n" + ("ALL CHECKS PASSED" if not failures else f"FAILURES: {failures}"))
sys.exit(1 if failures else 0)
