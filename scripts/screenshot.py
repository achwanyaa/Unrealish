"""Render the level-viewport camera to a PNG.

Runs inside the editor (Unreal Python):  screenshot.py <out.png> [width] [height]

Uses a temporary SceneCapture2D + render target instead of the editor viewport,
so it works with -RenderOffScreen (there is no on-screen viewport to grab).
"""

import glob
import json
import os
import sys

import unreal

out = sys.argv[1]
width = int(sys.argv[2]) if len(sys.argv) > 2 else 1280
height = int(sys.argv[3]) if len(sys.argv) > 3 else 720

ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
world = ues.get_editor_world()

camera = ues.get_level_viewport_camera_info()
location, rotation = camera if camera else (unreal.Vector(-350, -250, 220), unreal.Rotator(roll=0, pitch=-22, yaw=35))

rt = unreal.RenderingLibrary.create_render_target2d(world, width, height, unreal.TextureRenderTargetFormat.RTF_RGBA8)
capture = eas.spawn_actor_from_class(unreal.SceneCapture2D, location, rotation)
try:
    comp = capture.capture_component2d
    comp.set_editor_property("texture_target", rt)
    comp.set_editor_property("capture_source", unreal.SceneCaptureSource.SCS_FINAL_COLOR_LDR)
    comp.set_editor_property("capture_every_frame", False)
    comp.set_editor_property("capture_on_movement", False)
    comp.set_editor_property("fov_angle", 90.0)
    comp.capture_scene()

    out_dir, name = os.path.split(out)
    stem = os.path.splitext(name)[0]
    unreal.RenderingLibrary.export_render_target(world, rt, out_dir, stem)
    # The exporter picks the extension from the target format; normalise to the requested path.
    written = sorted(glob.glob(os.path.join(out_dir, stem + "*")), key=os.path.getmtime)
    if not written:
        raise RuntimeError(f"export_render_target wrote nothing to {out_dir}")
    if written[-1] != out:
        os.replace(written[-1], out)
finally:
    eas.destroy_actor(capture)

print(json.dumps({"path": out, "width": width, "height": height, "bytes": os.path.getsize(out)}))
