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

"""Remembering the settings last used for each import/export operator.

Blender keeps operator properties only for the current session, and only until the operator is
re-registered - which every addon reload does. Anyone repeatedly round-tripping a .gla ends up
retyping the base path, the scale, the skeleton fixes and the reference .gla every single time.

The values are stored as plain strings in the addon's preferences, which Blender saves in
userpref.blend, so they survive a restart. Strings rather than typed properties because the
operators own the typed definitions - this module must not duplicate their defaults, ranges or
enum items, or the two would drift apart.

Settings are saved when an operator runs and restored when its file dialog opens. File paths
are deliberately NOT restored into `filepath`: Blender's file browser handles the directory
itself, and forcing a stale path into it is more annoying than helpful. The base path IS
restored, since that is the tedious one to retype.
"""

from .mod_reload import reload_modules
reload_modules(locals(), __package__, [], [])  # nopep8

import bpy
import json
from typing import Any, Dict, List, Set

from .casts import OperatorReturnItems

# Property names that are never remembered, per operator - anything path-like that the file
# browser owns, plus properties whose value should not silently carry over.
_NEVER_REMEMBER = {"filepath", "filename", "directory", "files"}


class Preferences(bpy.types.AddonPreferences):
    """Holds the remembered settings. Blender writes these to userpref.blend, so they survive
    a restart - unlike operator properties, which only live for the session."""
    # __package__ is Optional[str] to a type checker; it is always set for an addon module.
    bl_idname = __package__ or "jediacademy"

    remember: bpy.props.BoolProperty(  # pyright: ignore [reportInvalidTypeForm]
        name="Remember import/export settings",
        description="Reuse the settings of the last import or export the next time the same "
                    "dialog is opened",
        default=True)

    # A JSON blob rather than typed properties: the operators own the real definitions, and
    # duplicating their defaults, ranges and enum items here would let the two drift apart.
    rememberedJson: bpy.props.StringProperty(  # pyright: ignore [reportInvalidTypeForm]
        name="Remembered settings", default="", options={'HIDDEN'})

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "remember")
        row = layout.row()
        row.enabled = bool(self.rememberedJson and self.rememberedJson != "{}")
        row.operator(ForgetSettings.bl_idname, icon='TRASH')


class ForgetSettings(bpy.types.Operator):
    """Discard the remembered import/export settings."""
    bl_idname = "jediacademy.forget_settings"
    bl_label = "Forget Saved Settings"
    bl_options = {'REGISTER'}

    def execute(self, context: bpy.types.Context) -> Set[OperatorReturnItems]:
        forget()
        self.report({'INFO'}, "Jedi Academy import/export settings forgotten")
        return {'FINISHED'}


def _preferences():
    """The addon's preferences, or None if the addon is not registered under a known name."""
    addons = getattr(getattr(bpy.context, "preferences", None), "addons", None)
    if addons is None:
        return None
    entry = addons.get(__package__)
    return getattr(entry, "preferences", None) if entry else None


def _load(prefs) -> Dict[str, Any]:
    if not prefs.rememberedJson:
        return {}
    try:
        data = json.loads(prefs.rememberedJson)
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _store(prefs, data: Dict[str, Any]) -> None:
    prefs.rememberedJson = json.dumps(data)


def _rememberedProperties(operator) -> List[str]:
    """Operator properties worth storing: everything declared by the operator itself."""
    names = []
    for name in operator.bl_rna.properties.keys():
        if name in _NEVER_REMEMBER:
            continue
        prop = operator.bl_rna.properties[name]
        if prop.is_readonly or name in ("rna_type",):
            continue
        # Properties inherited from Operator rather than declared by this one.
        if name in bpy.types.Operator.bl_rna.properties.keys():
            continue
        names.append(name)
    return names


def save(operator) -> None:
    """Store the operator's current settings. Never raises - this is a convenience only."""
    try:
        prefs = _preferences()
        if prefs is None or not prefs.remember:
            return
        stored: Dict[str, str] = {}
        for name in _rememberedProperties(operator):
            stored[name] = str(getattr(operator, name))
        data = _load(prefs)
        data[operator.bl_idname] = stored
        _store(prefs, data)
    except Exception as e:
        print("Note: could not remember settings for {}: {}".format(
            getattr(operator, "bl_idname", "?"), e))


def restore(operator) -> None:
    """Apply previously stored settings. Unknown or now-invalid values are skipped.

    Values are stored as strings, so each one is converted back through the property's own
    declared type - that way a property whose enum items or range changed simply fails to
    restore instead of putting the operator into an invalid state."""
    try:
        prefs = _preferences()
        if prefs is None or not prefs.remember:
            return
        stored = _load(prefs).get(operator.bl_idname)
        if not stored:
            return
        properties = operator.bl_rna.properties
        for name, raw in stored.items():
            if name not in properties:
                continue
            prop = properties[name]
            try:
                if prop.type == 'BOOLEAN':
                    setattr(operator, name, raw == "True")
                elif prop.type == 'INT':
                    setattr(operator, name, int(raw))
                elif prop.type == 'FLOAT':
                    setattr(operator, name, float(raw))
                elif prop.type == 'ENUM':
                    if raw in [item.identifier for item in prop.enum_items]:
                        setattr(operator, name, raw)
                else:
                    setattr(operator, name, raw)
            except Exception:
                # A single unusable value must not stop the rest from being restored.
                continue
    except Exception as e:
        print("Note: could not restore settings for {}: {}".format(
            getattr(operator, "bl_idname", "?"), e))


def forget() -> None:
    """Drop everything remembered. Backs the "Forget Saved Settings" button."""
    prefs = _preferences()
    if prefs is not None:
        prefs.rememberedJson = ""


def register():
    bpy.utils.register_class(ForgetSettings)
    bpy.utils.register_class(Preferences)


def unregister():
    bpy.utils.unregister_class(Preferences)
    bpy.utils.unregister_class(ForgetSettings)
