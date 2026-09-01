"""Cable extruder checks, run against a real Blender.

    pip install bpy
    cp "First Python Script.py" tests/hstools.py
    cd tests && python3 test_cable.py
"""
import bpy, bmesh, math, sys
from collections import Counter
from mathutils import Vector
sys.path.insert(0, ".")
import hstools

results=[]
def report(label, ok, extra=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  ' + extra) if extra else ''}")
    results.append(ok)

def fresh():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    hstools.register()

def evaluated(obj):
    dg = bpy.context.evaluated_depsgraph_get()
    return bpy.data.meshes.new_from_object(obj.evaluated_get(dg),
                                           preserve_all_data_layers=True, depsgraph=dg)

print("1. sag slider means the droop you actually get")
for sag in (0.0, 0.1, 0.25, 0.5):
    fresh()
    a, b = Vector((0,0,2)), Vector((2,0,2))
    o1=bpy.data.objects.new("A",None); o2=bpy.data.objects.new("B",None)
    for o,l in ((o1,a),(o2,b)):
        bpy.context.scene.collection.objects.link(o); o.location=l; o.select_set(True)
    bpy.context.view_layer.objects.active=o1
    bpy.context.view_layer.update()
    bpy.ops.object.drop_cable(sag=sag, radius=0.02, resolution=16)
    cable=[x for x in bpy.context.scene.objects if x.type=='CURVE'][0]
    me=evaluated(cable)
    lowest=min(v.co.z for v in me.vertices) + 0.02   # add radius back
    droop=2.0-lowest
    expected=sag*(b-a).length
    bpy.data.meshes.remove(me)
    report(f"sag {sag}", abs(droop-expected)<0.01, f"droop {droop:.4f} vs expected {expected:.4f}")
    hstools.unregister()

print("2. profile choice controls the silhouette cost")
counts=[]
for prof in ('0','1','2','3'):
    fresh()
    for l in ((0,0,1),(1,0,1)):
        o=bpy.data.objects.new("P",None); bpy.context.scene.collection.objects.link(o)
        o.location=l; o.select_set(True)
    bpy.context.view_layer.objects.active=bpy.context.selected_objects[0]
    bpy.ops.object.drop_cable(profile=prof, resolution=4)
    cable=[x for x in bpy.context.scene.objects if x.type=='CURVE'][0]
    me=evaluated(cable); counts.append(len(me.vertices)); bpy.data.meshes.remove(me)
    hstools.unregister()
report("vertex count rises with profile", counts==sorted(counts) and len(set(counts))==4, str(counts))

print("3. cable carries generated UVs, and is watertight once welded")
fresh()
for l in ((0,0,1),(1.5,0.4,1)):
    o=bpy.data.objects.new("P",None); bpy.context.scene.collection.objects.link(o)
    o.location=l; o.select_set(True)
bpy.context.view_layer.objects.active=bpy.context.selected_objects[0]
bpy.ops.object.drop_cable(sag=0.2, radius=0.015)
cable=[x for x in bpy.context.scene.objects if x.type=='CURVE'][0]
me=evaluated(cable)
report("has a UV map", len(me.uv_layers)>0, str([l.name for l in me.uv_layers]))
bm=bmesh.new(); bm.from_mesh(me)
bmesh.ops.remove_doubles(bm, verts=bm.verts[:], dist=1e-6)
ec=Counter()
for f in bm.faces:
    vs=[v.index for v in f.verts]
    for i in range(len(vs)): ec[frozenset((vs[i],vs[(i+1)%len(vs)]))]+=1
report("watertight after weld", set(ec.values())=={2}, str(dict(Counter(ec.values()))))
bm.free(); bpy.data.meshes.remove(me)

print("4. cable exports through the UE5 pipeline without hand conversion")
import tempfile, os
d=tempfile.mkdtemp()
bpy.context.scene.smart_export_path=d
for x in bpy.context.scene.objects: x.select_set(False)
cable.select_set(True); bpy.context.view_layer.objects.active=cable
bpy.ops.object.smart_export_ue5(export_type='LOW')
files=os.listdir(d)
report("FBX written from the curve", any(f.endswith(".fbx") for f in files), str(files))
hstools.unregister()

print("5. edit-mode: two selected vertices")
fresh()
bpy.ops.mesh.primitive_cube_add(size=2.0)
cube=bpy.context.active_object
bpy.ops.object.mode_set(mode='EDIT')
bm=bmesh.from_edit_mesh(cube.data); bm.verts.ensure_lookup_table()
for v in bm.verts: v.select=False
bm.verts[0].select=True; bm.verts[6].select=True
bmesh.update_edit_mesh(cube.data)
before=len(bpy.context.scene.objects)
bpy.ops.object.drop_cable(sag=0.3)
report("cable created from vertex selection", len(bpy.context.scene.objects)==before+1)
bpy.ops.object.mode_set(mode='OBJECT')
hstools.unregister()

print("6. guards")
fresh()
o=bpy.data.objects.new("only",None); bpy.context.scene.collection.objects.link(o); o.select_set(True)
bpy.context.view_layer.objects.active=o
try:
    r=bpy.ops.object.drop_cable()
    ok = r=={'CANCELLED'}
except RuntimeError as e:
    ok = "Select two" in str(e)      # bpy.ops raises when an operator errors
report("refuses a single selection", ok)
hstools.unregister()

print(f"\n{sum(results)}/{len(results)} checks pass")
