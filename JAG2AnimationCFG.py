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
reload_modules(locals(), __package__, ["JAFilesystem", "common_tokenizer"], [".casts", ".error_types"])  # nopep8

import re
import bpy
from . import JAFilesystem
from . import common_tokenizer
from .error_types import ErrorMessage
from typing import Iterator, List, Optional, Tuple

# Blender appends .001 etc. on datablock name collisions. JKA matches sequence names against
# a fixed enum table in BG_ParseAnimationFile, so a suffixed name is silently not mapped.
_DUPLICATE_SUFFIX = re.compile(r"\.\d{3}$")

# In animation.cfg the loop column is not a flag but the frame to jump back to; JKA reads it
# with atoi into animations[i].loopFrames. -1 means "no loop".
NO_LOOP = -1


# Tolerates trailing garbage after the number, like C's atoi (e.g. "5foo" -> 5), unlike Python's
# int() which raises. Returns 0 if there's no leading numeric prefix at all, also matching atoi.
def _atoi(token: str) -> int:
    i = 0
    n = len(token)
    if i < n and token[i] in "+-":
        i += 1
    digitsStart = i
    while i < n and token[i].isdigit():
        i += 1
    if i == digitsStart:
        return 0
    return int(token[:i])


def _strip_num_frames(nla_strip: "bpy.types.NlaStrip", length_difference: int) -> int:
    """Number of frames in the sequence this strip represents.

    Primary source is the action's own frame range, which is independent both of how Blender
    clamped the strip and of what the track is called. Blender cannot make a strip 0 frames
    long, so the previous arithmetic keyed off a "Stills Layer" prefix in the track name to
    decide whether to add 1 - which breaks as soon as a track is renamed or hand-created. The
    old formula stays as a fallback for strips whose action has no usable range."""
    action = nla_strip.action
    fallback = int(nla_strip.frame_end - nla_strip.frame_start + length_difference)
    if action is None:
        return fallback
    # Recorded by the importer, and authoritative: a 1-frame sequence cannot have a 1-frame
    # strip (Blender's minimum strip length is one frame of length), and its action deliberately
    # holds two baked frames so the extra frame does not corrupt the sequence that really owns
    # it - so neither the strip nor the action's range can tell us the true length any more.
    recorded = getattr(getattr(action, "g2_sequence_prop", None), "num_frames", 0)
    if recorded:
        return int(recorded)
    frame_range = getattr(action, "frame_range", None)
    if frame_range is None:
        return fallback
    count = int(round(frame_range[1] - frame_range[0])) + 1
    if count < 1:
        return fallback
    if count != fallback:
        print('Info: sequence "{}" - action range says {} frames, strip length says {}. '
              "Using {}.".format(action.name, count, fallback, count))
    return count


class AnimationSequence():
    def __init__(self):
        self.name = ""
        self.start_frame = -1
        self.num_frames = -1
        self.loop = NO_LOOP
        self.fps = -1

    def __str__(self):
        return f"{self.name}\t\t{self.start_frame}\t{self.num_frames}\t{self.loop}\t{self.fps}"

    @classmethod
    def from_cfg_tokens(cls, tokens: Iterator[str]) -> Optional["AnimationSequence"]:
        """Consumes the next 5 tokens (name, start, length, loop, fps) from a shared token
        stream produced by common_tokenizer.tokenize. Returns None once there's no further
        entry to read: an empty/missing name matches BG_ParseAnimationFile's own end-of-file
        check, and a stream that runs out mid-entry is treated the same way (silently stopping,
        rather than raising) since that can only happen at genuine end of file."""
        name = next(tokens, "")
        if not name:
            return None
        try:
            start_frame = next(tokens)
            num_frames = next(tokens)
            loop = next(tokens)
            fps = next(tokens)
        except StopIteration:
            return None
        new_frame = cls()
        new_frame.name = name
        new_frame.start_frame = _atoi(start_frame)
        new_frame.num_frames = _atoi(num_frames)
        # Keep the number. Collapsing to a bool here and re-emitting 0/-1 in __str__ turned
        # every loop value other than -1 into 0 on a round trip.
        new_frame.loop = _atoi(loop)
        new_frame.fps = _atoi(fps)
        return new_frame

    @classmethod
    def from_blender_markers(cls, marker1: bpy.types.TimelineMarker, marker2: bpy.types.TimelineMarker, fps: int, offset: int = 0):
        new_frame = cls()
        new_frame.name = marker1.name
        new_frame.start_frame = int(marker1.frame + offset)
        new_frame.num_frames = int(marker2.frame - marker1.frame)
        new_frame.loop = NO_LOOP
        new_frame.fps = int(fps)
        return new_frame

    @classmethod
    def from_blender_strip(cls, nla_strip: bpy.types.NlaStrip, length_difference: int, fps: int, offset: int = 0):
        assert nla_strip.action is not None
        new_frame = cls()
        new_frame.name = nla_strip.action.name
        new_frame.start_frame = int(nla_strip.frame_start + offset)
        new_frame.num_frames = _strip_num_frames(nla_strip, length_difference)
        new_frame.loop = int(nla_strip.action.g2_sequence_prop.loop_start_frame)  # pyright: ignore[reportAttributeAccessIssue]
        new_frame.fps = int(nla_strip.action.g2_sequence_prop.fps)  # pyright: ignore[reportAttributeAccessIssue]
        return new_frame


class AnimationCFG():

    def __init__(self):
        self.sequences: List[AnimationSequence] = []

    def __str__(self):
        lines = [str(seq) for seq in self.sequences]
        return "\n".join(lines)

    def _warn_about_names(self) -> None:
        """Blender enforces unique datablock names, so duplicating an action or hitting a
        collision yields e.g. BOTH_STAND1.001 - which JKA's fixed enum table won't match."""
        suspicious = [s.name for s in self.sequences if _DUPLICATE_SUFFIX.search(s.name)]
        if suspicious:
            shown = ", ".join(suspicious[:10])
            more = "" if len(suspicious) <= 10 else " (and {} more)".format(len(suspicious) - 10)
            print("Warning: {} sequence name(s) carry Blender's duplicate suffix, JKA will not "
                  "map them: {}{}".format(len(suspicious), shown, more))

    def load_from_cfg(self, cfg_file_path: str) -> Tuple[bool, ErrorMessage]:
        success, cfg_abs = JAFilesystem.FindFile(cfg_file_path + "/animation", "", ["cfg"])
        if not success:
            print("Could not find file: ", cfg_abs, sep="")
            return False, ErrorMessage("Could not find the animation.cfg next to the .gla file")

        try:
            with open(cfg_abs, mode="r") as file:
                text = file.read()
        except IOError:
            print("Could not open file: ", cfg_abs, sep="")
            return False, ErrorMessage("Could not open skin!")

        tokens = common_tokenizer.tokenize(text)
        while True:
            sequence = AnimationSequence.from_cfg_tokens(tokens)
            if sequence is None:
                break
            self.sequences.append(sequence)
        self.sequences.sort(key=lambda sequence: sequence.start_frame)
        return True, ErrorMessage("Nothing")

    def from_blender_markers(self, scene: bpy.types.Scene, offset: int):
        start_frame = scene.frame_start
        offset -= start_frame
        end_frame = scene.frame_end
        base_fps = scene.render.fps

        blender_markers = [
            marker for marker in scene.timeline_markers if (
                marker.frame >= start_frame and marker.frame <= end_frame + 1)
        ]
        blender_markers.sort(key=lambda marker: marker.frame)

        if (len(blender_markers) == 0 or
                (len(blender_markers) == 1 and blender_markers[0].frame == end_frame + 1)):
            return False, ErrorMessage("No timeline markers found! Add Markers to label animations.")

        if blender_markers[len(blender_markers) - 1].frame != end_frame + 1:
            blender_markers.append(
                scene.timeline_markers.new("LAST_EXPORT_FRAME", frame=end_frame + 1))

        for marker1, marker2 in zip(blender_markers[:-1], blender_markers[1:]):
            self.sequences.append(AnimationSequence().from_blender_markers(
                marker1,
                marker2,
                base_fps,
                offset
            ))

        last_frame = scene.timeline_markers.get("LAST_EXPORT_FRAME")
        if last_frame:
            scene.timeline_markers.remove(last_frame)

        self._warn_about_names()
        return True, ErrorMessage("Nothing")

    def from_blender_nla_tracks(self, scene: bpy.types.Scene, offset: int):
        start_frame = scene.frame_start
        offset -= start_frame
        end_frame = scene.frame_end
        base_fps = scene.render.fps

        skeleton_object = bpy.data.objects.get("skeleton_root")
        if skeleton_object is None:
            return False, ErrorMessage("Could not find skeleton object: skeleton_root")
        if skeleton_object.animation_data is None:
            return False, ErrorMessage('Skeleton object (skeleton_root) does not have animation data')
        if len(skeleton_object.animation_data.nla_tracks) == 0:
            return False, ErrorMessage("Couldn't find NLA tracks for the Skeleton object: skeleton_root")

        blender_strips: List[Tuple[bpy.types.NlaStrip, int]] = []
        skipped_no_action = 0
        for nla_track in [track for track in skeleton_object.animation_data.nla_tracks]:
            # Only a fallback for _strip_num_frames now; the length comes from the action.
            # TODO test if this works when not using English localisation
            length_difference = 0 if nla_track.name.startswith("Stills Layer") else 1
            for nla_strip in nla_track.strips:
                if nla_strip.action is None:
                    skipped_no_action += 1
                    continue
                # Overlap test rather than "starts inside the range": a strip beginning before
                # frame_start but reaching into it used to be dropped from the cfg entirely,
                # while one starting exactly at end_frame was included despite running past it.
                if nla_strip.frame_end < start_frame or nla_strip.frame_start > end_frame:
                    continue
                blender_strips.append((nla_strip, length_difference))
        blender_strips.sort(key=lambda strip: strip[0].frame_start)

        if skipped_no_action:
            print("Info: skipped {} NLA strip(s) with no action.".format(skipped_no_action))

        if len(blender_strips) == 0:
            return False, ErrorMessage("No NLA strips found! Add animation strips to label animations.")

        for strip, length_difference in blender_strips:
            self.sequences.append(AnimationSequence().from_blender_strip(
                strip,
                length_difference,
                base_fps,
                offset
            ))

        self._warn_about_names()
        return True, ErrorMessage("Nothing")
