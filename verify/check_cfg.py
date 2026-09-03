"""Standalone check of the animation.cfg parse/serialise round trip, without Blender.

JAG2AnimationCFG imports bpy, but only uses it for type annotations and the Blender-side
export paths - the cfg parsing and __str__ are pure Python, so a stub bpy is enough.
"""
import sys
import types
import importlib.util
import tempfile
import os

import os
WORK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --- stub bpy ---------------------------------------------------------------
bpy = types.ModuleType("bpy")
bpy.types = types.SimpleNamespace(
    NlaStrip=object, Scene=object, TimelineMarker=object, Action=object)
bpy.data = types.SimpleNamespace(objects={})
bpy.utils = types.SimpleNamespace(escape_identifier=lambda s: s)
sys.modules["bpy"] = bpy

# mathutils is only needed for type annotations on the paths this exercises.
mathutils = types.ModuleType("mathutils")
mathutils.Matrix = object
mathutils.Vector = object
mathutils.Quaternion = object
sys.modules["mathutils"] = mathutils

pkg = types.ModuleType("jediacademy")
pkg.__path__ = [WORK]
sys.modules["jediacademy"] = pkg
mod_reload = types.ModuleType("jediacademy.mod_reload")
mod_reload.reload_modules = lambda *a, **kw: None
sys.modules["jediacademy.mod_reload"] = mod_reload


def load(name):
    spec = importlib.util.spec_from_file_location(
        f"jediacademy.{name}", os.path.join(WORK, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"jediacademy.{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


load("JAStringhelper")
load("common_tokenizer")
load("error_types")
load("casts")
load("JAFilesystem")
CFG = load("JAG2AnimationCFG")

failures = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  -- " + detail) if detail else ""))
    if not cond:
        failures.append(name)


def parse(text):
    d = tempfile.mkdtemp() + os.sep
    with open(os.path.join(d, "animation.cfg"), "w") as f:
        f.write(text)
    cfg = CFG.AnimationCFG()
    ok, msg = cfg.load_from_cfg(d)
    assert ok, msg
    return cfg


print("[1] loop column keeps its value instead of collapsing to a bool")
src = (
    "// name        start length loop fps\n"
    "loop_none       0     10     -1   20\n"
    "loop_zero       10    10     0    20\n"
    "loop_from_five  20    11     5    24\n"
    "loop_big        31    10     999  20\n"
)
cfg = parse(src)
got = [(s.name, s.loop) for s in cfg.sequences]
check("parsed loop values preserved", got == [
    ("loop_none", -1), ("loop_zero", 0), ("loop_from_five", 5), ("loop_big", 999)], str(got))


def old_parse_loop(v):
    """Pre-patch behaviour, for contrast."""
    return v != -1


old = [(s.name, 0 if old_parse_loop(s.loop) else -1) for s in cfg.sequences]
check("old bool behaviour would have lost 5 and 999",
      old == [("loop_none", -1), ("loop_zero", 0), ("loop_from_five", 0), ("loop_big", 0)],
      str(old))

print("\n[2] serialise -> reparse is stable")
reparsed = parse(str(cfg) + "\n")
a = [(s.name, s.start_frame, s.num_frames, s.loop, s.fps) for s in cfg.sequences]
b = [(s.name, s.start_frame, s.num_frames, s.loop, s.fps) for s in reparsed.sequences]
check("round trip through __str__ is lossless", a == b, f"{a} vs {b}")

print("\n[3] atoi tolerance and tokenizer edge cases still behave")
cfg2 = parse('seq_garbage 0 5foo -1 20\n"quoted name" 10 5 3 20\nweird//name 20 5 0 20\n')
got2 = [(s.name, s.start_frame, s.num_frames, s.loop, s.fps) for s in cfg2.sequences]
check("edge cases parse as before, with int loop", got2 == [
    ("seq_garbage", 0, 5, -1, 20), ("quoted name", 10, 5, 3, 20), ("weird//name", 20, 5, 0, 20)],
    str(got2))

print("\n[4] duplicate-suffix warning fires on Blender-style names")
cfg3 = CFG.AnimationCFG()
s = CFG.AnimationSequence()
s.name, s.start_frame, s.num_frames, s.loop, s.fps = "BOTH_STAND1.001", 0, 5, -1, 20
cfg3.sequences.append(s)
import io
import contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    cfg3._warn_about_names()
check("warns about BOTH_STAND1.001", "BOTH_STAND1.001" in buf.getvalue(), buf.getvalue().strip())

print("\n[5] recorded sequence length wins over the strip measurement")
import types as _t


class _Strip:
    def __init__(self, recorded, frame_start, frame_end):
        self.frame_start, self.frame_end = frame_start, frame_end
        self.action = _t.SimpleNamespace(
            name="X", frame_range=(0.0, frame_end - frame_start),
            g2_sequence_prop=_t.SimpleNamespace(num_frames=recorded))


# A 1-frame still: the strip spans two frames and its action holds two baked keys, so both the
# strip and the action range say 2. Only the recorded value knows it is really 1.
check("recorded length 1 beats a 2-frame strip",
      CFG._strip_num_frames(_Strip(1, 18, 19), 1) == 1,
      str(CFG._strip_num_frames(_Strip(1, 18, 19), 1)))
check("a normal sequence keeps its recorded length",
      CFG._strip_num_frames(_Strip(6, 18, 23), 1) == 6)
check("without a recorded value it falls back to measuring",
      CFG._strip_num_frames(_Strip(0, 18, 23), 1) == 6,
      str(CFG._strip_num_frames(_Strip(0, 18, 23), 1)))

print("\n" + ("ALL CHECKS PASSED" if not failures else f"FAILURES: {failures}"))
sys.exit(1 if failures else 0)
