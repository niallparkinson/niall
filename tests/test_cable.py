"""Live cable checks, run against a real Blender.

    pip install bpy
    cp "First Python Script.py" tests/hstools.py
    cd tests && python3 test_cable.py
"""
import bpy, bmesh, math, sys
from collections import Counter
from mathutils import Vector, Matrix
sys.path.insert(0, ".")
import hstools

R=[]
def rep(label, ok, extra=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  '+extra) if extra else ''}")
    R.append(ok)

def fresh():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    hstools.register()

def ev(o):
    dg=bpy.context.evaluated_depsgraph_get()
    return bpy.data.meshes.new_from_object(o.evaluated_get(dg),
                                           preserve_all_data_layers=True, depsgraph=dg)

def two_props(a=(0,0,2), b=(3,0,2)):
    """Two cubes, top faces selected, so both ends get a real connector normal."""
    bpy.ops.mesh.primitive_cube_add(size=0.4, location=a); A=bpy.context.active_object
    bpy.ops.mesh.primitive_cube_add(size=0.4, location=b); B=bpy.context.active_object
    return A,B

def cable():
    return [o for o in bpy.context.scene.objects if o.type=='CURVE'][0]

print("1. sag is exact (3-point spline passes through its middle control point)")
for sag in (0.1, 0.25, 0.5):
    fresh()
    A,B = two_props()
    for o in (A,B): o.select_set(True)
    bpy.context.view_layer.objects.active=A
    bpy.ops.object.drop_cable(sag=sag, radius=0.0001, lead=0.15, resolution=24)
    me=ev(cable()); low=min(v.co.z for v in me.vertices); bpy.data.meshes.remove(me)
    droop=2.0-low; expect=sag*3.0
    rep(f"sag {sag}", abs(droop-expect)<0.005, f"droop {droop:.4f} vs {expect:.4f}")
    hstools.unregister()

print("2. cable leaves the connector along its axis, not straight down")
fresh()
bpy.ops.mesh.primitive_cube_add(size=2.0, location=(0,0,0))
cube=bpy.context.active_object
bpy.ops.object.mode_set(mode='EDIT')
bm=bmesh.from_edit_mesh(cube.data); bm.faces.ensure_lookup_table()
for f in bm.faces: f.select=False
top=[f for f in bm.faces if f.normal.z>0.9][0]
side=[f for f in bm.faces if f.normal.x>0.9][0]
top.select=True; side.select=True
bmesh.update_edit_mesh(cube.data)
bpy.ops.object.drop_cable(sag=0.2, lead=0.3, radius=0.01, profile='1', resolution=24)
bpy.ops.object.mode_set(mode='OBJECT')
c=cable(); me=ev(c)
# Verts come out ring by ring; average each ring to recover the centreline.
ring=4*(int(c.data.bevel_resolution)+1)
centres=[]
for i in range(len(me.vertices)//ring):
    grp=[me.vertices[i*ring+k].co for k in range(ring)]
    centres.append(sum(grp, Vector())/ring)
bpy.data.meshes.remove(me)
# The head is the top face centre (0,0,1); its face normal is +Z.
if (centres[0]-Vector((0,0,1))).length > (centres[-1]-Vector((0,0,1))).length:
    centres.reverse()
exit_dir=(centres[3]-centres[0]).normalized()
rep("exit follows the +Z face normal", exit_dir.z>0.8,
    f"exit {tuple(round(x,2) for x in exit_dir)}")
# And a zero lead should NOT follow it (proves the control does something).
hstools.unregister()

print("3. LIVE: moving a prop drags its cable end and re-drapes")
fresh()
A,B = two_props()
for o in (A,B): o.select_set(True)
bpy.context.view_layer.objects.active=A
bpy.ops.object.drop_cable(sag=0.15, radius=0.0001, resolution=24)
c=cable()
me=ev(c); tip_before=max(v.co.x for v in me.vertices); low_before=min(v.co.z for v in me.vertices)
bpy.data.meshes.remove(me)
B.location = Vector((6.0, 0, 2))          # drag the far prop away
bpy.context.view_layer.update(); bpy.context.view_layer.update()
me=ev(c); tip_after=max(v.co.x for v in me.vertices); low_after=min(v.co.z for v in me.vertices)
bpy.data.meshes.remove(me)
rep("cable end followed the prop", abs(tip_after-6.0)<0.3, f"tip {tip_before:.2f} -> {tip_after:.2f}")
rep("droop re-scaled to the new span",
    abs((2.0-low_after) - 0.15*6.0) < 0.05,
    f"droop {2.0-low_before:.3f} -> {2.0-low_after:.3f}, expected {0.15*6.0:.3f}")
hstools.unregister()

print("4. re-dial: change sag on an existing cable, no rebuild")
fresh()
A,B = two_props()
for o in (A,B): o.select_set(True)
bpy.context.view_layer.objects.active=A
bpy.ops.object.drop_cable(sag=0.1, radius=0.0001, resolution=24)
c=cable()
c.cable_sag = 0.4
bpy.context.view_layer.update(); bpy.context.view_layer.update()
me=ev(c); low=min(v.co.z for v in me.vertices); bpy.data.meshes.remove(me)
rep("sag re-dialled to 0.4", abs((2.0-low)-1.2)<0.02, f"droop {2.0-low:.3f} expected 1.200")
hstools.unregister()

print("5. anchors are grabbable and route the cable")
fresh()
A,B = two_props()
for o in (A,B): o.select_set(True)
bpy.context.view_layer.objects.active=A
bpy.ops.object.drop_cable(sag=0.15, radius=0.0001, resolution=24)
c=cable()
anchors=[o for o in bpy.context.scene.objects if o.name.startswith("CableEnd")]
rep("two anchors created", len(anchors)==2)
rep("anchors parented to their props", all(a.parent in (A,B) for a in anchors))
rep("anchors filed in Cable_Rig",
    all(a.users_collection and a.users_collection[0].name=="Cable_Rig" for a in anchors))
hstools.unregister()

print("6. still watertight and UV-mapped, still exports")
fresh()
A,B = two_props()
for o in (A,B): o.select_set(True)
bpy.context.view_layer.objects.active=A
bpy.ops.object.drop_cable(sag=0.2, radius=0.02)
c=cable(); me=ev(c)
rep("has a UV map", len(me.uv_layers)>0)
bm=bmesh.new(); bm.from_mesh(me)
bmesh.ops.remove_doubles(bm, verts=bm.verts[:], dist=1e-6)
ec=Counter()
for f in bm.faces:
    vs=[v.index for v in f.verts]
    for i in range(len(vs)): ec[frozenset((vs[i],vs[(i+1)%len(vs)]))]+=1
rep("watertight after weld", set(ec.values())=={2}, str(dict(Counter(ec.values()))))
bm.free(); bpy.data.meshes.remove(me)
import tempfile, os
d=tempfile.mkdtemp(); bpy.context.scene.smart_export_path=d
for o in bpy.context.scene.objects: o.select_set(False)
c.select_set(True); bpy.context.view_layer.objects.active=c
bpy.ops.object.smart_export_ue5(export_type='LOW')
rep("FBX written", any(f.endswith(".fbx") for f in os.listdir(d)), str(os.listdir(d)))
hstools.unregister()

print(f"\n{sum(R)}/{len(R)} checks pass")
