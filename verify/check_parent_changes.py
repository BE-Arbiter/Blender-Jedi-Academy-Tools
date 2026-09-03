"""Guards the skeleton-fix reparenting bug, without Blender.

JAG2Constants.PARENT_CHANGES gives 16 bones of the JKA humanoid a different parent in Blender
than the GLA has - rhumerus moves under rclavical, the finger joints and hang tag bones move
under the hand - so the rig is easier to pose. The GLA keeps its own hierarchy.

Blender evaluates a pose bone against its ACTUAL parent. Solving for matrix_basis against the
GLA parent therefore misplaces exactly those bones, and nothing else. On a real _humanoid
re-export that showed up as translation errors of up to 83 units on precisely those 16 bones,
while all 37 others were bit-exact - and it made the export fast path reject itself, because
its own reconstruction disagreed with frame_set on rhumerus by 4 quantisation steps.

This checks that the affected set is exactly the reparented set, and that a solve using the
correct parent reproduces the target pose while one using the GLA parent does not.
"""
import math
import os
import sys
import types
import importlib.util

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

bpy = types.ModuleType("bpy")
bpy.types = types.SimpleNamespace()
sys.modules["bpy"] = bpy
mathutils = types.ModuleType("mathutils")
sys.modules["mathutils"] = mathutils
pkg = types.ModuleType("jediacademy")
pkg.__path__ = [REPO]
sys.modules["jediacademy"] = pkg
mod_reload = types.ModuleType("jediacademy.mod_reload")
mod_reload.reload_modules = lambda *a, **kw: None
sys.modules["jediacademy.mod_reload"] = mod_reload

spec = importlib.util.spec_from_file_location(
    "jediacademy.JAG2Constants", os.path.join(REPO, "JAG2Constants.py"))
C = importlib.util.module_from_spec(spec)
sys.modules["jediacademy.JAG2Constants"] = C
spec.loader.exec_module(C)

failures = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  -- " + detail) if detail else ""))
    if not cond:
        failures.append(name)


# Bone order of _humanoid.gla, so the indices in PARENT_CHANGES can be named.
HUMANOID = [
    "model_root", "pelvis", "Motion", "lfemurYZ", "lfemurX", "ltibia", "ltalus", "rfemurYZ",
    "rfemurX", "rtibia", "rtalus", "lower_lumbar", "upper_lumbar", "thoracic", "cervical",
    "cranium", "ceyebrow", "jaw", "lblip2", "leye", "rblip2", "ltlip2", "rtlip2", "reye",
    "rclavical", "rhumerus", "rhumerusX", "rradius", "rradiusX", "rhand", "r_d1_j1", "r_d1_j2",
    "r_d2_j1", "r_d2_j2", "r_d4_j1", "r_d4_j2", "rhang_tag_bone", "lclavical", "lhumerus",
    "lhumerusX", "lradius", "lradiusX", "lhand", "l_d4_j1", "l_d4_j2", "l_d2_j1", "l_d2_j2",
    "l_d1_j1", "l_d1_j2", "ltail", "rtail", "lhang_tag_bone", "face",
]

# Bones a real re-export got wrong, measured against the original _humanoid.gla.
MEASURED_BAD = {
    "rhumerus", "lhumerus",
    "r_d1_j1", "r_d1_j2", "r_d2_j1", "r_d2_j2", "r_d4_j1", "r_d4_j2", "rhang_tag_bone",
    "l_d1_j1", "l_d1_j2", "l_d2_j1", "l_d2_j2", "l_d4_j1", "l_d4_j2", "lhang_tag_bone",
}

print("[1] the reparented set matches the bones a real export got wrong")
changes = C.PARENT_CHANGES[C.SkeletonFixes.JKA_HUMANOID]
reparented = {HUMANOID[i] for i in changes}
check("PARENT_CHANGES covers 16 bones", len(changes) == 16, str(len(changes)))
check("reparented set == measured broken set", reparented == MEASURED_BAD,
      f"only reparented: {sorted(reparented - MEASURED_BAD)}, "
      f"only broken: {sorted(MEASURED_BAD - reparented)}")

print("\n[2] every reparented bone really does get a different parent")
GLA_PARENT = {
    "rhumerus": "thoracic", "lhumerus": "thoracic",
    "r_d1_j1": "rradius", "r_d1_j2": "rradius", "r_d2_j1": "rradius", "r_d2_j2": "rradius",
    "r_d4_j1": "rradius", "r_d4_j2": "rradius", "rhang_tag_bone": "rradius",
    "l_d1_j1": "lradius", "l_d1_j2": "lradius", "l_d2_j1": "lradius", "l_d2_j2": "lradius",
    "l_d4_j1": "lradius", "l_d4_j2": "lradius", "lhang_tag_bone": "lradius",
}
mismatch = []
for index, newParent in changes.items():
    name = HUMANOID[index]
    blenderParent = HUMANOID[newParent]
    if GLA_PARENT.get(name) == blenderParent:
        mismatch.append(name)
check("Blender parent differs from the GLA parent for all of them", not mismatch, str(mismatch))

print("\n[3] solving against the wrong parent misplaces the bone")
N = 4


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


def rotZ(t, tx=0.0, tz=0.0):
    m = ident()
    m[0][0], m[0][1] = math.cos(t), -math.sin(t)
    m[1][0], m[1][1] = math.sin(t), math.cos(t)
    m[0][3], m[2][3] = tx, tz
    return m


# bone, its GLA parent, and its (different) Blender parent
rest = {"bone": rotZ(0.0, 5.0, 10.0), "gla": rotZ(0.0, 0.0, 4.0), "blender": rotZ(0.0, 3.0, 8.0)}
target = {"bone": rotZ(0.7, 6.0, 11.0), "gla": rotZ(0.2, 1.0, 4.5), "blender": rotZ(0.5, 3.5, 8.5)}


def solve(parentKey):
    """basis = rest^-1 @ parentRest @ parentTarget^-1 @ target"""
    return matmul(matmul(matmul(inverse(rest["bone"]), rest[parentKey]),
                         inverse(target[parentKey])), target["bone"])


def evaluate(basis, parentKey):
    """What Blender computes, always against its OWN parent."""
    return matmul(matmul(matmul(target["blender"], inverse(rest["blender"])),
                         rest["bone"]), basis)


def maxdiff(a, b):
    return max(abs(a[r][c] - b[r][c]) for r in range(N) for c in range(N))


correct = evaluate(solve("blender"), "blender")
wrong = evaluate(solve("gla"), "blender")
check("solving against the Blender parent reproduces the target",
      maxdiff(correct, target["bone"]) < 1e-12, f"{maxdiff(correct, target['bone']):.3e}")
check("solving against the GLA parent does not",
      maxdiff(wrong, target["bone"]) > 0.1, f"{maxdiff(wrong, target['bone']):.4f}")

print("\n" + ("ALL CHECKS PASSED" if not failures else f"FAILURES: {failures}"))
sys.exit(1 if failures else 0)
