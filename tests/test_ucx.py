"""UCX collision checks, run against a real Blender.

    pip install bpy
    cp "First Python Script.py" tests/hstools.py
    cd tests && python3 test_ucx.py
"""
import bpy, sys, tempfile, os
from mathutils import Vector
sys.path.insert(0,"."); import hstools as H
R=[]
def rep(l,ok,extra=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {l}{('  '+extra) if extra else ''}"); R.append(ok)
def fresh():
    bpy.ops.wm.read_factory_settings(use_empty=True); H.register()
def desk(n_boxes=3, name="SM_Desk_low"):
    bpy.ops.mesh.primitive_cube_add(size=2.0, location=(0,0,1)); d=bpy.context.active_object
    d.name=name; d.data.name=name
    boxes=[]
    for i in range(n_boxes):
        bpy.ops.mesh.primitive_cube_add(size=0.4, location=(i*0.8-0.8, 0, 0.2))
        boxes.append(bpy.context.active_object)
    return d, boxes
def pick(target, boxes):
    for o in bpy.context.scene.objects: o.select_set(False)
    for b in boxes: b.select_set(True)
    target.select_set(True); bpy.context.view_layer.objects.active=target

print("1. naming, numbering and parenting")
fresh()
d, boxes = desk(3)
pick(d, boxes)
bpy.ops.object.generate_ucx()
names = sorted(c.name for c in d.children)
rep("named to match the exported render mesh",
    names == ["UCX_SM_Desk_low_01","UCX_SM_Desk_low_02","UCX_SM_Desk_low_03"], str(names))
rep("all parented to the render mesh", len(d.children)==3)
rep("display set to wireframe", all(c.display_type=='WIRE' for c in d.children))
rep("excluded from renders", all(c.hide_render for c in d.children))
rep("mesh data renamed too", all(c.data.name==c.name for c in d.children))
H.unregister()

print("2. the short outliner-friendly form is available")
fresh()
d, boxes = desk(2)
pick(d, boxes)
bpy.ops.object.generate_ucx(match_export_name=False)
names=sorted(c.name for c in d.children)
rep("stripped base name", names==["UCX_Desk_01","UCX_Desk_02"], str(names))
H.unregister()

print("3. adding more hulls later renumbers cleanly, no .001 names")
fresh()
d, boxes = desk(2)
pick(d, boxes)
bpy.ops.object.generate_ucx()
bpy.ops.mesh.primitive_cube_add(size=0.4, location=(2,0,0.2)); extra=bpy.context.active_object
pick(d, [extra])
bpy.ops.object.generate_ucx()
names=sorted(c.name for c in d.children)
rep("renumbered 01..03 with no duplicates",
    names==["UCX_SM_Desk_low_01","UCX_SM_Desk_low_02","UCX_SM_Desk_low_03"], str(names))
rep("no Blender .001 suffixes", not any("." in n for n in names))
H.unregister()

print("4. hulls do not jump when parented")
fresh()
d, boxes = desk(1)
before = boxes[0].matrix_world.translation.copy()
pick(d, boxes)
bpy.ops.object.generate_ucx()
bpy.context.view_layer.update()
after = d.children[0].matrix_world.translation.copy()
rep("hull stayed put", (after-before).length < 1e-6, f"{before[:]} -> {after[:]}")
H.unregister()

print("5. export carries collision into the same FBX, not separate files")
fresh()
d, boxes = desk(3)
pick(d, boxes)
bpy.ops.object.generate_ucx()
out=tempfile.mkdtemp(); bpy.context.scene.smart_export_path=out
for o in bpy.context.scene.objects: o.select_set(False)
d.select_set(True)
for c in d.children: c.select_set(True)     # select hulls too, as an artist would
bpy.context.view_layer.objects.active=d
bpy.ops.object.smart_export_ue5(export_type='LOW')
files=sorted(os.listdir(out))
rep("exactly one FBX written", files==["SM_Desk_low.fbx"], str(files))
rep("file is non-trivial in size", os.path.getsize(os.path.join(out,files[0])) > 5000,
    f"{os.path.getsize(os.path.join(out,files[0]))} bytes")
H.unregister()

print("6. collision follows the render mesh when the origin is moved")
fresh()
d, boxes = desk(1)
pick(d, boxes)
bpy.ops.object.generate_ucx()
bpy.context.view_layer.update()
op=[c for c in H.classes if c.__name__=="OBJECT_OT_smart_export_ue5"][0]
name=H.export_asset_name(d.name,'LOW')
class Harness:
    export_type = "LOW"
    recentre_on_bounds = staticmethod(op.recentre_on_bounds)
    build_export_copy = op.build_export_copy
inst = Harness()
gaps={}
for mode in ('KEEP','WORLD','BOTTOM'):
    temp, shift = inst.build_export_copy(bpy.context, d, name, mode)
    hulls = op.build_collision_copies(bpy.context, H.collision_hulls(d), name, shift)
    bpy.context.view_layer.update()
    rl,rh = H.mesh_bounds(temp.data, temp.matrix_world)
    hl,hh = H.mesh_bounds(hulls[0].data, hulls[0].matrix_world)
    gaps[mode] = ((rl+rh)/2 - (hl+hh)/2)
    for h in hulls:
        m=h.data; bpy.data.objects.remove(h, do_unlink=True); bpy.data.meshes.remove(m)
    m=temp.data; bpy.data.objects.remove(temp, do_unlink=True); bpy.data.meshes.remove(m)

spread=max((gaps[a]-gaps[b]).length for a in gaps for b in gaps)
rep("hull keeps the same offset under every origin mode", spread < 1e-5,
    f"max difference {spread:.2e}, offset {tuple(round(v,3) for v in gaps['KEEP'])}")
rep("and the offset is the authored one", abs(gaps['KEEP'].length - 1.1314) < 1e-3,
    f"{gaps['KEEP'].length:.4f}")
H.unregister()

print("6b. exported names carry no Blender duplicate suffix")
fresh()
d, boxes = desk(3)
pick(d, boxes)
bpy.ops.object.generate_ucx()
op=[c for c in H.classes if c.__name__=="OBJECT_OT_smart_export_ue5"][0]
name=H.export_asset_name(d.name,'LOW')
class H2:
    export_type="LOW"
    recentre_on_bounds=staticmethod(op.recentre_on_bounds)
    build_export_copy=op.build_export_copy
source_hulls = H.collision_hulls(d)
with H.names_released([d] + source_hulls):
    temp, shift = H2().build_export_copy(bpy.context, d, name, 'KEEP')
    copies = op.build_collision_copies(bpy.context, source_hulls, name, shift)
    written = [(o.name, o.data.name) for o in [temp]+copies]
    for o in copies:
        m=o.data; bpy.data.objects.remove(o, do_unlink=True); bpy.data.meshes.remove(m)
    m=temp.data; bpy.data.objects.remove(temp, do_unlink=True); bpy.data.meshes.remove(m)

# A dot becomes an underscore in FBX, and Unreal matches collision by exact
# name, so UCX_..._03.001 arrives as UCX_..._03_001 and binds to nothing.
dotted=[n for pair in written for n in pair if "." in n]
rep("no duplicate suffixes on anything exported", not dotted, str(dotted))
rep("render mesh named exactly as intended", written[0]==(name, name), str(written[0]))
rep("collision named to match it",
    [w[0] for w in written[1:]] == [f"UCX_{name}_{i:02d}" for i in (1,2,3)],
    str([w[0] for w in written[1:]]))
rep("originals got their names back",
    d.name=="SM_Desk_low" and sorted(c.name for c in d.children)==
    [f"UCX_{name}_{i:02d}" for i in (1,2,3)])
H.unregister()

print("7. the archway workflow, from a bare mesh to a compound hull")
def archway():
    """Two legs and a span: concave, so one hull would block the opening."""
    bpy.ops.wm.read_factory_settings(use_empty=True); H.register()
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(-1.0,0,1.0))
    a=bpy.context.active_object; a.scale=(0.3,0.5,2.0)
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(1.0,0,1.0))
    b=bpy.context.active_object; b.scale=(0.3,0.5,2.0)
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0,0,2.25))
    c=bpy.context.active_object; c.scale=(2.6,0.5,0.5)
    for o in (a,b,c): o.select_set(True)
    bpy.context.view_layer.objects.active=c
    bpy.ops.object.join()
    arch=bpy.context.active_object
    arch.name="SM_Archway_low"; arch.data.name="SM_Archway_low"
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    return arch

print("1. the disabled state is real, and reachable")
arch=archway()
for o in bpy.context.scene.objects: o.select_set(False)
arch.select_set(True); bpy.context.view_layer.objects.active=arch
rep("Generate UCX unavailable with only the mesh selected",
    not bpy.ops.object.generate_ucx.poll())
rep("Add Hull Box IS available", bpy.ops.object.add_collision_box.poll())

print("2. Add Hull Box gives something to work with")
bpy.ops.object.add_collision_box(coverage=1.0)
box=bpy.context.active_object
low, high = H.mesh_bounds(arch.data, arch.matrix_world)
blow, bhigh = H.mesh_bounds(box.data, box.matrix_world)
rep("box covers the mesh bounds",
    (blow-low).length < 1e-4 and (bhigh-high).length < 1e-4,
    f"mesh {tuple(round(v,2) for v in (high-low))} box {tuple(round(v,2) for v in (bhigh-blow))}")
rep("box is wireframe, not blocking the view", box.display_type=='WIRE')
rep("box is the active object, ready to shape", bpy.context.active_object is box)

print("3. three hulls turn the concave arch into a compound shape")
H.unregister()
arch=archway()
for o in bpy.context.scene.objects: o.select_set(False)
arch.select_set(True); bpy.context.view_layer.objects.active=arch
hulls=[]
for cov in (0.4, 0.4, 0.4):
    bpy.ops.object.add_collision_box(coverage=cov)
    hulls.append(bpy.context.active_object)
    for o in bpy.context.scene.objects: o.select_set(False)
    arch.select_set(True); bpy.context.view_layer.objects.active=arch
rep("three boxes made", len(hulls)==3)

for o in bpy.context.scene.objects: o.select_set(False)
for hbox in hulls: hbox.select_set(True)
arch.select_set(True); bpy.context.view_layer.objects.active=arch
rep("Generate UCX now available", bpy.ops.object.generate_ucx.poll())
bpy.ops.object.generate_ucx()
names=sorted(c.name for c in arch.children)
rep("named as a compound set",
    names==["UCX_SM_Archway_low_01","UCX_SM_Archway_low_02","UCX_SM_Archway_low_03"],
    str(names))
H.unregister()

print(f"\n{sum(R)}/{len(R)} checks pass")
