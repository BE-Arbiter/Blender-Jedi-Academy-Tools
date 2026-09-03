import bpy
from bpy.props import StringProperty, BoolProperty, FloatProperty, IntProperty, PointerProperty


# -------------------------------------------------------------
#   PROPERTY GROUP
# -------------------------------------------------------------
class G2Props(bpy.types.PropertyGroup):
    name: StringProperty(
        name="Name",
        maxlen=64,
        default="",
        description="Ghoul2 surface or tag name"
    )  # type: ignore

    shader: StringProperty(
        name="Shader",
        maxlen=64,
        default="",
        description="Shader assigned to this surface"
    )  # type: ignore

    tag: BoolProperty(
        name="Tag",
        default=False,
        description="Marks object as a Ghoul2 tag"
    )  # type: ignore

    off: BoolProperty(
        name="Off",
        default=False,
        description="Surface initially disabled"
    )  # type: ignore

    scale: FloatProperty(
        name="Scale",
        default=100.0,
        min=0.0,
        subtype='PERCENTAGE',
        description="Skeleton scale (armature only)"
    )  # type: ignore


# Per-Action loop/fps metadata for animation.cfg NLA import/export, mirroring G2Props above --
# a nested PropertyGroup + PointerProperty rather than raw bpy.types.Action.loop_start_frame/fps
# assignment, since that raw-assignment style is exactly what Blender 5.0 broke for g2_prop
# (see the legacy migration below).
class G2SequenceProps(bpy.types.PropertyGroup):
    # An int, not a bool: in animation.cfg this column is the frame to loop back to, which JKA
    # reads with atoi into animations[i].loopFrames. Storing it as a bool collapsed every value
    # other than -1 to 0 on a round trip. -1 means "no loop".
    #
    # DELIBERATELY RENAMED from the old boolean "loop_frame". Blender keeps the stored value of
    # a property across a type change, so a .blend saved with the bool version would read False
    # back as the integer 0 - and 0 means "loop back to frame 0", not "no loop". Under the new
    # name the old data is simply absent and the -1 default applies, which is what a stored
    # False meant. A stored True (the old code could only ever write 0 or -1) is lost, which is
    # the safer direction.
    loop_start_frame: IntProperty(
        name="Loop Frame",
        default=-1,
        description="Frame this sequence loops back to, or -1 for no loop"
    )  # type: ignore

    fps: IntProperty(
        name="FPS",
        default=30,
        description="Sequence playback fps"
    )  # type: ignore

    # The sequence's true length in frames. Needed because an NLA strip cannot be shorter than
    # one frame of length, so a 1-frame sequence always occupies two - the strip's extent alone
    # cannot tell the two apart. 0 means "unknown", and the cfg export falls back to measuring
    # the strip.
    num_frames: IntProperty(
        name="Frames",
        default=0,
        min=0,
        description="Number of frames in this sequence (0 = derive it from the strip)"
    )  # type: ignore


# -------------------------------------------------------------
#   PROPERTY CHECK HELPERS
# -------------------------------------------------------------
# Stored as a genuine ad-hoc custom property (never registered through bpy.props), so its
# presence stays visible via plain dict access ("in"/.keys()) on every Blender version.
# hasG2MeshProperties/hasG2ArmatureProperties must check *this*, never `hasattr(obj, "g2_prop")`
# or `obj.g2_prop is not None`: reading `.g2_prop` at all -- even just to check it's not None --
# permanently materializes real backing data for the PointerProperty on the object, on every
# Blender version. That turned simply selecting an unrelated mesh/armature (with this panel
# visible) into something that silently wrote persistent data to it.
G2_CONFIGURED_KEY = "g2_configured"


def markG2Configured(obj: bpy.types.Object) -> None:
    obj[G2_CONFIGURED_KEY] = 1


def clearG2Configured(obj: bpy.types.Object) -> None:
    if G2_CONFIGURED_KEY in obj:
        del obj[G2_CONFIGURED_KEY]


def hasG2MeshProperties(obj: bpy.types.Object) -> bool:
    return G2_CONFIGURED_KEY in obj


def hasG2ArmatureProperties(obj: bpy.types.Object) -> bool:
    return G2_CONFIGURED_KEY in obj


# -------------------------------------------------------------
#   LEGACY PROPERTY MIGRATION
# -------------------------------------------------------------
# Versions before 2.0 stored these as flat custom properties directly on the object
# (bpy.types.Object.g2_prop_name = bpy.props.StringProperty(...) etc.) instead of nested under
# a single g2_prop PointerProperty. Blender 5.0 broke that scheme (bpy.props-registered
# properties are no longer visible via dict-style "in"/.keys() access), hence the PointerProperty
# above -- but already-existing .blend files saved under the old scheme need their data migrated,
# or it would silently look empty on next export. The legacy values themselves remain safely
# readable via plain dict access even under 5.0+ (Blender's own file-versioning duplicates
# pre-5.0 custom-property data into new storage on load), so this only ever reads them that way,
# never via `.g2_prop_name` attribute access, so objects without any legacy keys are never
# touched (and thus never materialized) by this scan.
_LEGACY_MESH_KEYS = ("g2_prop_name", "g2_prop_shader", "g2_prop_tag", "g2_prop_off")
_LEGACY_ARMATURE_KEY = "g2_prop_scale"


def _migrateLegacyG2Props(obj: bpy.types.Object) -> None:
    if obj.type == "MESH":
        if not any(key in obj for key in _LEGACY_MESH_KEYS):
            return
        props = obj.g2_prop  # pyright: ignore[reportAttributeAccessIssue]
        props.name = obj.get("g2_prop_name", "")
        props.shader = obj.get("g2_prop_shader", "")
        props.tag = bool(obj.get("g2_prop_tag", False))
        props.off = bool(obj.get("g2_prop_off", False))
        for key in _LEGACY_MESH_KEYS:
            if key in obj:
                del obj[key]
        markG2Configured(obj)
    elif obj.type == "ARMATURE":
        if _LEGACY_ARMATURE_KEY not in obj:
            return
        obj.g2_prop.scale = obj.get(_LEGACY_ARMATURE_KEY, 100)  # pyright: ignore[reportAttributeAccessIssue]
        del obj[_LEGACY_ARMATURE_KEY]
        markG2Configured(obj)


def migrateLegacyG2Props() -> None:
    for obj in bpy.data.objects:
        _migrateLegacyG2Props(obj)


@bpy.app.handlers.persistent
def _onLoadPost(_dummy) -> None:
    migrateLegacyG2Props()


# -------------------------------------------------------------
#   UI PANEL
# -------------------------------------------------------------
class G2PropertiesPanel(bpy.types.Panel):
    bl_label = "Ghoul 2 Properties"
    bl_idname = "OBJECT_PT_g2_prop"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "object"

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        obj = context.active_object
        return obj is not None and obj.type in {"MESH", "ARMATURE"}

    def draw(self, context: bpy.types.Context) -> None:
        if (layout := self.layout) is None:
            return
        # poll() already guarantees an object of the right type is active
        obj = context.active_object
        assert obj is not None

        configured = hasG2MeshProperties(obj) if obj.type == "MESH" else hasG2ArmatureProperties(obj)
        if not configured:
            layout.operator("object.add_g2_properties")
            return

        props = obj.g2_prop  # pyright: ignore[reportAttributeAccessIssue]

        if obj.type == "MESH":
            layout.operator("object.remove_g2_properties")
            layout.prop(props, "name")
            layout.prop(props, "shader")

            row = layout.row()
            row.prop(props, "tag")
            row.prop(props, "off")

        elif obj.type == "ARMATURE":
            layout.operator("object.remove_g2_properties")
            layout.prop(props, "scale")


class G2NLAPropertiesPanel(bpy.types.Panel):
    bl_label = "GLA Animation Properties"
    bl_idname = "STRIP_PT_g2sequence_props"
    bl_space_type = 'NLA_EDITOR'
    bl_region_type = 'UI'
    bl_category = "Strip"

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        strip = context.active_nla_strip
        return strip is not None and strip.action is not None

    def draw(self, context: bpy.types.Context) -> None:
        if (layout := self.layout) is None:
            return
        strip = context.active_nla_strip
        assert strip is not None and strip.action is not None
        props = strip.action.g2_sequence_prop  # pyright: ignore[reportAttributeAccessIssue]
        layout.use_property_split = True
        layout.prop(props, "loop_start_frame")
        layout.prop(props, "fps")


class G2ActionPropertiesPanel(bpy.types.Panel):
    bl_label = "GLA Animation Properties"
    bl_idname = "ACTION_PT_g2sequence_props"
    bl_space_type = 'DOPESHEET_EDITOR'
    bl_region_type = 'UI'
    bl_category = "Action"

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return context.active_action is not None

    def draw(self, context: bpy.types.Context) -> None:
        if (layout := self.layout) is None:
            return
        action = context.active_action
        assert action is not None
        props = action.g2_sequence_prop  # pyright: ignore[reportAttributeAccessIssue]
        layout.use_property_split = True
        layout.prop(props, "loop_start_frame")
        layout.prop(props, "fps")


# -------------------------------------------------------------
#   REGISTRATION
# -------------------------------------------------------------
classes = (
    G2Props,
    G2PropertiesPanel,
    G2SequenceProps,
    G2NLAPropertiesPanel,
    G2ActionPropertiesPanel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Object.g2_prop = PointerProperty(type=G2Props)  # pyright: ignore[reportAttributeAccessIssue]
    bpy.types.Action.g2_sequence_prop = PointerProperty(type=G2SequenceProps)  # pyright: ignore[reportAttributeAccessIssue]

    if _onLoadPost not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_onLoadPost)


def unregister():
    if _onLoadPost in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_onLoadPost)

    del bpy.types.Object.g2_prop  # pyright: ignore[reportAttributeAccessIssue]
    del bpy.types.Action.g2_sequence_prop  # pyright: ignore[reportAttributeAccessIssue]
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
