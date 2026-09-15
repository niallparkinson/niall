"""Smart triangulate checks, run against a real Blender.

    pip install bpy
    cp "First Python Script.py" tests/hstools.py
    cd tests && python3 test_triangulate.py
"""
import bpy, bmesh, sys, tempfile, os
from mathutils import Vector
sys.path.insert(0,"."); import hstools as H
R=[]
def rep(l,ok,extra=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {l}{('  '+extra) if extra else ''}"); R.append(ok)
def fresh():
    bpy.ops.wm.read_factory_settings(use_empty=True); H.register()
def evmesh(o):
    dg=bpy.context.evaluated_depsgraph_get()
    return bpy.data.meshes.new_from_object(o.evaluated_get(dg), preserve_all_data_layers=True, depsgraph=dg)
def sides(o):
    me=evmesh(o); c={}
    for f in me.polygons: c[len(f.vertices)]=c.get(len(f.vertices),0)+1
    bpy.data.meshes.remove(me); return c

print("1. non-destructive mode leaves the mesh editable")
fresh()
bpy.ops.mesh.primitive_cube_add(size=2.0); cube=bpy.context.active_object
bpy.ops.object.smart_triangulate(mode='MODIFIER')
rep("evaluated mesh is all triangles", sides(cube)=={3:12}, str(sides(cube)))
rep("base mesh still quads", {len(f.vertices) for f in cube.data.polygons}=={4})
m=cube.modifiers[H.TRIANGULATE_MOD]
rep("shortest diagonal set", m.quad_method=='SHORTEST_DIAGONAL', m.quad_method)
rep("custom normals kept", m.keep_custom_normals is True)
rep("min_vertices 4 (quads included)", m.min_vertices==4)
H.unregister()

print("2. it sits after the weighted normal, not before")
fresh()
bpy.ops.mesh.primitive_cube_add(size=2.0); cube=bpy.context.active_object
bpy.ops.object.smart_triangulate(mode='MODIFIER')     # triangulate added first
bpy.ops.object.smart_bevel()                          # bevel + weighted normal after
names=[m.name for m in cube.modifiers]
rep("triangulate ends up last", names[-1]==H.TRIANGULATE_MOD, str(names))
rep("weighted normal immediately before it",
    names[-2]==H.WEIGHTED_NORMAL_MOD, str(names))
H.unregister()

print("3. ngons-only leaves quads whole")
fresh()
bpy.ops.mesh.primitive_cylinder_add(radius=1.0, depth=2.0, vertices=12)
cyl=bpy.context.active_object
before=sides(cyl)
bpy.ops.object.smart_triangulate(mode='MODIFIER', ngon_only=True)
after=sides(cyl)
rep("ngons gone", after.get(12,0)==0 and before.get(12,0)>0, f"{before} -> {after}")
rep("quads untouched", after.get(4,0)==before.get(4,0), f"{before.get(4,0)} quads either side")
H.unregister()

print("4. bake-ready mode writes triangles into the mesh data")
fresh()
bpy.ops.mesh.primitive_cube_add(size=2.0); cube=bpy.context.active_object
bpy.ops.object.smart_bevel()
n_before=len(cube.modifiers)
bpy.ops.object.smart_triangulate(mode='APPLY')
target=bpy.context.view_layer.objects.active
rep("modifier stack consumed", len(target.modifiers)==0, f"{n_before} -> {len(target.modifiers)}")
rep("mesh data is triangles", {len(f.vertices) for f in target.data.polygons}=={3})
rep("bevel geometry survived the bake", len(target.data.polygons) > 12,
    f"{len(target.data.polygons)} faces")
H.unregister()

print("5. keep original leaves the editable version behind")
fresh()
bpy.ops.mesh.primitive_cube_add(size=2.0); cube=bpy.context.active_object
n0=len([o for o in bpy.context.scene.objects if o.type=='MESH'])
bpy.ops.object.smart_triangulate(mode='APPLY', keep_original=True)
n1=len([o for o in bpy.context.scene.objects if o.type=='MESH'])
rep("a copy was kept", n1==n0+1, f"{n0} -> {n1} meshes")
H.unregister()

print("6. shared mesh data is refused, not silently mangled")
fresh()
bpy.ops.mesh.primitive_cube_add(size=2.0); a=bpy.context.active_object
b=a.copy(); bpy.context.scene.collection.objects.link(b)   # linked duplicate
for o in (a,b): o.select_set(True)
bpy.context.view_layer.objects.active=a
faces_before=len(a.data.polygons)
try:
    bpy.ops.object.smart_triangulate(mode='APPLY')
except RuntimeError:
    pass
rep("linked duplicates left alone", len(a.data.polygons)==faces_before,
    f"{faces_before} -> {len(a.data.polygons)}")
H.unregister()

print("7. export writes triangles without committing the Blender mesh")
fresh()
bpy.ops.mesh.primitive_cube_add(size=2.0); cube=bpy.context.active_object
d=tempfile.mkdtemp(); bpy.context.scene.smart_export_path=d
bpy.context.scene.smart_export_triangulate=True
# Mirror what the exporter does to its throwaway copy.
temp=cube.copy(); temp.data=cube.data.copy()
bpy.context.scene.collection.objects.link(temp)
H.ensure_triangulation(temp, 'SHORTEST_DIAGONAL', 'BEAUTY', False, True)
rep("export copy evaluates to triangles", sides(temp)=={3:12}, str(sides(temp)))
rep("the artist's mesh is still quads", {len(f.vertices) for f in cube.data.polygons}=={4})
bpy.data.objects.remove(temp, do_unlink=True)
bpy.ops.object.smart_export_ue5(export_type='LOW')
rep("FBX written", any(f.endswith('.fbx') for f in os.listdir(d)), str(os.listdir(d)))
H.unregister()

print("8. the bevel shading survives the split")
bpy.ops.wm.read_factory_settings(use_empty=True); H.register()
bpy.ops.mesh.primitive_cube_add(size=2.0)
cube=bpy.context.active_object
bpy.ops.object.smart_bevel()          # bevel + weighted normal build the shading

def loop_normals(obj):
    dg=bpy.context.evaluated_depsgraph_get()
    me=bpy.data.meshes.new_from_object(obj.evaluated_get(dg),
                                       preserve_all_data_layers=True, depsgraph=dg)
    out=[(Vector(l.normal).copy(), me.vertices[l.vertex_index].co.copy()) for l in me.loops]
    bpy.data.meshes.remove(me); return out

base = loop_normals(cube)
print(f"  shading before triangulation: {len(base)} loop normals")

m = H.ensure_triangulation(cube, 'SHORTEST_DIAGONAL', 'BEAUTY', False, True)
kept = loop_normals(cube)
m.keep_custom_normals = False
dropped = loop_normals(cube)

def compare(a, b):
    """Match loops by vertex position and measure the worst normal deviation."""
    lookup = {}
    for n, co in a:
        lookup.setdefault(tuple(round(c,5) for c in co), []).append(n)
    worst = 0.0
    for n, co in b:
        key = tuple(round(c,5) for c in co)
        if key in lookup:
            worst = max(worst, min((n - o).length for o in lookup[key]))
    return worst

kept_delta = compare(base, kept)
dropped_delta = compare(base, dropped)
print(f"  keep_custom_normals=True  -> worst deviation from original shading {kept_delta:.4f}")
print(f"  keep_custom_normals=False -> worst deviation from original shading {dropped_delta:.4f}")
rep("keeping normals preserves the bevel shading", kept_delta < 1e-4)
rep("and turning it off measurably changes it", dropped_delta > kept_delta * 10 + 1e-3,
    f"{dropped_delta:.4f} vs {kept_delta:.4f}")
H.unregister()

print(f"\n{sum(R)}/{len(R)} checks pass")
