"""Dump the complete skeleton state to a JSON file, for comparing two imports.

Run this in Blender's Scripting workspace in BOTH instances - the old .blend and the new
import - then compare the two files with compare_skeletons.py. That settles what actually
differs instead of guessing from screenshots.

    Blender -> Scripting -> Open -> dump_skeleton.py -> Run Script

It writes next to the .blend file, or to the home directory for an unsaved file, and prints
the path it used.
"""
import bpy
import json
import os

ARMATURE_OBJECT = "skeleton_root"
# Which frame to sample the pose at. Compare the same frame in both files.
SAMPLE_FRAME = None  # None = whatever frame is currently set


def vec(v):
    return [round(float(x), 6) for x in v]


def mat(m):
    return [[round(float(m[r][c]), 6) for c in range(4)] for r in range(4)]


def main():
    obj = bpy.data.objects.get(ARMATURE_OBJECT)
    if obj is None:
        raise RuntimeError(f"no object named {ARMATURE_OBJECT}")
    armature = obj.data

    scene = bpy.context.scene
    if SAMPLE_FRAME is not None:
        scene.frame_set(SAMPLE_FRAME)

    data = {
        "blender": bpy.app.version_string,
        "blend_file": bpy.data.filepath or "<unsaved>",
        "frame": scene.frame_current,
        "frame_start": scene.frame_start,
        "frame_end": scene.frame_end,
        "object": {
            "matrix_world": mat(obj.matrix_world),
            "scale": vec(obj.scale),
        },
        "g2_scale": None,
        "bones": {},
        "pose": {},
        "actions": {},
        "nla": [],
    }

    try:
        data["g2_scale"] = round(float(obj.g2_prop.scale), 4)
    except Exception:
        pass

    # Rest skeleton: this is what determines appearance and the exported base pose.
    for bone in armature.bones:
        data["bones"][bone.name] = {
            "parent": bone.parent.name if bone.parent else None,
            "head": vec(bone.head_local),
            "tail": vec(bone.tail_local),
            "length": round(float(bone.length), 6),
            "use_connect": bool(bone.use_connect),
            "matrix_local": mat(bone.matrix_local),
            "inherit_scale": getattr(bone, "inherit_scale", None),
            "use_inherit_rotation": bool(getattr(bone, "use_inherit_rotation", True)),
            "use_local_location": bool(getattr(bone, "use_local_location", True)),
        }

    # Posed state at the sampled frame: this is what the exporter reads.
    for poseBone in obj.pose.bones:
        data["pose"][poseBone.name] = {
            "matrix": mat(poseBone.matrix),
            "matrix_basis": mat(poseBone.matrix_basis),
            "rotation_mode": poseBone.rotation_mode,
            "scale": vec(poseBone.scale),
            "location": vec(poseBone.location),
            "constraints": [c.type for c in poseBone.constraints],
        }

    animData = obj.animation_data
    if animData:
        for track in animData.nla_tracks:
            data["nla"].append({
                "name": track.name,
                "is_solo": bool(track.is_solo),
                "mute": bool(track.mute),
                "strips": [{
                    "name": s.name,
                    "action": s.action.name if s.action else None,
                    "frame_start": round(float(s.frame_start), 3),
                    "frame_end": round(float(s.frame_end), 3),
                    "action_frame_start": round(float(s.action_frame_start), 3),
                    "action_frame_end": round(float(s.action_frame_end), 3),
                    "extrapolation": s.extrapolation,
                    "blend_type": s.blend_type,
                    "influence": round(float(s.influence), 4),
                    "scale": round(float(s.scale), 6),
                    "repeat": round(float(s.repeat), 6),
                } for s in track.strips],
            })

    def count_fcurves(action):
        if not hasattr(action, "layers"):
            return len(action.fcurves)
        return sum(len(cb.fcurves)
                   for layer in action.layers
                   for strip in layer.strips
                   for cb in strip.channelbags)

    for action in bpy.data.actions:
        data["actions"][action.name] = {
            "fcurves": count_fcurves(action),
            "frame_range": [round(float(x), 3) for x in action.frame_range],
            "loop_frame": getattr(getattr(action, "g2_sequence_prop", None), "loop_frame", None),
            "fps": getattr(getattr(action, "g2_sequence_prop", None), "fps", None),
        }

    outDir = os.path.dirname(bpy.data.filepath) or os.path.expanduser("~")
    label = os.path.splitext(os.path.basename(bpy.data.filepath))[0] or "unsaved"
    outPath = os.path.join(outDir, f"skeleton_dump_{label}.json")
    with open(outPath, "w") as f:
        json.dump(data, f, indent=1, sort_keys=True)

    print("=" * 70)
    print(f"written: {outPath}")
    print(f"  {len(data['bones'])} bones, {len(data['actions'])} actions, "
          f"{sum(len(t['strips']) for t in data['nla'])} strips on {len(data['nla'])} tracks")
    print(f"  connected: {sum(1 for b in data['bones'].values() if b['use_connect'])}")
    print(f"  frame: {data['frame']}  blender: {data['blender']}")
    print("=" * 70)


main()
