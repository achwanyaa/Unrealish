"""Import a .glb into its own level, with sun, sky, fog and a ground plane.

Runs inside the editor (Unreal Python):  import_glb.py <file.glb> <content_dir> <level_path>

Exports from Archicad (via three.js) keep every vertex in world space with identity node
transforms, so each imported mesh is placed at the origin and the site lines up as drawn.
Re-runnable: replaces the actors it placed before.
"""

import json
import sys

import unreal

glb, content_dir, level = sys.argv[1:4]
TAG = "imported_model"
ENV_TAG = "model_env"
SUN_LUX = 75000.0  # clear-day sun; render_views.py pins exposure to match

les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)

if unreal.EditorAssetLibrary.does_asset_exist(level):
    les.load_level(level)
else:
    les.new_level(level)

for actor in eas.get_all_level_actors():
    if actor.actor_has_tag(TAG) or actor.actor_has_tag(ENV_TAG):
        eas.destroy_actor(actor)

task = unreal.AssetImportTask()
task.set_editor_property("filename", glb)
task.set_editor_property("destination_path", content_dir)
task.set_editor_property("automated", True)
task.set_editor_property("replace_existing", True)
task.set_editor_property("save", True)
unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])

paths = list(task.get_editor_property("imported_object_paths")) or list(
    unreal.EditorAssetLibrary.list_assets(content_dir, True)
)
meshes = [m for m in (unreal.load_asset(p) for p in paths) if isinstance(m, unreal.StaticMesh)]
if not meshes:
    raise RuntimeError(f"No static meshes imported from {glb} (got: {paths[:10]})")

lo = [float("inf")] * 3
hi = [float("-inf")] * 3
for mesh in meshes:
    actor = eas.spawn_actor_from_object(mesh, unreal.Vector(0, 0, 0))
    actor.set_editor_property("tags", [TAG])
    actor.set_folder_path("Model")
    origin, extent = actor.get_actor_bounds(False)
    for i, (o, e) in enumerate(((origin.x, extent.x), (origin.y, extent.y), (origin.z, extent.z))):
        lo[i] = min(lo[i], o - e)
        hi[i] = max(hi[i], o + e)


def env(cls, location, rotation=None):
    actor = eas.spawn_actor_from_class(cls, location, rotation or unreal.Rotator(roll=0, pitch=0, yaw=0))
    actor.set_editor_property("tags", [ENV_TAG])
    actor.set_folder_path("Environment")
    return actor


center = unreal.Vector((lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, lo[2])
sun = env(unreal.DirectionalLight, center, unreal.Rotator(roll=0, pitch=-38, yaw=135))
sun.light_component.set_intensity(SUN_LUX)
env(unreal.SkyAtmosphere, unreal.Vector(0, 0, 0))
sky = env(unreal.SkyLight, center)
sky.light_component.set_editor_property("real_time_capture", True)
fog = env(unreal.ExponentialHeightFog, center)
fog.component.set_editor_property("fog_density", 0.002)

# Ground: an engine plane (1 m square) scaled to three times the site, just below the lowest slab.
ground = env(unreal.StaticMeshActor, unreal.Vector(center.x, center.y, lo[2] - 5))
ground.static_mesh_component.set_static_mesh(unreal.load_asset("/Engine/BasicShapes/Plane"))
ground.static_mesh_component.set_material(0, unreal.load_asset("/Engine/BasicShapes/BasicShapeMaterial"))
ground.set_actor_scale3d(unreal.Vector((hi[0] - lo[0]) * 3 / 100, (hi[1] - lo[1]) * 3 / 100, 1))

les.save_current_level()
unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, True)

print(json.dumps({
    "level": level,
    "meshes": len(meshes),
    "size_m": [round((h - l) / 100, 1) for l, h in zip(lo, hi)],
    "bounds_min_cm": [round(v) for v in lo],
    "bounds_max_cm": [round(v) for v in hi],
}))
