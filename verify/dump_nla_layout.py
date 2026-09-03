"""Dump the complete NLA layout in a form that can be diffed between two imports.

Three identical import+export runs produced three different GLAs - 284, 68 and 432 wrong frames
out of 21376, with only ONE frame (15457) wrong in all three. So the process is not
reproducible, and every A/B measurement made without repeating each run was noise.

This narrows down where the variance enters. Run it after an import, save the file, import
again in a fresh scene, run it again, then diff the two outputs:

    Blender -> Scripting -> Open -> dump_nla_layout.py -> Run Script

It writes nla_layout_1.txt, nla_layout_2.txt, ... next to the .blend (or into the home
directory), never overwriting a previous one. One line per strip, sorted by action name, so a
plain text diff shows exactly what moved.

Reading the diff:
  * lines differ in TRACK    -> the importer places strips on different layers between runs,
                                so the variance is in the strip placement loop
  * lines differ in KEYS or the pose sample -> the placement is stable but the baked animation
                                data differs, so the variance is in _bakeAction / the transforms
  * files identical          -> the import is deterministic and the variance is in the EXPORT
"""

import bpy
import os

ARMATURE_OBJECT = "skeleton_root"

# A few frames whose evaluated pose is sampled as a fingerprint of the animation data.
SAMPLE_FRAMES = [0, 1000, 5000, 10000, 15457, 20000]


def keyCount(action):
    """Number of keyframes on the action's first curve, across Blender versions."""
    try:
        if not hasattr(action, "layers"):
            return len(action.fcurves[0].keyframe_points) if len(action.fcurves) else -1
        for layer in action.layers:
            for strip in layer.strips:
                for bag in strip.channelbags:
                    if len(bag.fcurves):
                        return len(bag.fcurves[0].keyframe_points)
    except Exception:
        pass
    return -1


def main():
    obj = bpy.data.objects.get(ARMATURE_OBJECT)
    if obj is None:
        raise RuntimeError(f"no object named {ARMATURE_OBJECT}")
    animData = obj.animation_data
    if animData is None:
        raise RuntimeError("armature has no animation data")

    lines = []
    tracks = list(animData.nla_tracks)
    lines.append("# tracks, bottom first")
    for i, track in enumerate(tracks):
        lines.append("TRACKORDER {:2d} {:<22} strips={:<5} solo={} mute={}".format(
            i, track.name, len(track.strips), track.is_solo, track.mute))

    lines.append("# bones: name, length, use_connect - the skeleton the animation is solved against")
    for bone in obj.data.bones:
        lines.append("BONE {:<20} len={:.6f} connect={} parent={}".format(
            bone.name, bone.length, bone.use_connect,
            bone.parent.name if bone.parent else "-"))

    lines.append("# one line per strip, sorted by action name")
    strips = []
    for i, track in enumerate(tracks):
        for strip in track.strips:
            action = strip.action
            strips.append(
                "STRIP {:<28} track={:<22} idx={:<2} frames={:.1f}-{:.1f} "
                "action={:.1f}-{:.1f} keys={:<4} extrap={:<12} blend={:<8} "
                "mute={} autoblend={} in={:.1f} out={:.1f} scale={:.4f} repeat={:.4f}".format(
                    action.name if action else "<none>", track.name, i,
                    strip.frame_start, strip.frame_end,
                    strip.action_frame_start, strip.action_frame_end,
                    keyCount(action) if action else -1,
                    strip.extrapolation, strip.blend_type, strip.mute,
                    strip.use_auto_blend, strip.blend_in, strip.blend_out,
                    strip.scale, strip.repeat))
    strips.sort()
    lines.extend(strips)

    # Fingerprint of the actual animation data, independent of how it is arranged.
    lines.append("# evaluated pose samples")
    scene = bpy.context.scene
    saved = scene.frame_current
    for frame in SAMPLE_FRAMES:
        scene.frame_set(frame)
        for boneName in ("pelvis", "rhumerus", "l_d1_j2", "rtibia"):
            poseBone = obj.pose.bones.get(boneName)
            if poseBone is None:
                continue
            loc = poseBone.matrix.translation
            lines.append("POSE frame={:<6} {:<12} {:.6f} {:.6f} {:.6f}".format(
                frame, boneName, loc[0], loc[1], loc[2]))
    scene.frame_set(saved)

    outDir = os.path.dirname(bpy.data.filepath) or os.path.expanduser("~")
    index = 1
    while os.path.exists(os.path.join(outDir, "nla_layout_{}.txt".format(index))):
        index += 1
    path = os.path.join(outDir, "nla_layout_{}.txt".format(index))
    with open(path, "w") as f:
        f.write("\n".join(lines))

    print("=" * 70)
    print("written: {}".format(path))
    print("  {} tracks, {} strips, {} bones".format(
        len(tracks), len(strips), len(obj.data.bones)))
    print("  connected bones: {}".format(
        sum(1 for b in obj.data.bones if b.use_connect)))
    print("Run this again after a second fresh import, then diff the two files.")
    print("=" * 70)


main()
