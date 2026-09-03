"""Checks the export fast path's NLA reconstruction, without Blender.

The exporter can skip scene.frame_set entirely and rebuild pose bone matrices from FCurves.
That is only safe if two things hold:

  1. it refuses every setup it does not model exactly, rather than approximating it
  2. its notion of "which strip wins at frame f" matches Blender's

This exercises both against stubbed bpy objects. The real safety net is still
PoseSampler.verifyAgainst, which compares the fast path against genuine frame_set samples
before the export uses it - this file only makes sure the logic underneath is sane.
"""
import os
import sys
import types
import importlib.util

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --- stub bpy / mathutils ---------------------------------------------------
class _Vec(list):
    pass


class _Quat(list):
    @property
    def magnitude(self):
        return sum(x * x for x in self) ** 0.5

    def normalize(self):
        m = self.magnitude
        if m:
            for i in range(len(self)):
                self[i] /= m


class _Mat:
    def __init__(self, tag="I"):
        self.tag = tag

    @staticmethod
    def Identity(_n):
        return _Mat("I")

    @staticmethod
    def LocRotScale(loc, rot, scale):
        return _Mat(f"basis(loc={[round(x, 4) for x in loc]},quat={[round(x, 4) for x in rot]})")

    def copy(self):
        return _Mat(self.tag)


mathutils = types.ModuleType("mathutils")
mathutils.Matrix = _Mat
mathutils.Vector = _Vec
mathutils.Quaternion = _Quat
sys.modules["mathutils"] = mathutils

bpy = types.ModuleType("bpy")
bpy.types = types.SimpleNamespace(Object=object, Scene=object, Armature=object,
                                  Pose=object, AnimData=object)
bpy.utils = types.SimpleNamespace(escape_identifier=lambda s: s)
sys.modules["bpy"] = bpy

pkg = types.ModuleType("jediacademy")
pkg.__path__ = [REPO]
sys.modules["jediacademy"] = pkg
mod_reload = types.ModuleType("jediacademy.mod_reload")
mod_reload.reload_modules = lambda *a, **kw: None
sys.modules["jediacademy.mod_reload"] = mod_reload

spec = importlib.util.spec_from_file_location(
    "jediacademy.JAG2PoseSampler", os.path.join(REPO, "JAG2PoseSampler.py"))
PS = importlib.util.module_from_spec(spec)
sys.modules["jediacademy.JAG2PoseSampler"] = PS
spec.loader.exec_module(PS)


# --- fake Blender data ------------------------------------------------------
class FakeKeyframePoints:
    """Bulk-readable keyframes, as _SampledCurve expects."""

    def __init__(self, cos):
        self.cos = cos

    def __len__(self):
        return len(self.cos) // 2

    def foreach_get(self, _attr, out):
        out[:] = self.cos


class FakeCurve:
    def __init__(self, data_path, array_index, fn, keys=None):
        self.data_path, self.array_index, self.fn = data_path, array_index, fn
        # By default no bulk-readable keys, so _SampledCurve falls back to evaluate().
        self.keyframe_points = FakeKeyframePoints(keys if keys else [])

    def evaluate(self, frame):
        return self.fn(frame)


class FakeAction:
    def __init__(self, name, curves):
        self.name = name
        self.fcurves = curves


class FakeStrip:
    def __init__(self, name, action, frame_start, frame_end,
                 action_frame_start=0.0, action_frame_end=None, **kw):
        self.name, self.action = name, action
        self.frame_start, self.frame_end = frame_start, frame_end
        self.action_frame_start = action_frame_start
        self.action_frame_end = (frame_end - frame_start) if action_frame_end is None \
            else action_frame_end
        self.type = kw.get("type", 'CLIP')
        self.blend_type = kw.get("blend_type", 'REPLACE')
        self.use_animated_influence = kw.get("use_animated_influence", False)
        # Blender reports influence for the currently evaluated frame, so a strip that is not
        # active right now reads 0 even though nothing is blended. Default to that, to keep the
        # sampler from ever depending on the value again.
        self.influence = kw.get("influence", 0.0)
        self.use_auto_blend = kw.get("use_auto_blend", False)
        self.blend_in = kw.get("blend_in", 0.0)
        self.blend_out = kw.get("blend_out", 0.0)
        self.use_animated_time = kw.get("use_animated_time", False)
        self.use_reverse = kw.get("use_reverse", False)
        self.extrapolation = kw.get("extrapolation", 'NOTHING')
        self.modifiers = kw.get("modifiers", [])
        self.action_slot = None
        self.mute = kw.get("mute", False)


class FakeTrack:
    def __init__(self, name, strips, is_solo=False, mute=False):
        self.name, self.strips, self.is_solo, self.mute = name, strips, is_solo, mute


class FakeBone:
    def __init__(self, name):
        self.name = name
        self.matrix_local = _Mat(f"rest[{name}]")

    def convert_local_to_pose(self, basis, matrix_local, parent_matrix=None,
                              parent_matrix_local=None):
        return _Mat(f"pose[{matrix_local.tag}|{basis.tag}]")


class FakePoseBone:
    def __init__(self, name):
        self.name, self.constraints, self.rotation_mode = name, [], 'QUATERNION'


class FakeArmatureObject:
    def __init__(self, boneNames, tracks, action=None, poseBoneTweak=None):
        self.data = types.SimpleNamespace(bones={n: FakeBone(n) for n in boneNames})
        self.pose = types.SimpleNamespace(bones=[FakePoseBone(n) for n in boneNames])
        if poseBoneTweak:
            poseBoneTweak(self.pose.bones)
        self.constraints = []
        self.animation_data = types.SimpleNamespace(
            nla_tracks=tracks, action=action, action_slot=None, drivers=[])


failures = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  -- " + detail) if detail else ""))
    if not cond:
        failures.append(name)


BONES = ["root", "child"]
PARENTS = [-1, 0]
ORDER = [0, 1]


def curvesFor(value):
    """One constant location.x curve per bone, so the winning action is identifiable."""
    out = []
    for name in BONES:
        out.append(FakeCurve(f'pose.bones["{name}"].location', 0, lambda f, v=value: v))
    return out


def sampler(tracks, action=None, tweak=None):
    return PS.PoseSampler(FakeArmatureObject(BONES, tracks, action, tweak),
                          BONES, PARENTS, ORDER)


print("[1] unsupported setups are refused, not approximated")
A = FakeAction("A", curvesFor(1.0))
cases = [
    ("constraints on a bone", [FakeTrack("T", [FakeStrip("s", A, 0, 10)])],
     lambda bones: bones[1].constraints.append(object())),
    ("euler rotation mode", [FakeTrack("T", [FakeStrip("s", A, 0, 10)])],
     lambda bones: setattr(bones[1], "rotation_mode", 'XYZ')),
]
for label, tracks, tweak in cases:
    s = sampler(tracks, tweak=tweak)
    check(f"refuses: {label}", s.unsupportedReason() is not None, str(s.unsupportedReason()))

for label, kw in (("non-REPLACE blending", {"blend_type": 'ADD'}),
                  ("animated influence", {"use_animated_influence": True}),
                  ("auto blend in/out", {"use_auto_blend": True}),
                  ("explicit blend in", {"blend_in": 3.0}),
                  ("animated strip time", {"use_animated_time": True}),
                  ("reversed playback", {"use_reverse": True}),
                  ("hold extrapolation", {"extrapolation": 'HOLD'}),
                  ("hold-forward extrapolation", {"extrapolation": 'HOLD_FORWARD'}),
                  ("strip F-modifiers", {"modifiers": [object()]})):
    s = sampler([FakeTrack("T", [FakeStrip("s", A, 0, 10, **kw)])])
    check(f"refuses: {label}", s.unsupportedReason() is not None, str(s.unsupportedReason()))

s = sampler([FakeTrack("A", [], is_solo=True), FakeTrack("B", [], is_solo=True)])
check("refuses: two soloed tracks", s.unsupportedReason() is not None, str(s.unsupportedReason()))

# The strips this addon's importer creates report influence 0 when inactive; that must NOT
# be mistaken for partial blending. A real _humanoid export was rejected over exactly this.
s = sampler([FakeTrack("T", [FakeStrip("s", A, 0, 10, influence=0.0)])])
check("influence reported as 0 for an inactive strip is NOT a rejection reason",
      s.unsupportedReason() is None, str(s.unsupportedReason()))

# The exporter mutes redundant 1-frame stills; the sampler must skip them, or its
# reconstruction would disagree with what frame_set evaluates.
s = sampler([FakeTrack("L1", [FakeStrip("s1", A, 0, 10),
                              FakeStrip("s2", A, 20, 30, mute=True)])])
check("a muted strip is ignored", s._activeAt(25) is None,
      "muted strips contribute nothing")
check("unmuted strips on the same track still apply", s._activeAt(5) is not None)

print("\n[2] a plain imported setup IS supported")
s = sampler([FakeTrack("Sequences Layer 1", [FakeStrip("s", A, 0, 10)])])
check("accepts a single REPLACE strip", s.unsupportedReason() is None, str(s.unsupportedReason()))


def winningValue(s, frame):
    active = s._activeAt(frame)
    if active is None:
        return None
    curves, actionFrame = active
    return curves.value('pose.bones["root"].location', 0, actionFrame, -999.0), actionFrame


print("\n[3] which strip wins at a frame")
A1 = FakeAction("A1", curvesFor(1.0))
A2 = FakeAction("A2", curvesFor(2.0))
s = sampler([FakeTrack("L1", [FakeStrip("s1", A1, 0, 10), FakeStrip("s2", A2, 20, 30)])])
check("inside the first strip", winningValue(s, 5)[0] == 1.0)
check("inside the second strip", winningValue(s, 25)[0] == 2.0)
check("in the gap: rest pose, not held", s._activeAt(15) is None,
      "extrapolation is NOTHING, so gaps fall back to rest")
check("before everything: rest pose", s._activeAt(-5) is None)
check("after everything: rest pose", s._activeAt(99) is None)

print("\n[4] higher tracks override lower ones")
s = sampler([FakeTrack("L1", [FakeStrip("a", A1, 0, 100)]),
             FakeTrack("L2", [FakeStrip("b", A2, 40, 60)])])
check("outside the upper strip, the lower one shows", winningValue(s, 10)[0] == 1.0)
check("inside the upper strip, it wins", winningValue(s, 50)[0] == 2.0)

print("\n[5] solo disables the other tracks")
s = sampler([FakeTrack("L1", [FakeStrip("a", A1, 0, 100)]),
             FakeTrack("L2", [FakeStrip("b", A2, 40, 60)], is_solo=True)])
check("outside the soloed strip: rest pose", s._activeAt(10) is None)
check("inside the soloed strip: its action", winningValue(s, 50)[0] == 2.0)

print("\n[6] muted tracks are ignored")
s = sampler([FakeTrack("L1", [FakeStrip("a", A1, 0, 100)]),
             FakeTrack("L2", [FakeStrip("b", A2, 40, 60)], mute=True)])
check("muted upper track does not override", winningValue(s, 50)[0] == 1.0)

print("\n[7] scene frame maps onto the action's own range")
strip = FakeStrip("s", A1, 100, 110, action_frame_start=0.0, action_frame_end=10.0)
s = sampler([FakeTrack("L1", [strip])])
check("strip start -> action start", winningValue(s, 100)[1] == 0.0)
check("strip end -> action end", winningValue(s, 110)[1] == 10.0)
check("midpoint maps linearly", winningValue(s, 105)[1] == 5.0)

print("\n[8] verification frames cover the ends")
frames = PS.verificationFrames(0, 21375)
check("includes both ends", frames[0] == 0 and frames[-1] == 21375, f"{frames[0]}..{frames[-1]}")
check("sorted and unique", frames == sorted(set(frames)))
check("a reasonable number", 10 <= len(frames) <= 30, str(len(frames)))
single = PS.verificationFrames(7, 7)
check("degenerate range is handled", single == [7], str(single))

print("\n[9] bulk keyframe read agrees with evaluate(), and falls back when it must not")
import random
rng = random.Random(3)
vals = [rng.uniform(-50, 50) for _ in range(200)]


def linear(frame):
    """What Blender's evaluate() returns for one LINEAR key per integer frame."""
    if frame <= 0:
        return vals[0]
    if frame >= len(vals) - 1:
        return vals[-1]
    lo = int(frame)
    t = frame - lo
    return vals[lo] * (1 - t) + vals[lo + 1] * t


cos = []
for i, v in enumerate(vals):
    cos += [float(i), v]
c = PS._SampledCurve(FakeCurve("p", 0, linear, keys=cos))
check("consecutive integer keys are pre-read", c.values is not None)
check("pre-read values match evaluate() on every frame",
      all(abs(c.value(f) - linear(f)) < 1e-12 for f in range(len(vals))))
check("non-integer frames still go through evaluate()",
      abs(c.value(10.5) - linear(10.5)) < 1e-12, f"{c.value(10.5):.6f}")
check("out-of-range frames still go through evaluate()",
      abs(c.value(999) - linear(999)) < 1e-12)

# A gap in the keys must disable the shortcut, or values would be read off by one.
gapped = [v for i, v in enumerate(cos) if i // 2 != 5]
c2 = PS._SampledCurve(FakeCurve("p", 0, linear, keys=gapped))
check("a gap disables the shortcut", c2.values is None)
c3 = PS._SampledCurve(FakeCurve("p", 0, linear, keys=[0.5, 1.0, 1.5, 2.0]))
check("non-integer key positions disable the shortcut", c3.values is None)

print("\n[10] quaternion sign flips are recognised, not counted as huge errors")
import struct as _struct


def comp(q, loc):
    return _struct.pack("<7H", *[max(0, min(65535, int(round((v + 2.0) * 16383.0)))) for v in q],
                        *[max(0, min(65535, int(round((v + 512.0) * 64.0)))) for v in loc])


qa = (0.5, 0.5, 0.5, 0.5)
a = _struct.unpack("<7H", comp(qa, (1.0, 2.0, 3.0)))
b = _struct.unpack("<7H", comp(tuple(-v for v in qa), (1.0, 2.0, 3.0)))
check("q and -q are detected as the same rotation", PS._isQuaternionSignFlip(a, b))
c = _struct.unpack("<7H", comp((0.5, 0.5, 0.5, -0.5), (1.0, 2.0, 3.0)))
check("a genuinely different rotation is not", not PS._isQuaternionSignFlip(a, c))

print("\n[11] the action curve cache must not confuse two actions")
# The cache key used to be id(action) ^ id(slot). Two distinct pairs can XOR to the same value,
# and with 1417 actions that is near certain: in a simulation of realistic allocation patterns,
# 200 runs of 200 had at least one collision. A collision hands one sequence another
# sequence's FCurves, so whole sequences export as the wrong animation - and because addresses
# move between runs, a different pair collides each time.
#
# That was the whole reason the export was not reproducible: three identical runs gave 284, 68
# and 432 wrong frames with only one frame wrong in all three, while the import was
# byte-identical between runs.


class _Obj:
    """Stands in for a bpy struct at a chosen address."""

    def __init__(self, address, name=None):
        self.address = address
        self.identifier = name
        self.name = name


def xor_key(action, slot):
    return action.address ^ (slot.address if slot is not None else 0)


def tuple_key(action, slot):
    return (action.name, getattr(slot, "identifier", None))


a1, s1 = _Obj(0x1000, "A"), _Obj(0x2000, "s1")
a2, s2 = _Obj(0x1010, "B"), _Obj(0x2010, "s2")
check("the old XOR key collides on distinct pairs",
      xor_key(a1, s1) == xor_key(a2, s2),
      f"{xor_key(a1, s1):#x} for both A/s1 and B/s2")
check("the tuple key keeps them apart",
      tuple_key(a1, s1) != tuple_key(a2, s2))

# Same action reached twice must still hit the cache, or the fix would cost performance.
check("the same action/slot maps to one key",
      tuple_key(a1, s1) == tuple_key(_Obj(0x9999, "A"), _Obj(0x8888, "s1")),
      "cache still works across repeated lookups")
check("a slotless action is handled",
      tuple_key(a1, None) == ("A", None))

print("\n" + ("ALL CHECKS PASSED" if not failures else f"FAILURES: {failures}"))
sys.exit(1 if failures else 0)
