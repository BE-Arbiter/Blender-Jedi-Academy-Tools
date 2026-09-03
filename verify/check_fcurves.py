"""Checks _newFCurve's reuse logic without Blender.

The RuntimeError seen on Blender 5.1 -

    F-Curve 'pose.bones["model_root"].location[0]' already exists in this channelbag

- happened because the container probe left a curve behind and _newFCurve then called new()
for a channel that already existed. From 4.4 on that raises instead of returning the existing
curve. This models the three container flavours the addon has to cope with and asserts that
_newFCurve returns an *empty* curve in every case, whether or not one was already there.
"""
import os
import sys
import types
import importlib.util

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --- stubs ------------------------------------------------------------------
bpy = types.ModuleType("bpy")
bpy.types = types.SimpleNamespace(
    Action=object, Object=object, Armature=object, PoseBone=object, Bone=object,
    EditBone=object, NlaTrack=object, NlaStrip=object, Keyframe=object, MeshVertex=object,
    Scene=object)
bpy.utils = types.SimpleNamespace(escape_identifier=lambda s: s)
bpy.data = types.SimpleNamespace(actions=None, objects={}, armatures=None)
bpy.app = types.SimpleNamespace(version_string="stub")
bpy.ops = types.SimpleNamespace()
bpy.context = types.SimpleNamespace(scene=None)
sys.modules["bpy"] = bpy

mathutils = types.ModuleType("mathutils")
mathutils.Matrix = object
mathutils.Vector = object
mathutils.Quaternion = object
sys.modules["mathutils"] = mathutils

pkg = types.ModuleType("jediacademy")
pkg.__path__ = [REPO]
sys.modules["jediacademy"] = pkg
mod_reload = types.ModuleType("jediacademy.mod_reload")
mod_reload.reload_modules = lambda *a, **kw: None
sys.modules["jediacademy.mod_reload"] = mod_reload

for name in ("JAStringhelper", "common_tokenizer", "error_types", "casts", "JAFilesystem",
             "JAG2Constants", "JAG2Math", "MrwProfiler", "JAG2Panels", "JAG2AnimationCFG"):
    try:
        spec = importlib.util.spec_from_file_location(
            f"jediacademy.{name}", os.path.join(REPO, f"{name}.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"jediacademy.{name}"] = mod
        spec.loader.exec_module(mod)
    except Exception:
        # Some modules need more of bpy than is stubbed here; a permissive placeholder is
        # enough, since only the annotations reference them at import time.
        placeholder = types.ModuleType(f"jediacademy.{name}")
        placeholder.__getattr__ = lambda _n: object  # type: ignore
        sys.modules[f"jediacademy.{name}"] = placeholder

spec = importlib.util.spec_from_file_location(
    "jediacademy.JAG2GLA", os.path.join(REPO, "JAG2GLA.py"))
GLA = importlib.util.module_from_spec(spec)
sys.modules["jediacademy.JAG2GLA"] = GLA
spec.loader.exec_module(GLA)


# --- fake containers --------------------------------------------------------
class FakeKeyframePoints(list):
    def clear(self):
        del self[:]

    def add(self, n):
        self.extend([None] * n)

    def remove(self, point, fast=False):
        list.remove(self, point)


class FakeFCurve:
    def __init__(self, data_path, array_index, existing=0):
        self.data_path = data_path
        self.array_index = array_index
        self.keyframe_points = FakeKeyframePoints([object() for _ in range(existing)])


class LegacyContainer(list):
    """4.1-4.3: new() takes action_group, has find(), returns existing on duplicate? No - errors."""
    supports = ("action_group",)
    has_find = True

    def find(self, data_path, index=0):
        for fc in self:
            if fc.data_path == data_path and fc.array_index == index:
                return fc
        return None

    def new(self, data_path, index=0, **kwargs):
        if set(kwargs) - set(self.supports):
            raise TypeError(f"unexpected keyword {set(kwargs) - set(self.supports)}")
        if self.find(data_path, index) is not None:
            raise RuntimeError(
                f"Error: F-Curve '{data_path}[{index}]' already exists in this channelbag")
        fc = FakeFCurve(data_path, index)
        self.append(fc)
        return fc


class ChannelbagContainer(LegacyContainer):
    """4.4+: group_name instead of action_group."""
    supports = ("group_name",)


class OldChannelbagContainer(LegacyContainer):
    """Hypothetical build accepting no group keyword at all."""
    supports = ()


class NoFindContainer(ChannelbagContainer):
    """Container without find(), to exercise the linear-scan fallback."""

    def find(self, data_path, index=0):
        raise AttributeError("no find here")

    def _scan(self, data_path, index):
        for fc in self:
            if fc.data_path == data_path and fc.array_index == index:
                return fc
        return None

    def new(self, data_path, index=0, **kwargs):
        if set(kwargs) - set(self.supports):
            raise TypeError("bad kwarg")
        if self._scan(data_path, index) is not None:
            raise RuntimeError("already exists in this channelbag")
        fc = FakeFCurve(data_path, index)
        self.append(fc)
        return fc


failures = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  -- " + detail) if detail else ""))
    if not cond:
        failures.append(name)


PATH = 'pose.bones["model_root"].location'

print("[1] fresh container: curve gets created, empty")
for cls in (LegacyContainer, ChannelbagContainer, OldChannelbagContainer, NoFindContainer):
    c = cls()
    fc = GLA._newFCurve(c, PATH, 0, "model_root")
    check(f"{cls.__name__}: created", fc is not None and len(c) == 1)
    check(f"{cls.__name__}: empty", len(fc.keyframe_points) == 0,
          f"{len(fc.keyframe_points)} points")

print("\n[2] curve already present (the probe case that crashed on 5.1)")
for cls in (LegacyContainer, ChannelbagContainer, OldChannelbagContainer, NoFindContainer):
    c = cls()
    c.append(FakeFCurve(PATH, 0, existing=1))  # probe left one keyframe behind
    try:
        fc = GLA._newFCurve(c, PATH, 0, "model_root")
        raised = None
    except Exception as e:
        fc, raised = None, e
    check(f"{cls.__name__}: no RuntimeError", raised is None, repr(raised))
    if fc is not None:
        check(f"{cls.__name__}: reused, not duplicated", len(c) == 1, f"{len(c)} curves")
        check(f"{cls.__name__}: cleared before reuse", len(fc.keyframe_points) == 0,
              f"{len(fc.keyframe_points)} points")

print("\n[3] re-import onto an action that is already fully keyed")
c = ChannelbagContainer()
c.append(FakeFCurve(PATH, 0, existing=250))
fc = GLA._newFCurve(c, PATH, 0, "model_root")
fc.keyframe_points.add(10)
check("reused curve holds exactly the new frame count", len(fc.keyframe_points) == 10,
      f"{len(fc.keyframe_points)} points")

print("\n[4] different array indices stay separate curves")
c = ChannelbagContainer()
curves = [GLA._newFCurve(c, PATH, i, "model_root") for i in range(3)]
check("three distinct curves", len(c) == 3 and len({id(x) for x in curves}) == 3)

print("\n" + ("ALL CHECKS PASSED" if not failures else f"FAILURES: {failures}"))
sys.exit(1 if failures else 0)
