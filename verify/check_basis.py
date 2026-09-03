"""Verifies the pose <-> basis relation, without Blender.

NOTE: _bakeAction no longer uses the plain formula modelled here. It calls Blender's own
Bone.convert_local_to_pose(), because the formula below is only valid while no bone's TAIL
moves. Blender puts a child's local origin at its parent's TAIL (BKE_pchan_to_pose_mat does
`offs_bone[3][1] += parent_bone->length`), which matrix_local alone does not encode. Once the
importer began stretching parent bones to their children, every child of a stretched bone
picked up a translation error equal to the parent's length change, projected into the child's
frame - measured against a reference import, 51 of 53 bones were affected.

The checks below still document the underlying relation and the quaternion continuity rule.

The rewrite replaces bpy.ops.pose.visual_transform_apply with an analytic solve for
matrix_basis. That is only correct if, for Blender's pose evaluation

    pose.matrix = parent.pose.matrix @ parent.bone.matrix_local^-1
                  @ bone.matrix_local @ matrix_basis

solving for matrix_basis and feeding it back reproduces the target exactly. This builds a
random bone hierarchy with pure-Python 4x4 matrices, runs the solve _bakeAction does, then
runs Blender's forward evaluation on the result and compares against the target.
"""
import random
import sys

N = 4


def ident():
    return [[1.0 if r == c else 0.0 for c in range(N)] for r in range(N)]


def matmul(a, b):
    return [[sum(a[r][k] * b[k][c] for k in range(N)) for c in range(N)] for r in range(N)]


def inverse(m):
    """Gauss-Jordan on an augmented 4x8."""
    a = [row[:] + [1.0 if r == c else 0.0 for c in range(N)] for r, row in enumerate(m)]
    for col in range(N):
        pivot = max(range(col, N), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            raise ZeroDivisionError("singular")
        a[col], a[pivot] = a[pivot], a[col]
        d = a[col][col]
        a[col] = [v / d for v in a[col]]
        for r in range(N):
            if r == col:
                continue
            f = a[r][col]
            if f:
                a[r] = [v - f * w for v, w in zip(a[r], a[col])]
    return [row[N:] for row in a]


def maxdiff(a, b):
    return max(abs(a[r][c] - b[r][c]) for r in range(N) for c in range(N))


def rand_transform(rng):
    """Random rigid-ish transform: rotation about a random axis plus a translation."""
    import math
    ax, ay, az = rng.uniform(-1, 1), rng.uniform(-1, 1), rng.uniform(-1, 1)
    n = math.sqrt(ax * ax + ay * ay + az * az) or 1.0
    ax, ay, az = ax / n, ay / n, az / n
    t = rng.uniform(-math.pi, math.pi)
    c, s, C = math.cos(t), math.sin(t), 1 - math.cos(t)
    m = ident()
    m[0][0], m[0][1], m[0][2] = c + ax * ax * C, ax * ay * C - az * s, ax * az * C + ay * s
    m[1][0], m[1][1], m[1][2] = ay * ax * C + az * s, c + ay * ay * C, ay * az * C - ax * s
    m[2][0], m[2][1], m[2][2] = az * ax * C - ay * s, az * ay * C + ax * s, c + az * az * C
    for r in range(3):
        m[r][3] = rng.uniform(-50, 50)
    return m


failures = []
rng = random.Random(20260815)

print("[1] solve-then-evaluate reproduces the target pose")
worst = 0.0
for trial in range(2000):
    numBones = rng.randint(1, 12)
    parents = [-1] + [rng.randrange(0, i) for i in range(1, numBones)]
    rest = [rand_transform(rng) for _ in range(numBones)]      # bone.matrix_local
    target = [rand_transform(rng) for _ in range(numBones)]    # desired pose.matrix

    # --- the solve _bakeAction performs -------------------------------------
    restInv = [inverse(m) for m in rest]
    basis = [None] * numBones
    targetInv = {}
    for i in range(numBones):
        if parents[i] == -1:
            basis[i] = matmul(restInv[i], target[i])
        else:
            p = parents[i]
            if p not in targetInv:
                targetInv[p] = inverse(target[p])
            preMat = matmul(restInv[i], rest[p])
            basis[i] = matmul(matmul(preMat, targetInv[p]), target[i])

    # --- Blender's forward evaluation ---------------------------------------
    evaluated = [None] * numBones
    for i in range(numBones):
        if parents[i] == -1:
            evaluated[i] = matmul(rest[i], basis[i])
        else:
            p = parents[i]
            evaluated[i] = matmul(matmul(matmul(evaluated[p], restInv[p]), rest[i]), basis[i])
        worst = max(worst, maxdiff(evaluated[i], target[i]))

ok = worst < 1e-9
print(("  PASS  " if ok else "  FAIL  ") + f"2000 random hierarchies, worst deviation {worst:.3e}")
if not ok:
    failures.append("solve")

print("\n[2] the export's algebraic simplification is equivalent")
# old: absolute[parent] @ (absolute[parent]^-1 @ (poseMat @ baseInv))
# new: poseMat @ baseInv
worst2 = 0.0
for _ in range(2000):
    poseMat = rand_transform(rng)
    baseInv = inverse(rand_transform(rng))
    parentAbs = rand_transform(rng)
    new = matmul(poseMat, baseInv)
    old = matmul(parentAbs, matmul(inverse(parentAbs), new))
    worst2 = max(worst2, maxdiff(new, old))
ok2 = worst2 < 1e-9
print(("  PASS  " if ok2 else "  FAIL  ")
      + f"equivalent to within {worst2:.3e} (not bit-identical, as documented)")
if not ok2:
    failures.append("simplification")

print("\n[3] quaternion sign continuity logic")


def flipped(prev, cur):
    return sum(a * b for a, b in zip(prev, cur)) < 0.0


seq = [(1.0, 0.0, 0.0, 0.0), (-0.99, -0.1, 0.0, 0.0), (0.98, 0.19, 0.0, 0.0)]
out = []
for q in seq:
    if out and flipped(out[-1], q):
        q = tuple(-v for v in q)
    out.append(q)
signs_ok = all(not flipped(out[i - 1], out[i]) for i in range(1, len(out)))
print(("  PASS  " if signs_ok else "  FAIL  ") + f"no adjacent sign flips remain: {out}")
if not signs_ok:
    failures.append("quat continuity")

print("\n" + ("ALL CHECKS PASSED" if not failures else f"FAILURES: {failures}"))
sys.exit(1 if failures else 0)
