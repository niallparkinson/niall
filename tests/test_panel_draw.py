"""Panel draw checks, run against a real Blender.

The panel is the one part of the addon no other suite touches: a typo there
raises on every viewport redraw and stays invisible until Blender is open.
The layout is stood in for by a recorder, so this catches name and attribute
errors without needing a UI.

    pip install bpy
    cp "First Python Script.py" tests/hstools.py
    cd tests && python3 test_panel_draw.py
"""
import bpy, bmesh, sys
sys.path.insert(0,"."); import hstools as H

class Rec:
    """Stands in for UILayout: records calls, returns itself for containers."""
    def __init__(s): s.calls=[]
    def _c(s,n):
        def f(*a,**k):
            s.calls.append((n,a,k)); return s
        return f
    def __getattr__(s,n):
        if n in ("alert","enabled","scale_y","active","alignment"): return False
        return s._c(n)
    def __setattr__(s,n,v):
        if n=="calls": object.__setattr__(s,n,v)
        else: pass
    def operator(s,*a,**k):
        s.calls.append(("operator",a,k)); return type("Op",(),{})()

class Ctx:
    def __init__(s,obj,mode): s.active_object=obj; s.mode=mode; s.scene=bpy.context.scene; s.view_layer=bpy.context.view_layer

H.register()
bpy.ops.mesh.primitive_cube_add(size=2.0); cube=bpy.context.active_object
bpy.ops.object.smart_bevel(limit_mode='WEIGHT', vertex_bevel=True)
bpy.ops.object.mode_set(mode='EDIT')
bpy.ops.mesh.select_mode(type='EDGE')
bm=bmesh.from_edit_mesh(cube.data); bm.edges.ensure_lookup_table()
for e in bm.edges: e.select = all(v.co.z>0 for v in e.verts)
bmesh.update_edit_mesh(cube.data)
bpy.ops.mesh.add_bevel_layer(width=0.1)
bpy.ops.object.mode_set(mode='OBJECT')

P = H.VIEW3D_PT_smart_tools
ok=True
for mode in ('OBJECT','EDIT_MESH'):
    for name in ("draw_live_bevel","draw_bevel_layers"):
        try:
            getattr(P, name)(Ctx(cube,mode), Rec())
            print(f"  PASS  {name} in {mode}")
        except Exception as ex:
            ok=False; print(f"  FAIL  {name} in {mode}: {type(ex).__name__}: {ex}")
# and with a layer whose attribute has been deleted underneath it
lay=H.bevel_layers(cube)[0]
cube.data.attributes.remove(H.layer_attribute(cube,lay))
try:
    P.draw_bevel_layers(Ctx(cube,'OBJECT'), Rec()); print("  PASS  draw_bevel_layers with a lost attribute")
except Exception as ex:
    ok=False; print(f"  FAIL  lost attribute: {type(ex).__name__}: {ex}")
# and with no object at all
try:
    P.draw_bevel_layers(Ctx(None,'OBJECT'), Rec()); print("  PASS  draw_bevel_layers with no active object")
except Exception as ex:
    ok=False; print(f"  FAIL  no object: {type(ex).__name__}: {ex}")
print("panel draw OK" if ok else "PANEL DRAW BROKEN")
sys.exit(0 if ok else 1)
