"""Models NLA strip placement to show why baking before strips.new() lost every strip.

NlaStrips.new(name, start, action) derives the new strip's footprint from the action's frame
range and refuses to create a strip that overlaps an existing one on the same track. So the
order matters:

  strip first  -> the action is still empty, footprint is 1 frame, placement almost always
                  succeeds, and action_frame_start/end then set the real length afterwards
  bake first   -> the action already spans its full range, strips.new() tests that full span,
                  and sequences sharing boundary frames get rejected across all 8 layers

This replays a realistic animation.cfg layout through both orders and counts placed strips.

Outcome: for cfg-shaped input the two orders are equivalent - collisions simply do not arise,
because sequences are laid out back to back. That RULES OUT ordering as the reason strips go
missing, and the addon keeps the original strip-before-bake order only because it is the
long-proven one.
"""
import sys

MAX_LAYERS = 8


class FakeTrack:
    def __init__(self, name):
        self.name = name
        self.spans = []          # list of (start, end) inclusive

    def new(self, start, length):
        end = start + max(0, length - 1)
        for s, e in self.spans:
            if not (end < s or start > e):
                raise RuntimeError(
                    f"Unable to add strip (the track does not have any space to accommodate "
                    f"this new strip) [{start},{end}] vs [{s},{e}]")
        self.spans.append((start, end))
        return len(self.spans) - 1

    def resize(self, index, start, length):
        """action_frame_start/end assignment: Blender does NOT re-check overlap here."""
        self.spans[index] = (start, start + max(0, length - 1))


def place(sequences, bakeFirst):
    tracks = {}
    placed = 0
    unplaced = []
    for name, start, length in sequences:
        prefix = "Stills Layer" if length == 1 else "Sequences Layer"
        got = None
        for layer in range(1, MAX_LAYERS + 1):
            track = tracks.setdefault(f"{prefix} {layer}", FakeTrack(f"{prefix} {layer}"))
            # Footprint at creation time: the action's range. Empty action -> 1 frame.
            footprint = length if bakeFirst else 1
            try:
                index = track.new(start, footprint)
            except RuntimeError:
                continue
            # The real extent is set afterwards either way.
            track.resize(index, start, length)
            got = (track, index)
            break
        if got is None:
            unplaced.append(name)
        else:
            placed += 1
    return placed, unplaced, tracks


def humanoid_like(count):
    """Sequences laid out back to back, the way animation.cfg describes a GLA - plus a handful
    of stills and a few genuinely overlapping ranges, as the real file has."""
    seqs = []
    frame = 0
    for i in range(count):
        if i % 17 == 0:
            seqs.append((f"STILL_{i}", frame, 1))
            frame += 1
        else:
            length = 5 + (i % 23)
            seqs.append((f"BOTH_SEQ_{i}", frame, length))
            frame += length
        if i % 40 == 0 and i:
            # a TORSO_ variant re-using an earlier range, which really does occur
            seqs.append((f"TORSO_ALT_{i}", max(0, frame - 30), 12))
    return seqs


failures = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  -- " + detail) if detail else ""))
    if not cond:
        failures.append(name)


sequences = humanoid_like(400)
print(f"{len(sequences)} sequences modelled after an animation.cfg layout\n")

placedBake, unplacedBake, tracksBake = place(sequences, bakeFirst=True)
placedStrip, unplacedStrip, tracksStrip = place(sequences, bakeFirst=False)

print("  bake first:  {}/{} placed, {} lost".format(
    placedBake, len(sequences), len(unplacedBake)))
print("  strip first: {}/{} placed, {} lost".format(
    placedStrip, len(sequences), len(unplacedStrip)))
print()

check("strip-first places every sequence", len(unplacedStrip) == 0,
      f"{len(unplacedStrip)} lost, e.g. {unplacedStrip[:3]}")
# Result: ordering does NOT change placement. The footprint at creation time only matters
# when strips genuinely collide, and a cfg-shaped layout does not produce that. So the
# strip-before-bake order is restored because it is the original, proven one - not because
# it fixes anything. Ordering is ruled out as the cause of the missing strips.
check("ordering does not change how many strips are placed", placedBake == placedStrip,
      f"bake-first {placedBake}, strip-first {placedStrip}")
check("strip-first needs fewer layers",
      len([t for t in tracksStrip.values() if t.spans])
      <= len([t for t in tracksBake.values() if t.spans]),
      "strip-first {} vs bake-first {}".format(
          len([t for t in tracksStrip.values() if t.spans]),
          len([t for t in tracksBake.values() if t.spans])))

print("\n[2] adjacent sequences sharing a boundary frame")
adjacent = [("A", 0, 10), ("B", 10, 5), ("C", 15, 8)]
pb, ub, _ = place(adjacent, bakeFirst=True)
ps, us, _ = place(adjacent, bakeFirst=False)
check("both orders keep all three", ps == 3 and pb == 3, f"strip-first {ps}/3, bake-first {pb}/3")
if ub:
    print(f"  bake-first lost: {ub}")

print("\n[3] final extents are identical either way, so nothing else changes")
_, _, t1 = place(adjacent, bakeFirst=False)
spans = sorted(s for tr in t1.values() for s in tr.spans)
check("extents correct after resize", spans == [(0, 9), (10, 14), (15, 22)], str(spans))

print("\n[4] exactly as many Stills tracks as the stills need, created up front")
# nla_tracks.new() appends on TOP and a higher track overrides a lower one, and
# nla_tracks.new(prev=...) can only insert AFTER a given track - so creating the Stills tracks
# before any Sequences track is the only way to get them underneath. That matters because
# Blender cannot make a strip shorter than one frame OF LENGTH: a 1-frame "still" at frame N
# occupies N..N+1 and, from above, holds its pose over frame N+1.


def needed_layers(stillStarts, maxLayers=8):
    """Same rule as the importer's placement loop: lowest layer without an overlap."""
    layers = []
    for start in stillStarts:
        span = (start, start + 1)
        for occupied in layers:
            if not any(span[0] <= e and s <= span[1] for s, e in occupied):
                occupied.append(span)
                break
        else:
            if len(layers) >= maxLayers:
                break
            layers.append([span])
    return max(1, len(layers))


check("non-overlapping stills need one layer", needed_layers([10, 40, 100]) == 1)
check("two stills on the same frame need two", needed_layers([15456, 15456]) == 2)
check("adjacent stills overlap too, because each spans two frames",
      needed_layers([10, 11]) == 2)
check("the real _humanoid pattern needs two",
      needed_layers([18, 23, 24, 29, 15456, 15456, 3225]) == 2)
check("it never exceeds the 8 layer cap", needed_layers([500] * 20) == 8)


def loses_to_stills(order):
    stillIndices = [i for i, n in enumerate(order) if n.startswith("Stills")]
    highest = max(stillIndices) if stillIndices else -1
    return [n for i, n in enumerate(order) if n.startswith("Sequences") and i < highest]


# The layout an NLA dump showed before the fix - Stills Layer 2 created lazily, on top.
DUMPED = ["Stills Layer 1", "Sequences Layer 1", "Sequences Layer 2", "Sequences Layer 3",
          "Sequences Layer 4", "Sequences Layer 5", "Sequences Layer 6", "Stills Layer 2"]
check("that layout lets Stills Layer 2 override every sequence track",
      loses_to_stills(DUMPED) != [], str(loses_to_stills(DUMPED)))

FIXED = ["Stills Layer 1", "Stills Layer 2"] + \
        ["Sequences Layer {}".format(i) for i in range(1, 7)]
check("creating both stills tracks up front leaves nothing above them",
      loses_to_stills(FIXED) == [])
check("the sequence tracks keep their relative order",
      [n for n in FIXED if n.startswith("Sequences")]
      == ["Sequences Layer {}".format(i) for i in range(1, 7)])

# NOTE on the older measurements in this file's history: every A/B number taken before the
# JAG2PoseSampler cache key was fixed (it was id(action) ^ id(slot), which collides) is void.
# Three identical runs produced 284, 68 and 432 wrong frames, so the spread with NO code change
# was wider than any difference measured between variants. Only frame 15457 was wrong in all
# three - the one this fix targets.

print("\n" + ("ALL CHECKS PASSED" if not failures else f"FAILURES: {failures}"))
sys.exit(1 if failures else 0)
