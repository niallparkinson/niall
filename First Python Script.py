bl_info = {
    "name": "Smart Hard Surface Tools",
    "author": "Niall",
    "version": (1, 5),
    "blender": (5, 2, 0),
    "location": "View3D > Sidebar (N) > Addon Test",
    "description": "Automates booleans, shading, UVs, and UE5 exporting.",
    "category": "Object",
}

import math
import os
import re
from contextlib import contextmanager

import bmesh
import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty
from mathutils import Matrix, Vector

CUTTER_COLLECTION = "Cutters_Collection"
ARRAY_COLLECTION = "Array_Helpers"
ARRAY_RADIAL_MOD = "Array_Radial"
ARRAY_LINEAR_MOD = "Array_Linear"

# Set while a radial operator is rebuilding, so the depsgraph handler
# does not fire against half-written state.
_array_sync_suspended = False
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


def get_helper_collection(context, name, hide_on_create=False):
    """Fetch or create a hidden utility collection linked to the scene root."""
    coll = bpy.data.collections.get(name)
    if coll is not None:
        return coll

    coll = bpy.data.collections.new(name)
    context.scene.collection.children.link(coll)
    coll.hide_render = True

    if hide_on_create:
        layer = find_layer_collection(context.view_layer.layer_collection, name)
        if layer is not None:
            # The eye only, never exclude: excluded objects leave the view layer,
            # and an array offset object has to stay evaluated to drive the array.
            layer.hide_viewport = True
    return coll


def get_cutter_collection(context):
    return get_helper_collection(context, CUTTER_COLLECTION)


def stash_in_collection(context, obj, collection):
    """Move an object so it lives only in the given collection."""
    if obj.name not in collection.objects:
        collection.objects.link(obj)
    for other in obj.users_collection:
        if other is not collection:
            other.objects.unlink(obj)


def find_layer_collection(layer_collection, name):
    """Depth-first search for the LayerCollection wrapping a named collection.

    view_layer.layer_collection is a tree mirroring the scene's collection
    hierarchy, and only the LayerCollection carries the per-view-layer eye
    toggle, so the collection datablock alone is not enough to find it.
    """
    if layer_collection.collection.name == name:
        return layer_collection
    for child in layer_collection.children:
        found = find_layer_collection(child, name)
        if found is not None:
            return found
    return None


def get_cutter_layer_collection(context):
    """The cutter collection's LayerCollection, or None if it does not exist yet."""
    if bpy.data.collections.get(CUTTER_COLLECTION) is None:
        return None
    return find_layer_collection(context.view_layer.layer_collection, CUTTER_COLLECTION)


def cutters_visible(layer_collection):
    """True while any cutter is actually on screen.

    Deliberately not just the collection's eye: a cutter hidden individually
    with H stays hidden when the collection is revealed, so testing the
    collection alone would make the toggle look broken. Any visible cutter means
    the next click should hide.
    """
    if layer_collection is None or layer_collection.hide_viewport or layer_collection.exclude:
        return False
    return any(not obj.hide_get() for obj in layer_collection.collection.objects)


def lock_cutter_render_visibility(collection):
    """Re-assert that cutters never reach a render, whatever the viewport shows."""
    collection.hide_render = True
    for obj in collection.objects:
        obj.hide_render = True


def stash_operand(context, operand):
    """Hide a boolean operand in the cutter collection and set it to wireframe.

    Union operands are not cutters, but they need identical treatment, so they
    share the one collection rather than proliferating bookkeeping.
    """
    stash_in_collection(context, operand, get_cutter_collection(context))
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


def mesh_bounds(mesh, matrix, skip_loose=True):
    """Min and max corners of a mesh, transformed by matrix.

    Loose vertices are excluded by default: a stray vertex left behind by an
    earlier edit would drag the bounding box away from the visible silhouette
    and drop the pivot somewhere the model does not reach.
    """
    if skip_loose and mesh.polygons:
        indices = [0] * len(mesh.loops)
        mesh.loops.foreach_get("vertex_index", indices)
        vertices = mesh.vertices
        coords = [matrix @ vertices[index].co for index in set(indices)]
    else:
        coords = [matrix @ vertex.co for vertex in mesh.vertices]

    if not coords:
        return None, None

    low = Vector((min(c.x for c in coords), min(c.y for c in coords), min(c.z for c in coords)))
    high = Vector((max(c.x for c in coords), max(c.y for c in coords), max(c.z for c in coords)))
    return low, high


def bottom_centre(low, high):
    """Centre in X and Y, floor in Z - the pivot Unreal expects on a ground prop."""
    return Vector(((low.x + high.x) * 0.5, (low.y + high.y) * 0.5, low.z))


def face_alignment_matrix(obj, faces, active_face):
    """World matrix whose Z points along the face normal and X along its tangent.

    Normals are transformed by the inverse transpose of the 3x3, not the matrix
    itself, so a non-uniformly scaled object still yields a perpendicular Z.
    The tangent comes from the face's longest edge, which gives a repeatable
    roll: a square cutter lands square to the panel rather than at some
    arbitrary angle around the normal.
    """
    matrix_world = obj.matrix_world
    normal_matrix = matrix_world.to_3x3().inverted_safe().transposed()

    centre = Vector((0.0, 0.0, 0.0))
    normal = Vector((0.0, 0.0, 0.0))
    for face in faces:
        centre += matrix_world @ face.calc_center_median()
        normal += normal_matrix @ face.normal
    centre /= len(faces)

    if normal.length < 1e-9:
        return None
    normal.normalize()

    source = active_face if active_face in faces else faces[0]
    try:
        tangent = matrix_world.to_3x3() @ source.calc_tangent_edge()
        tangent -= normal * tangent.dot(normal)
    except (ValueError, RuntimeError):
        tangent = None

    if tangent is None or tangent.length < 1e-9:
        # Degenerate tangent (or a normal parallel to it): fall back to a
        # tracked rotation, which is stable but has an arbitrary roll.
        rotation = normal.to_track_quat('Z', 'Y').to_matrix().to_4x4()
        return Matrix.Translation(centre) @ rotation

    x_axis = tangent.normalized()
    y_axis = normal.cross(x_axis)
    basis = Matrix((x_axis, y_axis, normal)).transposed().to_4x4()
    return Matrix.Translation(centre) @ basis


def is_smart_bevel(modifier):
    """Only modifiers this addon created.

    Type is checked alongside the name so a hand-built modifier that merely
    starts with the same word is never touched, and startswith covers the
    Smart_Bevel.001 Blender produces on some duplications.
    """
    return modifier.type == 'BEVEL' and modifier.name.startswith(BEVEL_MOD)


def iter_smart_bevels(scene, view_layer, selected_only):
    """Every Smart Bevel in the scene, or only on the selected objects.

    Scoped to scene.objects rather than bpy.data.objects: the latter also holds
    objects belonging to other scenes and orphaned data, which are not what
    "the whole scene" means to someone dragging the slider.
    """
    objects = view_layer.objects.selected if selected_only else scene.objects
    for obj in objects:
        if obj.type != 'MESH':
            continue
        for modifier in obj.modifiers:
            if is_smart_bevel(modifier):
                yield modifier


@contextmanager
def bevels_at_render_visibility(objects):
    """Restore muted Smart Bevels for the duration of an export.

    The exporter reads the viewport depsgraph, so a bevel muted for viewport
    performance would otherwise export as an unbevelled mesh: exactly the
    silent failure the mute toggle promises not to cause. Each bevel is forced
    to its own render visibility, then put back.
    """
    changed = []
    for obj in objects:
        for modifier in obj.modifiers:
            if is_smart_bevel(modifier) and modifier.show_viewport != modifier.show_render:
                changed.append((modifier, modifier.show_viewport))
                modifier.show_viewport = modifier.show_render

    if changed:
        bpy.context.view_layer.update()
    try:
        yield
    finally:
        for modifier, previous in changed:
            modifier.show_viewport = previous


def apply_bevel_resolution(scene, view_layer):
    """Push the scene's bevel settings onto every targeted Smart Bevel."""
    count = 0
    for modifier in iter_smart_bevels(scene, view_layer, scene.smart_bevel_selected_only):
        modifier.segments = scene.smart_bevel_segments
        if scene.smart_bevel_override_width:
            modifier.width = scene.smart_bevel_width
        count += 1
    return count


def _update_bevel_resolution(self, context):
    apply_bevel_resolution(self, context.view_layer)


def _update_bevel_mute(self, context):
    for modifier in iter_smart_bevels(self, context.view_layer, self.smart_bevel_selected_only):
        # Viewport only. show_render is never touched, so the export path and
        # any Blender render still see the bevels.
        modifier.show_viewport = not self.smart_bevel_mute


def ordered_paths(edge_pairs):
    """Sort loose edges into ordered vertex chains.

    Returns a list of (indices, closed) tuples. Selected edges arrive as an
    unordered set, but a swept tube needs them walked end to end, and a closed
    loop needs to be recognised as closed so the tube is not capped mid-run.
    Branching selections are split at the junction into separate runs.
    """
    neighbours = {}
    for first, second in edge_pairs:
        neighbours.setdefault(first, []).append(second)
        neighbours.setdefault(second, []).append(first)

    unused = {frozenset(pair) for pair in edge_pairs}

    def walk(start):
        chain = [start]
        closed = False
        while True:
            current = chain[-1]
            step = None
            for candidate in neighbours.get(current, ()):
                if frozenset((current, candidate)) in unused:
                    step = candidate
                    break
            if step is None:
                break
            unused.discard(frozenset((current, step)))
            if step == chain[0]:
                closed = True
                break
            chain.append(step)
        return chain, closed

    paths = []

    # Open runs first, started from their free ends, so a chain is never
    # picked up from the middle and returned as two half-chains.
    for vertex in [v for v, linked in neighbours.items() if len(linked) == 1]:
        while any(frozenset((vertex, other)) in unused for other in neighbours[vertex]):
            chain, closed = walk(vertex)
            if len(chain) > 1:
                paths.append((chain, closed))

    # Whatever survives has no free end, so it is a cycle.
    while unused:
        seed = next(iter(next(iter(unused))))
        chain, closed = walk(seed)
        if len(chain) < 2:
            break
        paths.append((chain, closed))

    return paths


def sweep_frames(points, normals, closed):
    """Yield (point, lateral, up) for each point along a path.

    Up is the surface normal re-orthogonalised against the direction of travel,
    so the groove's cross-section stays square to the hull. Taking up from the
    surface rather than propagating a frame along the curve is what stops the
    tube twisting as it crosses a curved panel.
    """
    count = len(points)
    for index in range(count):
        point = points[index]
        if closed:
            behind, ahead = points[index - 1], points[(index + 1) % count]
        else:
            behind = points[index - 1] if index > 0 else point
            ahead = points[index + 1] if index < count - 1 else point

        tangent = ahead - behind
        if tangent.length < 1e-12:
            continue
        tangent.normalize()

        lateral = tangent.cross(normals[index])
        if lateral.length < 1e-9:
            continue  # Surface normal parallel to the path: no usable frame.
        lateral.normalize()

        yield point, lateral, lateral.cross(tangent).normalized()


def panel_tube_geometry(frames, closed, width, depth, overshoot):
    """Vertices and quads for a rectangular tube swept along the frames.

    The cross-section deliberately pokes overshoot above the surface. A cutter
    whose top face is flush with the hull hands the solver a coplanar pair,
    which is the classic way a boolean groove comes out ragged.
    """
    half = width * 0.5
    rings = [
        [
            point - lateral * half + up * overshoot,
            point + lateral * half + up * overshoot,
            point + lateral * half - up * depth,
            point - lateral * half - up * depth,
        ]
        for point, lateral, up in frames
    ]
    if len(rings) < 2:
        return [], []

    vertices = [corner for ring in rings for corner in ring]

    faces = []
    segments = len(rings) if closed else len(rings) - 1
    for index in range(segments):
        near = index * 4
        far = ((index + 1) % len(rings)) * 4
        for corner in range(4):
            following = (corner + 1) % 4
            faces.append((near + corner, near + following, far + following, far + corner))

    if not closed:
        last = (len(rings) - 1) * 4
        faces.append((0, 1, 2, 3))
        faces.append((last + 3, last + 2, last + 1, last))

    return vertices, faces


def pivot_rotation_matrix(pivot, axis, angle):
    """World-space transform that rotates by angle about an arbitrary axis line."""
    return (Matrix.Translation(pivot)
            @ Matrix.Rotation(angle, 4, axis)
            @ Matrix.Translation(-pivot))


def iter_radial_arrays(scene):
    """Every (target, modifier, empty) radial array set up in the scene.

    Driven from the target rather than the empty: the modifier already holds the
    only link that matters, so nothing depends on parenting or on names.
    """
    for target in scene.objects:
        modifier = target.modifiers.get(ARRAY_RADIAL_MOD)
        if modifier is None or modifier.offset_object is None:
            continue
        yield target, modifier, modifier.offset_object


def radial_signature(modifier, matrix):
    """What the empty's placement was computed from, for change detection."""
    return [float(modifier.count)] + [round(value, 6) for row in matrix for value in row]


def sync_radial_array(target, modifier, empty):
    """Place the offset empty so the array sweeps a ring about the stored pivot.

    The Array modifier builds its per-copy step as inverse(target) @ empty, so
    the empty must hold R @ target_matrix, where R rotates about the pivot line.
    The step is then inverse(M) @ R @ M, whose i-th power is inverse(M) @ R^i @ M,
    placing copy i at R^i applied to the object's world geometry.

    Dropping the target matrix and storing R alone leaves the step as
    inverse(M) @ R, which is not a rotation at all: it compounds the object's own
    transform once per copy, so the copies spiral outwards and grow. Verified
    against Blender rather than derived: E = R @ M holds a ring, E = R does not.

    Pivot and axis are held in world space: the pivot belongs to the 3D cursor,
    not to the object, so moving the object changes the ring's radius and leaves
    its centre where it was put.
    """
    if "array_pivot" not in empty or "array_axis" not in empty:
        return False

    matrix = target.matrix_world.copy()
    pivot = Vector(empty["array_pivot"][:])
    axis = Vector(empty["array_axis"][:])
    if axis.length < 1e-9:
        return False

    angle = 2.0 * math.pi / max(modifier.count, 1)
    empty.matrix_world = pivot_rotation_matrix(pivot, axis.normalized(), angle) @ matrix
    empty["array_sig"] = radial_signature(modifier, matrix)
    return True


@contextmanager
def suspended_array_sync():
    """Stop the depsgraph handler running while an operator is mid-rebuild."""
    global _array_sync_suspended
    _array_sync_suspended = True
    try:
        yield
    finally:
        _array_sync_suspended = False


@bpy.app.handlers.persistent
def sync_radial_arrays_on_update(scene, depsgraph=None):
    """Keep every ring correct as the count changes or the target moves.

    The modifier's count has no update callback, and the empty's placement
    depends on the target's matrix, so both are watched. Writing re-triggers the
    handler, so the stored signature gates the write and the second pass finds
    nothing to do.
    """
    if _array_sync_suspended:
        return

    for target, modifier, empty in iter_radial_arrays(scene):
        stored = empty.get("array_sig")
        if stored is not None and list(stored[:]) == radial_signature(modifier,
                                                                     target.matrix_world):
            continue
        sync_radial_array(target, modifier, empty)


def resolve_array_axis(context, mode):
    """Rotation axis in world space for the chosen mode."""
    if mode == 'CURSOR':
        # Pairs with Align Cursor to Face: the cursor's Z is the surface normal,
        # which is the axis a ring of bolts on that panel should turn about.
        return (context.scene.cursor.matrix.to_3x3() @ Vector((0.0, 0.0, 1.0))).normalized()

    if mode == 'VIEW':
        space = context.space_data
        region_3d = getattr(space, "region_3d", None) if space else None
        if region_3d is not None:
            direction = region_3d.view_rotation @ Vector((0.0, 0.0, 1.0))
            dominant = max(range(3), key=lambda index: abs(direction[index]))
            axis = Vector((0.0, 0.0, 0.0))
            axis[dominant] = math.copysign(1.0, direction[dominant])
            return axis
        return Vector((0.0, 0.0, 1.0))

    axis = Vector((0.0, 0.0, 0.0))
    axis["XYZ".index(mode)] = 1.0
    return axis


def sanitize_filename(name):
    cleaned = "".join("_" if char in INVALID_FILENAME_CHARS else char for char in name).strip()
    return cleaned or "Mesh"


def selected_meshes(context):
    return [obj for obj in context.selected_objects if obj.type == 'MESH']


# --- ORIGIN & ALIGNMENT -------------------------------------------------------

# Modifiers whose result is measured from the object origin, so moving the
# pivot moves what they build. Mirror is the one that bites on hard-surface.
ORIGIN_SENSITIVE_MODIFIERS = {'MIRROR', 'SCREW', 'SIMPLE_DEFORM', 'CAST', 'WAVE'}


class MESH_OT_cursor_to_face(bpy.types.Operator):
    """Snap the 3D cursor to the selected face and tilt it to the surface"""
    bl_idname = "mesh.cursor_to_face"
    bl_label = "Align Cursor to Face"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.mode == 'EDIT_MESH'
                and context.active_object is not None
                and context.active_object.type == 'MESH')

    def execute(self, context):
        obj = context.active_object
        bm = bmesh.from_edit_mesh(obj.data)

        faces = [face for face in bm.faces if face.select]
        if not faces:
            self.report({'ERROR'}, "Select at least one face.")
            return {'CANCELLED'}

        matrix = face_alignment_matrix(obj, faces, bm.faces.active)
        if matrix is None:
            self.report({'ERROR'}, "Selected faces have no usable normal.")
            return {'CANCELLED'}

        cursor = context.scene.cursor
        cursor.rotation_mode = 'QUATERNION'
        cursor.matrix = matrix

        self.report({'INFO'}, f"Cursor aligned to {len(faces)} face(s).")
        return {'FINISHED'}


class OBJECT_OT_snap_to_cursor(bpy.types.Operator):
    """Move selected objects onto the 3D cursor, flush with its orientation"""
    bl_idname = "object.snap_to_cursor"
    bl_label = "Snap to Cursor"
    bl_options = {'REGISTER', 'UNDO'}

    align_rotation: BoolProperty(
        name="Match Cursor Rotation",
        description="Rotate the object flush to the cursor. Turn off to move it "
                    "without changing how it is oriented",
        default=True,
    )
    offset: FloatProperty(
        name="Surface Offset",
        description="Shift along the cursor's Z after snapping, to sit a bolt head "
                    "proud of the panel or sink a cutter into it",
        default=0.0, precision=4, step=0.01, subtype='DISTANCE',
    )

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and len(context.selected_objects) > 0

    def execute(self, context):
        cursor_matrix = context.scene.cursor.matrix
        cursor_location, cursor_rotation, _ = cursor_matrix.decompose()
        target = cursor_location + (cursor_rotation @ Vector((0.0, 0.0, self.offset)))

        for obj in context.selected_objects:
            _, rotation, scale = obj.matrix_world.decompose()
            # Scale is always preserved: a snapped cutter keeps the size it was
            # dialled in at, and only its placement changes.
            obj.matrix_world = Matrix.LocRotScale(
                target,
                cursor_rotation if self.align_rotation else rotation,
                scale,
            )

        self.report({'INFO'}, f"Snapped {len(context.selected_objects)} object(s) to the cursor.")
        return {'FINISHED'}


class OBJECT_OT_origin_to_bottom(bpy.types.Operator):
    """Drop the origin to the bottom centre of the mesh bounds"""
    bl_idname = "object.origin_to_bottom"
    bl_label = "Origin to Bottom"
    bl_options = {'REGISTER', 'UNDO'}

    use_evaluated: BoolProperty(
        name="Use Modifier Result",
        description="Measure the bounds after modifiers, matching what actually "
                    "gets exported. Turn off to measure the base mesh only",
        default=True,
    )
    skip_loose: BoolProperty(
        name="Ignore Loose Vertices",
        description="Exclude vertices that belong to no face, so a stray vertex "
                    "cannot drag the pivot off the model",
        default=True,
    )

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and any(
            obj.type == 'MESH' for obj in context.selected_objects
        )

    def execute(self, context):
        depsgraph = context.evaluated_depsgraph_get()
        moved = 0
        shared = []
        sensitive = set()

        for obj in selected_meshes(context):
            if obj.data.users > 1:
                # Mesh data is shared, so transforming it would drag every
                # linked duplicate along with it.
                shared.append(obj.name)
                continue

            low, high = self.measure(obj, depsgraph)
            if low is None:
                continue

            sensitive.update(mod.type for mod in obj.modifiers
                             if mod.type in ORIGIN_SENSITIVE_MODIFIERS)

            self.move_origin(context, obj, bottom_centre(low, high))
            moved += 1

        if shared:
            self.report({'WARNING'},
                        f"Skipped {len(shared)} object(s) with shared mesh data: "
                        f"{', '.join(shared[:3])}")
        if sensitive:
            # The brief assumes the pivot can always move without disturbing the
            # result. These modifiers measure from the origin, so it cannot.
            self.report({'WARNING'},
                        f"Moved the origin under origin-relative modifiers "
                        f"({', '.join(sorted(sensitive))}); check the result.")
        if not moved:
            return {'CANCELLED'}

        self.report({'INFO'}, f"Origin dropped to bottom centre on {moved} object(s).")
        return {'FINISHED'}

    def measure(self, obj, depsgraph):
        if not self.use_evaluated:
            return mesh_bounds(obj.data, obj.matrix_world, self.skip_loose)

        evaluated = obj.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh()
        try:
            # to_mesh() is in local space, so the object matrix still applies.
            return mesh_bounds(mesh, obj.matrix_world, self.skip_loose)
        finally:
            evaluated.to_mesh_clear()

    @staticmethod
    def move_origin(context, obj, world_target):
        """Shift the mesh under the object so the pivot lands on world_target.

        Children are pinned to their world matrices across the change, otherwise
        moving the parent transform would drag every child with it.
        """
        children = [(child, child.matrix_world.copy()) for child in obj.children]

        local_target = obj.matrix_world.inverted_safe() @ world_target
        obj.data.transform(Matrix.Translation(-local_target))

        matrix = obj.matrix_world.copy()
        matrix.translation = world_target
        obj.matrix_world = matrix

        context.view_layer.update()
        for child, child_matrix in children:
            child.matrix_world = child_matrix


# --- ARRAYS -------------------------------------------------------------------

class OBJECT_OT_radial_array(bpy.types.Operator):
    """Array the active object in a ring around the 3D cursor"""
    bl_idname = "object.radial_array"
    bl_label = "Radial Array"
    bl_options = {'REGISTER', 'UNDO'}

    count: IntProperty(
        name="Count", description="Copies around the full circle",
        default=6, min=2, max=256,
    )
    radius_offset: FloatProperty(
        name="Radius Offset",
        description="Slide the object toward or away from the pivot before "
                    "arraying, to dial the ring's radius without moving it by hand",
        default=0.0, precision=4, step=1.0, subtype='DISTANCE',
    )
    axis_mode: EnumProperty(
        name="Axis",
        items=[
            ('CURSOR', "Cursor Z", "Turn about the 3D cursor's Z, which Align "
                                   "Cursor to Face points along the surface normal"),
            ('VIEW', "View", "Turn about the world axis closest to the view direction"),
            ('X', "X", "Turn about world X"),
            ('Y', "Y", "Turn about world Y"),
            ('Z', "Z", "Turn about world Z"),
        ],
        default='CURSOR',
    )

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and context.active_object is not None

    @staticmethod
    def slide_radially(target, pivot, axis, distance):
        """Move the object in or out along its radius from the pivot."""
        direction = target.matrix_world.translation - pivot
        unit = axis.normalized()
        direction -= unit * direction.dot(unit)
        if direction.length < 1e-9:
            return

        matrix = target.matrix_world.copy()
        matrix.translation = matrix.translation + direction.normalized() * distance
        target.matrix_world = matrix

    def execute(self, context):
        target = context.active_object
        axis = resolve_array_axis(context, self.axis_mode)
        if axis.length < 1e-9:
            self.report({'ERROR'}, "Could not resolve a rotation axis.")
            return {'CANCELLED'}

        # Applied before anything is measured. A fresh run leaves this at zero,
        # and a redo re-applies it from the pre-operator position, so dragging
        # the slider dials the radius instead of accumulating.
        if self.radius_offset:
            self.slide_radially(target, context.scene.cursor.location, axis,
                                self.radius_offset)

        modifier = (target.modifiers.get(ARRAY_RADIAL_MOD)
                    or target.modifiers.new(name=ARRAY_RADIAL_MOD, type='ARRAY'))
        modifier.count = self.count
        modifier.use_relative_offset = False
        modifier.use_constant_offset = False
        modifier.use_object_offset = True

        empty = modifier.offset_object
        if empty is None or empty.type != 'EMPTY':
            empty = bpy.data.objects.new(f"ArrayPivot_{target.name}", None)
            empty.empty_display_type = 'PLAIN_AXES'
            empty.empty_display_size = 0.1
            context.scene.collection.objects.link(empty)
            modifier.offset_object = empty

        empty["array_pivot"] = tuple(context.scene.cursor.location)
        empty["array_axis"] = tuple(axis.normalized())
        empty.parent = None

        with suspended_array_sync():
            sync_radial_array(target, modifier, empty)
        stash_in_collection(context, empty,
                            get_helper_collection(context, ARRAY_COLLECTION, hide_on_create=True))
        sort_post_boolean_stack(target)

        self.report({'INFO'}, f"Radial array of {self.count} about the 3D cursor.")
        return {'FINISHED'}


class OBJECT_OT_linear_array(bpy.types.Operator):
    """Array the active object in a straight run"""
    bl_idname = "object.linear_array"
    bl_label = "Linear Array"
    bl_options = {'REGISTER', 'UNDO'}

    count: IntProperty(name="Count", default=4, min=1, max=256)
    axis_mode: EnumProperty(
        name="Axis",
        items=[('X', "X", "Along local X"), ('Y', "Y", "Along local Y"),
               ('Z', "Z", "Along local Z")],
        default='X',
    )
    spacing_mode: EnumProperty(
        name="Spacing",
        items=[
            ('RELATIVE', "Relative", "Multiples of the object's own bounding box, "
                                     "so the run rescales with the object"),
            ('CONSTANT', "Constant", "A fixed distance between copies"),
        ],
        default='RELATIVE',
    )
    factor: FloatProperty(name="Factor", default=1.25, min=-10.0, max=10.0)
    distance: FloatProperty(
        name="Distance", default=0.1, min=-10.0, max=10.0,
        precision=4, step=0.1, subtype='DISTANCE',
    )

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and context.active_object is not None

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.prop(self, "count")
        layout.prop(self, "axis_mode")
        layout.prop(self, "spacing_mode")
        layout.prop(self, "factor" if self.spacing_mode == 'RELATIVE' else "distance")

    def execute(self, context):
        target = context.active_object
        index = "XYZ".index(self.axis_mode)

        modifier = (target.modifiers.get(ARRAY_LINEAR_MOD)
                    or target.modifiers.new(name=ARRAY_LINEAR_MOD, type='ARRAY'))
        modifier.count = self.count
        modifier.use_object_offset = False

        relative = self.spacing_mode == 'RELATIVE'
        modifier.use_relative_offset = relative
        modifier.use_constant_offset = not relative
        for slot in range(3):
            modifier.relative_offset_displace[slot] = 0.0
            modifier.constant_offset_displace[slot] = 0.0
        if relative:
            modifier.relative_offset_displace[index] = self.factor
        else:
            modifier.constant_offset_displace[index] = self.distance

        sort_post_boolean_stack(target)

        self.report({'INFO'}, f"Linear array of {self.count} along {self.axis_mode}.")
        return {'FINISHED'}


class OBJECT_OT_sync_radial_arrays(bpy.types.Operator):
    """Re-space every radial array from its current modifier count"""
    bl_idname = "object.sync_radial_arrays"
    bl_label = "Sync Radial Arrays"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(True for _ in iter_radial_arrays(context.scene))

    def execute(self, context):
        # The handler keeps these in step already; this is a manual repair for
        # anything that has drifted, such as a hand-edited empty.
        synced = sum(1 for entry in iter_radial_arrays(context.scene)
                     if sync_radial_array(*entry))
        if not synced:
            self.report({'WARNING'}, "No radial arrays to sync.")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Re-spaced {synced} radial array(s).")
        return {'FINISHED'}


# --- CUTTER VISIBILITY --------------------------------------------------------

class OBJECT_OT_toggle_cutters(bpy.types.Operator):
    """Show or hide every cutter without touching anything else in the scene"""
    bl_idname = "object.toggle_cutters"
    bl_label = "Toggle Cutters"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        # No selection requirement: this has to work whatever is under the cursor.
        return bpy.data.collections.get(CUTTER_COLLECTION) is not None

    def execute(self, context):
        layer_coll = get_cutter_layer_collection(context)
        if layer_coll is None:
            self.report({'WARNING'}, f"{CUTTER_COLLECTION} is not linked to this view layer.")
            return {'CANCELLED'}

        collection = layer_coll.collection
        hiding = cutters_visible(layer_coll)

        # Only ever the eye. Never exclude: that pulls the collection out of the
        # view layer, which is a far heavier operation than hiding a display,
        # and clearing it is the only safe direction to move it in.
        if layer_coll.exclude:
            layer_coll.exclude = False

        layer_coll.hide_viewport = hiding
        for obj in collection.objects:
            obj.hide_set(hiding)

        # Viewport state is the only thing this operator owns.
        lock_cutter_render_visibility(collection)

        count = len(collection.objects)
        self.report({'INFO'}, f"{'Hid' if hiding else 'Revealed'} {count} cutter(s).")
        return {'FINISHED'}


class OBJECT_OT_cutter_display(bpy.types.Operator):
    """Switch cutters between wireframe and solid display"""
    bl_idname = "object.cutter_display"
    bl_label = "Toggle Cutter Display"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        collection = bpy.data.collections.get(CUTTER_COLLECTION)
        return collection is not None and len(collection.objects) > 0

    def execute(self, context):
        collection = bpy.data.collections[CUTTER_COLLECTION]
        to_solid = any(obj.display_type == 'WIRE' for obj in collection.objects)
        for obj in collection.objects:
            obj.display_type = 'SOLID' if to_solid else 'WIRE'

        self.report({'INFO'}, f"Cutters set to {'solid' if to_solid else 'wireframe'}.")
        return {'FINISHED'}


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


class MESH_OT_panel_line(SmartBooleanOptions, bpy.types.Operator):
    """Cut a recessed panel line along the selected edges"""
    bl_idname = "mesh.panel_line"
    bl_label = "Generate Panel Line"
    bl_options = {'REGISTER', 'UNDO'}

    width: FloatProperty(
        name="Groove Width", description="Width of the gap across the hull",
        default=0.004, min=0.00001, max=1.0, precision=4, step=0.01, subtype='DISTANCE',
    )
    depth: FloatProperty(
        name="Cut Depth", description="How far the groove sinks into the hull",
        default=0.004, min=0.00001, max=1.0, precision=4, step=0.01, subtype='DISTANCE',
    )
    overshoot: FloatProperty(
        name="Surface Overshoot",
        description="How far the cutter stands proud of the hull. Keep it above "
                    "zero: a cutter flush with the surface hands the solver a "
                    "coplanar pair, which is how a groove comes out ragged",
        default=0.001, min=0.0, max=1.0, precision=4, step=0.01, subtype='DISTANCE',
    )

    @classmethod
    def poll(cls, context):
        active = context.active_object
        return (context.mode in {'EDIT_MESH', 'OBJECT'}
                and active is not None and active.type == 'MESH')

    def draw_extra(self, layout):
        layout.separator()
        layout.prop(self, "width")
        layout.prop(self, "depth")
        layout.prop(self, "overshoot")

    def execute(self, context):
        source = context.active_object
        pairs, positions, normals = self.read_selection(context, source)

        if not pairs:
            self.report({'ERROR'}, "Select the edges the panel line should follow.")
            return {'CANCELLED'}

        vertices, faces, runs = [], [], 0
        for chain, closed in ordered_paths(pairs):
            frames = list(sweep_frames([positions[index] for index in chain],
                                       [normals[index] for index in chain], closed))
            part_vertices, part_faces = panel_tube_geometry(
                frames, closed, self.width, self.depth, self.overshoot)
            if not part_faces:
                continue

            offset = len(vertices)
            vertices.extend(part_vertices)
            faces.extend(tuple(index + offset for index in face) for face in part_faces)
            runs += 1

        if not faces:
            self.report({'ERROR'}, "Selected edges do not form a usable path.")
            return {'CANCELLED'}

        cutter = self.build_cutter(context, source, vertices, faces)
        add_boolean(source, cutter, 'DIFFERENCE', f"Bool_Panel_{cutter.name}", self)
        stash_operand(context, cutter)

        note = " Tab out to see it." if context.mode == 'EDIT_MESH' else ""
        self.report({'INFO'}, f"Panel line cut along {runs} run(s).{note}")
        return {'FINISHED'}

    @staticmethod
    def read_selection(context, source):
        """Selected edges as index pairs, plus each vertex's position and normal.

        Works from either mode: edge selection persists in mesh data after
        leaving Edit Mode, and running from Object Mode keeps object creation
        out of the edit-mode undo stack.
        """
        pairs, positions, normals = [], {}, {}

        if context.mode == 'EDIT_MESH':
            bm = bmesh.from_edit_mesh(source.data)
            bm.normal_update()
            for edge in bm.edges:
                if not edge.select:
                    continue
                first, second = edge.verts
                pairs.append((first.index, second.index))
                for vertex in (first, second):
                    positions[vertex.index] = vertex.co.copy()
                    normals[vertex.index] = vertex.normal.copy()
        else:
            mesh = source.data
            for edge in mesh.edges:
                if not edge.select:
                    continue
                first, second = edge.vertices
                pairs.append((first, second))
                for index in (first, second):
                    positions[index] = mesh.vertices[index].co.copy()
                    normals[index] = mesh.vertices[index].normal.copy()

        return pairs, positions, normals

    @staticmethod
    def build_cutter(context, source, vertices, faces):
        """A closed mesh tube, built in the source's local space.

        The cutter is a mesh rather than a curve because Blender's Boolean
        modifier only accepts mesh operands, so a curve cutter cannot drive the
        cut however good it looks in the viewport.
        """
        mesh = bpy.data.meshes.new(f"PanelLine_{source.name}")
        mesh.from_pydata([tuple(vertex) for vertex in vertices], [], faces)
        mesh.update()

        bm = bmesh.new()
        bm.from_mesh(mesh)
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
        bm.to_mesh(mesh)
        bm.free()

        cutter = bpy.data.objects.new(mesh.name, mesh)
        context.scene.collection.objects.link(cutter)
        cutter.matrix_world = source.matrix_world
        return cutter


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


class OBJECT_OT_apply_bevel_resolution(bpy.types.Operator):
    """Push the current bevel resolution onto every targeted Smart Bevel"""
    bl_idname = "object.apply_bevel_resolution"
    bl_label = "Apply Resolution"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def execute(self, context):
        # The slider updates live as it is dragged, so this is for after the
        # selection changes or new objects arrive without the slider moving.
        count = apply_bevel_resolution(context.scene, context.view_layer)
        if not count:
            self.report({'WARNING'}, "No Smart Bevel modifiers in range.")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Set {count} Smart Bevel(s) to "
                              f"{context.scene.smart_bevel_segments} segment(s).")
        return {'FINISHED'}


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
        with bevels_at_render_visibility((source,)):
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
        low, high = mesh_bounds(mesh, Matrix.Identity(4))
        if low is None:
            return
        mesh.transform(Matrix.Translation(-bottom_centre(low, high)))

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

        self.draw_cutter_toggle(context, layout)
        layout.separator()

        layout.label(text="Booleans:", icon='MOD_BOOLEAN')
        col = layout.column(align=True)
        col.scale_y = 1.5
        row = col.row(align=True)
        row.operator(OBJECT_OT_smart_difference.bl_idname, text="Difference")
        row.operator(OBJECT_OT_smart_slice.bl_idname, text="Slice")
        col.operator(OBJECT_OT_smart_union.bl_idname, text="Union")
        col.operator(MESH_OT_panel_line.bl_idname, text="Panel Line")

        layout.separator()

        layout.label(text="Align & Origin:", icon='ORIENTATION_NORMAL')
        col = layout.column(align=True)
        col.scale_y = 1.5
        col.operator(MESH_OT_cursor_to_face.bl_idname, text="Align Cursor to Face")
        col.operator(OBJECT_OT_snap_to_cursor.bl_idname, text="Snap to Cursor")
        col.operator(OBJECT_OT_origin_to_bottom.bl_idname, text="Origin to Bottom")

        layout.separator()

        layout.label(text="Arrays:", icon='MOD_ARRAY')
        col = layout.column(align=True)
        col.scale_y = 1.5
        row = col.row(align=True)
        row.operator(OBJECT_OT_radial_array.bl_idname, text="Radial")
        row.operator(OBJECT_OT_linear_array.bl_idname, text="Linear")
        col.operator(OBJECT_OT_sync_radial_arrays.bl_idname, text="Sync Radial",
                     icon='FILE_REFRESH')

        layout.separator()

        layout.label(text="Shading & Edges:", icon='MOD_BEVEL')
        row = layout.row()
        row.scale_y = 1.5
        row.operator(OBJECT_OT_smart_bevel.bl_idname, text="Smart Bevel")

        self.draw_bevel_resolution(context, layout)

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

    def draw_bevel_resolution(self, context, layout):
        scene = context.scene
        box = layout.box()

        row = box.row(align=True)
        row.prop(scene, "smart_bevel_segments")
        row.prop(
            scene, "smart_bevel_mute", text="",
            icon='HIDE_ON' if scene.smart_bevel_mute else 'HIDE_OFF', toggle=True,
        )

        box.prop(scene, "smart_bevel_selected_only")
        box.prop(scene, "smart_bevel_override_width")
        if scene.smart_bevel_override_width:
            box.prop(scene, "smart_bevel_width")

        box.operator(OBJECT_OT_apply_bevel_resolution.bl_idname, icon='FILE_REFRESH')

        # Two readouts, because they answer different questions. The active
        # object line follows the click and says what is under the cursor; the
        # scope line says what the slider above would change.
        self.draw_active_bevel(context, box)
        self.draw_scope_bevels(context, box)

    @staticmethod
    def draw_active_bevel(context, layout):
        active = context.active_object
        row = layout.row()

        if active is None or active.type != 'MESH':
            row.enabled = False
            row.label(text="No active mesh")
            return

        bevels = [mod for mod in active.modifiers if is_smart_bevel(mod)]
        if not bevels:
            row.enabled = False
            row.label(text=f"{active.name}: no Smart Bevel", icon='DOT')
            return

        if len(bevels) > 1:
            row.label(text=f"{active.name}: {len(bevels)} Smart Bevels", icon='INFO')
            return

        bevel = bevels[0]
        muted = "" if bevel.show_viewport else "  (muted)"
        row.label(
            text=f"{active.name}: {bevel.segments} seg, {bevel.width:.4g} wide{muted}",
            icon='MOD_BEVEL',
        )

    @staticmethod
    def draw_scope_bevels(context, layout):
        scene = context.scene
        scope = "Selected" if scene.smart_bevel_selected_only else "Scene"

        # Counts modifiers, not bevelled edges. One object carries one
        # Smart_Bevel whose angle limit handles every qualifying edge on the
        # mesh, so "1" here says nothing about how much geometry it touches.
        low = high = None
        count = 0
        for modifier in iter_smart_bevels(scene, context.view_layer,
                                          scene.smart_bevel_selected_only):
            count += 1
            segments = modifier.segments
            low = segments if low is None else min(low, segments)
            high = segments if high is None else max(high, segments)

        info = layout.row()
        if not count:
            info.enabled = False
            info.label(text=f"{scope}: no Smart Bevel modifiers")
        else:
            span = f"{low}" if low == high else f"{low}-{high}"
            info.label(text=f"{scope}: {count} modifier(s) at {span} segments",
                       icon='CHECKMARK' if low == high else 'INFO')

    @staticmethod
    def draw_cutter_toggle(context, layout):
        layer_coll = get_cutter_layer_collection(context)
        collection = bpy.data.collections.get(CUTTER_COLLECTION)

        if collection is None:
            row = layout.row()
            row.enabled = False
            row.label(text="No cutters yet", icon='GHOST_DISABLED')
            return

        visible = cutters_visible(layer_coll)

        row = layout.row()
        row.scale_y = 2.0
        row.operator(
            OBJECT_OT_toggle_cutters.bl_idname,
            text=f"Hide Cutters ({len(collection.objects)})" if visible else "Show Cutters",
            icon='HIDE_OFF' if visible else 'HIDE_ON',
        )

        sub = layout.row()
        sub.enabled = visible
        sub.operator(OBJECT_OT_cutter_display.bl_idname, text="Wire / Solid", icon='SHADING_WIRE')


classes = (
    MESH_OT_cursor_to_face,
    OBJECT_OT_snap_to_cursor,
    OBJECT_OT_origin_to_bottom,
    OBJECT_OT_radial_array,
    OBJECT_OT_linear_array,
    OBJECT_OT_sync_radial_arrays,
    OBJECT_OT_toggle_cutters,
    OBJECT_OT_cutter_display,
    OBJECT_OT_smart_difference,
    OBJECT_OT_smart_slice,
    OBJECT_OT_smart_union,
    MESH_OT_panel_line,
    OBJECT_OT_smart_bevel,
    OBJECT_OT_apply_bevel_resolution,
    OBJECT_OT_smart_uv,
    OBJECT_OT_smart_export_ue5,
    VIEW3D_PT_smart_tools,
)


SCENE_PROPS = {
    "smart_bevel_segments": IntProperty(
        name="Segments",
        description="Segment count pushed onto every Smart Bevel in range. Drag "
                    "to change the whole scene's edge resolution live",
        default=3, min=1, max=12,
        update=_update_bevel_resolution,
    ),
    "smart_bevel_selected_only": BoolProperty(
        name="Selected Objects Only",
        description="Limit the slider and mute toggle to the current selection "
                    "instead of the whole scene",
        default=False,
        # Deliberately no update callback: flipping the scope should not by
        # itself rewrite every bevel in the scene. It takes effect on the next
        # slider drag or Apply Resolution click.
    ),
    "smart_bevel_mute": BoolProperty(
        name="Mute Bevels in Viewport",
        description="Disable Smart Bevels in the viewport for framerate. Render "
                    "and export visibility are untouched",
        default=False,
        update=_update_bevel_mute,
    ),
    "smart_bevel_override_width": BoolProperty(
        name="Override Width",
        description="Also force one width on every Smart Bevel. Off by default: "
                    "the right width depends on each object's scale, so a global "
                    "value flattens that judgement across the whole scene",
        default=False,
        update=_update_bevel_resolution,
    ),
    "smart_bevel_width": FloatProperty(
        name="Width",
        default=0.01, min=0.0001, max=10.0, precision=4, step=0.01,
        subtype='DISTANCE',
        update=_update_bevel_resolution,
    ),
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


# Default hotkey for the cutter toggle. Change the last three arguments in
# register_keymaps() to rebind, or clear it in Preferences > Keymap > Add-ons.
addon_keymaps = []


def register_keymaps():
    keyconfig = bpy.context.window_manager.keyconfigs.addon
    if keyconfig is None:
        return  # Background mode has no addon keyconfig to bind into.

    keymap = keyconfig.keymaps.new(name='Object Mode', space_type='EMPTY')
    item = keymap.keymap_items.new(
        OBJECT_OT_toggle_cutters.bl_idname, 'H', 'PRESS', ctrl=True, shift=True,
    )
    addon_keymaps.append((keymap, item))


def unregister_keymaps():
    # Remove exactly the items this addon added; leaving them behind is the
    # classic way an addon breaks the user's keymap after a reload.
    for keymap, item in addon_keymaps:
        keymap.keymap_items.remove(item)
    addon_keymaps.clear()


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    for name, prop in SCENE_PROPS.items():
        setattr(bpy.types.Scene, name, prop)

    register_keymaps()

    if sync_radial_arrays_on_update not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(sync_radial_arrays_on_update)


def unregister():
    if sync_radial_arrays_on_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(sync_radial_arrays_on_update)

    unregister_keymaps()

    # Classes come off first: the panel's draw() reads these scene properties.
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

    for name in SCENE_PROPS:
        if hasattr(bpy.types.Scene, name):
            delattr(bpy.types.Scene, name)


if __name__ == "__main__":
    register()
