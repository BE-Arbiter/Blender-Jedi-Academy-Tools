"""Reproduces the "skeleton inflated by 1/header.scale" bug and checks the fix, without Blender.

A GLA stores a uniform scale in its base pose matrices (mdxaHeader.scale). For the Palpatine
asset that is 0.64, which is why an unstretched bone imports at BONELENGTH * 0.64 = 2.56.

Blender's bone.matrix_local is orthonormal, so that 0.64 ends up in matrix_basis. Scale is
never keyed - the old importer explicitly forced pose_bone.scale = [1,1,1] - so it gets
dropped. Dropping it from the LOCAL matrix is what breaks: the child's local translation was
derived in a parent frame that still had the scale, then gets applied in a parent frame that
no longer does.

Measured on a real import: parent-to-child distances came out at 1.5625x (= 1/0.64) their
rest length instead of 1.0x.

This models a bone chain with a scaled base pose and compares three strategies.
"""
import math
import sys

N = 4
SCALE = 0.64


def ident():
    return [[1.0 if r == c else 0.0 for c in range(N)] for r in range(N)]


def matmul(a, b):
    return [[sum(a[r][k] * b[k][c] for k in range(N)) for c in range(N)] for r in range(N)]


def inverse(m):
    a = [row[:] + [1.0 if r == c else 0.0 for c in range(N)] for r, row in enumerate(m)]
    for col in range(N):
        p = max(range(col, N), key=lambda r: abs(a[r][col]))
        a[col], a[p] = a[p], a[col]
        d = a[col][col]
        a[col] = [v / d for v in a[col]]
        for r in range(N):
            if r != col and a[r][col]:
                f = a[r][col]
                a[r] = [v - f * w for v, w in zip(a[r], a[col])]
    return [row[N:] for row in a]


def rot_z(t):
    m = ident()
    m[0][0], m[0][1] = math.cos(t), -math.sin(t)
    m[1][0], m[1][1] = math.sin(t), math.cos(t)
    return m


def scaled(m, s):
    out = [row[:] for row in m]
    for r in range(3):
        for c in range(3):
            out[r][c] *= s
    return out


def rigid(m):
    """Strip uniform scale, keep rotation and translation - the fix."""
    out = [row[:] for row in m]
    for c in range(3):
        n = math.sqrt(sum(out[r][c] ** 2 for r in range(3))) or 1.0
        for r in range(3):
            out[r][c] /= n
    return out


def translation(m):
    return [m[r][3] for r in range(3)]


def dist(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


# --- a 5 bone chain -----------------------------------------------------------
NUM = 5
SPACING = 10.0

# Rest skeleton as Blender stores it: orthonormal, heads spaced along Z.
rest = []
for i in range(NUM):
    m = ident()
    m[2][3] = i * SPACING
    rest.append(m)

# Absolute targets straight out of the GLA: rotated, translated - and carrying the base
# pose's uniform scale, exactly like offset @ basePoseMat does.
target = []
for i in range(NUM):
    m = rot_z(0.2 * i)
    m = scaled(m, SCALE)
    m[2][3] = i * SPACING
    m[0][3] = 0.5 * i
    target.append(m)


def bake(strategy):
    """Return the pose Blender would evaluate from the baked local matrices."""
    basis = []
    src = [rigid(t) for t in target] if strategy == "rigid" else target
    for i in range(NUM):
        if i == 0:
            b = matmul(inverse(rest[i]), src[i])
        else:
            b = matmul(matmul(matmul(inverse(rest[i]), rest[i - 1]),
                              inverse(src[i - 1])), src[i])
        if strategy in ("drop", "rigid"):
            b = rigid(b)          # scale is never keyed
        basis.append(b)
    pose = []
    for i in range(NUM):
        if i == 0:
            pose.append(matmul(rest[i], basis[i]))
        else:
            pose.append(matmul(matmul(matmul(pose[i - 1], inverse(rest[i - 1])),
                                      rest[i]), basis[i]))
    return pose


failures = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  -- " + detail) if detail else ""))
    if not cond:
        failures.append(name)


# True spacing between consecutive targets - the targets carry a small X offset too, so this
# is not exactly SPACING. This is the number a correct bake has to reproduce.
TRUE_SPACING = sum(dist(translation(target[i]), translation(target[i - 1]))
                   for i in range(1, NUM)) / (NUM - 1)
print(f"Bones are rigid: pose spacing must equal the GLA spacing ({TRUE_SPACING:.4f})\n")
print(f"  {'strategy':<26}{'spacing':>10}{'ratio':>10}")
results = {}
for strategy, label in (("keep", "keep scale in basis"),
                        ("drop", "drop scale from basis"),
                        ("rigid", "strip scale from target")):
    pose = bake(strategy)
    spacings = [dist(translation(pose[i]), translation(pose[i - 1])) for i in range(1, NUM)]
    avg = sum(spacings) / len(spacings)
    results[strategy] = avg
    print(f"  {label:<26}{avg:10.4f}{avg / TRUE_SPACING:10.4f}")

print(f"\n  1/{SCALE} = {1/SCALE:.4f}\n")

check("dropping scale from the basis inflates the skeleton",
      abs(results["drop"] / TRUE_SPACING - 1 / SCALE) < 0.01,
      f"ratio {results['drop']/TRUE_SPACING:.4f}, expected {1/SCALE:.4f}")
check("stripping scale from the target keeps spacing exact",
      abs(results["rigid"] - TRUE_SPACING) < 1e-9,
      f"spacing {results['rigid']:.6f} vs {TRUE_SPACING:.6f}")

print("\n[2] absolute positions match the GLA exactly with the fix")
pose = bake("rigid")
worst = max(dist(translation(pose[i]), translation(target[i])) for i in range(NUM))
check("every bone lands on its GLA position", worst < 1e-9, f"worst {worst:.3e}")

print("\n[3] and the resulting pose is rigid (no scale left)")
pose = bake("rigid")
norms = [math.sqrt(sum(pose[i][r][c] ** 2 for r in range(3)))
         for i in range(NUM) for c in range(3)]
check("all pose column norms are 1", max(abs(n - 1.0) for n in norms) < 1e-9,
      f"min {min(norms):.6f} max {max(norms):.6f}")

print("\n[4] quaternion normalisation on decompression")
# A GLA's compressed quaternions are 14 bit per component, so a decoded quaternion is off from
# unit by up to ~0.0015. Quaternion.to_matrix() does not normalise, so each offset matrix
# carries that as scale - and absolute transforms multiply offsets down the hierarchy, so it
# compounds. Measured on _humanoid-ja.gla: 8.9% at the deepest bones, up to 0.15 units of
# displacement. CompBone.loadFromFile normalises, which this models.
import math as _math


def chainScale(perLevel, depth, normalise):
    s = 1.0
    for _ in range(depth):
        s *= 1.0 if normalise else perLevel
    return s


OFF = 1.0015  # a decoded quaternion that is 0.15% long gives a matrix scaled by that
for depth, label in ((1, "shallow"), (10, "deepest bone")):
    without = chainScale(OFF, depth, False)
    with_ = chainScale(OFF, depth, True)
    print(f"  depth {depth:2d} ({label:12}): unnormalised {without:.5f}  normalised {with_:.5f}")
check("without normalisation the chain scale drifts",
      abs(chainScale(OFF, 10, False) - 1.0) > 0.01,
      f"{chainScale(OFF, 10, False):.5f}")
check("with normalisation it stays exactly 1",
      chainScale(OFF, 10, True) == 1.0)

print("\n" + ("ALL CHECKS PASSED" if not failures else f"FAILURES: {failures}"))
sys.exit(1 if failures else 0)
