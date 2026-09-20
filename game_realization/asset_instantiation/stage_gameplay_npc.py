"""Import and place the gameplay NPC in the realized Blender world.

This is part of gaming-world realization.  It deliberately does not create a
route, animate world-space locomotion, create a camera, or render a video.
Those presentation-only operations live in ``visual_showcase``.
"""
import argparse
import json
import math
import os
import sys
import traceback

import bpy
from mathutils import Vector


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
COMMON_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", "shared_representation"))
if COMMON_DIR not in sys.path:
    sys.path.insert(0, COMMON_DIR)

from staging_paths import get_demo_name, get_staging_root


PROJECT_ROOT = os.path.abspath(os.environ.get("CODE2WORLDS_ROOT") or os.path.join(SCRIPT_DIR, "..", ".."))
STAGING_ROOT = get_staging_root(PROJECT_ROOT)
DEFAULT_SCENE = os.path.join(STAGING_ROOT, "staged_scene.blend")
DEFAULT_NPC_FBX = os.path.join(PROJECT_ROOT, "assets", "mixamo", "mixamo_run.fbx")
DEFAULT_REPORT = os.path.join(STAGING_ROOT, "npc_staging", "npc_staging_report.json")
NPC_COLLECTION = "Code2Games_Gameplay_NPC"
NPC_ROOT_MARKER = "code2games_gameplay_npc_root"
SPAWN_PRIORITY = ("player_spawn", "npc_spawn", "navigation_anchor", "interaction_point")


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser(description="Place an animated NPC in staged_scene.blend")
    parser.add_argument("--scene_blend", default=DEFAULT_SCENE)
    parser.add_argument("--output_blend", default=DEFAULT_SCENE)
    parser.add_argument("--npc_fbx", default=DEFAULT_NPC_FBX)
    parser.add_argument("--npc_height_m", type=float, default=1.7)
    parser.add_argument("--npc_yaw_offset_degrees", type=float, default=90.0)
    parser.add_argument("--ground_clearance_m", type=float, default=0.01)
    parser.add_argument(
        "--spawn_element_type",
        default="auto",
        help="auto, player_spawn, npc_spawn, navigation_anchor, or interaction_point",
    )
    parser.add_argument("--report", default=DEFAULT_REPORT)
    return parser.parse_args(argv)


def project_path(value):
    value = os.path.expandvars(os.path.expanduser(str(value)))
    return os.path.normpath(value if os.path.isabs(value) else os.path.join(PROJECT_ROOT, value))


def write_json(path, value):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, path)


def open_scene(path):
    current = os.path.normcase(os.path.abspath(bpy.data.filepath)) if bpy.data.filepath else ""
    target = os.path.normcase(os.path.abspath(path))
    if current != target:
        if not os.path.isfile(path):
            raise FileNotFoundError("staged gaming world not found: " + path)
        bpy.ops.wm.open_mainfile(filepath=path)


def reset_npc_collection():
    collection = bpy.data.collections.get(NPC_COLLECTION)
    if collection is None:
        collection = bpy.data.collections.new(NPC_COLLECTION)
        bpy.context.scene.collection.children.link(collection)
        return collection
    for obj in list(collection.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    return collection


def link_only_to_collection(obj, collection):
    if obj.name not in collection.objects:
        collection.objects.link(obj)
    for existing in list(obj.users_collection):
        if existing != collection:
            existing.objects.unlink(obj)


def object_element_type(obj):
    return str(obj.get("code2games_element_type") or "").strip()


def select_spawn_anchor(requested_type):
    requested_type = str(requested_type or "auto").strip()
    priorities = SPAWN_PRIORITY if requested_type == "auto" else (requested_type,)
    candidates = [obj for obj in bpy.context.scene.objects if object_element_type(obj)]
    for element_type in priorities:
        matches = sorted(
            (obj for obj in candidates if object_element_type(obj) == element_type),
            key=lambda obj: obj.name,
        )
        if matches:
            return matches[0], element_type
    raise RuntimeError(
        "no compatible NPC spawn anchor exists in staged_scene.blend; "
        "expected one of: " + ", ".join(priorities)
    )


def mesh_world_bounds(meshes):
    points = [
        obj.matrix_world @ Vector(corner)
        for obj in meshes
        for corner in obj.bound_box
    ]
    if not points:
        zero = Vector((0.0, 0.0, 0.0))
        return zero, zero, zero
    minimum = Vector(tuple(min(point[index] for point in points) for index in range(3)))
    maximum = Vector(tuple(max(point[index] for point in points) for index in range(3)))
    return minimum, maximum, maximum - minimum


def imported_top_level_objects(imported):
    imported_set = set(imported)
    return [obj for obj in imported if obj.parent not in imported_set]


def make_action_cyclic(obj):
    animation = obj.animation_data
    action = animation.action if animation else None
    if action is None:
        return
    for curve in action.fcurves:
        cycles = curve.modifiers.new(type="CYCLES")
        cycles.mode_before = "REPEAT"
        cycles.mode_after = "REPEAT"


def import_animated_humanoid(collection, fbx_path, target_height, spawn_position):
    if not os.path.isfile(fbx_path):
        raise FileNotFoundError("animated NPC FBX not found: " + fbx_path)
    before = set(bpy.data.objects)
    bpy.ops.import_scene.fbx(filepath=fbx_path)
    imported = [obj for obj in bpy.data.objects if obj not in before]
    armatures = [obj for obj in imported if obj.type == "ARMATURE"]
    meshes = [obj for obj in imported if obj.type == "MESH"]
    if not armatures:
        raise RuntimeError("NPC FBX contains no armature: " + fbx_path)
    if not meshes:
        raise RuntimeError("NPC FBX contains no skinned mesh: " + fbx_path)

    root = bpy.data.objects.new("Code2Games_Gameplay_NPC_Root", None)
    root.empty_display_type = "PLAIN_AXES"
    root.empty_display_size = 0.6
    collection.objects.link(root)
    for obj in imported:
        link_only_to_collection(obj, collection)

    armature = armatures[0]
    armature.name = "Code2Games_Gameplay_NPC_Armature"
    for index, mesh in enumerate(meshes, start=1):
        mesh.name = "Code2Games_Gameplay_NPC_Mesh_%02d" % index

    bpy.context.view_layer.update()
    _minimum, _maximum, size = mesh_world_bounds(meshes)
    if size.z <= 1e-5:
        raise RuntimeError("NPC FBX has a degenerate mesh height")
    scale_factor = float(target_height) / float(size.z)
    top_level = imported_top_level_objects(imported)
    for obj in top_level:
        obj.scale = obj.scale * scale_factor
    bpy.context.view_layer.update()

    minimum, maximum, _size = mesh_world_bounds(meshes)
    foot_center = Vector(((minimum.x + maximum.x) * 0.5, (minimum.y + maximum.y) * 0.5, minimum.z))
    for obj in top_level:
        matrix = obj.matrix_world.copy()
        matrix.translation -= foot_center
        obj.matrix_world = matrix
    bpy.context.view_layer.update()

    for obj in top_level:
        matrix = obj.matrix_world.copy()
        obj.parent = root
        obj.matrix_world = matrix
    root.location = spawn_position
    for obj in armatures:
        make_action_cyclic(obj)

    root[NPC_ROOT_MARKER] = True
    root["source_fbx"] = fbx_path
    root["target_height_m"] = float(target_height)
    root["mixamo_armature"] = armature.name
    root["mixamo_mesh_count"] = len(meshes)
    return root, meshes, armature


def main():
    args = parse_args()
    scene_blend = project_path(args.scene_blend)
    output_blend = project_path(args.output_blend)
    npc_fbx = project_path(args.npc_fbx)
    report_path = project_path(args.report)
    if args.npc_height_m <= 0.0:
        raise ValueError("npc_height_m must be positive")
    if args.ground_clearance_m < 0.0:
        raise ValueError("ground_clearance_m may not be negative")

    report = {
        "ok": False,
        "demo_name": get_demo_name() or None,
        "source_blend": scene_blend,
        "output_blend": output_blend,
        "npc_fbx": npc_fbx,
    }
    error = None
    try:
        open_scene(scene_blend)
        original_fps = int(bpy.context.scene.render.fps)
        anchor, element_type = select_spawn_anchor(args.spawn_element_type)
        spawn_position = anchor.matrix_world.translation.copy()
        spawn_position.z += float(args.ground_clearance_m)
        collection = reset_npc_collection()
        root, meshes, armature = import_animated_humanoid(
            collection, npc_fbx, args.npc_height_m, spawn_position
        )
        bpy.context.scene.render.fps = original_fps
        root.rotation_euler = (0.0, 0.0, math.radians(float(args.npc_yaw_offset_degrees)))
        root["code2games_spawn_anchor"] = anchor.name
        root["code2games_spawn_element_type"] = element_type
        root["code2games_npc_yaw_offset_degrees"] = float(args.npc_yaw_offset_degrees)
        root["code2games_ground_clearance_m"] = float(args.ground_clearance_m)
        try:
            bpy.ops.file.pack_all()
        except Exception as exc:
            report["pack_warning"] = str(exc)
        os.makedirs(os.path.dirname(output_blend), exist_ok=True)
        bpy.ops.wm.save_as_mainfile(filepath=output_blend)
        report.update({
            "ok": True,
            "npc_root": root.name,
            "npc_armature": armature.name,
            "npc_meshes": [mesh.name for mesh in meshes],
            "npc_height_m": float(args.npc_height_m),
            "spawn_anchor": anchor.name,
            "spawn_element_type": element_type,
            "spawn_position": [round(float(value), 6) for value in root.location],
        })
        print("GAMEPLAY_NPC_ROOT", root.name)
        print("GAMEPLAY_NPC_SPAWN", anchor.name, element_type)
        print("GAMING_WORLD_WITH_NPC", output_blend)
    except Exception as exc:
        error = exc
        report.update({"error": str(exc), "traceback": traceback.format_exc()})
    finally:
        write_json(report_path, report)
        print("GAMEPLAY_NPC_REPORT", report_path)
    if error is not None:
        raise error


if __name__ == "__main__":
    main()
