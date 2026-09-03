"""Standalone check of JAG2Math's quantisation, without Blender.

Stubs out just enough of mathutils that JAG2Math imports, then exercises CompBone.compress
against the old (unclamped) formula. Verifies three things:
  1. in-range values produce byte-identical output to the old code
  2. out-of-range values used to raise struct.error and now clamp + get counted
  3. the quantisation is round-to-nearest, matching g2c's Nearest mode
"""
import os
import struct
import sys
import types

# Repo-Wurzel, egal von wo aus aufgerufen.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --- minimal mathutils stub -------------------------------------------------


class _Vec:
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x, self.y, self.z = x, y, z


class _Quat:
    def __init__(self, w=1.0, x=0.0, y=0.0, z=0.0):
        self.w, self.x, self.y, self.z = w, x, y, z


class Matrix:
    """Carries a fixed loc/quat; compress() only ever calls these two accessors."""

    def __init__(self, loc, quat):
        self._loc, self._quat = loc, quat

    def to_translation(self):
        return self._loc

    def to_quaternion(self):
        return self._quat


mathutils = types.ModuleType("mathutils")
mathutils.Matrix = Matrix
mathutils.Vector = _Vec
mathutils.Quaternion = _Quat
sys.modules["mathutils"] = mathutils

# --- stub the addon package so JAG2Math's relative imports resolve -----------

pkg = types.ModuleType("jediacademy")
pkg.__path__ = [REPO]
sys.modules["jediacademy"] = pkg

mod_reload = types.ModuleType("jediacademy.mod_reload")
mod_reload.reload_modules = lambda *a, **kw: None
sys.modules["jediacademy.mod_reload"] = mod_reload

import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "jediacademy.JAG2Constants", os.path.join(REPO, "JAG2Constants.py"))
consts = importlib.util.module_from_spec(spec)
sys.modules["jediacademy.JAG2Constants"] = consts
spec.loader.exec_module(consts)

spec = importlib.util.spec_from_file_location(
    "jediacademy.JAG2Math", os.path.join(REPO, "JAG2Math.py"))
JAG2Math = importlib.util.module_from_spec(spec)
sys.modules["jediacademy.JAG2Math"] = JAG2Math
spec.loader.exec_module(JAG2Math)

STEPS = consts.COMPBONE_LOCATION_STEPS_PER_UNIT
print("COMPBONE_LOCATION_STEPS_PER_UNIT =", STEPS)


def old_compress(mat):
    """Verbatim pre-patch formula, for byte-identity comparison."""
    loc, quat = mat.to_translation(), mat.to_quaternion()
    return struct.pack("7H",
                       round((quat.w + 2) * 16383),
                       round((quat.x + 2) * 16383),
                       round((quat.y + 2) * 16383),
                       round((quat.z + 2) * 16383),
                       round((loc.x + 512) * STEPS),
                       round((loc.y + 512) * STEPS),
                       round((loc.z + 512) * STEPS))


failures = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  -- " + detail) if detail else ""))
    if not cond:
        failures.append(name)


# --- 1. byte identity on in-range data --------------------------------------
print("\n[1] byte identity with the old formula, in-range values")
import random  # noqa: E402
random.seed(1234)
mismatch = 0
for _ in range(200000):
    q = _Quat(random.uniform(-1, 1), random.uniform(-1, 1),
              random.uniform(-1, 1), random.uniform(-1, 1))
    v = _Vec(random.uniform(-511, 511), random.uniform(-511, 511), random.uniform(-511, 511))
    m = Matrix(v, q)
    if JAG2Math.CompBone.compress(m) != old_compress(m):
        mismatch += 1
check("200000 random in-range offsets encode identically", mismatch == 0,
      f"{mismatch} mismatches")

# --- 2. the overflow that used to crash the export --------------------------
print("\n[2] out-of-range handling")
m = Matrix(_Vec(512.0, 0.0, 0.0), _Quat(1, 0, 0, 0))
try:
    old_compress(m)
    old_raised = False
except struct.error as e:
    old_raised = True
    old_err = str(e)
check("old code raised struct.error at loc.x = 512.0", old_raised,
      old_err if old_raised else "did NOT raise")

stats = JAG2Math.CompressionStats()
packed = JAG2Math.CompBone.compress(m, stats, "bone_thumb", 4711)
check("new code returns 14 bytes instead of raising", len(packed) == 14)
check("clamp was counted", stats.xlatClamped == 1, f"xlatClamped={stats.xlatClamped}")
check("offending bone/frame recorded", "bone_thumb" in stats.worst and "4711" in stats.worst,
      stats.worst)
check("clamped to the top of the range", struct.unpack("<7H", packed)[4] == 65472,
      str(struct.unpack("<7H", packed)[4]))

stats = JAG2Math.CompressionStats()
JAG2Math.CompBone.compress(Matrix(_Vec(-99999.0, 0, 0), _Quat(1, 0, 0, 0)), stats, "b", 0)
check("large negative clamps too", stats.xlatClamped == 1)

stats = JAG2Math.CompressionStats()
JAG2Math.CompBone.compress(Matrix(_Vec(float("nan"), 0, 0), _Quat(1, 0, 0, 0)), stats, "b", 0)
check("NaN is caught rather than reaching struct.pack", stats.xlatClamped == 1)

stats = JAG2Math.CompressionStats()
JAG2Math.CompBone.compress(Matrix(_Vec(0, 0, 0), _Quat(3.0, 0, 0, 0)), stats, "b", 0)
check("out-of-range quaternion component counted", stats.quatClamped == 1)

stats = JAG2Math.CompressionStats()
JAG2Math.CompBone.compress(Matrix(_Vec(1, 2, 3), _Quat(1, 0, 0, 0)), stats, "b", 0)
check("clean data reports clean", stats.clean(), stats.summary())

# --- 3. round-to-nearest, matching g2c ---------------------------------------
print("\n[3] quantisation is round-to-nearest (g2c 'Nearest' mode, not Carcass truncation)")
step = 1.0 / STEPS
worst = 0.0
for i in range(100000):
    x = -400.0 + i * 0.008
    packed = JAG2Math.CompBone.compress(Matrix(_Vec(x, 0, 0), _Quat(1, 0, 0, 0)))
    raw = struct.unpack("<7H", packed)[4]
    back = raw / STEPS - 512.0
    worst = max(worst, abs(back - x))
check("max round-trip translation error <= half a step", worst <= step / 2 + 1e-9,
      f"worst={worst:.8f}, half step={step/2:.8f}")

print("\n" + ("ALL CHECKS PASSED" if not failures else f"FAILURES: {failures}"))
sys.exit(1 if failures else 0)
