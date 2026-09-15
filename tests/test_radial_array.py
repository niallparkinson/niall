"""Radial array checks, run against a real Blender.

    pip install bpy
    cp "First Python Script.py" tests/hstools.py
    cd tests && python3 test_radial_array.py

Every earlier attempt at this feature was reasoned about rather than measured,
and shipped broken three times running. The offset the Array modifier builds
from an object offset is inverse(target) @ empty, so the empty must hold
R @ target_matrix. Storing R alone spirals the copies outwards and grows them.
These checks pin that down by reading the evaluated mesh.
"""
import bpy, math, sys
from mathutils import Vector, Matrix
sys.path.insert(0, ".")
import hstools

def scene(scale=0.2, loc=(2,0,0), cursor=(0,0,0), cursor_rot=None):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    hstools.register()
    c = bpy.context.scene.cursor
    c.location = Vector(cursor)
    if cursor_rot:
        c.rotation_mode='QUATERNION'
        c.rotation_quaternion = Vector(cursor_rot[:3]).to_track_quat('Z','Y')
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=loc)
    o = bpy.context.active_object
    o.scale=(scale,)*3
    bpy.context.view_layer.update()
    return o

def stats(cube, n, pivot, axis=(0,0,1)):
    dg = bpy.context.evaluated_depsgraph_get()
    me = bpy.data.meshes.new_from_object(cube.evaluated_get(dg),
                                         preserve_all_data_layers=True, depsgraph=dg)
    per = len(me.vertices)//n
    M = cube.matrix_world; P=Vector(pivot); u=Vector(axis).normalized()
    cen, size = [], []
    for i in range(n):
        ch=[me.vertices[per*i+k].co for k in range(per)]
        c=sum(ch, Vector())/per
        cen.append(M@c); size.append(max((M@v - M@c).length for v in ch))
    bpy.data.meshes.remove(me)
    rad=[ ((c-P)-u*(c-P).dot(u)).length for c in cen ]
    ang=[]
    for i in range(n):
        a=(cen[i]-P); a-=u*a.dot(u); b=(cen[(i+1)%n]-P); b-=u*b.dot(u)
        ang.append(math.degrees(math.atan2(a.cross(b).dot(u), a.dot(b))))
    return rad, size, ang

def check(label, rad, size, ang, n):
    ok = (max(rad)-min(rad) < 1e-4 and max(size)-min(size) < 1e-6
          and all(abs(abs(a)-360/n) < 1e-3 for a in ang))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"        radii {[round(r,4) for r in rad]}")
        print(f"        sizes {[round(s,4) for s in size]}")
        print(f"        steps {[round(a,3) for a in ang]}")
    return ok

results=[]
print("1. redo-panel path (operator re-run at new counts)")
for n in (3,6,9,10,24):
    cube=scene(); bpy.ops.object.radial_array(count=6, axis_mode='CURSOR')
    bpy.ops.object.radial_array(count=n, axis_mode='CURSOR')
    results.append(check(f"count {n}", *stats(cube,n,(0,0,0)), n))
    hstools.unregister()

print("2. modifier-panel path (count edited directly, handler re-spaces)")
for n in (8, 12):
    cube=scene(); bpy.ops.object.radial_array(count=6, axis_mode='CURSOR')
    cube.modifiers["Array_Radial"].count = n
    bpy.context.view_layer.update()
    results.append(check(f"count {n} via modifier", *stats(cube,n,(0,0,0)), n))
    hstools.unregister()

print("3. cursor away from the world origin (screws on a face)")
cube=scene(loc=(3.5,1.0,0.8), cursor=(1.0,0.5,0.8))
bpy.ops.object.radial_array(count=7, axis_mode='CURSOR')
results.append(check("cursor at (1,0.5,0.8), count 7", *stats(cube,7,(1.0,0.5,0.8)), 7))
hstools.unregister()

print("4. object rotated before arraying")
cube=scene(); cube.rotation_euler=(0.4,0.3,0.9); bpy.context.view_layer.update()
bpy.ops.object.radial_array(count=8, axis_mode='CURSOR')
results.append(check("rotated object, count 8", *stats(cube,8,(0,0,0)), 8))
hstools.unregister()

print("5. object moved after arraying (handler re-syncs)")
cube=scene(); bpy.ops.object.radial_array(count=6, axis_mode='CURSOR')
m=cube.matrix_world.copy(); m.translation=Vector((3.0,0.5,0.0)); cube.matrix_world=m
bpy.context.view_layer.update(); bpy.context.view_layer.update()
results.append(check("moved to (3,0.5,0), count 6", *stats(cube,6,(0,0,0)), 6))
hstools.unregister()

print("6. radius offset")
cube=scene(); bpy.ops.object.radial_array(count=6, axis_mode='CURSOR', radius_offset=1.5)
rad,size,ang = stats(cube,6,(0,0,0))
results.append(check("offset +1.5 -> radius 3.5", rad,size,ang, 6))
print(f"        measured radius {round(rad[0],4)} (expected 3.5): "
      f"{'PASS' if abs(rad[0]-3.5)<1e-4 else 'FAIL'}")
results.append(abs(rad[0]-3.5)<1e-4)
hstools.unregister()

print(f"\n{sum(results)}/{len(results)} checks pass")
