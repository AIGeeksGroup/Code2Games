"""Local smoke test for the authored Racing Director vehicle.

The test deliberately uses a sloped road and a curved route so it verifies the
three failure-prone parts without needing a production scene: ground contact,
route-facing orientation, and independent wheel spin/steering animation.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import sys
from pathlib import Path

import bpy
from mathutils import Vector


def parse_args():
    values = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--vehicle", required=True)
    parser.add_argument("--director_script", required=True)
    parser.add_argument("--output_dir", required=True)
    return parser.parse_args(values)


def load_director(path):
    spec = importlib.util.spec_from_file_location("code2games_director", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def clear_scene():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for collection in list(bpy.data.collections):
        bpy.data.collections.remove(collection)


def make_sloped_road():
    mesh = bpy.data.meshes.new("C2G_RACING_QA_ROAD_MESH")
    # z = 0.055*x + 0.018*y: enough pitch and roll to reveal flat-only motion.
    vertices = [(-24.0, -24.0), (24.0, -24.0), (24.0, 24.0), (-24.0, 24.0)]
    points = [(x, y, 0.055 * x + 0.018 * y) for x, y in vertices]
    mesh.from_pydata(points, [], [(0, 1, 2, 3)])
    mesh.update()
    road = bpy.data.objects.new("C2G_RACING_QA_ROAD", mesh)
    bpy.context.scene.collection.objects.link(road)
    material = bpy.data.materials.new("C2G_RACING_QA_ROAD_MAT")
    material.diffuse_color = (0.07, 0.09, 0.11, 1.0)
    material.use_nodes = True
    principled = next(node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED")
    principled.inputs["Base Color"].default_value = (0.07, 0.09, 0.11, 1.0)
    principled.inputs["Roughness"].default_value = 0.86
    road.data.materials.append(material)
    return road


def make_beats():
    # Deliberately contains three geometric right angles.  The Director must
    # turn these control points into a driveable curve before animation.
    route = [
        (-8.0, -8.0),
        (0.0, -8.0),
        (0.0, 0.0),
        (8.0, 0.0),
        (8.0, 8.0),
    ]
    beats = []
    for index, (x, y) in enumerate(route):
        z = 0.055 * x + 0.018 * y + 0.005
        beats.append(
            {
                "frame": 1 + index * 24,
                "staged_position": [x, y, z],
                "movement_mode": "drive",
                "event_type": "traverse",
            }
        )
    return beats


def look_at(camera, target):
    camera.rotation_euler = (Vector(target) - camera.location).to_track_quat("-Z", "Y").to_euler()


def world_bounds(objects):
    points = [obj.matrix_world @ Vector(corner) for obj in objects for corner in obj.bound_box]
    return {
        "min": [min(point[i] for point in points) for i in range(3)],
        "max": [max(point[i] for point in points) for i in range(3)],
    }


def configure_render(output_dir):
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_EEVEE_NEXT"
    scene.render.resolution_x = 800
    scene.render.resolution_y = 450
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.view_settings.look = "AgX - Medium High Contrast"
    scene.view_settings.exposure = 0.25
    world = scene.world or bpy.data.worlds.new("C2G_RACING_QA_WORLD")
    scene.world = world
    world.use_nodes = True
    background = next(node for node in world.node_tree.nodes if node.type == "BACKGROUND")
    background.inputs["Color"].default_value = (0.16, 0.28, 0.48, 1.0)
    background.inputs["Strength"].default_value = 0.65
    light_data = bpy.data.lights.new("C2G_RACING_QA_SUN_DATA", "SUN")
    light_data.energy = 2.2
    light_data.angle = math.radians(12.0)
    light = bpy.data.objects.new("C2G_RACING_QA_SUN", light_data)
    bpy.context.scene.collection.objects.link(light)
    light.rotation_euler = (math.radians(28.0), math.radians(-18.0), math.radians(-35.0))
    camera_data = bpy.data.cameras.new("C2G_RACING_QA_CAMERA_DATA")
    camera = bpy.data.objects.new("C2G_RACING_QA_CAMERA", camera_data)
    bpy.context.scene.collection.objects.link(camera)
    camera_data.lens = 53.0
    scene.camera = camera
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    return camera


def main():
    args = parse_args()
    output_dir = os.path.abspath(args.output_dir)
    clear_scene()
    make_sloped_road()
    director = load_director(os.path.abspath(args.director_script))
    collection = bpy.data.collections.new("C2G_RACING_QA_DIRECTOR")
    bpy.context.scene.collection.children.link(collection)
    vehicle = director.import_racing_vehicle(collection, os.path.abspath(args.vehicle), target_length=5.2)
    source_beats = make_beats()
    beats = director.smooth_racing_beats(source_beats, director.scene_z_extent())
    director.animate_actor(vehicle, beats, "racing")
    dynamics = director.animate_racing_vehicle_dynamics(vehicle, beats)
    mounted_camera = director.create_camera(collection, beats, "racing", vehicle)
    camera = configure_render(output_dir)
    mesh_objects = director.hierarchy_mesh_objects(vehicle)
    previews = []
    bounds_by_frame = {}
    for frame, camera_offset in (
        (1, Vector((-7.5, -9.0, 4.2))),
        (49, Vector((-7.2, -8.0, 3.8))),
        (97, Vector((-6.5, -8.5, 4.5))),
    ):
        bpy.context.scene.frame_set(frame)
        bpy.context.view_layer.update()
        camera.location = vehicle.location + vehicle.rotation_quaternion @ camera_offset
        look_at(camera, vehicle.location + Vector((0.0, 0.0, 0.9)))
        path = os.path.join(output_dir, "racing_vehicle_frame_%03d.png" % frame)
        bpy.context.scene.render.filepath = path
        bpy.ops.render.render(write_still=True)
        previews.append(path)
        bounds_by_frame[str(frame)] = world_bounds(mesh_objects)
    mounted_frame = int(beats[len(beats) // 2]["frame"])
    bpy.context.scene.frame_set(mounted_frame)
    bpy.context.scene.camera = mounted_camera
    mounted_preview = os.path.join(output_dir, "racing_vehicle_center_mount.png")
    bpy.context.scene.render.filepath = mounted_preview
    bpy.ops.render.render(write_still=True)
    previews.append(mounted_preview)
    minimum_camera_clearance = float("inf")
    minimum_forward_alignment = 1.0
    for beat in beats:
        bpy.context.scene.frame_set(int(beat["frame"]))
        bpy.context.view_layer.update()
        camera_position = mounted_camera.matrix_world.translation
        camera_ground = director.terrain_height(
            camera_position.x,
            camera_position.y,
            vehicle.location.z,
            director.scene_z_extent(),
        )
        minimum_camera_clearance = min(minimum_camera_clearance, camera_position.z - camera_ground)
        camera_forward = mounted_camera.matrix_world.to_quaternion() @ Vector((0.0, 0.0, -1.0))
        vehicle_forward = vehicle.matrix_world.to_quaternion() @ Vector((0.0, 1.0, 0.0))
        minimum_forward_alignment = min(
            minimum_forward_alignment,
            camera_forward.normalized().dot(vehicle_forward.normalized()),
        )
    maximum_heading_change = director.maximum_racing_heading_change_degrees(beats)
    spin_names = str(vehicle["code2games_wheel_spin_objects"]).split(",")
    steer_names = str(vehicle["code2games_wheel_steer_objects"]).split(",")
    spin_keyframes = sum(
        len(curve.keyframe_points)
        for name in spin_names
        for curve in (bpy.data.objects[name].animation_data.action.fcurves if bpy.data.objects[name].animation_data else [])
    )
    steer_keyframes = sum(
        len(curve.keyframe_points)
        for name in steer_names
        for curve in (bpy.data.objects[name].animation_data.action.fcurves if bpy.data.objects[name].animation_data else [])
    )
    camera_mounted_in_center = (
        mounted_camera.parent == vehicle
        and abs(float(mounted_camera.location.x)) < 1e-5
        and mounted_camera.get("code2games_camera_system") == "vehicle_center_mount_predictive_grade"
    )
    report = {
        "ok": (
            dynamics.get("enabled") is True
            and spin_keyframes >= 20
            and steer_keyframes >= 10
            and maximum_heading_change < 30.0
            and camera_mounted_in_center
            and minimum_camera_clearance > 0.75
            and minimum_forward_alignment > 0.72
            and int(mounted_camera.get("code2games_camera_unresolved_occlusions", 0)) == 0
        ),
        "vehicle": os.path.abspath(args.vehicle),
        "vehicle_forward_axis": "+Y",
        "mesh_count": len(mesh_objects),
        "spin_pivots": spin_names,
        "steer_pivots": steer_names,
        "spin_keyframe_points": spin_keyframes,
        "steer_keyframe_points": steer_keyframes,
        "dynamics": dynamics,
        "path_smoothing": {
            "source_beat_count": len(source_beats),
            "smoothed_beat_count": len(beats),
            "maximum_heading_change_degrees": round(float(maximum_heading_change), 4),
            "right_angle_control_points": 3,
            "fixed_control_points_preserved": all(
                any(
                    int(sample["frame"]) == int(source["frame"])
                    and sample.get("racing_route_control_position") is not None
                    and (
                        Vector(sample["racing_route_control_position"])
                        - Vector(source["staged_position"])
                    ).length < 1e-4
                    for sample in beats
                )
                for source in source_beats
            ),
            "maximum_actor_offset_from_route_control_m": round(
                max(float(beat.get("racing_actor_offset_from_route_control_m", 0.0)) for beat in beats),
                4,
            ),
        },
        "center_mount_camera": {
            "camera_system": mounted_camera.get("code2games_camera_system"),
            "parent": mounted_camera.parent.name if mounted_camera.parent else None,
            "local_location": [round(float(value), 4) for value in mounted_camera.location],
            "lookahead_m": float(mounted_camera.get("code2games_camera_lookahead_m", 0.0)),
            "minimum_ground_clearance_m": round(float(minimum_camera_clearance), 4),
            "minimum_forward_alignment": round(float(minimum_forward_alignment), 4),
            "occlusion_corrections": int(mounted_camera.get("code2games_camera_occlusion_corrections", 0)),
            "unresolved_occlusions": int(mounted_camera.get("code2games_camera_unresolved_occlusions", 0)),
            "preview": mounted_preview,
        },
        "bounds_by_frame": bounds_by_frame,
        "previews": previews,
    }
    report_path = os.path.join(output_dir, "racing_vehicle_qa_report.json")
    Path(report_path).write_text(json.dumps(report, indent=2), encoding="utf-8")
    bpy.ops.wm.save_as_mainfile(filepath=os.path.join(output_dir, "racing_vehicle_qa.blend"))
    if not report["ok"]:
        raise RuntimeError("racing vehicle QA failed; see %s" % report_path)
    print("RACING_VEHICLE_QA_OK", report_path)


if __name__ == "__main__":
    main()
