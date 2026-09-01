#life 

---
To build an efficient hard-surface addon without drowning in development overhead, focus on four functional pillars: **Non-Destructive Booleans**, **Smart Modifier Management**, **Mechanical Detailing**, and **Engine-Ready Pipeline Prep**.

### 1. Boolean & Cutter Engine (Core Geometry Operations)

Automate repetitive boolean setups into single-shortcut modal operators:

- **Smart Difference / Union / Slice:**
    
    - _Difference:_ Cuts the active object with the selected object and automatically moves the cutter mesh into a dedicated, hidden `_Cutters` collection.
        
    - _Slice:_ Cuts the base mesh, duplicates the intersection, and creates a separate, detached sub-part (for lids, panels, and battery compartments).
        
    - _Union:_ Joins shapes while automatically cleaning up redundant coplanar interior faces.
        
- **Cutter Visibility Toggle:** A global hotkey to cycle cutter meshes between Wireframe, Solid, and Hidden without opening the Outliner.
    
- **Auto-Origin & Transform Alignment:** Centers the 3D cursor or cutter origin to selected faces/normals with one click for placing circular booleans, screw holes, or vent cutouts.
    

### 2. Automated Modifier Stack (Shading & Edges)

Hard-surface models rely heavily on precise bevel and normal data. This system eliminates manual modifier stacking:

- **One-Click Bevel Weighting:**
    
    - Applies a `Bevel` modifier configured to **Weight** or **Angle (30°)**.
        
    - Adds an automatic `Weighted Normal` modifier with **Keep Sharp** enabled to ensure flat shaded surfaces remain artifact-free without complex support loops.
        
- **Global Bevel Resolution Switch:** A global slider or hotkey that adjusts the segment count of all bevel modifiers in the scene simultaneously (low segments for blockout performance, high segments for high-poly baking).
    
- **Auto-Mark Sharp:** Automatically marks sharp edges on all boolean boundaries to prevent normal stretching.
    

### 3. Mechanical Detailing Operators (Fast Prototyping)

These macros directly speed up the creation of tactical assets (ribs on Pelican cases, knurling on vise handles, vents on generators):

- **Panel Line Generator:** Turns an active edge loop or curve into an inset groove with adjustable width, depth, and bevel directly via mouse scroll.
    
- **Radial & Linear Array Helper:** Instant circular duplication around a selected vertex/normal (ideal for bolt patterns, revolver cylinders, and dial notches) with non-destructive count adjustments.
    
- **Auto-Cable / Tube Extruder:** Generates procedural hanging cables or coiled cords between two selected vertices with curve resolution and sag controls.
    

### 4. Game Engine Prep & Export (The UE5 Bridge)

This is the unique selling proposition that sets your tool apart from standard hard-surface addons:

- **Non-Destructive Mesh Cleanup:**
    
    - A pre-export check operator that scans for non-manifold geometry, loose vertices, and ngons on flat vs. curved surfaces.
        
    - Automated triangulation modifier application for game-ready low polys (`Quad Method: Shortest Diagonal`).
        
- **Prefix / Suffix Auto-Namer:** Auto-renames selected meshes based on Epic Games standards (`SM_[ObjectName]`, `UCX_[ObjectName]` for custom collision hulls).
    
- **Direct UE5 Nanite / LOD Exporter:**
    
    - Exports FBX at scale 1.0 (avoiding the 100x scale bug in UE5).
        
    - Places the mesh origin at world zero or at the bottom bounding box center.
        
    - Exports high-poly and low-poly FBX pairs named `_high` and `_low` directly to a target project folder for baking in Substance 3D Painter or Marmoset Toolbag.
        

### Recommended UI / Access Model

- **Viewport Pie Menu (`D` or `Shift + Q`):** Houses the 5 to 7 most common operators (Difference, Slice, Bevel Setup, Cutter Toggle) to keep the cursor in the modeling viewport.
    
- **N-Panel Sidebar:** Houses the configuration sliders (bevel segments, array counts, naming convention fields, and export paths).