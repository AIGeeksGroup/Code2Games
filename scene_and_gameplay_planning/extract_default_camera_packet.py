import json
import math
import os
import shutil
import sys

import bpy
from mathutils import Vector

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
COMMON_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "shared_representation"))
if COMMON_DIR not in sys.path:
    sys.path.insert(0, COMMON_DIR)

from staging_paths import get_packet_dir


PROJECT_ROOT = os.path.abspath(os.getcwd())
OUTPUT_DIR = get_packet_dir(PROJECT_ROOT)
OUTPUT_VIEW = os.path.join(OUTPUT_DIR, "default_camera_view.png")
OUTPUT_GRID_VIEW = os.path.join(OUTPUT_DIR, "default_camera_view_grid.png")
OUTPUT_METADATA = os.path.join(OUTPUT_DIR, "default_camera_metadata.json")
OUTPUT_REPORT = os.path.join(OUTPUT_DIR, "default_camera_packet_report.json")

RENDER_RESOLUTION = (1280, 720)
FRAME = 1
DEFAULT_DURATION_SECONDS = 30

GRID_CELLS = [
    ("upper_left", "upper-left", 1.0 / 6.0, 5.0 / 6.0),
    ("upper_center", "upper-center", 0.5, 5.0 / 6.0),
    ("upper_right", "upper-right", 5.0 / 6.0, 5.0 / 6.0),
    ("middle_left", "middle-left", 1.0 / 6.0, 0.5),
    ("middle_center", "middle-center", 0.5, 0.5),
    ("middle_right", "middle-right", 5.0 / 6.0, 0.5),
    ("lower_left", "lower-left", 1.0 / 6.0, 1.0 / 6.0),
    ("lower_center", "lower-center", 0.5, 1.0 / 6.0),
    ("lower_right", "lower-right", 5.0 / 6.0, 1.0 / 6.0),
]


def ensure_output_dir():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    for entry in os.scandir(OUTPUT_DIR):
        if entry.is_file() and entry.name.lower().endswith((".png", ".json", ".md")):
            os.remove(entry.path)
        elif entry.is_dir():
            shutil.rmtree(entry.path)


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def current_blend_path():
    return bpy.data.filepath or "CURRENT_BLENDER_SCENE"


def validate_current_scene():
    scene = bpy.context.scene
    if not scene:
        raise RuntimeError("No active Blender scene. Run this script with blender -b target_scene.blend.")
    if not scene.camera:
        raise RuntimeError("Scene has no default camera. Run with a blend that has bpy.context.scene.camera.")


def vec_to_list(vec):
    return [round(float(vec.x), 6), round(float(vec.y), 6), round(float(vec.z), 6)]


def euler_to_list(euler):
    return [round(float(value), 6) for value in euler]


def matrix_to_list(matrix):
    return [[round(float(matrix[row][col]), 6) for col in range(4)] for row in range(4)]


def camera_world_position(camera):
    return camera.matrix_world.translation.copy()


def camera_world_rotation_euler(camera):
    return camera.matrix_world.to_euler()


def camera_world_forward(camera):
    return (camera.matrix_world.to_quaternion() @ Vector((0.0, 0.0, -1.0))).normalized()


def camera_world_right(camera):
    return (camera.matrix_world.to_quaternion() @ Vector((1.0, 0.0, 0.0))).normalized()


def camera_world_up(camera):
    return (camera.matrix_world.to_quaternion() @ Vector((0.0, 1.0, 0.0))).normalized()


def configure_render(output_path):
    scene = bpy.context.scene
    scene.frame_set(FRAME)
    scene.render.resolution_x = RENDER_RESOLUTION[0]
    scene.render.resolution_y = RENDER_RESOLUTION[1]
    scene.render.film_transparent = False
    scene.render.image_settings.file_format = "PNG"
    scene.render.filepath = output_path


def render_default_camera(camera):
    previous_camera = bpy.context.scene.camera
    try:
        bpy.context.scene.camera = camera
        configure_render(OUTPUT_VIEW)
        bpy.context.evaluated_depsgraph_get().update()
        bpy.ops.render.render(write_still=True)
    finally:
        bpy.context.scene.camera = previous_camera


def load_pil():
    try:
        from PIL import Image, ImageDraw, ImageFont

        return Image, ImageDraw, ImageFont
    except Exception as exc:
        raise RuntimeError("Pillow is required to create default_camera_view_grid.png") from exc


def get_font():
    _image, _draw, image_font = load_pil()
    try:
        return image_font.load_default()
    except Exception:
        return None


def make_grid_overlay():
    image, image_draw, _font = load_pil()
    base = image.open(OUTPUT_VIEW).convert("RGBA")
    width, height = base.size
    layer = image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = image_draw.Draw(layer)
    font = get_font()

    line = (255, 255, 255, 105)
    for x in (width / 3.0, width * 2.0 / 3.0):
        draw.line([(x, 0), (x, height)], fill=line, width=2)
    for y in (height / 3.0, height * 2.0 / 3.0):
        draw.line([(0, y), (width, y)], fill=line, width=2)

    title = "DEFAULT_CAMERA"
    draw.rectangle([12, 12, 160, 38], fill=(0, 0, 0, 135))
    draw.text((20, 20), title, fill=(255, 255, 255, 235), font=font)

    for cell_id, _area, u, v in GRID_CELLS:
        x = int(u * width)
        y = int((1.0 - v) * height)
        label = cell_id
        box_w = max(92, len(label) * 7 + 14)
        draw.rectangle([x - box_w // 2, y - 11, x + box_w // 2, y + 11], fill=(0, 0, 0, 85))
        draw.text((x - box_w // 2 + 7, y - 7), label, fill=(255, 255, 255, 210), font=font)

    image.alpha_composite(base, layer).convert("RGB").save(OUTPUT_GRID_VIEW)


def camera_fov_degrees(camera, scene):
    try:
        fov_x = math.degrees(camera.data.angle_x)
        fov_y = math.degrees(camera.data.angle_y)
        return round(fov_x, 6), round(fov_y, 6)
    except Exception:
        aspect = scene.render.resolution_x / max(scene.render.resolution_y, 1)
        fov_x = math.degrees(camera.data.angle)
        fov_y = math.degrees(2.0 * math.atan(math.tan(camera.data.angle / 2.0) / aspect))
        return round(fov_x, 6), round(fov_y, 6)


def ray_for_screen_point(camera, scene, u, v):
    frame = camera.data.view_frame(scene=scene)
    bottom_left, bottom_right, top_right, top_left = frame
    bottom = bottom_left.lerp(bottom_right, u)
    top = top_left.lerp(top_right, u)
    local_point = bottom.lerp(top, v)
    world_forward = camera_world_forward(camera)

    if camera.data.type == "ORTHO":
        origin = camera.matrix_world @ local_point
        direction = world_forward
    else:
        origin = camera.matrix_world.translation
        world_point = camera.matrix_world @ local_point
        direction = (world_point - origin).normalized()
    return origin, direction


def material_names(obj):
    if not obj or not getattr(obj, "material_slots", None):
        return []
    names = []
    for slot in obj.material_slots:
        if slot.material:
            names.append(slot.material.name.lower())
    return names


def classify_surface(obj, normal):
    text = " ".join([obj.name.lower() if obj else "", *material_names(obj)])
    if any(token in text for token in ["water", "river", "lake", "ocean", "sea"]):
        return "water"
    if any(token in text for token in ["leaf", "tree", "grass", "plant", "bush", "vegetation"]):
        return "vegetation"
    if any(token in text for token in ["rock", "stone", "cliff", "boulder", "mountain"]):
        return "rock"
    if any(token in text for token in ["ground", "terrain", "floor", "sand", "soil", "dirt", "path"]):
        return "ground"
    if normal and normal.z < 0.45:
        return "slope"
    if normal and normal.z > 0.65:
        return "ground"
    return "unknown"


def action_for_surface(surface_type, normal):
    if surface_type == "ground":
        if normal and normal.z < 0.72:
            return "medium", "slow_down"
        return "good", "run"
    if surface_type == "rock":
        if normal and normal.z > 0.7:
            return "medium", "jump"
        return "poor", "avoid"
    if surface_type == "slope":
        return "poor", "slow_down"
    if surface_type == "water":
        return "poor", "avoid"
    if surface_type == "vegetation":
        return "medium", "dodge"
    return "unknown", "unknown"


def raycast_grid(camera):
    scene = bpy.context.scene
    depsgraph = bpy.context.evaluated_depsgraph_get()
    max_distance = max(float(camera.data.clip_end), 1000.0)
    cells = []
    for cell_id, screen_area, u, v in GRID_CELLS:
        origin, direction = ray_for_screen_point(camera, scene, u, v)
        hit, location, normal, _face_index, obj, _matrix = scene.ray_cast(
            depsgraph, origin, direction, distance=max_distance
        )
        surface_type = "unknown"
        walkability = "unknown"
        action = "unknown"
        if hit:
            surface_type = classify_surface(obj, normal)
            walkability, action = action_for_surface(surface_type, normal)
        cells.append(
            {
                "cell_id": cell_id,
                "screen_area": screen_area,
                "ray_hit": bool(hit),
                "world_position": vec_to_list(location) if hit else None,
                "hit_object": obj.name if hit and obj else None,
                "surface_type": surface_type,
                "walkability_hint": walkability,
                "recommended_action_hint": action,
            }
        )
    return cells


def local_scene_hints(cells, camera):
    hit_cells = [cell for cell in cells if cell["ray_hit"]]
    ground_like = [cell for cell in hit_cells if cell["walkability_hint"] in {"good", "medium"}]
    obstacle_like = [
        cell
        for cell in hit_cells
        if cell["walkability_hint"] == "poor" or cell["surface_type"] in {"water", "rock", "slope", "vegetation"}
    ]
    lower = [cell for cell in cells if cell["cell_id"].startswith("lower_")]
    middle_upper = [cell for cell in cells if cell["cell_id"].startswith("middle_") or cell["cell_id"].startswith("upper_")]
    foreground_walkable = any(cell["walkability_hint"] in {"good", "medium"} for cell in lower)
    midground_extension = any(cell["walkability_hint"] in {"good", "medium", "unknown"} for cell in middle_upper)
    world_forward = camera_world_forward(camera)
    horizontal_forward = Vector((world_forward.x, world_forward.y, 0.0))
    if horizontal_forward.length <= 1e-6:
        horizontal_forward = Vector((0.0, 0.0, 0.0))
    else:
        horizontal_forward.normalize()
    total = max(len(cells), 1)
    return {
        "visible_ground_ratio_hint": round(len(ground_like) / total, 3),
        "visible_obstacle_ratio_hint": round(len(obstacle_like) / total, 3),
        "foreground_walkable_hint": bool(foreground_walkable),
        "midground_extension_hint": bool(midground_extension),
        "suggested_forward_world_direction": vec_to_list(horizontal_forward),
    }


def build_metadata(camera, cells, duration_seconds):
    scene = bpy.context.scene
    fov_x, fov_y = camera_fov_degrees(camera, scene)
    world_forward = camera_world_forward(camera)
    world_right = camera_world_right(camera)
    world_up = camera_world_up(camera)
    return {
        "source": {
            "blend": current_blend_path(),
            "selected_view": "DEFAULT_CAMERA",
            "image": OUTPUT_GRID_VIEW,
        },
        "render": {
            "resolution": [scene.render.resolution_x, scene.render.resolution_y],
            "engine": scene.render.engine,
            "frame": FRAME,
        },
        "camera": {
            "name": camera.name,
            "world_position": vec_to_list(camera_world_position(camera)),
            "world_rotation_euler": euler_to_list(camera_world_rotation_euler(camera)),
            "world_forward": vec_to_list(world_forward),
            "world_right": vec_to_list(world_right),
            "world_up": vec_to_list(world_up),
            "matrix_world": matrix_to_list(camera.matrix_world),
            "local_position": vec_to_list(camera.location),
            "local_rotation_euler": euler_to_list(camera.rotation_euler),
            "lens": round(float(camera.data.lens), 6),
            "fov_x_deg": fov_x,
            "fov_y_deg": fov_y,
            "clip_start": round(float(camera.data.clip_start), 6),
            "clip_end": round(float(camera.data.clip_end), 6),
            "sensor_width": round(float(camera.data.sensor_width), 6),
        },
        "gameplay_context": {
            "duration_seconds": duration_seconds,
            "has_fixed_goal": False,
            "npc_is_main_subject": True,
            "camera_should_follow_npc": True,
            "intended_style": "third-person forward running gameplay video",
        },
        "screen_grid": {
            "grid": "3x3",
            "cells": cells,
        },
        "local_scene_hints": local_scene_hints(cells, camera),
    }


def main():
    ensure_output_dir()
    report = {
        "ok": False,
        "source_blend": current_blend_path(),
        "output_dir": OUTPUT_DIR,
    }
    try:
        validate_current_scene()
        camera = bpy.context.scene.camera
        render_default_camera(camera)
        make_grid_overlay()
        cells = raycast_grid(camera)
        metadata = build_metadata(camera, cells, DEFAULT_DURATION_SECONDS)
        write_json(OUTPUT_METADATA, metadata)
        report.update(
            {
                "ok": True,
                "default_camera": camera.name,
                "default_camera_view": OUTPUT_VIEW,
                "default_camera_view_grid": OUTPUT_GRID_VIEW,
                "metadata": OUTPUT_METADATA,
                "raycast_cell_count": len(cells),
            }
        )
        print("DEFAULT_CAMERA_PACKET_DONE", True)
        print("DEFAULT_CAMERA_VIEW", OUTPUT_VIEW)
        print("DEFAULT_CAMERA_VIEW_GRID", OUTPUT_GRID_VIEW)
        print("DEFAULT_CAMERA_METADATA", OUTPUT_METADATA)
    except Exception as exc:
        report.update({"ok": False, "error": str(exc)})
        write_json(OUTPUT_METADATA, report)
        print("DEFAULT_CAMERA_PACKET_ERROR", exc)
        raise
    finally:
        write_json(OUTPUT_REPORT, report)
        print("DEFAULT_CAMERA_PACKET_REPORT", OUTPUT_REPORT)


if __name__ == "__main__":
    main()
