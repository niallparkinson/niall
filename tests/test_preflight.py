"""Pre-flight scan checks, run against a real Blender.

    pip install bpy
    cp "First Python Script.py" tests/hstools.py
    cd tests && python3 test_preflight.py
"""
import bpy, bmesh, sys, math
from mathutils import Vector
sys.path.insert(0,"."); import hstools as H

R=[]
def rep(l, ok, extra=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {l}{('  '+extra) if extra else ''}")
    R.append(ok)
def fresh():
    bpy.ops.wm.read_factory_settings(use_empty=True); H.register()
def stats(obj):
    bm=bmesh.new(); bm.from_mesh(obj.data)
    d=H.diagnose_mesh(bm); out={k:len(v) for k,v in d.items()}
    out["faces"]=len(bm.faces); out["verts"]=len(bm.verts); bm.free(); return out

print("1. flat ngons are kept, curved ngons are split")
fresh()
# Flat: a grid with its corner faces merged into one planar ngon
bpy.ops.mesh.primitive_grid_add(x_subdivisions=6, y_subdivisions=6, size=2.0)
grid=bpy.context.active_object
bm=bmesh.new(); bm.from_mesh(grid.data)
chosen=[f for f in bm.faces if f.calc_center_median().x<0 and f.calc_center_median().y<0]
inner=[e for e in bm.edges if sum(1 for f in e.link_faces if f in chosen)==2]
bmesh.ops.dissolve_edges(bm, edges=inner, use_verts=False); bm.to_mesh(grid.data); bm.free()
before=stats(grid)
bpy.ops.object.preflight_scan(select_issues=False)
after=stats(grid)
rep("flat ngon left whole", before["flat_ngons"]>0 and after["flat_ngons"]==before["flat_ngons"],
    f"flat ngons {before['flat_ngons']} -> {after['flat_ngons']}")
H.unregister()

fresh()
bpy.ops.mesh.primitive_cylinder_add(radius=1.0, depth=2.0, vertices=32)
cyl=bpy.context.active_object
bm=bmesh.new(); bm.from_mesh(cyl.data); bm.normal_update()
side=[f for f in bm.faces if abs(f.normal.z)<0.1][:6]
inner=[e for e in bm.edges if sum(1 for f in e.link_faces if f in side)==2]
bmesh.ops.dissolve_edges(bm, edges=inner, use_verts=False); bm.to_mesh(cyl.data); bm.free()
before=stats(cyl)
bpy.ops.object.preflight_scan(select_issues=False)
after=stats(cyl)
rep("curved ngon triangulated", before["curved_ngons"]>0 and after["curved_ngons"]==0,
    f"curved ngons {before['curved_ngons']} -> {after['curved_ngons']}")
rep("flat cap survived the same pass", after["flat_ngons"]>0, f"{after['flat_ngons']} flat ngons kept")
H.unregister()

print("2. loose geometry and doubles are purged")
fresh()
bpy.ops.mesh.primitive_cube_add(size=2.0)
cube=bpy.context.active_object
bm=bmesh.new(); bm.from_mesh(cube.data)
for p in ((5,5,5),(6,6,6)): bm.verts.new(p)          # floating garbage
bm.verts.ensure_lookup_table()
v=bm.verts[0]; bm.verts.new(v.co + Vector((1e-6,0,0)))  # a double
bm.to_mesh(cube.data); bm.free()
before=stats(cube)
bpy.ops.object.preflight_scan(select_issues=False)
after=stats(cube)
rep("loose vertices removed", before["loose_verts"]>0 and after["loose_verts"]==0,
    f"{before['loose_verts']} -> {after['loose_verts']}")
rep("vertex count came back down", after["verts"]==8, f"{before['verts']} -> {after['verts']}")
H.unregister()

print("3. interior faces are removed")
fresh()
bpy.ops.mesh.primitive_cube_add(size=2.0)
cube=bpy.context.active_object
bm=bmesh.new(); bm.from_mesh(cube.data)
# Slice the cube in half and cap the cut while keeping both halves: the cap's
# edges then border three faces each, which is what makes it interior.
geom = bm.verts[:] + bm.edges[:] + bm.faces[:]
cut = bmesh.ops.bisect_plane(bm, geom=geom, plane_co=(0,0,0), plane_no=(0,0,1),
                             clear_inner=False, clear_outer=False)
loop = [e for e in cut["geom_cut"] if isinstance(e, bmesh.types.BMEdge)]
bmesh.ops.contextual_create(bm, geom=loop)
bm.to_mesh(cube.data); bm.free()
before=stats(cube)
bpy.ops.object.preflight_scan(select_issues=False)
after=stats(cube)
rep("interior wall detected", before["interior_faces"]>0, f"{before['interior_faces']} found")
rep("interior wall removed", after["interior_faces"]==0)
rep("junction edges cleared too", after["junctions"]==0,
    f"{before['junctions']} -> {after['junctions']}")
H.unregister()

print("4. an open hole is reported, not silently 'fixed'")
fresh()
bpy.ops.mesh.primitive_cube_add(size=2.0)
cube=bpy.context.active_object
bm=bmesh.new(); bm.from_mesh(cube.data); bm.faces.ensure_lookup_table()
bmesh.ops.delete(bm, geom=[bm.faces[0]], context='FACES_ONLY')
bm.to_mesh(cube.data); bm.free()
bpy.ops.object.preflight_scan(select_issues=False)
report=cube["preflight_report"]
rep("hole reported to the user", "open edges" in report, repr(report))
rep("hole was not quietly filled", stats(cube)["holes"]==4)
H.unregister()

print("5. report-only changes nothing")
fresh()
bpy.ops.mesh.primitive_cube_add(size=2.0)
cube=bpy.context.active_object
bm=bmesh.new(); bm.from_mesh(cube.data)
for p in ((5,5,5),(6,6,6)): bm.verts.new(p)
bm.to_mesh(cube.data); bm.free()
before=stats(cube)
bpy.ops.object.preflight_scan(report_only=True, select_issues=False)
after=stats(cube)
rep("mesh untouched in report-only", before==after, f"{before['verts']} verts either side")
rep("but the problem was still reported", "loose" in cube["preflight_report"],
    repr(cube["preflight_report"]))
H.unregister()

print("6. a clean mesh is left alone and says so")
fresh()
bpy.ops.mesh.primitive_cube_add(size=2.0)
cube=bpy.context.active_object
before=stats(cube)
bpy.ops.object.preflight_scan(select_issues=False)
after=stats(cube)
rep("clean cube untouched", before==after)
rep("reported as clean", cube["preflight_report"]=="Clean", repr(cube["preflight_report"]))
H.unregister()

print(f"\n{sum(R)}/{len(R)} checks pass")

print("6b. the Edit Mode hand-off")
bpy.ops.wm.read_factory_settings(use_empty=True); H.register()
bpy.ops.mesh.primitive_cube_add(size=2.0)
cube=bpy.context.active_object
bm=bmesh.new(); bm.from_mesh(cube.data); bm.faces.ensure_lookup_table()
bmesh.ops.delete(bm, geom=[bm.faces[0]], context='FACES_ONLY')
bm.to_mesh(cube.data); bm.free()

bpy.ops.object.preflight_scan(select_issues=True)
rep("dropped into Edit Mode", bpy.context.mode=='EDIT_MESH', bpy.context.mode)
rep("edge select mode active", tuple(bpy.context.tool_settings.mesh_select_mode)==(False,True,False))
em=bmesh.from_edit_mesh(cube.data)
sel=[e for e in em.edges if e.select]
rep("the hole's edges are the selection", len(sel)==4, f"{len(sel)} edges selected")
bpy.ops.object.mode_set(mode='OBJECT')

print("clean mesh must NOT hijack the mode:")
bpy.ops.mesh.primitive_cube_add(size=2.0, location=(5,0,0))
clean=bpy.context.active_object
for o in bpy.context.scene.objects: o.select_set(False)
clean.select_set(True); bpy.context.view_layer.objects.active=clean
bpy.ops.object.preflight_scan(select_issues=True)
rep("stays in Object Mode when clean", bpy.context.mode=='OBJECT', bpy.context.mode)
H.unregister()

print("7. the export path detects a mesh it cannot make watertight")
import tempfile, os
fresh()
bpy.ops.mesh.primitive_cube_add(size=2.0)
holed=bpy.context.active_object
bm=bmesh.new(); bm.from_mesh(holed.data); bm.faces.ensure_lookup_table()
bmesh.ops.delete(bm, geom=[bm.faces[0]], context='FACES_ONLY')
bm.to_mesh(holed.data); bm.free()
verts, faces, open_edges = H.clean_mesh(bpy.context, holed, 0.0001, True)
rep("clean_mesh reports the surviving hole", open_edges == 4, f"{open_edges} open edges")

bpy.ops.mesh.primitive_cube_add(size=2.0, location=(5,0,0))
solid=bpy.context.active_object
v2,f2,open2 = H.clean_mesh(bpy.context, solid, 0.0001, True)
rep("a watertight mesh reports none", open2 == 0, f"{open2} open edges")

d=tempfile.mkdtemp(); bpy.context.scene.smart_export_path=d
for o in bpy.context.scene.objects: o.select_set(False)
holed.select_set(True); bpy.context.view_layer.objects.active=holed
bpy.ops.object.smart_export_ue5(export_type='LOW')
rep("export still writes the file, warning rather than blocking",
    any(f.endswith('.fbx') for f in os.listdir(d)))
H.unregister()
print(f"\n{sum(R)}/{len(R)} checks pass")
