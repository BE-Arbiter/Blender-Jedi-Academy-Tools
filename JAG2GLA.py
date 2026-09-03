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

from .mod_reload import reload_modules
reload_modules(locals(), __package__, ["JAStringhelper", "JAG2AnimationCFG", "JAG2Constants", "JAG2Math", "JAG2PoseSampler", "MrwProfiler", "JAG2Panels"], [".casts", ".error_types"])  # nopep8

from . import JAStringhelper
from . import JAG2Constants
from . import JAG2Math
from . import JAG2PoseSampler
from . import JAG2AnimationCFG
from . import MrwProfiler
from . import JAG2Panels
from .casts import optional_cast, downcast, bpy_generic_cast, matrix_getter_cast, matrix_overload_cast, vector_getter_cast, vector_overload_cast
from .error_types import ErrorMessage, NoError

from typing import BinaryIO, Dict, List, Optional, Tuple
from enum import Enum
import struct
import time
import bpy
import mathutils

PROFILE = False
# show progress & remaining time every 30 seconds.
PROGRESS_UPDATE_INTERVAL = 30

# Largest per-element difference tolerated between a reference GLA's stored base pose and the
# Blender armature's rest pose. Generous: base poses go through float32 in the file and two axis
# conversions, so exact equality is not expected, but a genuine rest pose mismatch is far larger.
BASE_POSE_TOLERANCE = 1e-3


def _getFCurves(action: bpy.types.Action, slot):
    """Where to create FCurves on an Action, across supported Blender versions.

    Blender 4.4 introduced slotted actions (layers -> strips -> channelbags) and 5.0 removed the
    legacy Action.fcurves accessor. Getting this wrong does not raise - the FCurves are created
    successfully in a channelbag nothing reads, so the action silently stays empty. Hence the
    ordered fallbacks below and the _verifyFCurveContainer check that follows."""
    if not hasattr(action, "layers"):
        return action.fcurves  # 4.1 - 4.3

    # Preferred: the official porting helper, added in 5.0.
    try:
        from bpy_extras import anim_utils
        ensure = getattr(anim_utils, "action_ensure_channelbag_for_slot", None)
        if ensure is not None and slot is not None:
            return ensure(action, slot).fcurves
    except ImportError:
        pass

    # 4.4 / 4.5: navigate to the channelbag by hand.
    layer = action.layers[0] if len(action.layers) else action.layers.new("Layer")
    strip = layer.strips[0] if len(layer.strips) else layer.strips.new(type='KEYFRAME')
    return strip.channelbag(slot, ensure=True).fcurves


def _findFCurve(fcurves, dataPath: str, index: int):
    """Locate an existing FCurve, tolerating containers without a find() method."""
    try:
        return fcurves.find(dataPath, index=index)
    except Exception:
        for fcurve in fcurves:
            if fcurve.data_path == dataPath and fcurve.array_index == index:
                return fcurve
        return None


def _clearKeyframes(fcurve) -> None:
    try:
        fcurve.keyframe_points.clear()
    except AttributeError:
        for point in list(fcurve.keyframe_points):
            fcurve.keyframe_points.remove(point, fast=True)


def _newFCurve(fcurves, dataPath: str, index: int, group: str):
    """Get an empty FCurve for this channel, creating it if needed.

    Reuse matters: from 4.4 on, calling new() for a channel that already exists raises
    RuntimeError rather than returning the existing curve, and a curve can already be there
    from the container probe or from a re-import onto the same action.

    The keyword naming an FCurve's group also differs between the legacy and channelbag APIs,
    and older channelbag builds accept no group at all - hence the three attempts."""
    existing = _findFCurve(fcurves, dataPath, index)
    if existing is not None:
        _clearKeyframes(existing)
        return existing
    for kwargs in ({"group_name": group}, {"action_group": group}, {}):
        try:
            return fcurves.new(dataPath, index=index, **kwargs)
        except TypeError:
            continue
    return fcurves.new(dataPath, index=index)


def _verifyFCurveContainer(action: bpy.types.Action, slot, armature: bpy.types.Object,
                           boneName: str) -> Tuple[bool, str]:
    """Confirm that FCurves we create are the ones Blender reads back.

    Writing to the wrong channelbag fails silently - the import finishes quickly, the dope sheet
    is empty and the armature sits in its rest pose. So before baking anything, let Blender's own
    keyframe_insert create a curve and check it turns up in the container we would have used."""
    try:
        poseBone = armature.pose.bones[boneName]
    except Exception as e:
        return False, f"could not access pose bone {boneName}: {e}"

    dataPath = 'pose.bones["%s"].location' % bpy.utils.escape_identifier(boneName)
    try:
        poseBone.keyframe_insert('location', index=0, frame=0)
    except Exception as e:
        return False, f"keyframe_insert failed: {e}"

    try:
        fcurves = _getFCurves(action, slot)
        match = _findFCurve(fcurves, dataPath, 0)
    except Exception as e:
        return False, f"could not resolve the FCurve container: {e}"

    if match is None:
        return False, ("Blender stored the probe keyframe somewhere this addon does not write to "
                       f"(Blender {bpy.app.version_string}). Baking would produce empty actions.")

    # The probe curve is deliberately left in place. _newFCurve reuses and clears existing
    # curves, so there is nothing to clean up - and no removal call that could fail silently.
    return True, ""


# Interpolation enum ordinal for LINEAR, resolved from Blender's RNA rather than hardcoded,
# because foreach_set needs the raw int and hardcoding 1 would silently pick whatever enum
# value happens to sit at that index if the ordering ever changed.
def _linearInterpolationValue() -> int:
    try:
        return bpy.types.Keyframe.bl_rna.properties['interpolation'].enum_items['LINEAR'].value
    except Exception:
        return 1


# Set once per import so the reparenting note is printed once, not once per sequence.
_reportedReparenting = [False]


def _neededStillsLayers(animations, maxLayers: int = 8) -> int:
    """How many "Stills Layer" tracks the 1-frame sequences will occupy.

    Mirrors the placement loop's rule exactly - a strip goes on the lowest layer where it does
    not overlap anything already there - so the count matches what the loop actually uses. A
    1-frame sequence occupies two frames, because that is Blender's minimum strip length."""
    layers: List[List[Tuple[int, int]]] = []
    for sequence in animations.sequences:
        if sequence.num_frames != 1:
            continue
        span = (sequence.start_frame, sequence.start_frame + 1)
        for occupied in layers:
            if not any(span[0] <= end and start <= span[1] for start, end in occupied):
                occupied.append(span)
                break
        else:
            if len(layers) >= maxLayers:
                break
            layers.append([span])
    return max(1, len(layers))


def _rigid(m: mathutils.Matrix) -> mathutils.Matrix:
    """Rotation and translation only - scale and shear removed.

    A GLA's stored base pose carries the file's uniform scale (mdxaHeader.scale; 0.64 for the
    Palpatine asset, which is why an unstretched bone comes out 4 * 0.64 = 2.56 long). Every
    absolute transform built from it therefore carries that scale too, while Blender's
    bone.matrix_local is orthonormal - so the scale ends up in matrix_basis.

    Scale is never keyed (the old importer forced pose_bone.scale = [1,1,1] for the same
    reason), so it gets dropped. Dropping it from a *local* matrix is not harmless: the child's
    local translation was derived in a parent frame that still had the 0.64 scale, but is then
    applied in a parent frame that no longer does, so every bone lands 1/0.64 = 1.5625x too far
    out. Measured against a reference import, parent-to-child distances came out at 1.5625x
    their rest length instead of 1.0x.

    The old code avoided this by assigning pose_bone.matrix per bone in hierarchy order with a
    depsgraph refresh in between, so each bone was placed against an already-corrected parent.
    Removing the scale from the absolute transforms up front achieves the same thing without
    any evaluation: parent and child frames stay consistent and the translations stay exact."""
    out = m.to_quaternion().to_matrix().to_4x4()
    out.translation = m.to_translation()
    return out


def _poseToBasis(blenderBone, target, parentTarget, parentBone):
    """Convert an armature-space pose matrix to a bone's matrix_basis, without the depsgraph.

    Blender's own evaluation puts a bone's local origin at its PARENT'S TAIL, not its head -
    BKE_pchan_to_pose_mat does `offs_bone[3][1] += parent_bone->length`. A hand-rolled

        basis = rest^-1 @ parentRest @ parentTarget^-1 @ target

    encodes head positions and orientations but not that length term, so it only agrees while
    no tail ever moves. It matched a reference import on all 53 bones exactly - until parent
    bones started getting stretched to their children, at which point every child of a stretched
    bone picked up a translation error equal to the parent's length change, projected into the
    child's own frame.

    Bone.convert_local_to_pose is Blender's own implementation of that conversion and also
    handles Inherit Scale, Local Location and Connected, so use it rather than reimplementing.
    """
    if parentBone is None:
        return blenderBone.convert_local_to_pose(
            target, blenderBone.matrix_local, invert=True)
    return blenderBone.convert_local_to_pose(
        target, blenderBone.matrix_local,
        parent_matrix=parentTarget, parent_matrix_local=parentBone.matrix_local,
        invert=True)


def _bakeAction(action: bpy.types.Action, slot, skeleton: "MdxaSkel", armature: bpy.types.Object,
                transformsPerFrame: List[Dict[int, mathutils.Matrix]], hierarchyOrder: List[int],
                frameOffset: int, numFrames: int) -> None:
    """Write a baked pose animation into `action` without touching the dependency graph.

    The previous approach set pose_bone.matrix and then called bpy.ops.pose.visual_transform_apply
    once per bone per frame, because assigning .matrix on a child needs the parent's *evaluated*
    matrix. For _humanoid (53 bones, 21376 frames) that is ~1.1M operator invocations, each
    carrying context resolution, a depsgraph update and an undo push, plus ~2.3M
    keyframe_insert calls - which is where the import time actually goes.

    None of that is necessary: the parent's target matrix is already known, since we walk in
    hierarchy order, so the local matrix can be derived directly (see _poseToBasis). Keyframes
    then go in via keyframe_points.add + foreach_set, the same bulk path Blender's own BVH
    importer uses."""
    armatureData = downcast(bpy.types.Armature, armature.data)
    blenderBones: Dict[int, bpy.types.Bone] = {}
    parentBones: Dict[int, Optional[bpy.types.Bone]] = {}
    parentTargetIndex: Dict[int, int] = {}
    indexByName = {bone.name: bone.index for bone in skeleton.bones}
    reparented: List[str] = []
    for bone in skeleton.bones:
        blenderBone = bpy_generic_cast(bpy.types.Bone, armatureData.bones[bone.name])
        blenderBones[bone.index] = blenderBone
        # The parent to solve against is Blender's, NOT the GLA's. JAG2Constants.PARENT_CHANGES
        # reparents 16 bones of the JKA humanoid on import (rhumerus to rclavical, the finger
        # joints and hang tag bones to the hand) to make the rig easier to pose, while the GLA
        # keeps its own hierarchy. Blender evaluates a pose bone against its actual parent, so
        # solving for matrix_basis against the GLA parent puts exactly those bones in the wrong
        # place - which then shows up in a re-export as garbage on those 16 bones and nowhere
        # else. Absolute targets are hierarchy independent, so simply picking the right parent
        # out of them is enough.
        blenderParent = blenderBone.parent
        if blenderParent is None:
            parentBones[bone.index] = None
            parentTargetIndex[bone.index] = -1
        else:
            parentBones[bone.index] = bpy_generic_cast(bpy.types.Bone, blenderParent)
            parentTargetIndex[bone.index] = indexByName[blenderParent.name]
            if bone.parent != -1 and parentTargetIndex[bone.index] != bone.parent:
                reparented.append(bone.name)
    if reparented and not _reportedReparenting[0]:
        # _bakeAction runs once per sequence - 1417 times for a full _humanoid - so this would
        # otherwise bury the rest of the log.
        _reportedReparenting[0] = True
        print("Info: {} bone(s) are parented differently in Blender than in the GLA "
              "(skeleton fixes); solving against Blender's hierarchy.".format(len(reparented)))

    numBones = len(skeleton.bones)
    locs: List[List[Tuple[float, float, float]]] = [[] for _ in range(numBones)]
    quats: List[List[Tuple[float, float, float, float]]] = [[] for _ in range(numBones)]

    for f in range(numFrames):
        transforms = transformsPerFrame[frameOffset + f]
        # Remove the base pose's uniform scale once per frame, before anything is derived from
        # these matrices - see _rigid for why doing it later (per bone, on the local matrix)
        # inflates the whole skeleton.
        rigid = {index: _rigid(transforms[index]) for index in hierarchyOrder}
        for index in hierarchyOrder:
            bone = skeleton.bones[index]
            parentIndex = parentTargetIndex[index]
            basis = _poseToBasis(
                blenderBones[index], rigid[index],
                None if parentIndex == -1 else rigid[parentIndex],
                parentBones[index])
            loc, quat, _scale = basis.decompose()
            # Scale is deliberately dropped, matching the old code's pose_bone.scale = [1,1,1].
            # After _rigid there is nothing left to drop, so this is now a no-op rather than a
            # silent change of meaning.
            # Quaternion sign continuity: q and -q are the same rotation, but FCurves interpolate
            # componentwise, so an unflipped sign change between adjacent keys swings the pose
            # wildly in between. decompose() gives no continuity guarantee, so enforce it.
            previous = quats[index][-1] if quats[index] else None
            if previous is not None and (quat.w * previous[0] + quat.x * previous[1]
                                         + quat.y * previous[2] + quat.z * previous[3]) < 0.0:
                quat = -quat
            locs[index].append((loc.x, loc.y, loc.z))
            quats[index].append((quat.w, quat.x, quat.y, quat.z))

    fcurves = _getFCurves(action, slot)
    linear = _linearInterpolationValue()
    for bone in skeleton.bones:
        escaped = bpy.utils.escape_identifier(bone.name)
        for path, data, count in (
            ('pose.bones["%s"].location' % escaped, locs[bone.index], 3),
            ('pose.bones["%s"].rotation_quaternion' % escaped, quats[bone.index], 4),
        ):
            for component in range(count):
                fcurve = _newFCurve(fcurves, path, component, bone.name)
                fcurve.keyframe_points.add(numFrames)
                # foreach_set writes positionally, so the point count has to match exactly or
                # the data would be shifted rather than rejected. _newFCurve clears reused
                # curves; this catches any build where clearing did not take effect.
                if len(fcurve.keyframe_points) != numFrames:
                    raise RuntimeError(
                        "FCurve {}[{}] has {} keyframe points, expected {} - could not clear a "
                        "pre-existing curve".format(path, component,
                                                    len(fcurve.keyframe_points), numFrames))
                co = [0.0] * (2 * numFrames)
                for f in range(numFrames):
                    co[2 * f] = float(f)
                    co[2 * f + 1] = data[f][component]
                fcurve.keyframe_points.foreach_set("co", co)
                fcurve.keyframe_points.foreach_set("interpolation", [linear] * numFrames)
                fcurve.update()


# The 1-frame "still" problem, and three measured attempts at it - so nobody, including a
# future me, burns another round on it without new information.
#
# Blender's minimum NLA strip length is one frame OF LENGTH, so a 1-frame sequence at frame N
# gets a strip spanning N..N+1. The stills tracks sit above the sequence tracks, so that second
# frame overrides whichever sequence really owns it and holds the still's pose there. Measured
# against the original _humanoid.gla: 52 of 21376 frames wrong (0.24%), and the animation.cfg
# confirms 51 of those 52 sit directly after a 1-frame sequence.
#
#   attempt                                          result (frames wrong, was 52)
#   create the Stills tracks first, so sequences     521 - 20 contiguous blocks, constant
#   override them                                          where the original varies
#   bake the following GLA frame into the still      410 - 2 fixed, 360 broken, again blocks;
#   and widen its strip to match                           50 of the 52 remained
#   mute redundant stills for the export (all 60     525 - all 52 fixed, but whole sequences
#   are covered by a longer sequence, verified             break: BOTH_STAND9IDLE1 (150 frames),
#   against the cfg)                                       BOTH_VICTORY_STAFF (103), and 8 more,
#                                                          each one WITHOUT an overlapping
#                                                          neighbour in the cfg
#
# All three produce contiguous blocks of corruption whose mechanism is not understood. The
# third is the most puzzling: muting 60 one-frame strips should not be able to affect a
# 150-frame sequence at all. Until that is explained - most cheaply by opening one of those
# frames in the NLA editor and looking at which strips actually cover it - the 0.24% stands.


def _basePoseDelta(blenderBase: mathutils.Matrix, referenceBase: mathutils.Matrix) -> float:
    """How far apart two base poses are, ignoring a uniform scale difference.

    Returns the larger of the worst rotation-column difference (after normalising both) and the
    worst translation difference. A GLA base pose is scaled by the file's mdxaHeader.scale while
    Blender's matrix_local is orthonormal, and that difference is harmless - the relative offsets
    the exporter writes are scale free either way."""
    worst = 0.0
    for col in range(3):
        a = mathutils.Vector((blenderBase[0][col], blenderBase[1][col], blenderBase[2][col]))
        b = mathutils.Vector((referenceBase[0][col], referenceBase[1][col], referenceBase[2][col]))
        if a.length > 1e-9:
            a = a / a.length
        if b.length > 1e-9:
            b = b / b.length
        worst = max(worst, max(abs(a[row] - b[row]) for row in range(3)))
    worst = max(worst, max(abs(blenderBase[row][3] - referenceBase[row][3]) for row in range(3)))
    return worst


def readString(file: BinaryIO) -> str:
    return JAStringhelper.decode(struct.unpack("<64s", file.read(JAG2Constants.MAX_QPATH))[0])


class MdxaHeader:

    def __init__(self):
        self.name = ""
        self.scale = 1  # does not seem to be used by Jedi Academy anyway - or is it? I need it in import!
        self.numFrames = -1
        self.ofsFrames = -1
        self.numBones = -1
        self.ofsCompBonePool = -1
        # this is also MdxaSkelOffsets.baseOffset + MdxaSkelOffsets.boneOffsets[0] - probably a historic leftover
        self.ofsSkel = -1
        self.ofsEnd = -1

    def loadFromFile(self, file: BinaryIO) -> Tuple[bool, ErrorMessage]:
        # check ident
        ident, = struct.unpack("<4s", file.read(4))
        if ident != JAG2Constants.GLA_IDENT:
            print("File does not start with ", JAG2Constants.GLA_IDENT,
                  " but ", ident, " - no GLA!")
            return False, ErrorMessage("Is no GLA file, incorrect file identifier!")
        version, = struct.unpack("<i", file.read(4))
        if version != JAG2Constants.GLA_VERSION:
            return False, ErrorMessage(f"Wrong gla file version! {version} should be {JAG2Constants.GLA_VERSION}")
        self.name = readString(file)
        self.scale, self.numFrames, self.ofsFrames, self.numBones, self.ofsCompBonePool, self.ofsSkel, self.ofsEnd = struct.unpack(
            "<f6i", file.read(7 * 4))
        print("Scale: {:.3f}".format(self.scale))
        return True, NoError

    def saveToFile(self, file: BinaryIO) -> None:
        file.write(struct.pack("<4si64sf6i", JAG2Constants.GLA_IDENT, JAG2Constants.GLA_VERSION, self.name.encode(
        ), self.scale, self.numFrames, self.ofsFrames, self.numBones, self.ofsCompBonePool, self.ofsSkel, self.ofsEnd))


class MdxaBoneOffsets:

    def __init__(self):
        self.baseOffset: int = 2 * 4 + 64 + 4 * 7  # sizeof header
        self.boneOffsets: List[int] = []

    # fail-safe (except for exceptions)
    def loadFromFile(self, file: BinaryIO, numBones: int):
        assert (self.baseOffset == file.tell())
        for i in range(numBones):
            self.boneOffsets.append(struct.unpack("<i", file.read(4))[0])

    def saveToFile(self, file: BinaryIO) -> None:
        assert (file.tell() == self.baseOffset)  # must be after header
        for offset in self.boneOffsets:
            file.write(struct.pack("<i", offset))

# originally called MdxaSkel_t, but I find that name misleading


# whether auto-connecting boneIndex to parentIndex would faithfully reproduce its animation: a
# connected bone is rigidly attached to its parent - its head, expressed in the parent's own
# current local frame, never moves from where it sits at rest; only its own rotation can vary.
# So this checks whether the bone's head, re-expressed in the parent's frame each frame, ever
# drifts from that bind-pose-relative position. If it does, the bone needs independent
# translation that use_connect can't represent. transformsPerFrame is
# MdxaAnimation.computeAbsoluteFrameTransforms's output; an empty list (no animation data
# available) means there's nothing to check against, so it's fine to connect.
def _boneCanConnect(transformsPerFrame: List[Dict[int, "mathutils.Matrix"]], allBones: List["MdxaBone"], boneIndex: int, parentIndex: int) -> bool:
    # rest pose is just the frame where the compressed offset is identity, so put it through the
    # same GLABoneRotToBlender conversion computeAbsoluteFrameTransforms applies to every other
    # frame's combined (offset @ basePoseMat) - otherwise the two are in different coordinate
    # conventions and every comparison shows a large constant offset instead of a real signal.
    childBase = allBones[boneIndex].basePoseMat.toBlender()
    parentBase = allBones[parentIndex].basePoseMat.toBlender()
    JAG2Math.GLABoneRotToBlender(childBase)
    JAG2Math.GLABoneRotToBlender(parentBase)
    restRelativeHead = vector_overload_cast(parentBase.inverted() @ mathutils.Vector(childBase.translation))  # pyright: ignore [reportArgumentType]  # vector supports slices
    for transforms in transformsPerFrame:
        parentTransform = transforms[parentIndex]
        childHead = mathutils.Vector(transforms[boneIndex].translation)  # pyright: ignore [reportArgumentType]  # vector supports slices
        relativeHead = vector_overload_cast(parentTransform.inverted() @ childHead)
        if (relativeHead - restRelativeHead).length > JAG2Constants.BONE_TRANSLATION_ERROR_MARGIN:
            return False
    return True


class MdxaBone:
    def __init__(self):
        self.name = ""
        self.flags: int = 0
        self.parent: int = -1
        self.basePoseMat = JAG2Math.Matrix()
        self.basePoseMatInv = JAG2Math.Matrix()
        self.numChildren = 0
        self.children: List[int] = []
        # not saved, filled by loadBonesFromFile() and when loaded from blender
        self.index: int = -1

    def getSize(self) -> int:
        return struct.calcsize("<64sIi12f12fi{}i".format(self.numChildren))

    def loadFromFile(self, file: BinaryIO) -> None:
        self.name = readString(file)
        self.flags, self.parent = struct.unpack("<Ii", file.read(2 * 4))
        self.basePoseMat.loadFromFile(file)
        self.basePoseMatInv.loadFromFile(file)
        self.numChildren, = struct.unpack("<i", file.read(4))
        for _ in range(self.numChildren):
            self.children.append(struct.unpack("<i", file.read(4))[0])

    def saveToFile(self, file: BinaryIO) -> None:
        file.write(struct.pack(
            "<64sIi", self.name.encode(), self.flags, self.parent))
        self.basePoseMat.saveToFile(file)
        self.basePoseMatInv.saveToFile(file)
        file.write(struct.pack("<i", self.numChildren))
        assert (len(self.children) == self.numChildren)
        for child in self.children:
            file.write(struct.pack("<i", child))

    def loadFromBlender(self, editbone: bpy.types.EditBone, boneIndicesByName: Dict[str, int], bones: List["MdxaBone"], objLocalMat: mathutils.Matrix) -> None:
        # set name
        self.name = editbone.name

        # add index to dictionary
        boneIndicesByName[self.name] = self.index

        # parent is -1 by default - change if there is one.
        if editbone.parent is not None:
            self.parent = boneIndicesByName[editbone.parent.name]
            parent = bones[self.parent]
            parent.numChildren += 1
            parent.children.append(self.index)

        # save (inverted) base pose matrix
        mat = matrix_overload_cast(objLocalMat @ matrix_getter_cast(editbone.matrix))
        # must not be used for blender-internal calculations anymore!
        JAG2Math.BlenderBoneRotToGLA(mat)
        matInv = mat.inverted()
        self.basePoseMat.fromBlender(mat)
        self.basePoseMatInv.fromBlender(matInv)

    # blenderBonesSoFar is a dictionary of boneIndex -> BlenderBone
    # allBones is the list of all MdxaBones
    # use it to set up hierarchy and add yourself once done.
    def saveToBlender(self, armature: bpy.types.Armature, blenderBonesSoFar: Dict[int, bpy.types.EditBone], allBones: List["MdxaBone"], skeletonFixes: JAG2Constants.SkeletonFixes, transformsPerFrame: List[Dict[int, "mathutils.Matrix"]], rollAxes: Dict[int, "mathutils.Vector"], unstretchedLengths: Dict[str, float]) -> None:
        # create bone
        bone = armature.edit_bones.new(self.name)

        # set position
        mat = self.basePoseMat.toBlender()
        pos = mathutils.Vector(mat.translation)  # pyright: ignore [reportArgumentType]  # vector supports slices
        bone.head = pos
        # head is offset a bit.
        # X points towards next bone.
        x_axis = mathutils.Vector(mat.col[0][0:3])  # pyright: ignore [reportArgumentType]  # vector supports slices
        bone.tail = pos + x_axis * JAG2Constants.BONELENGTH
        # set roll
        y_axis = -mathutils.Vector(mat.col[1][0:3])  # pyright: ignore [reportArgumentType]  # vector supports slices
        bone.align_roll(y_axis)
        # Blender's roll is an angle around the bone's OWN axis (head -> tail). Moving the tail
        # later - which is what stretching a parent to its child does - changes that axis, so the
        # stored roll value then means something else and the bone comes out twisted. Keep the
        # intended up-axis so the roll can be re-applied after any such move.
        rollAxes[self.index] = y_axis
        unstretchedLengths[self.name] = bone.length

        # set parent, if any, keeping in mind it might be overwritten
        parentIndex = self.parent
        parentChanges = JAG2Constants.PARENT_CHANGES[skeletonFixes]
        if self.index in parentChanges:
            parentIndex = parentChanges[self.index]
        if parentIndex != -1:
            blenderParent = blenderBonesSoFar[parentIndex]
            bone.parent = blenderParent

            # how many children does the parent have?
            numParentChildren = allBones[parentIndex].numChildren
            # we actually need to take into account the hierarchy changes.
            # so for any bone that used to have this parent but does not anymore, remove one
            for mdxaBone in allBones:
                # if a bone gets its parent changed, and it used to be "my" parent, my parent has one child less.
                if mdxaBone.parent == parentIndex and mdxaBone.index in parentChanges:
                    numParentChildren -= 1
            assert (numParentChildren >= 0)
            # and for any bone that got this as the parent, add one child.
            for _, newParentIndex in parentChanges.items():
                if newParentIndex == parentIndex:
                    numParentChildren += 1
            assert (numParentChildren > 0)  # at least this bone is child.

            # if this is the only child of its parent or has priority: Connect the parent to this.
            if numParentChildren == 1 or self.name in JAG2Constants.PRIORITY_BONES[skeletonFixes]:
                # but only if that doesn't rotate the bone (much) - check this first since it's
                # much cheaper than _boneCanConnect below (which loops over every frame)
                # so calculate the directions...
                oldDir = vector_getter_cast(blenderParent.tail) - vector_getter_cast(blenderParent.head)
                newDir = pos - blenderParent.head
                oldDir.normalize()
                newDir.normalize()
                dotProduct = oldDir.dot(newDir)
                # ... and compare them using the dot product, which is the cosine of the angle between two unit vectors
                if dotProduct > JAG2Constants.BONE_ANGLE_ERROR_MARGIN:
                    canConnect = _boneCanConnect(
                        transformsPerFrame, allBones, self.index, parentIndex)
                    # Stretching the parent's tail to this child is cosmetic in the sense that
                    # it changes neither head positions nor directions - but it is NOT free.
                    # It changes bone.length, and Blender's pose evaluation places a child's
                    # local origin at its parent's TAIL. Decoupling it from use_connect (to make
                    # a full _humanoid.gla import look less like a field of stubs) turned out to
                    # corrupt the posed frames: measured against a reference import, the error
                    # grew monotonically with the number of stretched ancestors a bone had -
                    # 0 ancestors gave exactly 0.0000, 6 gave 1.4828.
                    #
                    # So the original coupling is the default. STRETCH_BONES_TO_CHILDREN exists
                    # only to reproduce the decoupled appearance, at that cost.
                    newLength = (pos - vector_getter_cast(blenderParent.head)).length
                    tailMoved = False
                    if ((canConnect or JAG2Constants.STRETCH_BONES_TO_CHILDREN)
                            and newLength > JAG2Constants.MIN_BONE_LENGTH):
                        blenderParent.tail = pos
                        # The parent's direction just changed, so its stored roll no longer means
                        # what it did when align_roll was called at creation time. Re-apply the
                        # parent's intended up-axis, otherwise a stretched bone comes out twisted
                        # about its own length.
                        parentRollAxis = rollAxes.get(parentIndex)
                        if parentRollAxis is not None:
                            blenderParent.align_roll(parentRollAxis)
                        tailMoved = True
                    # use_connect requires the head to actually sit on the parent's tail, so it
                    # is only valid where the tail was moved there.
                    if tailMoved and canConnect:
                        bone.use_connect = True

        # save to created bones
        blenderBonesSoFar[self.index] = bone


class MdxaSkel:
    def __init__(self):
        self.bones: List[MdxaBone] = []
        self.armature = None
        self.armatureObject = None

    def loadFromFile(self, file: BinaryIO, offsets: MdxaBoneOffsets):
        for i, offset in enumerate(offsets.boneOffsets):
            file.seek(offsets.baseOffset + offset)
            bone = MdxaBone()
            bone.loadFromFile(file)
            bone.index = i
            self.bones.append(bone)

    def saveToFile(self, file: BinaryIO, header: MdxaHeader):
        assert (file.tell() == header.ofsSkel)
        for bone in self.bones:
            bone.saveToFile(file)

    def fitsArmature(self, armature) -> Tuple[bool, ErrorMessage]:
        for bone in self.bones:
            if bone.name not in armature.bones:
                return False, ErrorMessage(f"Bone {bone.name} not found in existing skeleton_root armature!")
        return True, NoError

    def saveToBlender(self, scene_root: bpy.types.Object, skeletonFixes: JAG2Constants.SkeletonFixes, animation: Optional["MdxaAnimation"] = None) -> Tuple[bool, ErrorMessage]:
        # computed once up front (not per-bone) since it doesn't depend on Blender bone state -
        # see MdxaBone.saveToBlender/_boneCanConnect for why it's needed.
        transformsPerFrame = animation.computeAbsoluteFrameTransforms(self) if animation is not None else []

        #  Creation
        # create armature
        self.armature = bpy.data.armatures.new("skeleton_root")
        # create object
        self.armature_object = bpy.data.objects.new(
            "skeleton_root", self.armature)
        # set parent
        self.armature_object.parent = scene_root
        # link object to scene
        assert bpy.context.scene is not None
        bpy.context.scene.collection.objects.link(self.armature_object)

        #  Set the armature as active and go to edit mode to add bones
        assert bpy.context.view_layer is not None
        bpy.context.view_layer.objects.active = self.armature_object
        bpy.ops.object.mode_set(mode='EDIT')
        # list of indices of already created bones - only those bones with this as parent will be added
        createdBonesIndices = [-1]
        # bones yet to be created
        uncreatedBones = list(self.bones)
        parentChanges = JAG2Constants.PARENT_CHANGES[skeletonFixes]
        # Blender EditBones so far by index
        blenderEditBones: Dict[int, bpy.types.EditBone] = {}
        # Intended up-axis per bone, so the roll can be restored whenever a tail moves later.
        rollAxes: Dict[int, mathutils.Vector] = {}
        # Each bone's length before any child stretched its parent, so the summary below can
        # tell a stretched bone from one that is merely scaled by the file's base pose scale.
        unstretchedLengths: Dict[str, float] = {}
        while len(uncreatedBones) > 0:
            # whether a new bone was created this time - if not, there's a hierarchy problem
            createdBone = False
            newUncreatedBones = []
            for bone in uncreatedBones:
                # only create those bones whose parent has already been created.
                if bone.index in parentChanges:
                    parent = parentChanges[bone.index]
                else:
                    parent = bone.parent
                if parent in createdBonesIndices:
                    bone.saveToBlender(
                        self.armature, blenderEditBones, self.bones, skeletonFixes,
                        transformsPerFrame, rollAxes, unstretchedLengths)
                    createdBonesIndices.append(bone.index)
                    createdBone = True
                else:
                    newUncreatedBones.append(bone)
            uncreatedBones = newUncreatedBones
            if not createdBone:
                bpy.ops.object.mode_set(mode='OBJECT')
                return False, ErrorMessage("gla has hierarchy problems!")
        # leave armature edit mode
        bpy.ops.object.mode_set(mode='OBJECT')
        connected = sum(1 for bone in self.armature.bones if bone.use_connect)
        # NOT "length != BONELENGTH": a GLA base pose carries the file's uniform scale, so an
        # UNstretched bone is BONELENGTH * scale long (2.56 for a 0.64 file) and the naive test
        # counted all 53 as stretched. Compare against each bone's own unstretched length.
        stretched = sum(1 for bone in self.armature.bones
                        if abs(bone.length - unstretchedLengths.get(bone.name, bone.length)) > 1e-4)
        print("Skeleton: {} bones, {} stretched to their child (appearance), "
              "{} rigidly connected (locks out independent translation).".format(
                  len(self.armature.bones), stretched, connected))
        return True, NoError


class MdxaFrame:
    def __init__(self):
        self.boneIndices: List[int] = []

    # returns the highest referenced index - not nice from a design standpoint but saves space, which is probably good.
    def loadFromFile(self, file, numBones):
        maxIndex = 0
        for i in range(numBones):
            # bone indices are only 3 bytes long - with 20k+ frames 25% less is quite a bit, reportedly.
            index, = struct.unpack("<I", file.read(3) + b"\0")
            maxIndex = max(maxIndex, index)
            self.boneIndices.append(index)
        return maxIndex

    def saveToFile(self, file):
        # See MdxaAnimation.saveToFile - frames are normally written in one bulk pass. This
        # stays for any caller that writes a single frame.
        file.write(self.packedIndices())

    def packedIndices(self) -> bytes:
        raw = struct.pack("<%dI" % len(self.boneIndices), *self.boneIndices)
        packed = bytearray(len(self.boneIndices) * 3)
        # Drop the high byte of every little-endian uint32: indices are 3 bytes in the file.
        packed[0::3] = raw[0::4]
        packed[1::3] = raw[1::4]
        packed[2::3] = raw[2::4]
        return bytes(packed)


class MdxaBonePool:
    def __init__(self):
        # during import, this is a list of CompBone objects
        # during exports, it's a list of 14-byte-objects (compressed bones)
        self.bones: List[JAG2Math.CompBone] | List[bytes] = []

    def loadFromFile(self, file, numCompBones):
        for i in range(numCompBones):
            compBone = JAG2Math.CompBone.loadFromFile(file)
            downcast(List[JAG2Math.CompBone], self.bones).append(compBone)

    def saveToFile(self, file: BinaryIO) -> None:
        # One write instead of one per pool entry (~727k for a re-exported _humanoid).
        file.write(b"".join(downcast(List[bytes], self.bones)))

# Frames & Compressed Bone Pool


class MdxaAnimation:
    def __init__(self):
        self.frames: List[MdxaFrame] = []
        self.bonePool = MdxaBonePool()

    def loadFromFile(self, file: BinaryIO, header: MdxaHeader, startFrame: int, numFrames: int) -> Tuple[bool, ErrorMessage]:
        # read frames
        if file.tell() != header.ofsFrames:
            print("Info: Frames in .gla not encountered when expected (at ", file.tell(), " instead of ", header.ofsFrames,
                  "), seeking correct position. There could be a bug in the importer (bad) or the file could be unusual - but not necessarily wrong (no problem).", sep="")
            file.seek(header.ofsFrames)

        # prepare frame start/end settings
        if numFrames == -1:
            assert (startFrame == 0)
            numFrames = header.numFrames
        else:
            print("Reading {} frames, starting at {}".format(
                numFrames, startFrame))
        if startFrame >= header.numFrames:
            print("Warning: StartFrame beyond existing frames, using last one")
            startFrame = header.numFrames - 1
            numFrames = 1
        if startFrame + numFrames > header.numFrames:
            print("Warning: Trying to import more frames than there are, fixing")
            numFrames = header.numFrames - startFrame
        # skip first startFrame frames
        # 1 = from current position
        file.seek(startFrame * 3 * header.numBones, 1)

        # read (remaining) frames
        maxIndex = -1
        for i in range(numFrames):
            frame = MdxaFrame()
            # loadFromFile returns highest read index
            maxIndex = max(maxIndex, frame.loadFromFile(file, header.numBones))
            self.frames.append(frame)

        # read compressed bone pool
        # see if we reached it yet
        curPos = file.tell()
        if curPos != header.ofsCompBonePool:
            # we're not yet there. If we're off by 0-3 bytes, it's because 32-bit-alignment is forced. Silently seek correct position. Otherwise: warn (and seek correct position, too)
            # if we're only importing some frames, we may or may not be there yet, of course, so don't warn.
            if curPos > header.ofsCompBonePool or (header.ofsCompBonePool > curPos + 3 and numFrames == header.numFrames):
                print("Info: Bone Pool in .gla not encountered when expected (at ", file.tell(), " instead of ", header.ofsCompBonePool,
                      "), seeking correct position. There could be a bug in the importer (bad) or the file could be unusual - but not necessarily wrong (no problem).", sep="")
            file.seek(header.ofsCompBonePool)
        # there's one more object than the highest index since those start at 0
        self.bonePool.loadFromFile(file, maxIndex + 1)

        # file should be over now, bone pool is usually the last thing. I'm not sure it has to be, but so far it has always been.
        if file.tell() != header.ofsEnd and numFrames == header.numFrames:
            print(
                "Info: .gla Bone Pool read but file not over yet - this likely indicates a problem.")
        return True, NoError

    # bones in parent-first order - shared by saveToBlender and computeAbsoluteFrameTransforms.
    @staticmethod
    def _hierarchyOrder(skeleton: MdxaSkel) -> List[int]:
        hierarchyOrder: List[int] = []
        while len(hierarchyOrder) < len(skeleton.bones):
            addedSomething = False
            for bone in skeleton.bones:
                if bone.index in hierarchyOrder:
                    continue
                if bone.parent != -1 and bone.parent not in hierarchyOrder:
                    continue
                hierarchyOrder.append(bone.index)
                addedSomething = True
            assert (addedSomething)
        return hierarchyOrder

    # for each frame, each bone's absolute (armature-space, Blender-convention) posed transform,
    # via the same forward-kinematics math applied to actual Blender pose bones in saveToBlender
    # below - but pure Python, so it can run before any Blender armature exists (used to decide
    # auto-connect behavior ahead of bone creation, see MdxaBone.saveToBlender/_boneCanConnect).
    # Memory: measured against real production skeletons (headless Blender, resource.getrusage
    # RSS) - _humanoid-ja.gla (53 bones, 21376 frames) costs ~193MB for the returned structure,
    # _humanoid-jk2.gla (72 bones, 17278 frames) ~196MB - both well under 1GB, and it's transient:
    # the caller (MdxaSkel.saveToBlender) only keeps this in a local variable for the duration of
    # bone creation, not for the lifetime of the import.
    def computeAbsoluteFrameTransforms(self, skeleton: MdxaSkel) -> List[Dict[int, mathutils.Matrix]]:
        if not self.frames:
            return []
        hierarchyOrder = self._hierarchyOrder(skeleton)
        basePoses = [bone.basePoseMat.toBlender() for bone in skeleton.bones]
        compBones = downcast(List[JAG2Math.CompBone], self.bonePool.bones)
        result: List[Dict[int, mathutils.Matrix]] = []
        for frame in self.frames:
            absoluteOffsets: Dict[int, mathutils.Matrix] = {}
            transforms: Dict[int, mathutils.Matrix] = {}
            for index in hierarchyOrder:
                bone = skeleton.bones[index]
                offset = compBones[frame.boneIndices[index]].matrix
                if bone.parent != -1:
                    offset = matrix_overload_cast(absoluteOffsets[bone.parent] @ offset)
                absoluteOffsets[index] = offset
                transformation = matrix_overload_cast(offset @ basePoses[index])
                JAG2Math.GLABoneRotToBlender(transformation)
                transforms[index] = transformation
            result.append(transforms)
        return result

    def saveToFile(self, file: BinaryIO, header: MdxaHeader):
        assert (file.tell() == header.ofsFrames)
        # One buffer, one write. Writing each bone index individually meant ~1.13M
        # struct.pack + file.write calls for _humanoid (53 bones x 21376 frames); building the
        # whole block at once measured 3.5x faster and produces byte-identical output.
        file.write(b"".join(frame.packedIndices() for frame in self.frames))
        # add padding if not 32 bit aligned (due to 3-byte-indices)
        if file.tell() % 4 != 0:
            # from_what = 1 -> from current position
            file.seek(4 - (file.tell() % 4), 1)
        assert (file.tell() == header.ofsCompBonePool)
        self.bonePool.saveToFile(file)

    def saveToBlender(self, skeleton: MdxaSkel, armature: bpy.types.Object, scale, animations: Optional[JAG2AnimationCFG.AnimationCFG] = None) -> Tuple[bool, ErrorMessage]:
        startTime = time.time()
        #   Bone Position Set Order
        # bones have to be set in hierarchical order - their position depends on their parent's absolute position, after all.
        # so this is the order in which bones have to be processed.
        hierarchyOrder = self._hierarchyOrder(skeleton)
        # for going leaf to root

        assert armature.pose is not None

        # Absolute (armature-space, Blender-convention) posed transform per frame per bone.
        # This is exactly the forward kinematics the old per-frame loop did inline; hoisting it
        # lets _bakeAction consume it directly. Transient - roughly 193MB for _humanoid-ja.gla
        # (53 bones, 21376 frames), released when this function returns.
        _reportedReparenting[0] = False
        transformsPerFrame = self.computeAbsoluteFrameTransforms(skeleton)

        #   Prepare animation
        scene = bpy.context.scene
        assert scene is not None
        scene.frame_start = 0
        numFrames = len(self.frames)
        scene.frame_end = numFrames - 1

        if scale == 0:
            # if True:
            scale = 1
        else:
            scale = 1 / scale
        scaleMatrix = mathutils.Matrix([
            [scale, 0, 0, 0],
            [0, scale, 0, 0],
            [0, 0, scale, 0],
            [0, 0, 0, 1]
        ])

        # show progress every 1000 steps, but at least 10 times)
        nextProgressDisplayTime = time.time() + PROGRESS_UPDATE_INTERVAL

        #   Export animation
        if animations:
            # enter pose mode to make edits to the bone transforms
            bpy.ops.object.mode_set(mode='POSE', toggle=False)
            lastSequenceNum = 0
            unplacedSequences: List[str] = []
            containerVerified = False
            lastStripError = ""
            placedStrips = 0
            # create NLA tracks keeping track of all animations
            animData = armature.animation_data_create()
            animData.use_nla = True

            # Track order. nla_tracks.new() appends on top, and a higher track overrides a
            # lower one. Blender cannot make a strip shorter than one frame OF LENGTH, so a
            # 1-frame "still" at frame N occupies N..N+1 - and if it sits above the sequence
            # that owns frame N+1, it holds its own pose over that frame instead.
            #
            # A dump of the real NLA state confirmed exactly that, e.g. at frame 19:
            #     [0] Sequences Layer 1  BOTH_A1_BL_TR  frames 18-23
            #     [1] Stills Layer 1     BOTH_B1_BL___  frames 18-19   <- wins
            # and showed that only "Sequences Layer 1" is affected: the lazily created
            # "Sequences Layer 2".."Layer 6" all end up ABOVE "Stills Layer 1" anyway.
            #
            # So only the Stills tracks need to move. A previous attempt reordered ALL sixteen
            # tracks AND removed the unused ones afterwards, which broke 521 other frames; the
            # dump shows that was far more invasive than necessary. Creating the Stills tracks
            # up front puts them at the bottom, while the Sequences tracks stay lazily created
            # and keep the relative order they had. Nothing is removed afterwards.
            #
            # Exactly as many Stills tracks as the 1-frame sequences need, created BEFORE any
            # Sequences track. nla_tracks.new() appends on top and a higher track overrides a
            # lower one, and nla_tracks.new(prev=...) can only insert AFTER a given track - so
            # creating them up front is the only way to get them underneath.
            #
            # This matters because Blender cannot make a strip shorter than one frame OF LENGTH:
            # a 1-frame "still" at frame N occupies N..N+1 and, from above, holds its pose over
            # frame N+1, which belongs to another sequence.
            #
            # Counting rather than pre-creating all eight: the stock _humanoid needs two,
            # because TORSO_WEAPONIDLE2 and TORSO_WEAPONREADY2 both start at 15456 - the only
            # duplicate still start frame in the whole cfg.
            #
            # (Earlier measurements appeared to rule this out - "2 tracks: 478 wrong frames" -
            # but those were taken while the export was non-deterministic, from an id() ^ id()
            # cache key collision in JAG2PoseSampler. Three identical runs then gave 284, 68 and
            # 432 wrong frames, so every one of those numbers was noise.)
            for layerNum in range(1, _neededStillsLayers(animations) + 1):
                nla_track = animData.nla_tracks.new()
                nla_track.name = "Stills Layer {}".format(layerNum)

            nla_track = animData.nla_tracks.new()
            nla_track.name = "Sequences Layer 1"
            nla_track.select = True
            # Only make the first layer visible. NOTE: solo mode disables every other track, so
            # GLA.loadFromBlender must clear this before sampling poses or everything not on
            # this layer exports as the rest pose - see the save/restore there.
            nla_track.is_solo = True

            for sequenceNum, sequence in enumerate(animations.sequences):
                action = bpy.data.actions.new(sequence.name)
                action.g2_sequence_prop.loop_frame = sequence.loop  # pyright: ignore[reportAttributeAccessIssue]
                action.g2_sequence_prop.fps = sequence.fps  # pyright: ignore[reportAttributeAccessIssue]
                # The sequence's true length, so the cfg export does not have to reconstruct it
                # from the strip (which cannot be shorter than one frame of length) or from the
                # track's name.
                action.g2_sequence_prop.num_frames = sequence.num_frames  # pyright: ignore[reportAttributeAccessIssue]

                # NOTE, so this is not "fixed" a third time. A 1-frame "still" cannot have a
                # 1-frame strip - Blender's minimum strip length is one frame - so its strip
                # spans [start, start+1] and, sitting above the sequence tracks, wins that
                # second frame. That costs 52 of 21376 frames on a full _humanoid round trip
                # (0.24%), and the animation.cfg confirms 51 of those 52 sit directly after a
                # 1-frame sequence.
                #
                # Two fixes were tried and MEASURED against the original GLA:
                #   create the Stills tracks first, so sequences override them
                #       -> 52 fixed, 521 broken (20 contiguous blocks that come out constant)
                #   bake the following GLA frame into the still as a second key, and widen the
                #   strip to match
                #       -> 2 fixed, 360 broken (again contiguous blocks); 50 of the 52 remained
                # Both made it worse, and neither mechanism is understood. The 0.24% stands
                # until someone can explain the block corruption, not guess at it.
                bakeFrames = sequence.num_frames
                # Action Slots (multi-user actions) were only introduced in Blender 4.4 - older
                # supported versions (down to 4.1) have neither Action.slots nor
                # AnimData/NlaStrip.action_slot, and don't need them either.
                slot = None
                if hasattr(action, "slots"):
                    slot = action.slots.get("Armature")
                    if not slot:
                        slot = action.slots.new('OBJECT', "Armature")
                animData.action = action
                if hasattr(animData, "action_slot"):
                    animData.action_slot = slot

                # Once per import: make sure the FCurves we are about to create are the ones
                # Blender reads back. Getting this wrong does not raise, it just yields empty
                # actions - so fail loudly here instead of silently importing nothing.
                if not containerVerified:
                    ok, why = _verifyFCurveContainer(
                        action, slot, armature, skeleton.bones[0].name)
                    if not ok:
                        return False, ErrorMessage(f"Cannot write animation FCurves: {why}")
                    containerVerified = True

                # The strip is created *before* baking, deliberately. strips.new() derives the
                # strip's footprint from the action's frame range and rejects overlaps against
                # it. With an empty action that footprint is one frame, so a sequence can be
                # placed even where it shares boundary frames with a neighbour; the real length
                # is then set explicitly below. Baking first made strips.new() test the full
                # span and fail for most sequences, leaving the file with no strips at all.
                strip = None
                nla_track_index = 1
                # pick a nla track that can hold the animation, overlapping strips is not possible
                while strip is None and nla_track_index < 9:
                    track_name = "Stills Layer {}".format(nla_track_index) if sequence.num_frames == 1 else "Sequences Layer {}".format(nla_track_index)
                    nla_track = animData.nla_tracks.get(track_name)
                    if nla_track is None:
                        nla_track = animData.nla_tracks.new()
                        nla_track.name = track_name
                    nla_track.select = True
                    try:
                        strip = nla_track.strips.new(action.name, sequence.start_frame, action)
                    except Exception as e:
                        # Only an overlap should land here. Anything else means the strip could
                        # not be created at all, which previously vanished into a bare `except`.
                        strip = None
                        lastStripError = str(e)
                        nla_track_index += 1
                        continue
                    strip.action_frame_start = 0
                    strip.action_frame_end = max(0, bakeFrames - 1)
                    # strips.new() defaults to extrapolation HOLD, which makes a strip hold
                    # its first pose over every earlier frame and its last over every later
                    # one, on its own track. With tracks stacked in REPLACE blend mode, a
                    # strip on layer 2 would then override layer 1 across the whole timeline,
                    # not just its own range - which would corrupt the re-export as soon as
                    # is_solo is off. Sequences are discrete ranges, so: no extrapolation.
                    strip.extrapolation = 'NOTHING'
                    strip.blend_type = 'REPLACE'
                    if hasattr(strip, "action_slot"):
                        strip.action_slot = slot
                    nla_track_index += 1
                if strip is None:
                    # All 8 candidate layers were already occupied at this frame position. The
                    # action still exists but has no strip, so the cfg export won't see it -
                    # previously this happened without a word.
                    unplacedSequences.append(sequence.name)
                else:
                    placedStrips += 1

                # Bake the sequence's frames into the action's FCurves. No frame_set is needed:
                # _bakeAction solves for matrix_basis analytically instead of letting the
                # depsgraph evaluate it.
                _bakeAction(action, slot, skeleton, armature, transformsPerFrame,
                            hierarchyOrder, sequence.start_frame, bakeFrames)

                # Re-assert the strip's extent now that the action actually has keyframes, so a
                # strip created against an empty action does not end up time-scaling it.
                if strip is not None:
                    strip.action_frame_start = 0
                    strip.action_frame_end = max(0, bakeFrames - 1)
                    strip.frame_start = sequence.start_frame
                    strip.frame_end = sequence.start_frame + max(0, bakeFrames - 1)
                    if abs(strip.scale - 1.0) > 1e-6:
                        strip.scale = 1.0
                    strip.repeat = 1.0

                # show progress bar / remaining time
                if time.time() >= nextProgressDisplayTime:
                    numProcessedFrames = sequenceNum - lastSequenceNum
                    framesRemaining = len(animations.sequences) - sequenceNum
                    # only take the frames since the last update into account since the speed varies.
                    # speed's roughly inversely proportional to the current frame number so I could use that to predict remaining time...
                    timeRemaining = PROGRESS_UPDATE_INTERVAL * framesRemaining / numProcessedFrames

                    print("Sequence {}/{} - {:.2%} - remaining time: ca. {:.0f}m {:.0f}s".format(
                        sequenceNum, len(animations.sequences), sequenceNum / len(animations.sequences), timeRemaining // 60, timeRemaining % 60))

                    lastSequenceNum = sequenceNum
                    nextProgressDisplayTime = time.time() + PROGRESS_UPDATE_INTERVAL

                # Bake the sequence's frames straight into the action's FCurves. No frame_set is
                # needed any more: _bakeAction solves for matrix_basis analytically instead of
                # letting the depsgraph evaluate it, so there is no stale-scene-frame hazard of
                # the kind that used to corrupt the first frame of every sequence after the first.
                _bakeAction(action, slot, skeleton, armature, transformsPerFrame,
                            hierarchyOrder, sequence.start_frame, sequence.num_frames)

            # Blender re-derives extend modes and blend in/out from strip OVERLAPS whenever a
            # track changes ("Extend modes (other than 'nothing') now get automatically
            # determined (after transforms)"), so setting extrapolation='NOTHING' when a strip
            # is created is not enough - every later strip added to the same track can flip an
            # earlier one back to HOLD or HOLD_FORWARD. A strip left on HOLD then holds its pose
            # across neighbouring ranges, which is exactly the contiguous-block corruption that
            # made four different attempts at the 1-frame-still problem come out worse, in
            # amounts that were not monotonic in anything we changed.
            #
            # Sequences are discrete ranges, so re-assert it on everything, once, at the end.
            reset = 0
            for track in animData.nla_tracks:
                for strip in track.strips:
                    if strip.extrapolation != 'NOTHING':
                        strip.extrapolation = 'NOTHING'
                        reset += 1
                    strip.use_auto_blend = False
                    strip.blend_in = 0.0
                    strip.blend_out = 0.0
            if reset:
                print("NLA: reset extrapolation on {} strip(s) that Blender had auto-changed "
                      "to hold.".format(reset))

            print("NLA: created {} strip(s) for {} sequence(s) across {} track(s).".format(
                placedStrips, len(animations.sequences),
                len([t for t in animData.nla_tracks if len(t.strips)])))
            for track in animData.nla_tracks:
                if not len(track.strips):
                    continue
                first = track.strips[0]
                print("  track '{}': {} strip(s), solo={}, muted={}; first='{}' "
                      "frames {:.0f}-{:.0f}, action='{}', extrapolation={}, scale={:.3f}".format(
                          track.name, len(track.strips), track.is_solo, track.mute,
                          first.name, first.frame_start, first.frame_end,
                          first.action.name if first.action else "<none>",
                          first.extrapolation, first.scale))
            if unplacedSequences:
                shown = ", ".join(unplacedSequences[:10])
                more = "" if len(unplacedSequences) <= 10 else " (and {} more)".format(
                    len(unplacedSequences) - 10)
                detail = " Last error from strips.new(): {}".format(
                    lastStripError) if lastStripError else ""
                print("Warning: {} sequence(s) could not be placed on any of the 8 NLA layers "
                      "and have no strip, so they will not appear in an exported animation.cfg: "
                      "{}{}.{}".format(len(unplacedSequences), shown, more, detail))
            # remove action from the animation data to stop previewing a single action
            if hasattr(animData, "action_slot"):
                animData.action_slot = None  # type: ignore
            animData.action = None  # type: ignore

            # enter object mode when done
            bpy.ops.object.mode_set(mode='OBJECT', toggle=False)
        else:
            # No animation.cfg: one action covering every frame. Same bulk bake as above; the
            # old code here called bpy.ops.object.mode_set twice per bone per frame, which was
            # even slower than the CFG path's visual_transform_apply.
            action = bpy.data.actions.new("all_frames")
            slot = None
            if hasattr(action, "slots"):
                slot = action.slots.get("Armature")
                if not slot:
                    slot = action.slots.new('OBJECT', "Armature")
            animData = armature.animation_data_create()
            animData.action = action
            if hasattr(animData, "action_slot"):
                animData.action_slot = slot
            ok, why = _verifyFCurveContainer(action, slot, armature, skeleton.bones[0].name)
            if not ok:
                return False, ErrorMessage(f"Cannot write animation FCurves: {why}")
            _bakeAction(action, slot, skeleton, armature, transformsPerFrame,
                        hierarchyOrder, 0, numFrames)

            scene.frame_current = 1

        print("Applied animation in {:.1f}s".format(time.time() - startTime))
        return True, NoError


class AnimationLoadMode(Enum):
    NONE = 'NONE'
    CFG = "CFG"
    ALL = 'ALL'
    RANGE = 'RANGE'


class GLA:

    def __init__(self):
        # whether this is the automatic default skeleton
        self.isDefault = False  # TODO replace with `skeleton_object is None`
        self.header = MdxaHeader()
        self.boneOffsets = MdxaBoneOffsets()
        self.skeleton = MdxaSkel()
        self.boneIndexByName: Dict[str, int] = {}
        # boneNameByIndex = {} #just use bones[index].name
        # the Blender Armature / Object
        self.skeleton_armature: Optional[bpy.types.Armature] = None
        self.skeleton_object: Optional[bpy.types.Object] = None
        self.animation = MdxaAnimation()

    def loadFromFile(self, filepath_abs: str, loadAnimation: AnimationLoadMode, startFrame: int, numFrames: int) -> Tuple[bool, ErrorMessage]:
        print("Loading {}...".format(filepath_abs))
        try:
            file: BinaryIO = open(filepath_abs, mode="rb")
        except IOError:
            print("Could not open file: {}".format(filepath_abs))
            return False, ErrorMessage("Could not open file!")
        profiler = MrwProfiler.SimpleProfiler(True)
        # load header
        profiler.start("reading header")
        success, message = self.header.loadFromFile(file)
        if not success:
            return False, message
        profiler.stop("reading header")
        # load offsets (directly after header, always)
        profiler.start("reading bone hierarchy")
        self.boneOffsets.loadFromFile(file, self.header.numBones)
        # load bones
        self.skeleton.loadFromFile(file, self.boneOffsets)
        # build lookup map
        for bone in self.skeleton.bones:
            self.boneIndexByName[bone.name] = bone.index
        profiler.stop("reading bone hierarchy")
        if loadAnimation != AnimationLoadMode.NONE:
            profiler.start("reading animations")
            if loadAnimation in [AnimationLoadMode.ALL, AnimationLoadMode.CFG]:
                success, message = self.animation.loadFromFile(
                    file, self.header, 0, -1)
            else:
                assert (loadAnimation == AnimationLoadMode.RANGE)
                success, message = self.animation.loadFromFile(
                    file, self.header, startFrame, numFrames)
            if not success:
                return False, message
            profiler.stop("reading animations")
        return True, NoError

    def loadFromBlender(self, gla_filepath_rel: str, gla_reference_abs: str) -> Tuple[bool, ErrorMessage]:
        # fill out header name
        # The header name is the reference other files use to find this GLA. Carcass writes it
        # as a base-relative path with forward slashes, no leading slash and no extension - e.g.
        # "models/players/_humanoid/_humanoid". Writing the caller's path verbatim produced
        # "/models/players/_humanoid/_humanoid.gla", which matches neither, so normalise it.
        name = gla_filepath_rel.replace("\\", "/").lstrip("/")
        if name.lower().endswith(".gla"):
            name = name[:-4]
        self.header.name = name

        # find skeleton_root
        if "skeleton_root" not in bpy.data.objects:
            return False, ErrorMessage("No skeleton_root object found!")
        skeleton_object = bpy_generic_cast(bpy.types.Object, bpy.data.objects["skeleton_root"])
        self.skeleton_object = skeleton_object
        if self.skeleton_object.type != 'ARMATURE':
            return False, ErrorMessage("skeleton_root is no Armature!")
        self.skeleton_armature = downcast(bpy.types.Armature, optional_cast(bpy.types.Object, self.skeleton_object).data)
        self.header.scale = self.skeleton_object.g2_prop.scale / 100  # pyright: ignore [reportAttributeAccessIssue]

        # make skeleton_root the active object
        assert bpy.context.view_layer is not None
        bpy.context.view_layer.objects.active = self.skeleton_object
        self.skeleton_object.select_set(True)
        self.skeleton_object.hide_viewport = False

        # in case of rescaled/moved skeleton object: get transformation (assuming we're a child of scene_root)
        localMat = matrix_getter_cast(self.skeleton_object.matrix_local)

        # if there's a reference GLA (for bone indices), load that
        if gla_reference_abs != "":
            print("Using reference GLA skeleton - warning: there's no check beyond bone names (hierarchy, base pose etc.)")

            # load reference GLA
            referenceGLA = GLA()
            success, message = referenceGLA.loadFromFile(
                gla_reference_abs, AnimationLoadMode.NONE, 0, 0)
            if not success:
                return False, ErrorMessage(f"Could not load reference GLA: {message}")

            # copy relevant data from reference
            self.boneIndexByName = referenceGLA.boneIndexByName
            # will be changed, but reference is discarded later anyway
            self.skeleton = referenceGLA.skeleton
            self.boneOffsets = referenceGLA.boneOffsets
            self.header.ofsFrames = referenceGLA.header.ofsFrames
            self.header.ofsSkel = referenceGLA.header.ofsSkel
            self.header.numBones = referenceGLA.header.numBones

            # verify all bones exist
            success, message = self.skeleton.fitsArmature(
                self.skeleton_armature)
            if not success:
                return False, ErrorMessage(f"Armature does not fit reference: {message}")

            # Names matching is not enough. The offsets written below are computed against
            # Blender's rest pose, but the file stores the *reference's* basePoseMat, and the
            # engine reconstructs pose = offset @ storedBasePose. If the two disagree in
            # orientation or position, every frame comes out displaced.
            #
            # They legitimately disagree in SCALE though: a GLA base pose carries the file's
            # uniform scale (0.64 for _humanoid), while Blender's bone.matrix_local is always
            # orthonormal. That difference cancels out - the relative offsets are scale free
            # either way - so comparing the matrices element by element would flag every single
            # export (|1.0 - 0.64| = 0.36 on the diagonal). Normalise before comparing.
            worstBone = ""
            worstDelta = 0.0
            for bone in self.skeleton.bones:
                blenderBase = matrix_overload_cast(localMat @ matrix_getter_cast(
                    bpy_generic_cast(bpy.types.Bone,
                                     self.skeleton_armature.bones[bone.name]).matrix_local))
                JAG2Math.BlenderBoneRotToGLA(blenderBase)
                referenceBase = bone.basePoseMat.toBlender()
                delta = _basePoseDelta(blenderBase, referenceBase)
                if delta > worstDelta:
                    worstDelta = delta
                    worstBone = bone.name
            if worstDelta > BASE_POSE_TOLERANCE:
                print("Warning: the armature's rest pose differs from the reference GLA's base "
                      "pose (worst: {} by {:.4f}). The engine reconstructs poses against the "
                      "reference's base pose, so the exported animation will be displaced. Make "
                      "the rest poses identical, or export without a reference.".format(
                          worstBone, worstDelta))
            else:
                print("Reference GLA base pose matches the armature's rest pose (max deviation "
                      "{:.6f}).".format(worstDelta))

        # or no reference GLA? build new skeleton then.
        else:

            # enter edit mode so we can access editbones
            bpy.ops.object.mode_set(mode='EDIT')

            # populate bone hierarchy
            bonesToAdd = [bpy_generic_cast(bpy.types.EditBone, bone) for bone in self.skeleton_armature.edit_bones]
            while len(bonesToAdd) > 0:
                addedSomething = False
                newBonesToAdd = []
                for bone in bonesToAdd:
                    # add bones whose parents have already been added
                    if bone.parent is None or bone.parent.name in self.boneIndexByName:
                        # create this bone
                        newBone = MdxaBone()

                        # set its index (will be appended, hence the size)
                        newBone.index = len(self.skeleton.bones)

                        # read the rest from the editbone
                        newBone.loadFromBlender(
                            bone, self.boneIndexByName, self.skeleton.bones, localMat)

                        # append bone
                        self.skeleton.bones.append(newBone)
                        addedSomething = True
                    else:
                        newBonesToAdd.append(bone)
                bonesToAdd = newBonesToAdd
                if not addedSomething:
                    return False, ErrorMessage("Hierarchy error, failed to find bone parent (most likely a bug, actually)")

            # calculate bone file position offsets
            # first bone starts after the bone offsets
            offset = 4 * len(self.skeleton.bones)
            self.header.ofsSkel = offset + \
                self.boneOffsets.baseOffset  # save first bones position
            for bone in self.skeleton.bones:
                self.boneOffsets.boneOffsets.append(offset)
                offset += bone.getSize()

            self.header.ofsFrames = self.boneOffsets.baseOffset + \
                offset  # frames start after last bone
            self.header.numBones = len(self.skeleton.bones)

        #   retrieve animations

        print("Compressing animation...")

        # enter pose mode
        bpy.ops.object.mode_set(mode='POSE')

        # create a dictionary containing the indices of already added compressed bones - lookup should be faster than a linear search through the existing compressed bones (at the cost of more RAM usage - that's ok)
        compBoneIndices = {}

        scene = bpy.context.scene
        assert scene is not None
        assert self.skeleton_object.pose is not None

        numBones = self.header.numBones

        # Everything frame-invariant, computed once. Previously the PoseBone/Bone name lookups,
        # the axis conversion and the base pose inversion all ran per bone *per frame* - for
        # _humanoid that is ~1.1M redundant matrix inversions and ~2.3M dict lookups.
        poseBones: List[bpy.types.PoseBone] = []
        baseInvs: List[mathutils.Matrix] = []
        for bone in self.skeleton.bones:
            basebone = bpy_generic_cast(bpy.types.Bone, self.skeleton_armature.bones[bone.name])
            poseBones.append(bpy_generic_cast(
                bpy.types.PoseBone, self.skeleton_object.pose.bones[bone.name]))
            basePoseMat = matrix_overload_cast(
                localMat @ matrix_getter_cast(basebone.matrix_local))
            JAG2Math.BlenderBoneRotToGLA(basePoseMat)
            baseInvs.append(basePoseMat.inverted())

        # Processing order, once rather than per frame (the FIXME that used to sit in the loop).
        hierarchyOrder: List[int] = []
        placed = set()
        pending = list(range(numBones))
        while pending:
            stillPending: List[int] = []
            for index in pending:
                parent = self.skeleton.bones[index].parent
                if parent == -1 or parent in placed:
                    hierarchyOrder.append(index)
                    placed.add(index)
                else:
                    stillPending.append(index)
            if len(stillPending) == len(pending):
                return False, ErrorMessage("Hierarchy error: cycle in the bone hierarchy")
            pending = stillPending

        stats = JAG2Math.CompressionStats()

        # NLA solo mode disables every other track *and* the active action, so sampling poses
        # with it on exports everything outside the solo track as the rest pose. The importer
        # sets it on "Sequences Layer 1" for viewing convenience; turn it off for the duration
        # of the export and restore it afterwards.
        soloTracks: List[bpy.types.NlaTrack] = []
        animData = self.skeleton_object.animation_data
        if animData is not None:
            for track in animData.nla_tracks:
                if track.is_solo:
                    soloTracks.append(track)
                    track.is_solo = False
            if soloTracks:
                print("Info: temporarily disabled {} soloed NLA track(s) for the export.".format(
                    len(soloTracks)))

        # Sampling every frame with scene.frame_set re-evaluates the entire view layer - meshes,
        # modifiers, shape keys - once per frame, although the export only reads pose bone
        # matrices. JAG2PoseSampler reconstructs those from the FCurves instead, with no
        # dependency graph involved at all.
        #
        # It is only used once it has proved itself: the sampler refuses any setup it does not
        # model exactly (constraints, drivers, animated influence, non-REPLACE strips...), and
        # then a spread of frames is compared against real frame_set samples. Only if every one
        # of those matches does the export skip frame_set for the remaining frames.
        def compressFrame(sourceMatrices, stats=None, frameNum=-1) -> List[bytes]:
            """Pose matrices (armature space, one per bone) -> the frame's 14 byte blocks.

            Shared by the export loop and the fast path's verification, so both go through
            exactly the same arithmetic and the verification compares what really gets written."""
            relative: List[Optional[mathutils.Matrix]] = [None] * numBones
            absolutes: List[Optional[mathutils.Matrix]] = [None] * numBones
            inverses: List[Optional[mathutils.Matrix]] = [None] * numBones
            for index in hierarchyOrder:
                bone = self.skeleton.bones[index]
                poseMat = matrix_overload_cast(localMat @ sourceMatrices[index])
                # change rotation axes from blender style to gla style
                JAG2Math.BlenderBoneRotToGLA(poseMat)
                # The old code wrote this as absolute[parent] @ (absolute[parent]^-1 @ X), which
                # is algebraically X but not bit-identical to it. Computing X directly is both
                # cheaper and more accurate.
                absolute = matrix_overload_cast(poseMat @ baseInvs[index])
                absolutes[index] = absolute
                if bone.parent == -1:
                    relative[index] = absolute
                else:
                    parentInv = inverses[bone.parent]
                    if parentInv is None:
                        parentInv = optional_cast(
                            mathutils.Matrix, absolutes[bone.parent]).inverted()
                        inverses[bone.parent] = parentInv
                    relative[index] = matrix_overload_cast(parentInv @ absolute)
            return [JAG2Math.CompBone.compress(
                optional_cast(mathutils.Matrix, relative[i]), stats,
                self.skeleton.bones[i].name, frameNum) for i in range(numBones)]

        sampler = None
        boneNames = [bone.name for bone in self.skeleton.bones]
        # Blender's hierarchy, not the GLA's: JAG2Constants.PARENT_CHANGES reparents 16 bones of
        # the JKA humanoid on import, and the sampler has to reproduce what Blender evaluates.
        # Using the GLA parents here made the fast path's rhumerus disagree with frame_set by
        # 4 quantisation steps, which is what rejected it on a real export.
        indexByBoneName = {name: i for i, name in enumerate(boneNames)}
        parentIndices = []
        for name in boneNames:
            blenderParent = bpy_generic_cast(
                bpy.types.Bone, self.skeleton_armature.bones[name]).parent
            parentIndices.append(-1 if blenderParent is None
                                 else indexByBoneName.get(blenderParent.name, -1))
        try:
            candidate = JAG2PoseSampler.PoseSampler(
                self.skeleton_object, boneNames, parentIndices, hierarchyOrder)
            reason = candidate.unsupportedReason()
            if reason is not None:
                print("Export: sampling every frame via frame_set - {}.".format(reason))
            else:
                checkFrames = JAG2PoseSampler.verificationFrames(
                    scene.frame_start, scene.frame_end)
                ok, why = candidate.verifyAgainst(
                    scene, poseBones, checkFrames, compressFrame)
                if ok:
                    sampler = candidate
                    print("Export: fast path verified on {} frames ({}), skipping "
                          "frame_set.".format(len(checkFrames), why))
                else:
                    print("Export: fast path rejected ({}) - falling back to frame_set.".format(why))
        except Exception as e:
            print("Export: fast path unavailable ({}) - falling back to frame_set.".format(e))

        # Progress is reported on a timer, not every 10th frame: the latter printed 2138 lines
        # for a full _humanoid export, which buries everything else in the console.
        totalFrames = scene.frame_end - scene.frame_start + 1
        nextProgressTime = time.time() + PROGRESS_UPDATE_INTERVAL
        compressStart = time.time()

        # for each frame:
        for curFrame in range(scene.frame_start, scene.frame_end + 1):
            now = time.time()
            if now >= nextProgressTime:
                done = curFrame - scene.frame_start + 1
                elapsed = now - compressStart
                remaining = elapsed / done * (totalFrames - done) if done else 0.0
                print("Compressing: {}/{} frames ({:.0f}%), about {:.0f}s left".format(
                    done, totalFrames, 100.0 * done / totalFrames, remaining))
                nextProgressTime = now + PROGRESS_UPDATE_INTERVAL

            frame = MdxaFrame()
            if sampler is None:
                scene.frame_set(curFrame)
                sourceMatrices = [matrix_getter_cast(pb.matrix) for pb in poseBones]
            else:
                sourceMatrices = sampler.poseMatrices(curFrame)

            for compOffset in compressFrame(sourceMatrices, stats, curFrame):
                index = compBoneIndices.get(compOffset)
                if index is None:
                    index = len(self.animation.bonePool.bones)
                    downcast(List[bytes], self.animation.bonePool.bones).append(compOffset)
                    compBoneIndices[compOffset] = index
                frame.boneIndices.append(index)

            self.animation.frames.append(frame)

        for track in soloTracks:
            track.is_solo = True

        # Frame bone indices are written as 3 bytes; anything wider would be silently truncated
        # by struct.pack("<I", index)[:3]. Unreachable in practice (_humanoid's pool is ~748k
        # entries) but free to assert.
        assert len(self.animation.bonePool.bones) <= 0xFFFFFF, \
            "bone pool exceeds the 24 bit frame index width"

        print("Compression: " + stats.summary())
        if not stats.clean():
            return False, ErrorMessage(
                "Bone outside the representable range (max 511 units of translation, see the "
                "readme): " + stats.summary())

        self.header.numFrames = scene.frame_end - scene.frame_start + 1
        # enforce 32 bit alignment after 3-byte-indices
        framesSize = 3 * self.header.numFrames * self.header.numBones
        if framesSize % 4 != 0:
            framesSize += 4 - (framesSize % 4)
        self.header.ofsCompBonePool = self.header.ofsFrames + framesSize
        self.header.ofsEnd = self.header.ofsCompBonePool + \
            len(self.animation.bonePool.bones) * 14

        return True, NoError

    def saveToFile(self, filepath_abs: str) -> Tuple[bool, ErrorMessage]:
        try:
            # A context manager, so the file is closed even on error and a failure while
            # flushing surfaces as an IOError rather than being swallowed at refcount time.
            with open(filepath_abs, mode="wb") as file:
                self.header.saveToFile(file)
                self.boneOffsets.saveToFile(file)
                self.skeleton.saveToFile(file, self.header)
                self.animation.saveToFile(file, self.header)
                assert (file.tell() == self.header.ofsEnd)
        except IOError as e:
            print("Could not write file: ", filepath_abs, " (", e, ")", sep="")
            return False, ErrorMessage("Could not write file!")
        return True, NoError

    def saveToBlender(self, scene_root: bpy.types.Object, useAnimation: bool, skeletonFixes: JAG2Constants.SkeletonFixes, animations: Optional[JAG2AnimationCFG.AnimationCFG] = None) -> Tuple[bool, ErrorMessage]:
        print("Applying skeleton/skeleton to Blender")
        profiler = MrwProfiler.SimpleProfiler(True)
        # default skeleton = no skeleton.
        if self.isDefault:
            return True, NoError

        #  try using existing skeletons
        # first check if there's already an armature object called skeleton_root. Try using that.
        if "skeleton_root" in bpy.data.objects:
            print("Found a skeleton_root object, trying to use it.")
            self.skeleton_object = bpy.data.objects["skeleton_root"]
            if self.skeleton_object.type != 'ARMATURE':
                return False, ErrorMessage("Existing skeleton_root object is no armature!")
            self.skeleton_armature = downcast(bpy.types.Armature, self.skeleton_object.data)
            self.skeleton_object.g2_prop.scale = self.header.scale * 100  # pyright: ignore[reportAttributeAccessIssue]
            JAG2Panels.markG2Configured(self.skeleton_object)
        # If there's no skeleton, there may yet still be an armature. Use that.
        elif "skeleton_root" in bpy.data.armatures:
            print("Found skeleton_root armature, trying to use it.")
            self.skeleton_armature = bpy.data.armatures["skeleton_root"]

        # for profiling, possibly
        global g_temp
        # if we found an existing armature, we need to make sure it's linked to an object and valid
        if self.skeleton_armature:
            # see if the armature fits
            success, message = self.skeleton.fitsArmature(
                self.skeleton_armature)
            if not success:
                return False, message

            # this armature would work, add it to an object if necessary
            if not self.skeleton_object:
                self.skeleton_object = bpy.data.objects.new(
                    "skeleton_root", self.skeleton_armature)
                self.skeleton_object.g2_prop.scale = self.header.scale * 100  # pyright: ignore [reportAttributeAccessIssue]
                JAG2Panels.markG2Configured(self.skeleton_object)

            # link the object to the current scene if necessary
            scene = bpy.context.scene
            assert scene is not None
            if self.skeleton_object.name not in scene.collection.objects:
                scene.collection.objects.link(self.skeleton_object)

            # set its parent to the scene_root (not strictly speaking necessary but keeps output consistent)
            self.skeleton_object.parent = scene_root

            # add animations, if any
            if useAnimation:
                profiler.start("applying animations")
                # go to object mode
                assert bpy.context.view_layer is not None
                bpy.context.view_layer.objects.active = self.skeleton_object
                bpy.ops.object.mode_set(mode='OBJECT', toggle=False)
                if PROFILE:
                    import cProfile
                    print("=== Profile start ===")
                    cProfile.runctx(
                        "self.animation.saveToBlender(self.skeleton, self.skeleton_object, self.header.scale)", globals(), locals())
                    print("=== Profile stop ===")
                else:
                    success, message = self.animation.saveToBlender(
                        self.skeleton, self.skeleton_object, self.header.scale, animations)
                    if not success:
                        return False, message
                profiler.stop("applying animations")

            # that's all
            return True, NoError

        # no existing Armature found, create a new one.

        # create armature
        profiler.start("creating armature")
        success, message = self.skeleton.saveToBlender(
            scene_root, skeletonFixes, self.animation)
        if not success:
            return False, message
        self.skeleton_armature = self.skeleton.armature
        self.skeleton_object = self.skeleton.armature_object
        self.skeleton_object.g2_prop.scale = self.header.scale * 100  # pyright: ignore [reportAttributeAccessIssue]
        JAG2Panels.markG2Configured(self.skeleton_object)
        profiler.stop("creating armature")

        # add animations, if any
        if useAnimation:
            profiler.start("applying animations")
            # go to object mode
            assert bpy.context.view_layer is not None
            bpy.context.view_layer.objects.active = self.skeleton_object
            bpy.ops.object.mode_set(mode='OBJECT', toggle=False)
            if PROFILE:
                import cProfile
                print("=== Profile start ===")
                cProfile.runctx(
                    "self.animation.saveToBlender(self.skeleton, self.skeleton_object, self.header.scale)", globals(), locals())
                print("=== Profile stop ===")
            else:
                success, message = self.animation.saveToBlender(
                    self.skeleton, self.skeleton_object, self.header.scale, animations)
                if not success:
                    return False, message
            profiler.stop("applying animations")
        return True, NoError
