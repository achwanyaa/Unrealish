"""Step-4 scene: a floor, a cube, sun + sky, and a viewport camera aimed at them.

Runs inside the editor (Unreal Python). Re-runnable: replaces its own actors.
"""

import unreal

LEVEL = "/Game/Maps/Sandbox"
TAG = "spawn_test"

les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)

if unreal.EditorAssetLibrary.does_asset_exist(LEVEL):
    les.load_level(LEVEL)
else:
    les.new_level(LEVEL)

for actor in eas.get_all_level_actors():
    if actor.actor_has_tag(TAG):
        eas.destroy_actor(actor)


def spawn(cls, location, rotation=None):
    actor = eas.spawn_actor_from_class(cls, location, rotation or unreal.Rotator(roll=0, pitch=0, yaw=0))
    actor.set_editor_property("tags", [TAG])
    return actor


def mesh_actor(mesh_path, location, scale):
    actor = spawn(unreal.StaticMeshActor, location)
    actor.static_mesh_component.set_static_mesh(unreal.load_asset(mesh_path))
    actor.set_actor_scale3d(scale)
    return actor


mesh_actor("/Engine/BasicShapes/Plane", unreal.Vector(0, 0, 0), unreal.Vector(20, 20, 1))
mesh_actor("/Engine/BasicShapes/Cube", unreal.Vector(0, 0, 50), unreal.Vector(1, 1, 1))
spawn(unreal.DirectionalLight, unreal.Vector(0, 0, 500), unreal.Rotator(roll=0, pitch=-40, yaw=-30))
spawn(unreal.SkyAtmosphere, unreal.Vector(0, 0, 0))
sky = spawn(unreal.SkyLight, unreal.Vector(0, 0, 300))
sky.light_component.set_editor_property("real_time_capture", True)

ues.set_level_viewport_camera_info(unreal.Vector(-350, -250, 220), unreal.Rotator(roll=0, pitch=-22, yaw=35))
les.save_current_level()
print(f"spawn_test: level {LEVEL} ready with {sum(1 for a in eas.get_all_level_actors() if a.actor_has_tag(TAG))} test actors")
