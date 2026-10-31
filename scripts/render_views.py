"""Render a fixed set of views of the imported model to PNGs.

Runs inside the editor (Unreal Python):  render_views.py <out_dir> [width] [height]

Views: four aerial obliques (one per corner), an orthographic site plan, and a street-level
view at 1.7 m eye height. Uses SceneCapture2D, so it works headless (-RenderOffScreen).
Frames the actors tagged by import_glb.py.
"""

import glob
import json
import math
import os
import sys

import unreal

out_dir = sys.argv[1]
width = int(sys.argv[2]) if len(sys.argv) > 2 else 1920
height = int(sys.argv[3]) if len(sys.argv) > 3 else 1080
TAG = "imported_model"
EV100 = 14.5  # fixed exposure for the SUN_LUX daylight set in import_glb.py
FOV = 50.0

ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
world = ues.get_editor_world()

actors = [a for a in eas.get_all_level_actors() if a.actor_has_tag(TAG)]
if not actors:
    raise RuntimeError("No imported model in this level. Run import_glb.py first.")
lo = [float("inf")] * 3
hi = [float("-inf")] * 3
for actor in actors:
    origin, extent = actor.get_actor_bounds(False)
    for i, (o, e) in enumerate(((origin.x, extent.x), (origin.y, extent.y), (origin.z, extent.z))):
        lo[i] = min(lo[i], o - e)
        hi[i] = max(hi[i], o + e)
cx, cy, cz = ((l + h) / 2 for l, h in zip(lo, hi))
ex, ey, ez = ((h - l) / 2 for l, h in zip(lo, hi))


def capture(name, location, rotation, fov=FOV, ortho_width=None):
    rt = unreal.RenderingLibrary.create_render_target2d(world, width, height, unreal.TextureRenderTargetFormat.RTF_RGBA8)
    cap = eas.spawn_actor_from_class(unreal.SceneCapture2D, location, rotation)
    try:
        comp = cap.capture_component2d
        comp.set_editor_property("texture_target", rt)
        comp.set_editor_property("capture_source", unreal.SceneCaptureSource.SCS_FINAL_COLOR_LDR)
        comp.set_editor_property("capture_every_frame", False)
        comp.set_editor_property("capture_on_movement", False)
        comp.set_editor_property("fov_angle", fov)
        if ortho_width:
            comp.set_editor_property("projection_type", unreal.CameraProjectionMode.ORTHOGRAPHIC)
            comp.set_editor_property("ortho_width", ortho_width)
        # One-shot captures have no exposure history, so pin exposure instead of auto-adapting.
        pps = comp.get_editor_property("post_process_settings")
        pps.set_editor_property("override_auto_exposure_method", True)
        pps.set_editor_property("auto_exposure_method", unreal.AutoExposureMethod.AEM_HISTOGRAM)
        pps.set_editor_property("override_auto_exposure_min_brightness", True)
        pps.set_editor_property("auto_exposure_min_brightness", EV100)
        pps.set_editor_property("override_auto_exposure_max_brightness", True)
        pps.set_editor_property("auto_exposure_max_brightness", EV100)
        comp.set_editor_property("post_process_settings", pps)
        comp.set_editor_property("post_process_blend_weight", 1.0)
        comp.capture_scene()

        unreal.RenderingLibrary.export_render_target(world, rt, out_dir, name)
        written = sorted(glob.glob(os.path.join(out_dir, name + "*")), key=os.path.getmtime)
        if not written:
            raise RuntimeError(f"export_render_target wrote nothing for {name}")
        target = os.path.join(out_dir, name + ".png")
        if written[-1] != target:
            os.replace(written[-1], target)
        return target
    finally:
        eas.destroy_actor(cap)


os.makedirs(out_dir, exist_ok=True)
shots = []

# Aerial obliques: back off far enough that a sphere around the site fits the field of view.
pitch = 30.0
dist = math.sqrt(ex * ex + ey * ey + ez * ez) / math.sin(math.radians(FOV / 2)) * 1.05
for label, sx, sy in (("aerial_ne", 1, 1), ("aerial_se", 1, -1), ("aerial_sw", -1, -1), ("aerial_nw", -1, 1)):
    horiz = dist * math.cos(math.radians(pitch))
    dx, dy = sx / math.sqrt(2), sy / math.sqrt(2)
    loc = unreal.Vector(cx + dx * horiz, cy + dy * horiz, cz + dist * math.sin(math.radians(pitch)))
    yaw = math.degrees(math.atan2(-dy, -dx))
    shots.append(capture(label, loc, unreal.Rotator(roll=0, pitch=-pitch, yaw=yaw)))

# Site plan: orthographic, straight down, X across the frame.
plan_width = max(2 * ex, 2 * ey * width / height) * 1.08
shots.append(capture("plan", unreal.Vector(cx, cy, hi[2] + 100000), unreal.Rotator(roll=0, pitch=-90, yaw=-90), ortho_width=plan_width))

# Street level: 1.7 m eye height, 30 m outside the southern (min Y) edge, looking across the site.
shots.append(capture("street", unreal.Vector(cx, lo[1] - 3000, lo[2] + 170), unreal.Rotator(roll=0, pitch=2, yaw=90), fov=75.0))

print(json.dumps({"out_dir": out_dir, "views": [os.path.basename(s) for s in shots], "size_m": [round(2 * v / 100, 1) for v in (ex, ey, ez)]}))
