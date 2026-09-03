"""Dump the armature's real NLA state, so the remaining export errors can be diagnosed.

Everything about the NLA has so far been reconstructed from animation.cfg - but the cfg is
only a projection of what the importer built, not the thing itself. Three attempts to fix the
last 0.24% of frames all made things worse in ways that contradict the cfg, so the cfg is not
enough. This prints what Blender actually holds.

Run it in Blender: Scripting workspace -> Open -> dump_nla.py -> Run Script.
Output goes to the System Console (Window -> Toggle System Console on Windows) and to a text
file next to the .blend.

FRAMES lists the frames to inspect. The defaults are the ones still unaccounted for after the
Stills Layer 1 reorder took a full _humanoid round trip from 52 wrong frames down to 1.
"""
import bpy
import os

ARMATURE_OBJECT = "skeleton_root"

FRAMES = [
    # The one frame a full _humanoid round trip still gets wrong. TORSO_WEAPONIDLE2 and
    # TORSO_WEAPONREADY2 both start at 15456 - the only duplicate still start frame in the whole
    # cfg - so the second needs "Stills Layer 2", which is created lazily and lands on top.
    15456, 15457, 15458,
    # These broke when all eight Stills tracks were pre-created instead of just the first, and
    # nobody knows why: they are covered by two SEQUENCES both starting at 464
    # (BOTH_A3__R__L, 9 frames and BOTH_B3__R___, 3 frames), with no still anywhere near.
    463, 464, 465, 466, 467,
]


def describe(strip, frame):
    covers = strip.frame_start <= frame <= strip.frame_end
    return {
        "name": strip.name,
        "action": strip.action.name if strip.action else "<none>",
        "covers": covers,
        "frame_start": round(float(strip.frame_start), 3),
        "frame_end": round(float(strip.frame_end), 3),
        "action_frame_start": round(float(strip.action_frame_start), 3),
        "action_frame_end": round(float(strip.action_frame_end), 3),
        "mute": bool(strip.mute),
        "extrapolation": strip.extrapolation,
        "blend_type": strip.blend_type,
        "influence": round(float(strip.influence), 4),
        "use_animated_influence": bool(strip.use_animated_influence),
        "blend_in": round(float(strip.blend_in), 3),
        "blend_out": round(float(strip.blend_out), 3),
        "scale": round(float(strip.scale), 4),
        "repeat": round(float(strip.repeat), 4),
        "keys": len(strip.action.fcurves[0].keyframe_points)
        if (strip.action and hasattr(strip.action, "fcurves") and len(strip.action.fcurves))
        else -1,
        "recorded_num_frames": getattr(
            getattr(strip.action, "g2_sequence_prop", None), "num_frames", None)
        if strip.action else None,
    }


def main():
    obj = bpy.data.objects.get(ARMATURE_OBJECT)
    if obj is None:
        raise RuntimeError(f"no object named {ARMATURE_OBJECT}")
    animData = obj.animation_data
    if animData is None:
        raise RuntimeError("armature has no animation data")

    lines = []

    def out(text=""):
        print(text)
        lines.append(text)

    out("=" * 78)
    out(f"Blender {bpy.app.version_string}   object '{ARMATURE_OBJECT}'")
    out(f"active action: {animData.action.name if animData.action else '<none>'}")
    tracks = list(animData.nla_tracks)
    out(f"{len(tracks)} NLA track(s), listed BOTTOM first (later tracks override earlier ones)")
    for i, track in enumerate(tracks):
        out(f"  [{i}] '{track.name}'  strips={len(track.strips)}  mute={track.mute}  "
            f"solo={track.is_solo}  lock={track.lock}")
    total = sum(len(t.strips) for t in tracks)
    out(f"total strips: {total}   total actions: {len(bpy.data.actions)}")

    # Actions with no strip anywhere: those never reach the cfg, and their frames are covered
    # by whatever else happens to be there.
    withStrip = {s.action.name for t in tracks for s in t.strips if s.action}
    orphans = [a.name for a in bpy.data.actions if a.name not in withStrip]
    out(f"actions WITHOUT any strip: {len(orphans)}")
    for name in orphans[:20]:
        out(f"    {name}")
    if len(orphans) > 20:
        out(f"    ... and {len(orphans) - 20} more")

    for frame in FRAMES:
        out("")
        out("-" * 78)
        out(f"FRAME {frame}")
        anyCover = False
        for i, track in enumerate(tracks):
            covering = [s for s in track.strips if s.frame_start <= frame <= s.frame_end]
            if not covering:
                continue
            anyCover = True
            for strip in covering:
                d = describe(strip, frame)
                out(f"  [{i}] {track.name:<20} mute={track.mute} | "
                    f"{d['action']:<26} frames {d['frame_start']}-{d['frame_end']} "
                    f"action {d['action_frame_start']}-{d['action_frame_end']} "
                    f"keys={d['keys']} recorded={d['recorded_num_frames']}")
                out(f"      mute={d['mute']} extrap={d['extrapolation']} "
                    f"blend={d['blend_type']} infl={d['influence']} "
                    f"animInfl={d['use_animated_influence']} "
                    f"blendIn={d['blend_in']} blendOut={d['blend_out']} "
                    f"scale={d['scale']} repeat={d['repeat']}")
        if not anyCover:
            out("  NOTHING covers this frame - the pose falls back to the rest pose")

        # What the pose actually is, for cross-checking against the GLA.
        scene = bpy.context.scene
        saved = scene.frame_current
        scene.frame_set(frame)
        bone = obj.pose.bones[0]
        loc = obj.pose.bones[10].matrix.translation if len(obj.pose.bones) > 10 else bone.matrix.translation
        out(f"  evaluated: {obj.pose.bones[10].name if len(obj.pose.bones) > 10 else bone.name}"
            f" at ({loc[0]:.4f}, {loc[1]:.4f}, {loc[2]:.4f})")
        scene.frame_set(saved)

    outDir = os.path.dirname(bpy.data.filepath) or os.path.expanduser("~")
    path = os.path.join(outDir, "nla_dump.txt")
    with open(path, "w") as f:
        f.write("\n".join(lines))
    print("=" * 78)
    print(f"written: {path}")


main()
