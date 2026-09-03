"""Compare two dumps produced by dump_skeleton.py.

    python3 compare_skeletons.py skeleton_dump_old.json skeleton_dump_new.json

Reports, in order of how much it matters:
  1. bones present in one but not the other
  2. rest pose differences (head/tail/roll -> appearance AND the exported base pose)
  3. use_connect differences (locks out independent translation)
  4. posed differences at the sampled frame (this is what the exporter writes)
  5. NLA / action differences

Runs on plain Python 3, no Blender needed.
"""
import json
import math
import sys

POS_TOL = 1e-4
MAT_TOL = 1e-4


def load(path):
    with open(path) as f:
        return json.load(f)


def mat_maxdiff(a, b):
    return max(abs(a[r][c] - b[r][c]) for r in range(4) for c in range(4))


def vec_dist(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def direction(head, tail):
    d = [t - h for h, t in zip(head, tail)]
    n = math.sqrt(sum(x * x for x in d)) or 1.0
    return [x / n for x in d]


def angle_between(u, v):
    dot = max(-1.0, min(1.0, sum(a * b for a, b in zip(u, v))))
    return math.degrees(math.acos(dot))


def section(title):
    print("\n" + title)
    print("-" * len(title))


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    old, new = load(sys.argv[1]), load(sys.argv[2])

    print(f"old: {old['blend_file']}  (Blender {old['blender']}, frame {old['frame']})")
    print(f"new: {new['blend_file']}  (Blender {new['blender']}, frame {new['frame']})")
    if old["frame"] != new["frame"]:
        print("\n!! different frames sampled - pose comparison below is meaningless.")
    if old.get("g2_scale") != new.get("g2_scale"):
        print(f"\n!! g2 scale differs: {old.get('g2_scale')} vs {new.get('g2_scale')}")

    oldBones, newBones = old["bones"], new["bones"]
    onlyOld = sorted(set(oldBones) - set(newBones))
    onlyNew = sorted(set(newBones) - set(oldBones))
    shared = sorted(set(oldBones) & set(newBones))

    section("1. bone set")
    if onlyOld or onlyNew:
        print(f"  only in old ({len(onlyOld)}): {onlyOld[:15]}")
        print(f"  only in new ({len(onlyNew)}): {onlyNew[:15]}")
    else:
        print(f"  identical, {len(shared)} bones")

    section("2. rest pose (appearance + exported base pose)")
    headMoved, dirChanged, lengthChanged, rollTwisted = [], [], [], []
    for name in shared:
        a, b = oldBones[name], newBones[name]
        if vec_dist(a["head"], b["head"]) > POS_TOL:
            headMoved.append((name, vec_dist(a["head"], b["head"])))
        angle = angle_between(direction(a["head"], a["tail"]), direction(b["head"], b["tail"]))
        if angle > 0.05:
            dirChanged.append((name, angle))
        if abs(a["length"] - b["length"]) > POS_TOL:
            lengthChanged.append((name, a["length"], b["length"]))
        # Twist = rest matrix differs even though head and direction agree.
        if angle <= 0.05 and vec_dist(a["head"], b["head"]) <= POS_TOL:
            d = mat_maxdiff(a["matrix_local"], b["matrix_local"])
            if d > MAT_TOL:
                rollTwisted.append((name, d))

    print(f"  heads moved:        {len(headMoved)}")
    for n, d in sorted(headMoved, key=lambda x: -x[1])[:8]:
        print(f"      {n:<20} by {d:.4f}")
    print(f"  directions changed: {len(dirChanged)}")
    for n, d in sorted(dirChanged, key=lambda x: -x[1])[:8]:
        print(f"      {n:<20} by {d:.2f} deg")
    print(f"  lengths changed:    {len(lengthChanged)}")
    for n, a, b in sorted(lengthChanged, key=lambda x: -abs(x[1] - x[2]))[:8]:
        print(f"      {n:<20} {a:.3f} -> {b:.3f}")
    print(f"  TWISTED (same head+direction, different rest matrix): {len(rollTwisted)}")
    for n, d in sorted(rollTwisted, key=lambda x: -x[1])[:10]:
        print(f"      {n:<20} matrix delta {d:.4f}   <-- roll problem")

    section("3. use_connect")
    connOld = {n for n in shared if oldBones[n]["use_connect"]}
    connNew = {n for n in shared if newBones[n]["use_connect"]}
    print(f"  old: {len(connOld)}   new: {len(connNew)}")
    if connOld - connNew:
        print(f"  no longer connected ({len(connOld - connNew)}): {sorted(connOld - connNew)[:12]}")
        print("      -> these can now translate independently; expected, and safer")
    if connNew - connOld:
        print(f"  newly connected ({len(connNew - connOld)}): {sorted(connNew - connOld)[:12]}")
        print("      -> these LOSE independent translation on re-export; investigate")

    section("4. posed state at the sampled frame (what the exporter writes)")
    oldPose, newPose = old["pose"], new["pose"]
    poseShared = sorted(set(oldPose) & set(newPose))
    poseDiff = []
    for name in poseShared:
        d = mat_maxdiff(oldPose[name]["matrix"], newPose[name]["matrix"])
        if d > MAT_TOL:
            poseDiff.append((name, d))
    print(f"  pose matrices differing: {len(poseDiff)}/{len(poseShared)}")
    for n, d in sorted(poseDiff, key=lambda x: -x[1])[:12]:
        print(f"      {n:<20} delta {d:.4f}")
    if not poseDiff:
        print("      -> the exported GLA will be identical")

    badMode = [n for n in poseShared if newPose[n]["rotation_mode"] != "QUATERNION"]
    if badMode:
        print(f"  !! rotation_mode not QUATERNION in new ({len(badMode)}): {badMode[:8]}")
    badScale = [n for n in poseShared
                if any(abs(s - 1.0) > 1e-4 for s in newPose[n]["scale"])]
    if badScale:
        print(f"  !! pose scale != 1 in new ({len(badScale)}): {badScale[:8]}")
        print("      -> this is what makes bones look stretched")

    section("5. NLA and actions")
    print(f"  tracks  old {len(old['nla'])}  new {len(new['nla'])}")
    print(f"  strips  old {sum(len(t['strips']) for t in old['nla'])}  "
          f"new {sum(len(t['strips']) for t in new['nla'])}")
    print(f"  actions old {len(old['actions'])}  new {len(new['actions'])}")
    emptyNew = [n for n, a in new["actions"].items() if a["fcurves"] == 0]
    if emptyNew:
        print(f"  !! actions with no fcurves in new ({len(emptyNew)}): {emptyNew[:8]}")
    scaled = [(t["name"], s["name"], s["scale"])
              for t in new["nla"] for s in t["strips"] if abs(s["scale"] - 1.0) > 1e-4]
    if scaled:
        print(f"  !! strips with scale != 1 ({len(scaled)}): {scaled[:5]}")

    print("\ndone.")
    return 0


sys.exit(main())
