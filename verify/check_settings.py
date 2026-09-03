"""Checks the remembered import/export settings, without Blender.

Blender keeps operator properties only for the current session, and drops them on every addon
reload - so a .gla round trip means retyping the base path, scale, skeleton fixes and reference
gla each time. JAG2Settings stores them in the addon preferences, which Blender writes to
userpref.blend.

The tricky parts are all about NOT corrupting an operator: values are stored as strings and
converted back through each property's declared type, so a setting whose enum items or range
changed since it was saved is skipped rather than applied. File paths are never restored, since
the file browser owns those.
"""

import os
import sys
import types
import importlib.util

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --- stub bpy ---------------------------------------------------------------
class _Prop:
    def __init__(self, type_, is_readonly=False, enum_items=()):
        self.type = type_
        self.is_readonly = is_readonly
        self.enum_items = [types.SimpleNamespace(identifier=i) for i in enum_items]


class _Rna:
    def __init__(self, props):
        self.properties = props


bpy = types.ModuleType("bpy")
bpy.props = types.SimpleNamespace(
    BoolProperty=lambda **kw: None, StringProperty=lambda **kw: None)
# Properties every Operator inherits; JAG2Settings must not try to remember these.
_BASE = {"rna_type": _Prop('POINTER', is_readonly=True),
         "bl_idname": _Prop('STRING', is_readonly=True)}
class _FakeOperatorBase:
    """Stand-in for bpy.types.Operator: subclassable, and carries the inherited properties."""
    bl_rna = _Rna(dict(_BASE))


bpy.types = types.SimpleNamespace(
    AddonPreferences=object, Operator=_FakeOperatorBase, Context=object)
mathutils = types.ModuleType("mathutils")
mathutils.Matrix = object
mathutils.Vector = object
sys.modules["mathutils"] = mathutils

bpy.utils = types.SimpleNamespace(register_class=lambda c: None,
                                  unregister_class=lambda c: None)
bpy.context = types.SimpleNamespace(preferences=None)
sys.modules["bpy"] = bpy

pkg = types.ModuleType("jediacademy")
pkg.__path__ = [REPO]
sys.modules["jediacademy"] = pkg
mod_reload = types.ModuleType("jediacademy.mod_reload")
mod_reload.reload_modules = lambda *a, **kw: None
sys.modules["jediacademy.mod_reload"] = mod_reload

spec = importlib.util.spec_from_file_location(
    "jediacademy.JAG2Settings", os.path.join(REPO, "JAG2Settings.py"))
S = importlib.util.module_from_spec(spec)
sys.modules["jediacademy.JAG2Settings"] = S
spec.loader.exec_module(S)


# --- fakes ------------------------------------------------------------------
class FakePrefs:
    def __init__(self, remember=True):
        self.remember = remember
        self.rememberedJson = ""


class FakeOperator:
    def __init__(self, idname, props, values):
        self.bl_idname = idname
        allProps = dict(_BASE)
        allProps.update(props)
        self.bl_rna = _Rna(allProps)
        for name, value in values.items():
            setattr(self, name, value)


def install(prefs):
    """Point JAG2Settings at these preferences."""
    S._preferences = lambda: prefs


IMPORT_PROPS = {
    "filepath": _Prop('STRING'),
    "basepath": _Prop('STRING'),
    "scale": _Prop('FLOAT'),
    "skeletonFixes": _Prop('ENUM', enum_items=("NONE", "JKA_HUMANOID")),
    "loadAnimations": _Prop('ENUM', enum_items=("NONE", "CFG", "ALL", "RANGE")),
    "startFrame": _Prop('INT'),
    "guessTextures": _Prop('BOOLEAN'),
}
IMPORT_VALUES = {
    "filepath": "C:/jka/models/_humanoid.gla",
    "basepath": "C:/jka_animations/Anims/GameData/base/",
    "scale": 10.0,
    "skeletonFixes": "JKA_HUMANOID",
    "loadAnimations": "CFG",
    "startFrame": 0,
    "guessTextures": True,
}

failures = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  -- " + detail) if detail else ""))
    if not cond:
        failures.append(name)


print("[1] a round trip restores every setting except the file path")
prefs = FakePrefs()
install(prefs)
source = FakeOperator("import_scene.gla", IMPORT_PROPS, IMPORT_VALUES)
S.save(source)
check("something was stored", prefs.rememberedJson not in ("", "{}"), prefs.rememberedJson[:60])

target = FakeOperator("import_scene.gla", IMPORT_PROPS,
                      dict(IMPORT_VALUES, filepath="", basepath="", scale=100.0,
                           skeletonFixes="NONE", loadAnimations="NONE", startFrame=5,
                           guessTextures=False))
S.restore(target)
check("base path restored", target.basepath == IMPORT_VALUES["basepath"], target.basepath)
check("scale restored as a float", target.scale == 10.0 and isinstance(target.scale, float),
      repr(target.scale))
check("enum restored", target.skeletonFixes == "JKA_HUMANOID", target.skeletonFixes)
check("second enum restored", target.loadAnimations == "CFG", target.loadAnimations)
check("int restored as an int", target.startFrame == 0 and isinstance(target.startFrame, int),
      repr(target.startFrame))
check("bool restored as a bool", target.guessTextures is True, repr(target.guessTextures))
check("file path deliberately NOT restored", target.filepath == "",
      "the file browser owns it")

print("\n[2] operators do not read each other's settings")
other = FakeOperator("export_scene.gla", IMPORT_PROPS, dict(IMPORT_VALUES, basepath=""))
other.basepath = ""
S.restore(other)
check("a different operator id gets nothing", other.basepath == "")

print("\n[3] stale values are skipped, not forced in")
prefs2 = FakePrefs()
install(prefs2)
S.save(FakeOperator("import_scene.gla", IMPORT_PROPS,
                    dict(IMPORT_VALUES, skeletonFixes="SOME_OLD_PRESET")))
narrowed = dict(IMPORT_PROPS)
narrowed["skeletonFixes"] = _Prop('ENUM', enum_items=("NONE",))   # that preset no longer exists
victim = FakeOperator("import_scene.gla", narrowed, dict(IMPORT_VALUES, skeletonFixes="NONE"))
S.restore(victim)
check("an enum value that no longer exists is skipped", victim.skeletonFixes == "NONE",
      victim.skeletonFixes)
check("the other settings still came through", victim.basepath == IMPORT_VALUES["basepath"])

print("\n[4] corrupt or missing storage never breaks an operator")
prefs3 = FakePrefs()
prefs3.rememberedJson = "{not json at all"
install(prefs3)
survivor = FakeOperator("import_scene.gla", IMPORT_PROPS, dict(IMPORT_VALUES, basepath="keep"))
S.restore(survivor)
check("corrupt json is ignored", survivor.basepath == "keep")
install(lambda: None and None)
S._preferences = lambda: None
S.restore(survivor)
S.save(survivor)
check("no preferences at all is survivable", survivor.basepath == "keep")

print("\n[5] the feature can be turned off, and forgotten")
prefs4 = FakePrefs(remember=False)
install(prefs4)
S.save(FakeOperator("import_scene.gla", IMPORT_PROPS, IMPORT_VALUES))
check("nothing is stored when disabled", prefs4.rememberedJson == "")

prefs5 = FakePrefs()
install(prefs5)
S.save(FakeOperator("import_scene.gla", IMPORT_PROPS, IMPORT_VALUES))
S.forget()
check("forget() clears the store", prefs5.rememberedJson == "")
blank = FakeOperator("import_scene.gla", IMPORT_PROPS, dict(IMPORT_VALUES, basepath="untouched"))
S.restore(blank)
check("nothing is restored afterwards", blank.basepath == "untouched")

print("\n" + ("ALL CHECKS PASSED" if not failures else f"FAILURES: {failures}"))
sys.exit(1 if failures else 0)
