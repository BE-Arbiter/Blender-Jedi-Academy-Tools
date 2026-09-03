# Changelog — GLA/GLM import and export

Work on the correctness and speed of the Ghoul2 round trip. Based on the fork with NLA/CFG
support and Blender 4.1–5.x, not on mrwonko's `master` (which has no NLA support at all — see
"Things worth knowing" below).

---

## Result

Full round trip of `_humanoid.gla` (21,376 frames, 53 bones, 1417 sequences), imported and
exported again, measured against the original file across **all 1,132,928 bone-frames**:

| | before | after |
|---|---|---|
| frames with a noticeable error | practically all | **0** |
| max rotation error | up to 180° | **0.0068°** |
| max translation error | up to 363 units | **0.0000** |
| skeleton | — | **bit-identical** (base poses 0.00000000) |
| file size | 13.6 MB | 11.74 MB (original 11.80 MB) |
| bone pool | 728,735 | 595,350 (original 599,570) |
| export reproducible | no | **yes** |

The remaining 0.0068° is the format's own resolution limit — 14 bits per quaternion component.
Getting closer would mean reproducing Carcass' exact rounding decisions. The file is smaller
than the original because properly normalised offsets deduplicate better.

**Speed:** the import lost ~1.1M operator invocations and ~2.3M `keyframe_insert` calls. The
export now needs 24 `scene.frame_set` calls instead of 21,376.

---

## Bugs fixed

Ordered by impact. Every point was measured against real data, not inferred.

### 1. Non-reproducible export — `id(action) ^ id(slot)` as a cache key

The most serious find, and the one that hid everything else.

`JAG2PoseSampler` cached each action's FCurves under `id(action) ^ id(slot)`. XORing two memory
addresses is not a unique key:

```
Action@0x1000 ^ Slot@0x2000 = 0x3000
Action@0x1010 ^ Slot@0x2010 = 0x3000   <- same key, different objects
```

With 1417 actions this is not an edge case — in a simulation of realistic allocation patterns,
**200 out of 200 runs** had at least one collision. A collision hands one sequence another
sequence's curves: a whole animation exports as a different one.

Because object addresses move between Blender starts, a different pair collided every time.
Three identical runs produced **284, 68 and 432** wrong frames, with exactly one frame wrong in
all three.

Fixed by keying on `(action.name, slot.identifier)` — action names are unique within `bpy.data`.

> **Consequence for everything before this:** every A/B measurement taken before this fix is
> void. The spread with no code change at all (68–432) was wider than any difference measured
> between variants. At least one working change was reverted because of it.

### 2. Quaternions were not normalised on decompression

GLA quaternions use 14 bits per component, so a decoded quaternion is off from unit length by up
to ~0.0015 — and `Quaternion.to_matrix()` does not normalise. Every offset matrix carried that as
scale, and absolute transforms are built by multiplying offsets down the hierarchy. Measured on
`_humanoid`: **8.93% accumulated scale drift**, up to 0.15 units of displacement at the deepest
bones.

`CompBone.loadFromFile` normalises now. The mean rotation error in the export dropped from
14.66° to 0.00°.

### 3. matrix_basis was solved against the GLA hierarchy instead of Blender's

`JAG2Constants.PARENT_CHANGES` reparents 16 bones on import (`rhumerus` → `rclavical`, the finger
joints and `hang_tag_bone`s → the hand) to make the rig easier to pose. The GLA keeps its own
hierarchy.

Blender evaluates a pose bone against its **actual** parent. Solving for matrix_basis against the
GLA parent misplaces exactly those 16 bones — translation errors up to 83 units, while the other
37 stayed bit-exact.

The proof: the set of reparented bones is **identical** to the set of broken ones.

### 4. Scale was removed from the local matrices instead of the absolute ones

A GLA base pose carries the file's uniform scale (0.64 for `_humanoid`, which is why an
unstretched bone is 4 × 0.64 = 2.56 long). Blender's `bone.matrix_local` is orthonormal, so that
scale ends up in `matrix_basis`. Scale is never keyed, so it has to go — but removing it from the
*local* matrix is not harmless: the child's local translation was derived in a parent frame that
still had the 0.64 scale, then applied in one that no longer did.

Result: every bone landed **1/0.64 = 1.5625×** too far out; parent-to-child distances came out at
1.5625× their rest length instead of 1.0×.

Fixed by stripping the scale from the absolute transforms once per frame, before anything is
derived from them.

### 5. 1-frame sequences overrode the following frame

Blender cannot make an NLA strip shorter than one frame of length. A 1-frame sequence ("still")
at frame N therefore occupies N..N+1 — and because the stills tracks sat above the sequence
tracks, the still held its pose over frame N+1, which belongs to a different sequence. Cost: 52
of 21,376 frames, confirmed against `animation.cfg` (51 of the 52 sat directly after a 1-frame
sequence).

`nla_tracks.new()` appends on top, and `new(prev=…)` can only insert *after* a given track — so
creating them up front is the only way. The importer now counts how many stills tracks are
needed, using the same overlap rule as the placement loop (two for the stock `_humanoid`, because
`TORSO_WEAPONIDLE2` and `TORSO_WEAPONREADY2` both start at 15456), and creates them before any
sequence track.

### 6. Wrong name written into the header

```
original:   models/players/_humanoid/_humanoid
exported:  /models/players/_humanoid/_humanoid.gla
```

Leading slash and a `.gla` extension. Carcass writes it base-relative with neither. Normalised
now.

### 7. Export crashed instead of reporting

`CompBone.compress` packed into `"7H"` unchecked. At |translation| ≥ 512 a bare `struct.error`
flew out of the middle of the export, naming neither bone nor frame. Now it clamps to ±511
(because `(511+512)·64 = 65472` still fits a uint16), catches NaN, and `loadFromBlender` aborts
with the offending bone and frame.

**For valid data the output is byte-identical to before** — verified over 200,000 random values.

### 8. The animation.cfg loop value was flattened to a bool

In the cfg the loop column is not a flag but the frame to jump back to, which JKA reads with
`atoi` into `animations[i].loopFrames`. The parser turned it into `_atoi(loop) != -1`:

| cfg input | old | new |
|---|---|---|
| `-1` | -1 | -1 |
| `0` | 0 | 0 |
| `5` | **0** | 5 |
| `999` | **0** | 999 |

`loop` is an `int` throughout now; `g2_sequence_prop.loop_frame` changed from `BoolProperty` to
`IntProperty(default=-1)`.

### 9. Twisted bones after stretching

Blender's `roll` is an angle about the bone's own axis. Moving the tail afterwards — which is
what stretching a parent to its child does — makes the stored roll mean something else, and the
bone comes out twisted. The roll is now re-applied after every tail move.

### 10. Reference GLA only checked bone names

When exporting against a reference, `basePoseMat` comes from the reference file while the offsets
are computed against Blender's rest pose. If those disagree, the whole animation is displaced.
There is a numeric comparison now — scale-invariant, because a GLA base pose carries the file
scale while Blender's `matrix_local` is orthonormal (a naive comparison would have reported 0.36
on every single export).

### 11. Smaller things

- `GLA.saveToFile` had no `with` — write errors during flush were lost
- the 24-bit pool index was silently truncated → `assert`
- 16 `struct` formats ran in native mode; measured that `<` introduces no padding, then made it
  explicit
- sequences that fit on none of the 8 NLA layers vanished without a word → warning
- the cfg export's strip filter tested only the start frame, not overlap
- warning for Blender duplicate suffixes (`BOTH_STAND1.001`) — JKA will not map those names
- `strip.extrapolation = 'NOTHING'` on import (the default is `HOLD`, which lets a strip affect
  the entire timeline)
- `is_solo` is disabled for the export and restored afterwards
- sequence length comes from `g2_sequence_prop.num_frames` rather than the track *name*
- two logging bugs: the "stretched bones" counter compared against `BONELENGTH` instead of each
  bone's actual starting length and therefore always reported all of them; the reparenting note
  was printed 1417 times instead of once
- export progress: one line every 30 seconds instead of every 10 frames (which was 2138 lines)

---

## Speed

### Import: operators removed

Previously, per bone **and** frame: `pose_bone.matrix = …` followed by
`bpy.ops.pose.visual_transform_apply()`, then two `keyframe_insert` calls per bone. For
`_humanoid` (53 bones, 21,376 frames):

| call | count |
|---|---|
| `bpy.ops.pose.visual_transform_apply` | ~1,133,000 |
| `keyframe_insert` | ~2,266,000 |
| `scene.frame_set` | 21,376 |

Every `bpy.ops` call carries context resolution, a depsgraph update and an undo push.

`visual_transform_apply` was only needed because assigning `pose_bone.matrix` reads the
*evaluated* parent matrix — which we know ourselves when walking in hierarchy order.
`_bakeAction` solves for matrix_basis through Blender's own `Bone.convert_local_to_pose()`, with
no depsgraph involved, and writes keyframes in bulk via `keyframe_points.add` + `foreach_set` —
the same path Blender's own BVH importer uses.

Also added: quaternion sign continuity (`dot(prev, cur) ≥ 0`), without which the pose swings
wildly between keys, since FCurves interpolate componentwise.

### Export: depsgraph-free fast path

`scene.frame_set` re-evaluates the entire view layer — every mesh, every modifier — although the
export only reads `pose_bone.matrix`. `JAG2PoseSampler` reconstructs the poses from the FCurves
instead.

Two safeguards, because a wrong fast path would produce a plausible but incorrect GLA:

1. **It refuses anything it does not model exactly**: constraints, drivers, euler rotation mode,
   non-REPLACE blending, animated influence or strip time, blend in/out, reversed playback, strip
   F-modifiers, extrapolation other than `NOTHING`, more than one soloed track, object-level
   animation. In all of those it falls back to `frame_set` with a printed reason.
2. **It proves itself before use**: 24 frames spread across the range are sampled for real with
   `frame_set` and compared on the **compressed output** — that is, on exactly what goes into the
   file. Only at zero difference does the rest run fast.

Result: 21,376 → **24** `frame_set` calls.

On top of that, keyframes are read once per curve with `foreach_get` instead of calling
`fcurve.evaluate()` 7.9 million times — without that the fast path might well have been slower
than the depsgraph.

### Export: constant work hoisted out of the loop

Base poses, their inverses, the pose bone references and the topological sort all ran per bone
**and** frame — ~1.1M redundant matrix inversions. Computed once now.

### Writing the file

`MdxaFrame.saveToFile` did ~1.13M individual `struct.pack` + `file.write` calls. Now one buffer,
measured 3.5× faster, byte-identical output. (Honestly: 0.24 s → 0.07 s, so not the bottleneck.)

### GLM export: quadratic vertex dedup

The protovert search scanned the entire list built so far for **each** of the three corners of
**every** face. Replaced with buckets keyed on `(vertex index, UV)` — the two fields compared
exactly; the 0.05 normal tolerance now only runs inside the bucket.

| verts | faces | before | after | factor |
|---|---|---|---|---|
| 500 | 1,000 | 0.227 s | 0.003 s | 83× |
| 2,000 | 4,000 | 3.388 s | 0.014 s | 237× |
| 6,000 | 12,000 | 31.083 s | 0.067 s | **462×** |

300 randomised meshes — including UV seams and normals right at the tolerance boundary — produce
identical triangle indices.

Also hoisted out of the per-vertex path: a `scene_root` lookup plus a 4×4 inversion **per
vertex**, two `to_quaternion()` conversions, the modifier stack scan in `getBoneWeights`, and
another inversion in the envelope path. The multiplication order was left untouched, so GLM
floats stay bit-identical.

---

## New feature: remembered import/export settings

Blender keeps operator properties only for the current session and drops them on every addon
reload. `JAG2Settings.py` stores them in the addon preferences, which Blender writes to
`userpref.blend`, so they survive a restart.

Covers all five operators (GLM/GLA import, GLM/GLA export, cfg export): base path, scale,
skeleton fixes, animation mode, reference GLA, offset.

Three deliberate decisions:

- **The file path is not restored** — the file browser owns that. The base path is, since that is
  the tedious one to retype.
- **Values are stored as strings** and converted back through each property's declared type. If
  an enum or range changed since it was saved, that value is skipped rather than putting the
  operator into an invalid state.
- **Nothing can break** — corrupt JSON, missing preferences and unknown properties are all
  silently ignored.

Edit → Preferences → Add-ons → Jedi Academy has a checkbox to disable it and a **Forget Saved
Settings** button.

---

## Changed files

```
JAG2GLA.py            import rewrite, export caching, fast path, NLA track order,
                      clamping report, reference check, bulk writing, header name
JAG2Math.py           quaternion normalisation, clamping, named constants, statistics
JAG2PoseSampler.py    NEW — depsgraph-free sampling with self-verification
JAG2Settings.py       NEW — remembered import/export settings
JAG2AnimationCFG.py   loop as int, length from the action, overlap filter, name warning
JAG2GLM.py            bucketed vertex dedup, per-mesh context
JAG2Constants.py      MIN_BONE_LENGTH, STRETCH_BONES_TO_CHILDREN
JAG2Panels.py         loop_frame as IntProperty, num_frames
JAG2Operators.py      save/restore settings
Makefile              new modules in PY_FILES
tests/run_tests.py    loop semantics updated, three new regression tests
verify/               NEW — 11 verification scripts + 4 diagnostic tools
```

---

## `verify/` — checking without Blender

Eleven scripts that run on plain Python 3. They demonstrate the findings numerically rather than
asserting them:

```
check_compress.py        clamping, byte identity over 200,000 values, round-to-nearest
check_basepose_scale.py  reproduces the 1/0.64 bug and verifies the fix
check_basis.py           pose↔basis algebra over 2000 random hierarchies
check_parent_changes.py  reparented bones == broken bones
check_pose_sampler.py    NLA reconstruction, every rejection reason, cache collision
check_nla_placement.py   stills layer counting and track order
check_cfg.py             loop round trip, sequence length, tokenizer edge cases
check_fcurves.py         FCurve reuse across four container flavours
check_bone_connect.py    stretching vs connecting
check_protoverts.py      GLM dedup: identity + speedup measurement
check_settings.py        remembered settings, 15 cases
```

All at once:

```
cd verify
for f in check_*.py; do python3 "$f"; done
```

### Diagnostic tools (run inside Blender)

```
dump_skeleton.py       skeleton + poses as JSON
compare_skeletons.py   compares two dumps, separating twist / displacement / pose
dump_nla.py            which strips cover a given frame
dump_nla_layout.py     complete NLA state, diffable
```

`dump_nla_layout.py` is what localised the non-determinism: two imports, two dumps, `diff` —
identical, so the variance was in the export.

---

## Rejected approaches

Recorded so nobody retries them without re-measuring. All the numbers below come from **before**
the cache fix and are therefore **void** — they are here only because the approaches themselves
were investigated:

| approach | measured (void) |
|---|---|
| reorder all 16 tracks + remove unused | 521 frames |
| bake a second frame into the still | 410 |
| mute redundant stills for the export | 525 |

The last one was particularly tempting: the cfg proves **all 60 stills are redundant** (every
still's frame is also covered by a longer sequence). The reasoning was sound — and the
measurement that rejected it was noise.

Also rejected, but for a real reason: decoupling stretching from connecting
(`STRETCH_BONES_TO_CHILDREN`, default `False`). Stretching changes `bone.length`, and Blender
places a child's local origin at its parent's **tail**, so every additionally stretched bone
displaces its whole subtree. The switch exists for anyone who wants the longer bones; the comment
says what it costs.