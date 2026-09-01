#life 

---
```
bl_info = {
    "name": "Smart Hard Surface Tools",
    "author": "Your Name",
    "version": (1, 4),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar (N) > Hard Ops",
    "description": "Automates booleans, shading, UVs, and UE5 exporting.",
    "category": "Object",
}

import bpy
import os
from bpy.props import FloatProperty, IntProperty, StringProperty, EnumProperty

# --- EXISTING BOOLEAN & BEVEL CLASSES ---

class OBJECT_OT_smart_difference(bpy.types.Operator):
    """Apply Smart Difference Boolean and organize cutter"""
    bl_idname = "object.smart_difference"
    bl_label = "Smart Difference"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return len(context.selected_objects) >= 2 and context.active_object is not None

    def execute(self, context):
        target = context.active_object
        cutters = [obj for obj in context.selected_objects if obj != target]
        if not cutters: return {'CANCELLED'}

        cutter_coll_name = "Cutters_Collection"
        cutter_coll = bpy.data.collections.get(cutter_coll_name)
        if not cutter_coll:
            cutter_coll = bpy.data.collections.new(cutter_coll_name)
            context.scene.collection.children.link(cutter_coll)
            cutter_coll.hide_render = True 

        for cutter in cutters:
            mod = target.modifiers.new(name=f"Bool_Diff_{cutter.name}", type='BOOLEAN')
            mod.operation = 'DIFFERENCE'
            mod.object = cutter
            mod.solver = 'EXACT'

            if cutter.name not in cutter_coll.objects:
                cutter_coll.objects.link(cutter)
                for coll in cutter.users_collection:
                    if coll != cutter_coll: coll.objects.unlink(cutter)

            cutter.display_type = 'WIRE'
            cutter.hide_render = True

        self.report({'INFO'}, f"Applied {len(cutters)} smart booleans.")
        return {'FINISHED'}


class OBJECT_OT_smart_slice(bpy.types.Operator):
    """Cut and detach a piece from the target object"""
    bl_idname = "object.smart_slice"
    bl_label = "Smart Slice"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return len(context.selected_objects) >= 2 and context.active_object is not None

    def execute(self, context):
        target = context.active_object
        cutters = [obj for obj in context.selected_objects if obj != target]
        if not cutters: return {'CANCELLED'}

        cutter_coll_name = "Cutters_Collection"
        cutter_coll = bpy.data.collections.get(cutter_coll_name)
        if not cutter_coll:
            cutter_coll = bpy.data.collections.new(cutter_coll_name)
            context.scene.collection.children.link(cutter_coll)
            cutter_coll.hide_render = True 

        for cutter in cutters:
            slice_obj = target.copy()
            slice_obj.data = target.data.copy()
            slice_obj.name = f"{target.name}_Slice"
            for coll in target.users_collection: coll.objects.link(slice_obj)

            mod_intersect = slice_obj.modifiers.new(name=f"Bool_Slice_{cutter.name}", type='BOOLEAN')
            mod_intersect.operation = 'INTERSECT'
            mod_intersect.object = cutter
            mod_intersect.solver = 'EXACT'

            mod_diff = target.modifiers.new(name=f"Bool_Diff_{cutter.name}", type='BOOLEAN')
            mod_diff.operation = 'DIFFERENCE'
            mod_diff.object = cutter
            mod_diff.solver = 'EXACT'

            if cutter.name not in cutter_coll.objects:
                cutter_coll.objects.link(cutter)
                for coll in cutter.users_collection:
                    if coll != cutter_coll: coll.objects.unlink(cutter)

            cutter.display_type = 'WIRE'
            cutter.hide_render = True

        self.report({'INFO'}, f"Sliced {len(cutters)} pieces from {target.name}.")
        return {'FINISHED'}


class OBJECT_OT_smart_bevel(bpy.types.Operator):
    """Applies Hard-Surface Bevel and Weighted Normals"""
    bl_idname = "object.smart_bevel"
    bl_label = "Smart Bevel"
    bl_options = {'REGISTER', 'UNDO'}

    bevel_width: FloatProperty(name="Bevel Width", default=0.01, min=0.001, step=0.1)
    bevel_segments: IntProperty(name="Segments", default=3, min=1, max=10)

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == 'MESH'

    def execute(self, context):
        for obj in context.selected_objects:
            if obj.type != 'MESH': continue

            bpy.ops.object.select_all(action='DESELECT')
            obj.select_set(True)
            context.view_layer.objects.active = obj
            bpy.ops.object.shade_smooth()
            
            if hasattr(obj.data, "use_auto_smooth"):
                obj.data.use_auto_smooth = True
                obj.data.auto_smooth_angle = 1.0472
            
            mod_bevel = obj.modifiers.get("Smart_Bevel")
            if not mod_bevel:
                mod_bevel = obj.modifiers.new(name="Smart_Bevel", type='BEVEL')
            
            mod_bevel.limit_method = 'ANGLE'
            mod_bevel.angle_limit = 0.523599
            mod_bevel.width = self.bevel_width
            mod_bevel.segments = self.bevel_segments
            mod_bevel.profile = 0.7
            mod_bevel.miter_outer = 'MITER_ARC'

            mod_wn = obj.modifiers.get("Smart_Weighted_Normal")
            if not mod_wn:
                mod_wn = obj.modifiers.new(name="Smart_Weighted_Normal", type='WEIGHTED_NORMAL')
            mod_wn.keep_sharp = True

        for obj in context.selected_objects: obj.select_set(True)
        self.report({'INFO'}, "Applied Smart Bevel & Normals.")
        return {'FINISHED'}


# --- NEW SMART UV CLASSES ---

class OBJECT_OT_smart_uv(bpy.types.Operator):
    """Auto-mark seams by angle and unwrap"""
    bl_idname = "object.smart_uv"
    bl_label = "Smart UV Unwrap"
    bl_options = {'REGISTER', 'UNDO'}

    # Properties adjustable in the F9 menu
    angle_limit: FloatProperty(name="Sharp Angle", default=0.523599, subtype='ANGLE') # 30 degrees
    island_margin: FloatProperty(name="Margin", default=0.02, min=0.001, max=1.0)

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == 'MESH'

    def execute(self, context):
        obj = context.active_object
        
        # Save current mode so we can return to it smoothly
        original_mode = obj.mode
        if original_mode != 'EDIT':
            bpy.ops.object.mode_set(mode='EDIT')
        
        # 1. Clear the slate
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.mesh.mark_seam(clear=True)
        bpy.ops.mesh.select_all(action='DESELECT')
        
        # 2. Find sharp edges and mark them
        bpy.ops.mesh.edges_select_sharp(sharpness=self.angle_limit)
        bpy.ops.mesh.mark_seam(clear=False)
        
        # 3. Select everything and Unwrap
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.uv.unwrap(method='ANGLE_BASED', margin=self.island_margin)
        
        # Return to user's original mode
        bpy.ops.object.mode_set(mode=original_mode)
        
        self.report({'INFO'}, f"Smart UV Unwrapped with {self.island_margin} margin.")
        return {'FINISHED'}


# --- EXISTING UE5 EXPORT PIPELINE ---

class OBJECT_OT_smart_export_ue5(bpy.types.Operator):
    """Export selected meshes as UE5-ready FBX files"""
    bl_idname = "object.smart_export_ue5"
    bl_label = "Export to UE5"
    
    export_type: EnumProperty(
        name="Export Type",
        items=[('LOW', "Low Poly (_low)", ""), ('HIGH', "High Poly (_high)", "")]
    )

    @classmethod
    def poll(cls, context):
        return context.active_object is not None

    def execute(self, context):
        export_dir = context.scene.smart_export_path
        if not export_dir or not os.path.isdir(bpy.path.abspath(export_dir)):
            self.report({'ERROR'}, "Please set a valid Export Directory first.")
            return {'CANCELLED'}

        base_name = context.active_object.name
        if base_name.startswith("SM_"): base_name = base_name[3:]
        if base_name.endswith("_high"): base_name = base_name[:-5]
        if base_name.endswith("_low"): base_name = base_name[:-4]

        suffix = "_high" if self.export_type == 'HIGH' else "_low"
        final_name = f"SM_{base_name}{suffix}"
        
        filepath = os.path.join(bpy.path.abspath(export_dir), f"{final_name}.fbx")

        bpy.ops.export_scene.fbx(
            filepath=filepath,
            use_selection=True,                     
            apply_scale_options='FBX_SCALE_ALL',    
            mesh_smooth_type='FACE',                
            add_leaf_bones=False,                   
            use_armature_deform_only=True,
            bake_anim=False                         
        )

        self.report({'INFO'}, f"Exported: {final_name}.fbx")
        return {'FINISHED'}


class VIEW3D_PT_smart_tools(bpy.types.Panel):
    """Creates a Panel in the 3D Viewport Sidebar"""
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Hard Ops" 
    bl_label = "Smart Tools"

    def draw(self, context):
        layout = self.layout
        
        layout.label(text="Booleans:", icon='MOD_BOOLEAN')
        row = layout.row(align=True)
        row.scale_y = 1.5 
        row.operator(OBJECT_OT_smart_difference.bl_idname, text="Difference")
        row.operator(OBJECT_OT_smart_slice.bl_idname, text="Slice")
        
        layout.separator()
        
        layout.label(text="Shading & Edges:", icon='MOD_BEVEL')
        row2 = layout.row()
        row2.scale_y = 1.5
        row2.operator(OBJECT_OT_smart_bevel.bl_idname, text="Smart Bevel")
        
        layout.separator()
        
        layout.label(text="UV & Prep:", icon='UV')
        row3 = layout.row()
        row3.scale_y = 1.5
        row3.operator(OBJECT_OT_smart_uv.bl_idname, text="Auto UV")

        layout.separator()
        
        layout.label(text="UE5 Pipeline:", icon='EXPORT')
        layout.prop(context.scene, "smart_export_path", text="")
        
        row4 = layout.row(align=True)
        row4.scale_y = 1.5
        op_high = row4.operator(OBJECT_OT_smart_export_ue5.bl_idname, text="High Poly")
        op_high.export_type = 'HIGH'
        op_low = row4.operator(OBJECT_OT_smart_export_ue5.bl_idname, text="Low Poly")
        op_low.export_type = 'LOW'

classes = (
    OBJECT_OT_smart_difference,
    OBJECT_OT_smart_slice,
    OBJECT_OT_smart_bevel,
    OBJECT_OT_smart_uv,
    OBJECT_OT_smart_export_ue5,
    VIEW3D_PT_smart_tools,
)

def register():
    bpy.types.Scene.smart_export_path = StringProperty(
        name="Export Directory",
        description="Choose a directory to export the FBX files",
        subtype='DIR_PATH'
    )
    for cls in classes:
        bpy.utils.register_class(cls)

def unregister():
    del bpy.types.Scene.smart_export_path
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

if __name__ == "__main__":
    register()
```