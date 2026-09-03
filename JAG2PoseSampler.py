# ##### BEGIN GPL LICENSE BLOCK #####
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU General Public License
#  as published by the Free Software Foundation; either version 2
#  of the License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software Foundation,
#  Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.
#
# ##### END GPL LICENSE BLOCK #####

"""Sampling an armature's posed bones at arbitrary frames without the dependency graph.

The GLA exporter needs `pose_bone.matrix` for every bone on every frame. The obvious way is
scene.frame_set(f) followed by reading the pose, but frame_set re-evaluates the whole scene -
every mesh, modifier and shape key in the view layer - once per frame. For a 21376 frame
_humanoid that dominates the export.

Nothing in the GLA needs the rest of the scene. If the armature is driven purely by keyframes
(no constraints, no drivers, no animated strip influence or time), its pose is a pure function
of its FCurves, and FCurve.evaluate() reads those at any frame without touching the depsgraph.

This module implements that path, deliberately narrowly: it refuses anything it does not
model exactly rather than approximating it. PoseSampler.unsupportedReason() returns a string
whenever the setup is outside what it reproduces, and the caller falls back to frame_set.

Even then the result is not trusted blindly - see PoseSampler.verifyAgainst, which checks the
fast path against real frame_set samples before the export relies on it.
"""

from .mod_reload import reload_modules
reload_modules(locals(), __package__, [], [])  # nopep8

import bpy
import struct
import mathutils
from typing import Dict, List, Optional, Tuple

# Channels the sampler reconstructs, with the value used when no FCurve supplies one.
_LOC = "location"
_QUAT = "rotation_quaternion"
_SCALE = "scale"
_DEFAULTS = {_LOC: (0.0, 0.0, 0.0), _QUAT: (1.0, 0.0, 0.0, 0.0), _SCALE: (1.0, 1.0, 1.0)}


def _channelbagFCurves(action, slot):
    """All FCurves of an action for the given slot, across Blender versions."""
    if not hasattr(action, "layers"):
        return list(action.fcurves)  # 4.1 - 4.3
    curves = []
    for layer in action.layers:
        for strip in layer.strips:
            try:
                bag = strip.channelbag(slot) if slot is not None else None
            except Exception:
                bag = None
            if bag is not None:
                curves.extend(bag.fcurves)
            else:
                # No slot given, or it has none: take every channelbag on the strip.
                for anyBag in getattr(strip, "channelbags", ()):
                    curves.extend(anyBag.fcurves)
    return curves


class _SampledCurve:
    """A single FCurve, pre-read where possible.

    The naive route is fcurve.evaluate(frame) per channel per bone per frame - for _humanoid
    that is 7 channels x 53 bones x 21376 frames, about 7.9 million calls. Actions written by
    this addon's importer hold exactly one LINEAR keyframe per integer frame starting at 0, so
    their values can be read out in one foreach_get and then indexed directly.

    Anything that does not match that shape (hand-edited keys, gaps, non-integer frames) falls
    back to evaluate(), so correctness never depends on the assumption holding."""

    __slots__ = ("curve", "values", "first")

    def __init__(self, curve):
        self.curve = curve
        self.values = None
        self.first = 0
        points = curve.keyframe_points
        count = len(points)
        if count < 2:
            return
        flat = [0.0] * (2 * count)
        points.foreach_get("co", flat)
        first = flat[0]
        if first != int(first):
            return
        # Consecutive integer frames, one key each?
        for i in range(count):
            if flat[2 * i] != first + i:
                return
        self.first = int(first)
        self.values = [flat[2 * i + 1] for i in range(count)]

    def value(self, frame: float) -> float:
        values = self.values
        if values is not None:
            index = frame - self.first
            whole = int(index)
            if whole == index and 0 <= whole < len(values):
                return values[whole]
        return self.curve.evaluate(frame)


class _ActionCurves:
    """FCurves of one action/slot, indexed by (data_path, array_index)."""

    def __init__(self, action, slot):
        self.byChannel: Dict[Tuple[str, int], _SampledCurve] = {}
        for curve in _channelbagFCurves(action, slot):
            self.byChannel[(curve.data_path, curve.array_index)] = _SampledCurve(curve)

    def value(self, dataPath: str, index: int, frame: float, default: float) -> float:
        curve = self.byChannel.get((dataPath, index))
        return default if curve is None else curve.value(frame)


class PoseSampler:
    """Reconstructs pose bone matrices at any frame from FCurves alone."""

    def __init__(self, armatureObject: bpy.types.Object, boneNames: List[str],
                 parentIndices: List[int], hierarchyOrder: List[int]):
        self.object = armatureObject
        self.boneNames = boneNames
        self.parentIndices = parentIndices
        self.hierarchyOrder = hierarchyOrder
        self._reason: Optional[str] = None
        self._curveCache: Dict[Tuple[str, Optional[str]], _ActionCurves] = {}

        armatureData = armatureObject.data
        self.bones = [armatureData.bones[name] for name in boneNames]
        self.rest = [bone.matrix_local.copy() for bone in self.bones]
        # Per bone, the escaped data path prefix used by pose bone FCurves.
        self.paths = ['pose.bones["%s"].' % bpy.utils.escape_identifier(name)
                      for name in boneNames]

        self._reason = self._findUnsupported()
        if self._reason is None:
            self._layers = self._collectLayers()

    # -- support checks ----------------------------------------------------

    def _findUnsupported(self) -> Optional[str]:
        obj = self.object
        animData = obj.animation_data
        if animData is None:
            return "the armature has no animation data"

        for poseBone in obj.pose.bones:
            if len(poseBone.constraints):
                return f"bone '{poseBone.name}' has constraints"
            if poseBone.rotation_mode != 'QUATERNION':
                return f"bone '{poseBone.name}' uses rotation mode {poseBone.rotation_mode}"
        if len(obj.constraints):
            return "the armature object has constraints"
        if animData.drivers and len(animData.drivers):
            return "the armature has drivers"
        # Object level animation would move the whole armature per frame.
        if animData.action is not None:
            for curve in _channelbagFCurves(animData.action, getattr(animData, "action_slot", None)):
                if not curve.data_path.startswith("pose.bones["):
                    return f"the active action animates '{curve.data_path}' on the object itself"

        soloTracks = [t for t in animData.nla_tracks if t.is_solo]
        if len(soloTracks) > 1:
            return "more than one NLA track is soloed"

        tracks = soloTracks if soloTracks else [t for t in animData.nla_tracks if not t.mute]
        for track in tracks:
            for strip in track.strips:
                if strip.type != 'CLIP':
                    return f"strip '{strip.name}' is of type {strip.type}"
                if strip.action is None:
                    return f"strip '{strip.name}' has no action"
                if strip.blend_type != 'REPLACE':
                    return f"strip '{strip.name}' uses blend mode {strip.blend_type}"
                # NlaStrip.influence is the value for the CURRENTLY evaluated frame, so it reads
                # as 0 for every strip that is not active right now. Testing it against 1.0
                # therefore rejected practically every setup - which is exactly what happened on
                # a real _humanoid export. What matters is whether the influence VARIES: an
                # animated influence curve, or blending in/out.
                if strip.use_animated_influence:
                    return f"strip '{strip.name}' has an animated influence curve"
                if strip.use_auto_blend:
                    return f"strip '{strip.name}' uses auto blend in/out"
                if strip.blend_in > 0.0 or strip.blend_out > 0.0:
                    return f"strip '{strip.name}' blends in or out"
                if strip.use_animated_time:
                    return f"strip '{strip.name}' has animated strip time"
                if strip.use_reverse:
                    return f"strip '{strip.name}' plays reversed"
                # _activeAt only considers strips whose own range covers the frame, so anything
                # but NOTHING would be modelled wrongly rather than approximately. Blender
                # re-derives extend modes from strip overlaps whenever a track changes, so this
                # can differ from what the importer set - and silently producing a plausible but
                # wrong GLA is exactly what this whole class exists to prevent.
                if strip.extrapolation != 'NOTHING':
                    return (f"strip '{strip.name}' extrapolates with {strip.extrapolation}, "
                            "which the fast path does not model")
                if len(strip.modifiers):
                    return f"strip '{strip.name}' has F-modifiers"
        return None

    def unsupportedReason(self) -> Optional[str]:
        return self._reason

    # -- NLA evaluation ----------------------------------------------------

    def _collectLayers(self):
        """Strips to consider, bottom track first. Later entries override earlier ones."""
        animData = self.object.animation_data
        soloTracks = [t for t in animData.nla_tracks if t.is_solo]
        tracks = soloTracks if soloTracks else [t for t in animData.nla_tracks if not t.mute]
        layers = []
        for track in tracks:
            # Muted strips contribute nothing, matching Blender's own evaluation.
            layers.append(sorted(
                ((s.frame_start, s.frame_end, s) for s in track.strips if not s.mute),
                key=lambda entry: entry[0]))
        # Blender evaluates the active action on top of the NLA stack, unless a track is soloed.
        if not soloTracks and animData.action is not None:
            layers.append(None)  # sentinel: the active action, covering every frame
        return layers

    def _curvesFor(self, action, slot) -> _ActionCurves:
        """Cached FCurve lookup for one action/slot pair.

        The key MUST NOT combine the two with XOR. Two distinct pairs can XOR to the same value
        - id(0x1000) ^ id(0x2000) and id(0x1010) ^ id(0x2010) are both 0x3000 - and with 1417
        actions in a full _humanoid that is not a remote possibility: in a simulation of
        realistic allocation patterns, every single run of 200 had at least one collision.
        A collision hands one sequence another sequence's curves, so whole sequences export as
        the wrong animation.
        Because object addresses differ from run to run, a different pair collides each time.
        That was the entire cause of the export being non-reproducible: three identical runs
        produced 284, 68 and 432 wrong frames, with only one frame wrong in all three, while
        the import was byte-identical between runs.
        Action names are unique within bpy.data, so name plus slot identifier is both stable
        and collision free."""
        key = (action.name, getattr(slot, "identifier", None))
        curves = self._curveCache.get(key)
        if curves is None:
            curves = _ActionCurves(action, slot)
            self._curveCache[key] = curves
        return curves

    def _activeAt(self, frame: float):
        """The (curves, actionFrame) pair that wins at this frame, or None for the rest pose."""
        winner = None
        animData = self.object.animation_data
        for layer in self._layers:
            if layer is None:
                winner = (self._curvesFor(animData.action,
                                          getattr(animData, "action_slot", None)), frame)
                continue
            for start, end, strip in layer:
                if start <= frame <= end:
                    # Extrapolation is irrelevant inside the strip, and scale/repeat are checked
                    # by the caller via verifyAgainst - map linearly onto the action's range.
                    span = end - start
                    actionSpan = strip.action_frame_end - strip.action_frame_start
                    if span > 0:
                        actionFrame = strip.action_frame_start + (frame - start) * actionSpan / span
                    else:
                        actionFrame = strip.action_frame_start
                    winner = (self._curvesFor(strip.action,
                                              getattr(strip, "action_slot", None)), actionFrame)
                    break
        return winner

    # -- sampling ----------------------------------------------------------

    def poseMatrices(self, frame: float) -> List[mathutils.Matrix]:
        """Armature space matrices for every bone, equivalent to pose_bone.matrix."""
        active = self._activeAt(frame)
        result: List[Optional[mathutils.Matrix]] = [None] * len(self.bones)

        for index in self.hierarchyOrder:
            path = self.paths[index]
            if active is None:
                basis = mathutils.Matrix.Identity(4)
            else:
                curves, actionFrame = active
                loc = [curves.value(path + _LOC, i, actionFrame, _DEFAULTS[_LOC][i])
                       for i in range(3)]
                quat = [curves.value(path + _QUAT, i, actionFrame, _DEFAULTS[_QUAT][i])
                        for i in range(4)]
                scale = [curves.value(path + _SCALE, i, actionFrame, _DEFAULTS[_SCALE][i])
                         for i in range(3)]
                rotation = mathutils.Quaternion(quat)
                # A quaternion FCurve interpolated componentwise is not unit; Blender normalises
                # it before use, so do the same.
                if rotation.magnitude > 1e-12:
                    rotation.normalize()
                else:
                    rotation = mathutils.Quaternion((1.0, 0.0, 0.0, 0.0))
                basis = mathutils.Matrix.LocRotScale(
                    mathutils.Vector(loc), rotation, mathutils.Vector(scale))

            parent = self.parentIndices[index]
            bone = self.bones[index]
            if parent == -1:
                result[index] = bone.convert_local_to_pose(basis, self.rest[index])
            else:
                result[index] = bone.convert_local_to_pose(
                    basis, self.rest[index],
                    parent_matrix=result[parent], parent_matrix_local=self.rest[parent])
        return [m for m in result]  # type: ignore

    # -- verification ------------------------------------------------------

    def verifyAgainst(self, scene: bpy.types.Scene, poseBones, frames, compressFrame,
                      maxSteps: int = 1) -> Tuple[bool, str]:
        """Compare the fast path against real frame_set samples on the given frames.

        The comparison is made on the *compressed* output, not on the pose matrices, because
        that is what actually lands in the file. Comparing matrices meant judging float64 Python
        arithmetic against Blender's float32 evaluation: a real export was rejected over a
        0.0029 difference on rhumerus, which is under a fifth of the format's own 1/64
        translation step and could not have changed a single byte.

        `compressFrame(poseMatrices)` must return this frame's list of 14 byte blocks. A
        component may differ by at most `maxSteps` quantisation steps - one step is 1/64 of a
        unit in translation, i.e. the finest distinction the format can make."""
        savedFrame = scene.frame_current
        worstSteps = 0
        try:
            for frame in frames:
                scene.frame_set(frame)
                truth = compressFrame([poseBone.matrix.copy() for poseBone in poseBones])
                fast = compressFrame(self.poseMatrices(frame))
                for index, (a, b) in enumerate(zip(truth, fast)):
                    if a == b:
                        continue
                    va = struct.unpack("<7H", a)
                    vb = struct.unpack("<7H", b)
                    steps = max(abs(x - y) for x, y in zip(va, vb))
                    # A quaternion and its negation encode the same rotation, so a sign flip is
                    # not a difference - recognise it instead of reporting a huge step count.
                    if steps > maxSteps and _isQuaternionSignFlip(va, vb):
                        steps = max(abs(x - y) for x, y in zip(va[4:], vb[4:]))
                    if steps > worstSteps:
                        worstSteps = steps
                    if steps > maxSteps:
                        return False, ("bone '{}' differs by {} quantisation step(s) at frame "
                                       "{}".format(self.boneNames[index], steps, frame))
        finally:
            scene.frame_set(savedFrame)
        return True, "worst difference {} quantisation step(s)".format(worstSteps)


def _isQuaternionSignFlip(a, b) -> bool:
    """True when two compressed bones hold the same rotation with opposite quaternion signs."""
    for i in range(4):
        # Components are (value + 2) * 16383, so negating gives 4 * 16383 - value.
        if abs((4 * 16383 - a[i]) - b[i]) > 2:
            return False
    return True


def verificationFrames(start: int, end: int, count: int = 24) -> List[int]:
    """Frames to check the fast path on: the ends, plus an even spread in between.

    The ends matter because that is where strip boundaries and extrapolation live."""
    if end <= start:
        return [start]
    count = max(2, min(count, end - start + 1))
    step = (end - start) / (count - 1)
    frames = sorted({int(round(start + i * step)) for i in range(count)})
    for edge in (start, end):
        if edge not in frames:
            frames.append(edge)
    return sorted(frames)
