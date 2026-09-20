"""Minimal visual/structural QA for the reusable Director SWAT actor."""

import importlib.util
import json
import math
import sys
from pathlib import Path

import bpy
from mathutils import Vector


ROOT = Path(__file__).resolve().parents[2]
STAGE_PATH = Path(__file__).with_name("stage_genre_director_gameplay.py")
LIBRARY = ROOT / "assets" / "gameplay" / "swat_combat" / "final" / "swat_combat_library.blend"
OUTPUT = ROOT / "output" / "local_qa" / "swat_director_actor"


def load_stage():
    spec = importlib.util.spec_from_file_location("c2g_director_stage", STAGE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def look_at(obj, target):
    obj.rotation_euler = (Vector(target) - obj.location).to_track_quat("-Z", "Y").to_euler()


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    stage = load_stage()
    collection = bpy.data.collections.new(stage.COLLECTION_NAME)
    bpy.context.scene.collection.children.link(collection)
    actor = stage.append_swat_actor(collection, str(LIBRARY), "QA_SWAT", 1.80)
    installed, missing = stage.install_enemy_action_sequence(
        actor,
        [("fire", 1, 40)],
    )
    actor["root"].location = (0.0, 0.0, 0.0)

    bpy.ops.mesh.primitive_plane_add(size=18.0)
    plane = bpy.context.object
    plane.data.materials.append(stage.material("QA_GROUND", (0.08, 0.09, 0.11, 1.0), roughness=0.9))

    world = bpy.context.scene.world or bpy.data.worlds.new("QA_WORLD")
    bpy.context.scene.world = world
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.025, 0.035, 0.055, 1.0)
    world.node_tree.nodes["Background"].inputs["Strength"].default_value = 0.28
    for name, location, energy, size in (
        ("KEY", (4.0, -4.0, 6.0), 1300.0, 4.0),
        ("FILL", (-3.0, -2.0, 3.0), 850.0, 3.0),
    ):
        data = bpy.data.lights.new(name + "_DATA", "AREA")
        data.energy = energy
        data.size = size
        light = bpy.data.objects.new(name, data)
        bpy.context.scene.collection.objects.link(light)
        light.location = location
        look_at(light, (0.0, 0.0, 1.0))

    camera_data = bpy.data.cameras.new("QA_CAMERA_DATA")
    camera = bpy.data.objects.new("QA_CAMERA", camera_data)
    bpy.context.scene.collection.objects.link(camera)
    camera.location = (4.5, -5.8, 2.35)
    look_at(camera, (0.0, 0.0, 1.05))
    camera_data.lens = 58.0
    bpy.context.scene.camera = camera

    scene = bpy.context.scene
    scene.frame_start = 1
    scene.frame_end = 40
    scene.frame_set(20)
    scene.render.engine = "BLENDER_EEVEE_NEXT"
    scene.render.resolution_x = 640
    scene.render.resolution_y = 640
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    OUTPUT.mkdir(parents=True, exist_ok=True)
    scene.render.filepath = str(OUTPUT / "swat_nla_fire.png")
    bpy.ops.render.render(write_still=True)

    meshes = [obj for obj in actor["objects"] if obj.type == "MESH"]
    bounds = stage.mesh_bounds(meshes)
    report = {
        "ok": not missing,
        "installed": installed,
        "missing": missing,
        "action_map": {role: action.name for role, action in actor["actions"].items()},
        "root_rotation": list(actor["root"].rotation_euler),
        "orientation_rotation": list(actor["orientation"].rotation_euler),
        "content_scale": list(actor["content"].scale),
        "world_bounds": {
            "minimum": list(bounds[0]) if bounds else None,
            "maximum": list(bounds[1]) if bounds else None,
        },
        "nla": [
            {
                "track": track.name,
                "mute": track.mute,
                "strips": [
                    {
                        "name": strip.name,
                        "action": strip.action.name if strip.action else None,
                        "frame_start": strip.frame_start,
                        "frame_end": strip.frame_end,
                        "action_frame_start": strip.action_frame_start,
                        "action_frame_end": strip.action_frame_end,
                        "scale": strip.scale,
                        "repeat": strip.repeat,
                        "influence": strip.influence,
                    }
                    for strip in track.strips
                ],
            }
            for track in actor["armatures"][0].animation_data.nla_tracks
        ],
    }
    (OUTPUT / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("SWAT_DIRECTOR_ACTOR_QA_OK", scene.render.filepath)


if __name__ == "__main__":
    main()
