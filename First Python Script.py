bl_info = {
    "name": "Smart Hard Surface Tools",
    "author": "Niall",
    "version": (1, 5),
    "blender": (5, 2, 0),
    "location": "View3D > Sidebar (N) > Addon Test",
    "description": "Automates booleans, shading, UVs, and UE5 exporting.",
    "category": "Object",
}

import os
import re
from contextlib import contextmanager

import bmesh
import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty
from mathutils import Matrix, Vector

CUTTER_COLLECTION = "Cutters_Collection"
WELD_MOD = "Smart_Weld"
BEVEL_MOD = "Smart_Bevel"
WEIGHTED_NORMAL_MOD = "Smart_Weighted_Normal"

# Modifiers that must always evaluate after every boolean, in this order.
POST_BOOLEAN_STACK = (WELD_MOD, BEVEL_MOD, WEIGHTED_NORMAL_MOD)
POST_BOOLEAN_TYPES = {'WELD', 'BEVEL', 'WEIGHTED_NORMAL'}

INVALID_FILENAME_CHARS = set('\\/:*?"<>|')
DUPLICATE_SUFFIX = re.compile(r"\.\d{3}$")


def _boolean_solver_items():
    """Read the solver list off the Boolean modifier's RNA.

    Built from RNA rather than hardcoded so the addon picks up whatever solvers
    the running Blender actually ships (4.5+ adds Manifold alongside Fast and
    Exact) without a version check.
    """
    try:
        rna = bpy.types.BooleanModifier.bl_rna.properties["solver"]
        return [(item.identifier, item.name, item.description) for item in rna.enum_items]
    except Exception:
        return [('FAST', "Fast", "Simple solver"), ('EXACT', "Exact", "Robust solver")]


BOOLEAN_SOLVERS = _boolean_solver_items()


# --- HELPERS -----------------------------------------------------------------

@contextmanager
def sole_active(context, obj):
    """Make obj the only selected + active object, then restore the user's selection.

    Every operator that needs bpy.ops to act on one specific object goes through
    here, so the caller never has to hand-roll (and forget to undo) the
    deselect/select/active dance.
    """
    view_layer = context.view_layer
    prev_active = view_layer.objects.active
    prev_selected = list(context.selected_objects)
    prev_mode = prev_active.mode if prev_active else 'OBJECT'

    if prev_active and prev_active.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for other in prev_selected:
        other.select_set(False)

    obj.select_set(True)
    view_layer.objects.active = obj
    try:
        yield
    finally:
        active = view_layer.objects.active
        if active and active.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        # obj may have been removed by the caller's teardown.
        if obj.name in view_layer.objects:
            obj.select_set(False)
        for other in prev_selected:
            if other.name in view_layer.objects:
                other.select_set(True)
        if prev_active and prev_active.name in view_layer.objects:
            view_layer.objects.active = prev_active
            if prev_mode != 'OBJECT':
                bpy.ops.object.mode_set(mode=prev_mode)


def get_cutter_collection(context):
    coll = bpy.data.collections.get(CUTTER_COLLECTION)
    if coll is None:
        coll = bpy.data.collections.new(CUTTER_COLLECTION)
        context.scene.collection.children.link(coll)
        coll.hide_render = True
    return coll


def stash_operand(context, operand):
    """Hide a boolean operand in the cutter collection and set it to wireframe.

    Union operands are not cutters, but they need identical treatment, so they
    share the one collection rather than proliferating bookkeeping.
    """
    coll = get_cutter_collection(context)
    if operand.name not in coll.objects:
        coll.objects.link(operand)
    # users_collection is a tuple snapshot, so unlinking while iterating is safe.
    for other in operand.users_collection:
        if other is not coll:
            other.objects.unlink(operand)
    operand.display_type = 'WIRE'
    operand.hide_render = True


def add_boolean(target, operand, operation, name, options=None):
    """Add a boolean modifier ahead of the weld/bevel/weighted-normal stack.

    Booleans appended after the shading stack produce exactly the normal
    artifacts the bevel setup exists to avoid, so new cuts are moved in front
    of the first post-boolean modifier.
    """
    mod = target.modifiers.new(name=name, type='BOOLEAN')
    mod.operation = operation
    mod.object = operand
    mod.solver = options.solver if options else 'EXACT'

    if options:
        # Exact-solver only; harmless on other solvers, absent on old builds.
        if hasattr(mod, "use_hole_tolerant"):
            mod.use_hole_tolerant = options.hole_tolerant
        if hasattr(mod, "use_self"):
            mod.use_self = options.self_intersection
        # Fast-solver coplanar tolerance.
        if hasattr(mod, "double_threshold"):
            mod.double_threshold = options.overlap_threshold

    for index, existing in enumerate(target.modifiers):
        if existing is not mod and existing.type in POST_BOOLEAN_TYPES:
            target.modifiers.move(len(target.modifiers) - 1, index)
            break
    return mod


def ensure_weld(obj, merge_distance, connected_only=True):
    """Add or update the seam-welding modifier.

    The brief asks for Merge by Distance after the union, but the union is an
    unapplied modifier, so there is no result mesh in Object Mode to weld. A
    Weld modifier is the non-destructive equivalent and keeps the whole stack
    live. Connected mode only merges vertices that already share an edge, so it
    cleans boolean slivers without collapsing unrelated nearby surfaces.
    """
    weld = obj.modifiers.get(WELD_MOD) or obj.modifiers.new(name=WELD_MOD, type='WELD')
    weld.merge_threshold = merge_distance
    if hasattr(weld, "mode"):
        weld.mode = 'CONNECTED' if connected_only else 'ALL'
    return weld


def sort_post_boolean_stack(obj):
    """Force weld -> bevel -> weighted normal to the tail of the stack, in order.

    Moving each to the end in sequence leaves them correctly ordered relative to
    one another and after every boolean, whatever order they were created in.
    """
    for name in POST_BOOLEAN_STACK:
        index = obj.modifiers.find(name)
        if index != -1:
            obj.modifiers.move(index, len(obj.modifiers) - 1)


def auto_unwrap(context, obj, angle_limit, island_margin, clear_seams):
    """Mark seams on sharp edges and unwrap. Object must be in Object Mode."""
    if not obj.data.uv_layers:
        obj.data.uv_layers.new(name="UVMap")

    with sole_active(context, obj):
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_mode(type='EDGE')
        if clear_seams:
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.mesh.mark_seam(clear=True)
        bpy.ops.mesh.select_all(action='DESELECT')
        bpy.ops.mesh.edges_select_sharp(sharpness=angle_limit)
        bpy.ops.mesh.mark_seam(clear=False)
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.uv.unwrap(method='ANGLE_BASED', margin=island_margin)
        bpy.ops.object.mode_set(mode='OBJECT')


def clean_mesh(context, obj, merge_distance, remove_interior):
    """Weld, strip interior faces and loose geometry, and fix normals.

    This runs on the evaluated export copy rather than the live object because
    the booleans are unapplied modifiers: interior faces of a union do not exist
    as editable geometry until the stack has been evaluated. A correct Exact or
    Manifold union already discards the interior, so on a clean merge the
    interior pass finds nothing; it earns its place on coplanar overlaps and on
    operands that were not watertight to begin with.
    """
    removed_faces = len(obj.data.polygons)
    removed_verts = len(obj.data.vertices)

    with sole_active(context, obj):
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.mesh.remove_doubles(threshold=merge_distance)

        if remove_interior:
            bpy.ops.mesh.select_all(action='DESELECT')
            bpy.ops.mesh.select_mode(type='FACE')
            bpy.ops.mesh.select_interior_faces()
            bpy.ops.mesh.delete(type='FACE')

        # Vertices and edges attached to no face at all.
        bpy.ops.mesh.select_mode(type='VERT')
        bpy.ops.mesh.select_all(action='DESELECT')
        bpy.ops.mesh.select_loose()
        bpy.ops.mesh.delete(type='VERT')

        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.mesh.normals_make_consistent(inside=False)
        bpy.ops.object.mode_set(mode='OBJECT')

    return removed_verts - len(obj.data.vertices), removed_faces - len(obj.data.polygons)


def sanitize_filename(name):
    cleaned = "".join("_" if char in INVALID_FILENAME_CHARS else char for char in name).strip()
    return cleaned or "Mesh"


def selected_meshes(context):
    return [obj for obj in context.selected_objects if obj.type == 'MESH']


# --- BOOLEAN & CUTTER OPERATORS ----------------------------------------------

class SmartBooleanOptions:
    """Solver settings shared by every boolean operator.

    Annotations on a plain mixin are picked up by register_class, the same way
    bpy_extras.io_utils.ExportHelper supplies filepath to exporters.
    """

    solver: EnumProperty(
        name="Solver",
        description="Exact handles coplanar faces correctly; Fast is the escape "
                    "hatch for dense meshes where Exact hangs",
        items=BOOLEAN_SOLVERS,
        default='EXACT',
    )
    hole_tolerant: BoolProperty(
        name="Hole Tolerant",
        description="Let the Exact solver cope with operands that are not fully "
                    "watertight",
        default=False,
    )
    self_intersection: BoolProperty(
        name="Self Intersection",
        description="Correctly handle operands whose own geometry overlaps itself",
        default=False,
    )
    overlap_threshold: FloatProperty(
        name="Overlap Threshold",
        description="Coplanar tolerance used by the Fast solver",
        default=1e-6, min=0.0, max=1.0, precision=6, step=0.001,
    )

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.prop(self, "solver")
        if self.solver == 'FAST':
            layout.prop(self, "overlap_threshold")
        else:
            layout.prop(self, "hole_tolerant")
            layout.prop(self, "self_intersection")
        self.draw_extra(layout)

    def draw_extra(self, layout):
        """Hook for operators with options beyond the shared solver settings."""


class OBJECT_OT_smart_difference(SmartBooleanOptions, bpy.types.Operator):
    """Apply Smart Difference Boolean and organize cutter"""
    bl_idname = "object.smart_difference"
    bl_label = "Smart Difference"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.mode == 'OBJECT'
                and context.active_object is not None
                and len(context.selected_objects) >= 2)

    def execute(self, context):
        target = context.active_object
        cutters = [obj for obj in context.selected_objects if obj is not target]
        if not cutters:
            self.report({'ERROR'}, "Select at least one cutter alongside the target.")
            return {'CANCELLED'}

        for cutter in cutters:
            add_boolean(target, cutter, 'DIFFERENCE', f"Bool_Diff_{cutter.name}", self)
            stash_operand(context, cutter)

        self.report({'INFO'}, f"Applied {len(cutters)} smart booleans.")
        return {'FINISHED'}


class OBJECT_OT_smart_slice(SmartBooleanOptions, bpy.types.Operator):
    """Cut and detach a piece from the target object"""
    bl_idname = "object.smart_slice"
    bl_label = "Smart Slice"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.mode == 'OBJECT'
                and context.active_object is not None
                and len(context.selected_objects) >= 2)

    def execute(self, context):
        target = context.active_object
        cutters = [obj for obj in context.selected_objects if obj is not target]
        if not cutters:
            self.report({'ERROR'}, "Select at least one cutter alongside the target.")
            return {'CANCELLED'}

        # Every slice is copied before any DIFFERENCE lands on the target, so
        # slice N is not pre-cut by cutters 1..N-1 from this same operation.
        # Cuts made in earlier operations are inherited on purpose.
        for cutter in cutters:
            slice_obj = target.copy()
            slice_obj.data = target.data.copy()
            slice_obj.name = f"{target.name}_Slice"
            for coll in target.users_collection:
                coll.objects.link(slice_obj)
            add_boolean(slice_obj, cutter, 'INTERSECT', f"Bool_Slice_{cutter.name}", self)

        for cutter in cutters:
            add_boolean(target, cutter, 'DIFFERENCE', f"Bool_Diff_{cutter.name}", self)
            stash_operand(context, cutter)

        self.report({'INFO'}, f"Sliced {len(cutters)} pieces from {target.name}.")
        return {'FINISHED'}


class OBJECT_OT_smart_union(SmartBooleanOptions, bpy.types.Operator):
    """Merge shapes into the active object and clean up the new seam"""
    bl_idname = "object.smart_union"
    bl_label = "Smart Union"
    bl_options = {'REGISTER', 'UNDO'}

    weld_seams: BoolProperty(
        name="Weld Seams",
        description="Add a Weld modifier after the union so rogue vertices along "
                    "the intersection cannot break the Smart Bevel",
        default=True,
    )
    weld_distance: FloatProperty(
        name="Weld Distance",
        description="Merge distance for the seam weld. Keep this well below your "
                    "smallest intended detail",
        default=0.0001, min=0.0, max=0.1, precision=5, step=0.001,
        subtype='DISTANCE',
    )
    weld_connected_only: BoolProperty(
        name="Connected Only",
        description="Only merge vertices that already share an edge. Turn off to "
                    "merge every vertex pair within the distance, which is more "
                    "thorough and more destructive",
        default=True,
    )

    @classmethod
    def poll(cls, context):
        return (context.mode == 'OBJECT'
                and context.active_object is not None
                and context.active_object.type == 'MESH'
                and len(context.selected_objects) >= 2)

    def draw_extra(self, layout):
        layout.separator()
        layout.prop(self, "weld_seams")
        if self.weld_seams:
            layout.prop(self, "weld_distance")
            layout.prop(self, "weld_connected_only")

    def execute(self, context):
        target = context.active_object
        operands = [obj for obj in context.selected_objects
                    if obj is not target and obj.type == 'MESH']
        if not operands:
            self.report({'ERROR'}, "Select at least one mesh to merge into the active object.")
            return {'CANCELLED'}

        for operand in operands:
            add_boolean(target, operand, 'UNION', f"Bool_Union_{operand.name}", self)
            stash_operand(context, operand)

        if self.weld_seams:
            ensure_weld(target, self.weld_distance, self.weld_connected_only)

        # Union must evaluate before the bevel so the new seam is bevelled with
        # everything else, and before the weighted normal so the seam shades
        # like the rest of the surface.
        sort_post_boolean_stack(target)

        self.report({'INFO'}, f"Merged {len(operands)} object(s) into {target.name}.")
        return {'FINISHED'}


# --- SHADING ------------------------------------------------------------------

class OBJECT_OT_smart_bevel(bpy.types.Operator):
    """Applies Hard-Surface Bevel and Weighted Normals"""
    bl_idname = "object.smart_bevel"
    bl_label = "Smart Bevel"
    bl_options = {'REGISTER', 'UNDO'}

    bevel_width: FloatProperty(name="Bevel Width", default=0.01, min=0.001, step=0.1)
    bevel_segments: IntProperty(name="Segments", default=3, min=1, max=10)
    sharp_angle: FloatProperty(name="Sharp Angle", default=0.523599, subtype='ANGLE')
    mark_sharp: BoolProperty(
        name="Auto-Mark Sharp",
        description="Rebuild sharp edges from the angle threshold (overwrites manual sharps)",
        default=True,
    )

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and any(
            obj.type == 'MESH' for obj in context.selected_objects
        )

    def execute(self, context):
        meshes = selected_meshes(context)
        if not meshes:
            self.report({'ERROR'}, "Select at least one mesh.")
            return {'CANCELLED'}

        for obj in meshes:
            self.shade(obj.data)
            self.build_stack(obj)

        self.report({'INFO'}, f"Applied Smart Bevel & Normals to {len(meshes)} object(s).")
        return {'FINISHED'}

    def shade(self, mesh):
        """Smooth-shade every face and rebuild sharp edges from the angle threshold.

        Written against mesh data rather than bpy.ops.object.shade_smooth() so it
        never touches the user's selection, and so it does not depend on the
        pre-4.1 use_auto_smooth flag (removed in Blender 4.1).
        """
        mesh.polygons.foreach_set("use_smooth", [True] * len(mesh.polygons))

        if self.mark_sharp:
            bm = bmesh.new()
            bm.from_mesh(mesh)
            for edge in bm.edges:
                edge.smooth = edge.calc_face_angle(0.0) < self.sharp_angle
            bm.to_mesh(mesh)
            bm.free()

        mesh.update()

    def build_stack(self, obj):
        bevel = obj.modifiers.get(BEVEL_MOD) or obj.modifiers.new(name=BEVEL_MOD, type='BEVEL')
        bevel.limit_method = 'ANGLE'
        bevel.angle_limit = self.sharp_angle
        bevel.width = self.bevel_width
        bevel.segments = self.bevel_segments
        bevel.profile = 0.7
        bevel.miter_outer = 'MITER_ARC'

        weighted_normal = (obj.modifiers.get(WEIGHTED_NORMAL_MOD)
                           or obj.modifiers.new(name=WEIGHTED_NORMAL_MOD, type='WEIGHTED_NORMAL'))
        weighted_normal.keep_sharp = True

        # Keeps weld -> bevel -> weighted normal in order at the tail, after
        # every boolean, however the user built the stack up.
        sort_post_boolean_stack(obj)


# --- UV -----------------------------------------------------------------------

class OBJECT_OT_smart_uv(bpy.types.Operator):
    """Auto-mark seams by angle and unwrap"""
    bl_idname = "object.smart_uv"
    bl_label = "Smart UV Unwrap"
    bl_options = {'REGISTER', 'UNDO'}

    angle_limit: FloatProperty(name="Sharp Angle", default=0.523599, subtype='ANGLE')
    island_margin: FloatProperty(name="Margin", default=0.02, min=0.001, max=1.0)
    clear_seams: BoolProperty(
        name="Clear Existing Seams",
        description="Discard every seam on the mesh before marking new ones",
        default=False,
    )

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and any(
            obj.type == 'MESH' for obj in context.selected_objects
        )

    def execute(self, context):
        meshes = selected_meshes(context)
        if not meshes:
            self.report({'ERROR'}, "Select at least one mesh.")
            return {'CANCELLED'}

        for obj in meshes:
            auto_unwrap(context, obj, self.angle_limit, self.island_margin, self.clear_seams)

        self.report({'INFO'}, f"Unwrapped {len(meshes)} object(s). Booleans are not applied here "
                              "- the exporter unwraps the evaluated mesh.")
        return {'FINISHED'}


# --- UE5 EXPORT PIPELINE -------------------------------------------------------

class OBJECT_OT_smart_export_ue5(bpy.types.Operator):
    """Export selected meshes as UE5-ready FBX files"""
    bl_idname = "object.smart_export_ue5"
    bl_label = "Export to UE5"
    bl_options = {'REGISTER'}

    # Only export_type is an operator property: it is set by the panel button.
    # The rest live on the scene because this operator has no UNDO, so Blender
    # never shows an adjust-last-operation panel for it.
    export_type: EnumProperty(
        name="Export Type",
        items=[('LOW', "Low Poly (_low)", ""), ('HIGH', "High Poly (_high)", "")],
        default='LOW',
    )

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and any(
            obj.type == 'MESH' for obj in context.selected_objects
        )

    def execute(self, context):
        scene = context.scene
        export_dir = bpy.path.abspath(scene.smart_export_path) if scene.smart_export_path else ""
        if not export_dir or not os.path.isdir(export_dir):
            self.report({'ERROR'}, "Please set a valid Export Directory first.")
            return {'CANCELLED'}

        unit_scale = scene.unit_settings.scale_length
        if abs(unit_scale - 1.0) > 1e-6:
            self.report({'WARNING'},
                        f"Scene Unit Scale is {unit_scale:g}; UE5 expects 1.0. "
                        "Sizes in Unreal will not match Blender.")

        sources = selected_meshes(context)
        if not sources:
            self.report({'ERROR'}, "Select at least one mesh to export.")
            return {'CANCELLED'}

        written = 0
        cleaned_verts = 0
        cleaned_faces = 0
        for source in sources:
            name = self.asset_name(source.name)
            temp = self.build_export_copy(context, source, name, scene.smart_export_origin)
            try:
                # Clean before unwrapping: the UVs must describe the final mesh.
                if scene.smart_export_clean:
                    verts, faces = clean_mesh(context, temp,
                                              scene.smart_export_merge_distance,
                                              scene.smart_export_remove_interior)
                    cleaned_verts += verts
                    cleaned_faces += faces

                # High poly is a bake source only, so it never needs UVs.
                if self.export_type == 'LOW' and scene.smart_export_unwrap:
                    auto_unwrap(context, temp, scene.smart_export_seam_angle,
                                scene.smart_export_margin, True)
                self.write_fbx(context, temp, os.path.join(export_dir, f"{name}.fbx"))
                written += 1
            finally:
                mesh = temp.data
                bpy.data.objects.remove(temp, do_unlink=True)
                bpy.data.meshes.remove(mesh)

        summary = f"Exported {written} FBX file(s) to {export_dir}"
        if cleaned_verts or cleaned_faces:
            summary += f" (removed {cleaned_verts} vertices, {cleaned_faces} faces)"
        self.report({'INFO'}, summary)
        return {'FINISHED'}

    def asset_name(self, raw_name):
        name = DUPLICATE_SUFFIX.sub("", raw_name)
        if name.startswith("SM_"):
            name = name[3:]
        for suffix in ("_high", "_low"):
            if name.endswith(suffix):
                name = name[:-len(suffix)]
                break
        suffix = "_high" if self.export_type == 'HIGH' else "_low"
        return sanitize_filename(f"SM_{name}{suffix}")

    def build_export_copy(self, context, source, name, origin_mode):
        """Build a throwaway object holding the fully evaluated mesh.

        Exporting a copy rather than the live object means booleans and bevels
        are real geometry before the UVs are generated, and lets rotation and
        scale be baked in so the FBX carries a clean transform.
        """
        depsgraph = context.evaluated_depsgraph_get()
        mesh = bpy.data.meshes.new_from_object(
            source.evaluated_get(depsgraph),
            preserve_all_data_layers=True,
            depsgraph=depsgraph,
        )
        mesh.name = name

        temp = bpy.data.objects.new(name, mesh)
        context.scene.collection.objects.link(temp)

        # Bake rotation and scale into the mesh, leaving translation on the object.
        location = source.matrix_world.translation.copy()
        mesh.transform(Matrix.Translation(-location) @ source.matrix_world)
        if source.matrix_world.determinant() < 0.0:
            # Mirrored objects come out inside-out once the transform is baked.
            mesh.flip_normals()
        temp.matrix_world = Matrix.Translation(location)

        if origin_mode == 'WORLD':
            temp.matrix_world = Matrix.Identity(4)
        elif origin_mode == 'BOTTOM':
            self.recentre_on_bounds(mesh)
            temp.matrix_world = Matrix.Identity(4)

        return temp

    @staticmethod
    def recentre_on_bounds(mesh):
        if not mesh.vertices:
            return
        coords = [vertex.co for vertex in mesh.vertices]
        low = Vector((min(c.x for c in coords), min(c.y for c in coords), min(c.z for c in coords)))
        high = Vector((max(c.x for c in coords), max(c.y for c in coords), max(c.z for c in coords)))
        offset = Vector(((low.x + high.x) * 0.5, (low.y + high.y) * 0.5, low.z))
        mesh.transform(Matrix.Translation(-offset))

    @staticmethod
    def write_fbx(context, obj, filepath):
        """FBX_SCALE_NONE keeps the scale in the geometry instead of the FBX unit
        header, which is what Unreal reads inconsistently and the cause of the
        100x/1000x import mismatch."""
        with sole_active(context, obj):
            bpy.ops.export_scene.fbx(
                filepath=filepath,
                use_selection=True,
                object_types={'MESH'},
                global_scale=1.0,
                apply_unit_scale=True,
                apply_scale_options='FBX_SCALE_NONE',
                use_space_transform=True,
                bake_space_transform=False,
                axis_forward='-Z',
                axis_up='Y',
                mesh_smooth_type='FACE',
                use_mesh_modifiers=True,
                add_leaf_bones=False,
                bake_anim=False,
            )


# --- UI ------------------------------------------------------------------------

class VIEW3D_PT_smart_tools(bpy.types.Panel):
    """Creates a Panel in the 3D Viewport Sidebar"""
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Addon Test"
    bl_label = "Smart Tools"

    def draw(self, context):
        layout = self.layout

        layout.label(text="Booleans:", icon='MOD_BOOLEAN')
        col = layout.column(align=True)
        col.scale_y = 1.5
        row = col.row(align=True)
        row.operator(OBJECT_OT_smart_difference.bl_idname, text="Difference")
        row.operator(OBJECT_OT_smart_slice.bl_idname, text="Slice")
        col.operator(OBJECT_OT_smart_union.bl_idname, text="Union")

        layout.separator()

        layout.label(text="Shading & Edges:", icon='MOD_BEVEL')
        row = layout.row()
        row.scale_y = 1.5
        row.operator(OBJECT_OT_smart_bevel.bl_idname, text="Smart Bevel")

        layout.separator()

        layout.label(text="UV & Prep:", icon='UV')
        row = layout.row()
        row.scale_y = 1.5
        row.operator(OBJECT_OT_smart_uv.bl_idname, text="Auto UV")

        layout.separator()

        layout.label(text="UE5 Pipeline:", icon='EXPORT')
        if abs(context.scene.unit_settings.scale_length - 1.0) > 1e-6:
            warning = layout.box()
            warning.alert = True
            warning.label(text="Scene Unit Scale is not 1.0", icon='ERROR')

        scene = context.scene
        layout.prop(scene, "smart_export_path", text="")
        layout.prop(scene, "smart_export_origin", text="Origin")

        layout.prop(scene, "smart_export_clean")
        if scene.smart_export_clean:
            col = layout.column(align=True)
            col.prop(scene, "smart_export_merge_distance")
            col.prop(scene, "smart_export_remove_interior")

        layout.prop(scene, "smart_export_unwrap")

        if scene.smart_export_unwrap:
            col = layout.column(align=True)
            col.prop(scene, "smart_export_seam_angle")
            col.prop(scene, "smart_export_margin")

        row = layout.row(align=True)
        row.scale_y = 1.5
        row.operator(OBJECT_OT_smart_export_ue5.bl_idname, text="High Poly").export_type = 'HIGH'
        row.operator(OBJECT_OT_smart_export_ue5.bl_idname, text="Low Poly").export_type = 'LOW'


classes = (
    OBJECT_OT_smart_difference,
    OBJECT_OT_smart_slice,
    OBJECT_OT_smart_union,
    OBJECT_OT_smart_bevel,
    OBJECT_OT_smart_uv,
    OBJECT_OT_smart_export_ue5,
    VIEW3D_PT_smart_tools,
)


SCENE_PROPS = {
    "smart_export_path": StringProperty(
        name="Export Directory",
        description="Choose a directory to export the FBX files",
        subtype='DIR_PATH',
    ),
    "smart_export_origin": EnumProperty(
        name="Origin",
        description="Where the exported mesh origin ends up",
        items=[
            ('KEEP', "Keep Origin", "Export with the origin exactly as authored"),
            ('WORLD', "World Zero", "Move the object origin to the world origin"),
            ('BOTTOM', "Bounds Bottom", "Recentre on the bounding box, origin at bottom centre"),
        ],
        default='KEEP',
    ),
    "smart_export_clean": BoolProperty(
        name="Clean Mesh",
        description="Weld, strip interior faces and loose geometry, and recalculate "
                    "normals on the evaluated mesh before it is written",
        default=True,
    ),
    "smart_export_merge_distance": FloatProperty(
        name="Merge Distance",
        description="Merge by Distance threshold applied to the evaluated mesh",
        default=0.0001, min=0.0, max=0.1, precision=5, step=0.001,
        subtype='DISTANCE',
    ),
    "smart_export_remove_interior": BoolProperty(
        name="Remove Interior Faces",
        description="Delete faces buried inside the solid, which a union over "
                    "coplanar or non-watertight operands can leave behind",
        default=True,
    ),
    "smart_export_unwrap": BoolProperty(
        name="Unwrap Low Poly",
        description="Unwrap the evaluated mesh so UVs match the boolean result",
        default=True,
    ),
    "smart_export_margin": FloatProperty(
        name="UV Margin", default=0.02, min=0.001, max=1.0,
    ),
    "smart_export_seam_angle": FloatProperty(
        name="Seam Angle", default=0.523599, subtype='ANGLE',
    ),
}


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    for name, prop in SCENE_PROPS.items():
        setattr(bpy.types.Scene, name, prop)


def unregister():
    # Classes come off first: the panel's draw() reads these scene properties.
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

    for name in SCENE_PROPS:
        if hasattr(bpy.types.Scene, name):
            delattr(bpy.types.Scene, name)


if __name__ == "__main__":
    register()
