"""UCX collision checks, run against a real Blender.

    pip install bpy
    cp "First Python Script.py" tests/hstools.py
    cd tests && python3 test_ucx.py

Unverified assumption, flagged deliberately: that Unreal matches UCX_<name> to
the render mesh's name *inside the FBX*. The Epic documentation is unreachable
from this environment, so the code derives the hull name and the render mesh
name from one shared value, which makes them agree whatever the rule turns out
to be. If Unreal instead wants the bare asset name, flip Match Export Name off.
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
    hulls = op.build_collision_copies(bpy.context, d, name, shift)
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

print(f"\n{sum(R)}/{len(R)} checks pass")
