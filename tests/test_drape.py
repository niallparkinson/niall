"""Cable drape checks, run against a real Blender.

    pip install bpy
    cp "First Python Script.py" tests/hstools.py
    cd tests && python3 test_drape.py
"""
import bpy, bmesh, sys, time
from collections import Counter
from mathutils import Vector
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

BENCH_TOP = 0.6
def bench_scene():
    """Two posts with a bench slab between them; a hung cable sinks through it."""
    bpy.ops.mesh.primitive_cube_add(size=0.3, location=(-1.5,0,1.5)); A=bpy.context.active_object
    bpy.ops.mesh.primitive_cube_add(size=0.3, location=( 1.5,0,1.5)); B=bpy.context.active_object
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0,0,0.1))
    bench=bpy.context.active_object
    bench.scale=(2.0,1.0,0.5); bpy.context.view_layer.update()   # top at 0.1+0.25 = ~0.6
    return A,B,bench

def cable_obj():
    return [o for o in bpy.context.scene.objects if o.type=='CURVE'][0]

def lowest(c):
    me=ev(c); z=min(v.co.z for v in me.vertices); bpy.data.meshes.remove(me); return z

print("1. a hung cable sinks through the bench; draping lifts it out")
fresh()
A,B,bench = bench_scene()
top = max((bench.matrix_world @ Vector(c)).z for c in bench.bound_box)
for o in (A,B): o.select_set(True)
bpy.context.view_layer.objects.active=A
bpy.ops.object.drop_cable(sag=0.45, radius=0.02, resolution=12)
c=cable_obj()
before = lowest(c)
rep("hung cable penetrates the bench", before < top - 0.05,
    f"lowest {before:.3f} vs bench top {top:.3f}")

for o in bpy.context.scene.objects: o.select_set(False)
c.select_set(True); bpy.context.view_layer.objects.active=c
t=time.time()
bpy.ops.object.drape_cable(segments=28, iterations=200, clearance=0.02, slack=0.0)
dt=time.time()-t
after = lowest(c)
rep("draped cable rests on the bench", after > top - 0.03,
    f"lowest {before:.3f} -> {after:.3f}, bench top {top:.3f}")
rep("bake is fast", dt < 3.0, f"{dt:.2f}s")

print("2. the ends stay in their connectors")
sp=c.data.splines[0]
head=Vector(sp.bezier_points[0].co); tail=Vector(sp.bezier_points[-1].co)
anchors={o.name:o for o in bpy.context.scene.objects if o.name.startswith("CableEnd")}
locs=[o.matrix_world.translation for o in anchors.values()]
rep("head pinned to its anchor", min((head-l).length for l in locs) < 1e-4)
rep("tail pinned to its anchor", min((tail-l).length for l in locs) < 1e-4)

print("3. the cable does not stretch")
pts=[Vector(p.co) for p in sp.bezier_points]
segs=sorted((pts[i+1]-pts[i]).length for i in range(len(pts)-1))
median=segs[len(segs)//2]
# Chords shorten where the cable bends, so exact evenness is not the goal.
# What matters is no coincident control points and no run left un-subdivided.
rep("no degenerate control points", segs[0] > 0.25*median, f"min/median {segs[0]/median:.2f}")
rep("no over-long segment", segs[-1] < 2.0*median, f"max/median {segs[-1]/median:.2f}")

hstools.unregister()

print("4. slack makes it puddle further")
fresh()
A,B,bench = bench_scene()
for o in (A,B): o.select_set(True)
bpy.context.view_layer.objects.active=A
bpy.ops.object.drop_cable(sag=0.45, radius=0.02, resolution=12)
c=cable_obj()
for o in bpy.context.scene.objects: o.select_set(False)
c.select_set(True); bpy.context.view_layer.objects.active=c
bpy.ops.object.drape_cable(segments=28, iterations=200, clearance=0.02, slack=0.0)
me=ev(c); tight=sum(1 for v in me.vertices if v.co.z < BENCH_TOP+0.1); bpy.data.meshes.remove(me)
bpy.ops.object.drape_cable(segments=28, iterations=200, clearance=0.02, slack=0.6)
me=ev(c); loose=sum(1 for v in me.vertices if v.co.z < BENCH_TOP+0.1); bpy.data.meshes.remove(me)
rep("more slack means more cable lying on the surface", loose > tight,
    f"{tight} -> {loose} verts near the bench")

print("5. a drape survives; a slider clears it")
rep("marked as draped", bool(c.get("cable_draped")))
z_draped = lowest(c)
bpy.context.view_layer.update(); bpy.context.view_layer.update()
rep("handler leaves the bake alone", abs(lowest(c)-z_draped) < 1e-5)
c.cable_sag = 0.2
bpy.context.view_layer.update()
rep("moving a slider re-hangs it", not c.get("cable_draped"))

hstools.unregister()

print("5b. the resting height does not depend on the quality setting")
fresh()
A,B,bench = bench_scene()
top = max((bench.matrix_world @ Vector(cc)).z for cc in bench.bound_box)
for o in (A,B): o.select_set(True)
bpy.context.view_layer.objects.active=A
bpy.ops.object.drop_cable(sag=0.45, radius=0.02, resolution=12)
c=cable_obj()
for o in bpy.context.scene.objects: o.select_set(False)
c.select_set(True); bpy.context.view_layer.objects.active=c
heights=[]
for q in (60, 150, 400, 900):
    bpy.ops.object.drape_cable(segments=28, iterations=q, clearance=0.02)
    heights.append(lowest(c))
rep("same resting height at every quality", max(heights)-min(heights) < 0.01,
    f"{[round(h,4) for h in heights]}")
rep("and it is actually touching", abs(min(heights)-top) < 0.02,
    f"min {min(heights):.4f} vs bench top {top:.4f}")
hstools.unregister()

print("6. still watertight, UV'd and exportable after a drape")
fresh()
A,B,bench = bench_scene()
for o in (A,B): o.select_set(True)
bpy.context.view_layer.objects.active=A
bpy.ops.object.drop_cable(sag=0.45, radius=0.02)
c=cable_obj()
for o in bpy.context.scene.objects: o.select_set(False)
c.select_set(True); bpy.context.view_layer.objects.active=c
bpy.ops.object.drape_cable()
me=ev(c)
rep("has a UV map", len(me.uv_layers)>0)
bm=bmesh.new(); bm.from_mesh(me); bmesh.ops.remove_doubles(bm, verts=bm.verts[:], dist=1e-6)
ec=Counter()
for f in bm.faces:
    vs=[v.index for v in f.verts]
    for i in range(len(vs)): ec[frozenset((vs[i],vs[(i+1)%len(vs)]))]+=1
rep("watertight after weld", set(ec.values())=={2}, str(dict(Counter(ec.values()))))
bm.free(); bpy.data.meshes.remove(me)
import tempfile, os
d=tempfile.mkdtemp(); bpy.context.scene.smart_export_path=d
bpy.ops.object.smart_export_ue5(export_type='LOW')
rep("FBX written", any(f.endswith(".fbx") for f in os.listdir(d)))
hstools.unregister()

print(f"\n{sum(R)}/{len(R)} checks pass")
