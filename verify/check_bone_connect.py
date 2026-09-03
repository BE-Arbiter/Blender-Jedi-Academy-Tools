"""Checks the bone-connection split, without Blender.

MdxaBone.saveToBlender used to gate two unrelated things on the same test:

    blenderParent.tail = pos    -> how long the parent bone is DRAWN (cosmetic)
    bone.use_connect = True     -> rigidly locks the head to that tail (can lose data)

Only the second one matters for correctness: Blender ignores the location channel of a
connected bone, so a bone that translates independently would be silently clipped on
reimport. _boneCanConnect exists to prevent exactly that.

Gating the cosmetic one on _boneCanConnect too is what made a full _humanoid.gla import
come out as uniform BONELENGTH stubs: across 21376 frames almost no bone stays inside the
3-quantum translation margin, so nothing got stretched.

This models the three variants against synthetic bone motion and checks that the split gives
the old appearance AND the new safety.
"""
import sys

BONELENGTH = 4.0
QUANTUM = 1.0 / 64.0
TRANSLATION_MARGIN = 3 * QUANTUM
ANGLE_MARGIN = 0.996
MIN_BONE_LENGTH = 0.01


def bone_can_connect(motionPerFrame):
    """True when the child's head never leaves its rest offset by more than the margin."""
    return all(d <= TRANSLATION_MARGIN for d in motionPerFrame)


def simulate(bones, variant):
    """bones: list of (angleOk, distance, motionPerFrame). Returns (stretched, connected)."""
    stretched = connected = 0
    for angleOk, distance, motion in bones:
        if not angleOk:
            continue
        canConnect = bone_can_connect(motion)
        if variant == "old":
            # pre-_boneCanConnect: stretch and connect on the angle test alone
            if distance > MIN_BONE_LENGTH:
                stretched += 1
                connected += 1
        elif variant == "strict":
            # current: both gated on _boneCanConnect
            if canConnect and distance > MIN_BONE_LENGTH:
                stretched += 1
                connected += 1
        elif variant == "split":
            # STRETCH_BONES_TO_CHILDREN = True: stretch on the angle test, connect only when safe.
            # NOT the default - see below.
            tailMoved = distance > MIN_BONE_LENGTH
            if tailMoved:
                stretched += 1
            if tailMoved and canConnect:
                connected += 1
    return stretched, connected


def humanoid_like(numBones, numFrames):
    """Most bones pass the angle test and sit at a sensible distance. Realistically almost all
    of them move at least a little relative to their parent somewhere across a full GLA."""
    import random
    rng = random.Random(4242)
    bones = []
    for i in range(numBones):
        angleOk = rng.random() < 0.85
        distance = rng.uniform(0.5, 12.0)
        if i % 11 == 0:
            # a genuinely rigid bone: never moves beyond quantisation noise
            motion = [rng.uniform(0.0, QUANTUM) for _ in range(numFrames)]
        else:
            motion = [rng.uniform(0.0, QUANTUM) for _ in range(numFrames - 1)]
            motion.append(rng.uniform(0.2, 5.0))  # moves in at least one frame
        bones.append((angleOk, distance, motion))
    return bones


failures = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  -- " + detail) if detail else ""))
    if not cond:
        failures.append(name)


bones = humanoid_like(53, 400)
angleOkCount = sum(1 for b in bones if b[0])

oldS, oldC = simulate(bones, "old")
strictS, strictC = simulate(bones, "strict")
splitS, splitC = simulate(bones, "split")

print(f"53 bones, {angleOkCount} pass the angle test\n")
print("  {:<8} {:>10} {:>10}".format("variant", "stretched", "connected"))
print("  {:<8} {:>10} {:>10}".format("old", oldS, oldC))
print("  {:<8} {:>10} {:>10}".format("strict", strictS, strictC))
print("  {:<8} {:>10} {:>10}".format("split", splitS, splitC))
print()

check("strict variant loses the appearance", strictS < oldS,
      f"only {strictS} stretched vs {oldS} before - this is the regression")
check("split restores the pre-_boneCanConnect appearance", splitS == oldS,
      f"split {splitS} vs old {oldS}")
# The addon defaults to "strict", i.e. the original coupling. Decoupling changes bone.length,
# and Blender places a child's local origin at its parent's TAIL, so every extra stretched
# bone displaces its whole subtree in the posed frames. Against a reference import the error
# grew monotonically with the number of stretched ancestors: 0 -> 0.0000, 6 -> 1.4828.
check("the default keeps stretching coupled to connecting", strictS == strictC,
      f"stretched {strictS} vs connected {strictC}")
check("split never connects more than is safe", splitC <= strictC,
      f"split {splitC} vs strict {strictC}")
check("split connects strictly fewer than the old variant", splitC < oldC,
      f"split {splitC} vs old {oldC} - the difference is what used to be silently clipped")

print("\n[2] a bone that translates must never be connected, in any variant that claims safety")
translating = [(True, 5.0, [0.0] * 50 + [1.0])]
for variant in ("strict", "split"):
    _, c = simulate(translating, variant)
    check(f"{variant}: translating bone not connected", c == 0, f"connected={c}")
_, c = simulate(translating, "old")
print(f"  (old variant connects it anyway: connected={c} - the data loss being fixed)")

print("\n[3] a rigid bone still gets connected")
rigid = [(True, 5.0, [0.0] * 50)]
for variant in ("old", "strict", "split"):
    s, c = simulate(rigid, variant)
    check(f"{variant}: rigid bone stretched and connected", s == 1 and c == 1, f"{s}/{c}")

print("\n[4] degenerate spacing must not produce a zero-length bone")
degenerate = [(True, 0.0, [0.0] * 10)]
s, c = simulate(degenerate, "split")
check("no stretch, no connect when child sits on the parent head", s == 0 and c == 0, f"{s}/{c}")

print("\n" + ("ALL CHECKS PASSED" if not failures else f"FAILURES: {failures}"))
sys.exit(1 if failures else 0)
