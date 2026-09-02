"""Edge and vertex bevel weight checks, run against a real Blender.

    pip install bpy
    cp "First Python Script.py" tests/hstools.py
    cd tests && python3 test_bevel_weights.py
"""
import bpy, bmesh, sys
sys.path.insert(0,"."); import hstools as H
R=[]
def rep(l,ok,extra=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {l}{('  '+extra) if extra else ''}"); R.append(ok)
def fresh():
    try:
        H.unregister()
    except Exception:
        pass
    bpy.ops.wm.read_factory_settings(use_empty=True); H.register()
def faces(o):
    dg=bpy.context.evaluated_depsgraph_get()
    me=bpy.data.meshes.new_from_object(o.evaluated_get(dg), preserve_all_data_layers=True, depsgraph=dg)
    n=len(me.polygons); bpy.data.meshes.remove(me); return n
def weights(o, name):
    a=o.data.attributes.get(name)
    return [round(d.value,3) for d in a.data] if a else None

print("1. weight mode seeds from the angle test, so the look is unchanged")
fresh()
bpy.ops.mesh.primitive_cube_add(size=2.0); cube=bpy.context.active_object
bpy.ops.object.smart_bevel(limit_mode='ANGLE')
angle_faces = faces(cube)
fresh()
bpy.ops.mesh.primitive_cube_add(size=2.0); cube=bpy.context.active_object
bpy.ops.object.smart_bevel(limit_mode='WEIGHT')
rep("weight mode matches angle mode out of the box", faces(cube)==angle_faces,
    f"angle {angle_faces} vs weight {faces(cube)}")
w=weights(cube, H.EDGE_WEIGHT_ATTR)
rep("every sharp edge seeded to 1.0", w is not None and set(w)=={1.0}, str(set(w or [])))
H.unregister()

print("2. hand-dialled weights survive a re-run")
fresh()
bpy.ops.mesh.primitive_cube_add(size=2.0); cube=bpy.context.active_object
bpy.ops.object.smart_bevel(limit_mode='WEIGHT')
a=cube.data.attributes[H.EDGE_WEIGHT_ATTR]
a.data[0].value=0.0; a.data[1].value=0.25
before=weights(cube, H.EDGE_WEIGHT_ATTR)
bpy.ops.object.smart_bevel(limit_mode='WEIGHT')
rep("re-run left the weights alone", weights(cube,H.EDGE_WEIGHT_ATTR)==before,
    f"{before[:3]} -> {weights(cube,H.EDGE_WEIGHT_ATTR)[:3]}")
bpy.ops.object.smart_bevel(limit_mode='WEIGHT', reseed_weights=True)
rep("re-seed on request restores them", set(weights(cube,H.EDGE_WEIGHT_ATTR))=={1.0})
H.unregister()

print("3. weight scales width, giving variable fillets")
fresh()
bpy.ops.mesh.primitive_cube_add(size=2.0); cube=bpy.context.active_object
bpy.ops.object.smart_bevel(limit_mode='WEIGHT', bevel_width=0.2, bevel_segments=1)
a=cube.data.attributes[H.EDGE_WEIGHT_ATTR]
for i in range(len(a.data)): a.data[i].value = 1.0 if i%2 else 0.4
dg=bpy.context.evaluated_depsgraph_get()
me=bpy.data.meshes.new_from_object(cube.evaluated_get(dg), preserve_all_data_layers=True, depsgraph=dg)
bm=bmesh.new(); bm.from_mesh(me)
strips=sorted(round(f.calc_area(),4) for f in bm.faces if len(f.verts)==4)
distinct=sorted(set(x for x in strips if x < 1.0))
bm.free(); bpy.data.meshes.remove(me)
rep("two different strip widths present", len(distinct)>=2, f"{distinct[:4]}")
H.unregister()

print("4. zero weight excludes an edge entirely")
fresh()
bpy.ops.mesh.primitive_cube_add(size=2.0); cube=bpy.context.active_object
bpy.ops.object.smart_bevel(limit_mode='WEIGHT', bevel_width=0.15)
full=faces(cube)
a=cube.data.attributes[H.EDGE_WEIGHT_ATTR]
for i in range(len(a.data)): a.data[i].value=0.0
rep("all weights zero -> back to the raw cube", faces(cube)==6, f"{full} -> {faces(cube)}")
H.unregister()

print("5. vertex bevel is a second modifier, weight driven")
fresh()
bpy.ops.mesh.primitive_cube_add(size=2.0); cube=bpy.context.active_object
bpy.ops.object.smart_bevel(limit_mode='WEIGHT', vertex_bevel=True, vertex_width=0.15)
names=[m.name for m in cube.modifiers]
rep("vertex bevel added", H.VERTEX_BEVEL_MOD in names, str(names))
rep("rounds corners before the edge bevel splits them",
    names.index(H.VERTEX_BEVEL_MOD) < names.index(H.BEVEL_MOD) < names.index(H.WEIGHTED_NORMAL_MOD),
    str(names))
quiet=faces(cube)
bm=bmesh.new(); bm.from_mesh(cube.data)
lay=H.bevel_weight_layer(bm,'VERTEX'); bm.verts.ensure_lookup_table()
for i in range(3): bm.verts[i][lay]=1.0
bm.to_mesh(cube.data); bm.free()
rep("does nothing until corners are weighted", faces(cube)>quiet,
    f"{quiet} -> {faces(cube)} after weighting 3 corners")
# Ordering regression: with the vertex bevel after the edge bevel, one weighted
# corner collapsed 342 of 548 faces to zero area. The corner must be rounded
# while it is still a single vertex.
dg=bpy.context.evaluated_depsgraph_get()
me=bpy.data.meshes.new_from_object(cube.evaluated_get(dg), preserve_all_data_layers=True, depsgraph=dg)
bm=bmesh.new(); bm.from_mesh(me)
degenerate=sum(1 for f in bm.faces if f.calc_area() < 1e-9)
bm.free(); bpy.data.meshes.remove(me)
rep("no degenerate faces at the rounded corners", degenerate==0, f"{degenerate} zero-area faces")
bpy.ops.object.smart_bevel(limit_mode='WEIGHT', vertex_bevel=False)
rep("toggling it off removes it", H.VERTEX_BEVEL_MOD not in [m.name for m in cube.modifiers])
H.unregister()

print("6. Set Bevel Weight works on an edit-mode selection")
fresh()
bpy.ops.mesh.primitive_cube_add(size=2.0); cube=bpy.context.active_object
bpy.ops.object.smart_bevel(limit_mode='WEIGHT')
bpy.ops.object.mode_set(mode='EDIT')
bm=bmesh.from_edit_mesh(cube.data); bm.edges.ensure_lookup_table()
for e in bm.edges: e.select=False
for i in range(4): bm.edges[i].select=True
bmesh.update_edit_mesh(cube.data)
bpy.ops.mesh.set_bevel_weight(weight=0.5, domain='EDGE')
bpy.ops.object.mode_set(mode='OBJECT')
w=weights(cube,H.EDGE_WEIGHT_ATTR)
rep("selection set to 0.5", w.count(0.5)==4, f"{w.count(0.5)} edges at 0.5")
rep("the rest untouched", w.count(1.0)==len(w)-4)
H.unregister()

print("7. the global resolution slider still reaches both bevels")
fresh()
bpy.ops.mesh.primitive_cube_add(size=2.0); cube=bpy.context.active_object
bpy.ops.object.smart_bevel(limit_mode='WEIGHT', vertex_bevel=True)
bpy.context.scene.smart_bevel_segments = 6
found={m.name: m.segments for m in cube.modifiers if H.is_smart_bevel(m)}
rep("both bevels followed the slider", set(found.values())=={6}, str(found))
H.unregister()
print(f"\n{sum(R)}/{len(R)} checks pass")
