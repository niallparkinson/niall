"""Independent bevel layer checks, run against a real Blender.

    pip install bpy
    cp "First Python Script.py" tests/hstools.py
    cd tests && python3 test_bevel_layers.py
"""
import bpy, bmesh, sys
sys.path.insert(0,"."); import hstools as H
R=[]
def rep(l,ok,extra=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {l}{('  '+extra) if extra else ''}"); R.append(ok)
def fresh():
    try: H.unregister()
    except Exception: pass
    bpy.ops.wm.read_factory_settings(use_empty=True); H.register()
def ev(o):
    dg=bpy.context.evaluated_depsgraph_get()
    return bpy.data.meshes.new_from_object(o.evaluated_get(dg), preserve_all_data_layers=True, depsgraph=dg)
def faces(o):
    me=ev(o); n=len(me.polygons); bpy.data.meshes.remove(me); return n
def zlevels(o, positive=True):
    me=ev(o)
    s=sorted({round(abs(v.co.z),3) for v in me.vertices if (v.co.z>0)==positive})
    bpy.data.meshes.remove(me); return s
def edit_select(o, pick):
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_mode(type='EDGE')
    bm=bmesh.from_edit_mesh(o.data); bm.edges.ensure_lookup_table()
    for e in bm.edges: e.select=False
    for e in bm.edges:
        if pick(e): e.select=True
    bmesh.update_edit_mesh(o.data)

print("1. two layers bevel different edges at different widths")
fresh()
bpy.ops.mesh.primitive_cube_add(size=2.0); cube=bpy.context.active_object
edit_select(cube, lambda e: all(v.co.z>0 for v in e.verts))
bpy.ops.mesh.add_bevel_layer(width=0.30, segments=4)
edit_select(cube, lambda e: all(v.co.z<0 for v in e.verts))
bpy.ops.mesh.add_bevel_layer(width=0.05, segments=2)
bpy.ops.object.mode_set(mode='OBJECT')
layers=H.bevel_layers(cube)
rep("two layers exist", len(layers)==2, str([m.name for m in layers]))
rep("each has its own edge attribute",
    layers[0].edge_weight != layers[1].edge_weight,
    f"{layers[0].edge_weight} vs {layers[1].edge_weight}")
top, bot = zlevels(cube, True), zlevels(cube, False)
rep("wide layer cut deeper than the narrow one", len(top)>len(bot) and top[0]<bot[0],
    f"top {top} bottom {bot}")
rep("both attributes present on the mesh",
    sum(1 for a in cube.data.attributes if a.name.startswith(H.BEVEL_LAYER_ATTR))==2)
H.unregister()

print("2. layers sit between the main bevel and the weighted normal")
fresh()
bpy.ops.mesh.primitive_cube_add(size=2.0); cube=bpy.context.active_object
bpy.ops.object.smart_bevel(limit_mode='WEIGHT')
edit_select(cube, lambda e: all(v.co.z>0 for v in e.verts))
bpy.ops.mesh.add_bevel_layer(width=0.1)
bpy.ops.object.mode_set(mode='OBJECT')
names=[m.name for m in cube.modifiers]
rep("ordered main bevel -> layer -> weighted normal",
    names.index(H.BEVEL_MOD) < names.index(H.bevel_layers(cube)[0].name)
    < names.index(H.WEIGHTED_NORMAL_MOD), str(names))
bpy.ops.object.smart_bevel(limit_mode='WEIGHT')
names=[m.name for m in cube.modifiers]
rep("re-running Smart Bevel keeps the layer in place",
    names.index(H.BEVEL_MOD) < names.index(H.bevel_layers(cube)[0].name)
    < names.index(H.WEIGHTED_NORMAL_MOD), str(names))
H.unregister()

print("3. removing a layer takes its attribute with it")
fresh()
bpy.ops.mesh.primitive_cube_add(size=2.0); cube=bpy.context.active_object
edit_select(cube, lambda e: all(v.co.z>0 for v in e.verts))
bpy.ops.mesh.add_bevel_layer(width=0.2)
bpy.ops.object.mode_set(mode='OBJECT')
name=H.bevel_layers(cube)[0].name
before=faces(cube)
bpy.ops.object.remove_bevel_layer(modifier_name=name)
rep("modifier gone", not H.bevel_layers(cube))
rep("attribute gone",
    not [a for a in cube.data.attributes if a.name.startswith(H.BEVEL_LAYER_ATTR)])
rep("mesh back to the raw cube", faces(cube)==6, f"{before} -> {faces(cube)}")
H.unregister()

print("4. assigning and unassigning edges on a live layer")
fresh()
bpy.ops.mesh.primitive_cube_add(size=2.0); cube=bpy.context.active_object
edit_select(cube, lambda e: all(v.co.z>0 for v in e.verts))
bpy.ops.mesh.add_bevel_layer(width=0.1, segments=2)
bpy.ops.object.mode_set(mode='OBJECT')
name=H.bevel_layers(cube)[0].name
four=faces(cube)
edit_select(cube, lambda e: all(v.co.z<0 for v in e.verts))
bpy.ops.mesh.assign_bevel_layer(modifier_name=name, remove=False)
bpy.ops.object.mode_set(mode='OBJECT')
eight=faces(cube)
rep("assigning more edges grows the bevel", eight>four, f"{four} -> {eight}")
edit_select(cube, lambda e: all(v.co.z<0 for v in e.verts))
bpy.ops.mesh.assign_bevel_layer(modifier_name=name, remove=True)
bpy.ops.object.mode_set(mode='OBJECT')
rep("removing them again restores it", faces(cube)==four, f"{eight} -> {faces(cube)}")
H.unregister()

print("5. width means the width of the chamfer strip")
fresh()
bpy.ops.mesh.primitive_cube_add(size=2.0); cube=bpy.context.active_object
edit_select(cube, lambda e: all(v.co.z>0 for v in e.verts))
bpy.ops.mesh.add_bevel_layer(width=0.2, segments=1)
bpy.ops.object.mode_set(mode='OBJECT')
rep("offset_type is WIDTH", H.bevel_layers(cube)[0].offset_type=='WIDTH')
# a 0.2-wide strip on a 90 degree edge insets 0.2/sqrt(2) = 0.1414 per face
inset=1.0-zlevels(cube,True)[0]
rep("a 0.2 strip insets 0.1414 per face", abs(inset-0.1414)<0.002, f"inset {inset:.4f}")
H.unregister()

print("6. face strength and weighted normal influence are set")
fresh()
bpy.ops.mesh.primitive_cube_add(size=2.0); cube=bpy.context.active_object
bpy.ops.object.smart_bevel(limit_mode='WEIGHT', vertex_bevel=True)
rep("main bevel marks its faces weak",
    cube.modifiers[H.BEVEL_MOD].face_strength_mode=='FSTR_AFFECTED')
rep("vertex bevel too",
    cube.modifiers[H.VERTEX_BEVEL_MOD].face_strength_mode=='FSTR_AFFECTED')
rep("weighted normal reads face strength",
    cube.modifiers[H.WEIGHTED_NORMAL_MOD].use_face_influence)
H.unregister()

print("7. the global resolution slider reaches layers too")
fresh()
bpy.ops.mesh.primitive_cube_add(size=2.0); cube=bpy.context.active_object
bpy.ops.object.smart_bevel(limit_mode='WEIGHT')
edit_select(cube, lambda e: all(v.co.z>0 for v in e.verts))
bpy.ops.mesh.add_bevel_layer(width=0.1, segments=2)
bpy.ops.object.mode_set(mode='OBJECT')
bpy.context.scene.smart_bevel_selected_only=False
bpy.context.scene.smart_bevel_segments=5
bpy.ops.object.apply_bevel_resolution()
got={m.name:m.segments for m in cube.modifiers if m.type=='BEVEL'}
rep("every bevel followed the slider", set(got.values())=={5}, str(got))
H.unregister()

print()
print(f"{sum(R)}/{len(R)} checks pass")
sys.exit(0 if all(R) else 1)
