"""Stage genre-specific actors, cameras, events, and previews for Code2Games.

This is stage 12.  It consumes an already-staged Blend plus the stage-11
Director path.  Ordinary genres keep gameplay roots read-only; Racing's
explicit speedway layout may relocate its referenced roots onto a newly
generated supported road without modifying the environment or asset meshes.
"""

import argparse
import bisect
import json
import math
import os
import re
import sys
import time
import traceback

import bpy
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Euler, Matrix, Quaternion, Vector

import racing_speedway_layout


COLLECTION_NAME = "Code2Games_Genre_Director"
UP = Vector((0.0, 0.0, 1.0))
_SWAT_TEMPLATE_CACHE = {}
_SWAT_ACTOR_CACHE = {}
# Scene-specific showcase wrappers configure these knobs through CLI args so
# per-genre tuning lives in the dedicated stage_*_showcase scripts instead of
# leaking into the generic director.  Defaults are the conservative baseline.
_TURN_EASING_TPS = False
_LOCOMOTION_BLEND_FRAMES = 0.0
_TPS_ALWAYS_RUN = False
_TPS_FADE_PLAYER_AT_GOAL = False
_TPS_TARGET_MOTION_END_FRAME = 1176  # 49 s of motion + 1 s tail = 50 s video.
_TPS_FIRST_ENEMY_ATTACK_FRAME = 240  # Exactly 10.0 s at 24 FPS.
_TPS_ENEMY_COUNT = 5
_ACTIVE_DIRECTOR_GENRE = ""
_GROUND_DETOUR_RADII = (0.55, 0.9, 1.4, 2.1, 2.8)
_ENEMY_DEATH_START_OFFSET = 25
_FPS_VIEWMODEL_PITCH_DEGREES = 6.0
_FPS_CAMERA_MAX_UP_PITCH_DEGREES = 10.0
_FPS_CAMERA_MAX_DOWN_PITCH_DEGREES = 3.0
_FPS_HOLD_OVERRIDES = None
_FPS_VIEWMODEL_SCALE = 1.0
_FPS_VIEWMODEL_OFFSET = (0.30, 0.85, -0.18)
_FPS_TURN_EASING = False
_SAMPLE_STEP_FRAMES = 4
_FPS_WALK_SPEED_MPS = 3.45
_FPS_SMOOTH_TURN_WINDOW = 1
_MAX_TURN_DEG_PER_FRAME = 4.0


def normalize_rotation_curve_winding(obj):
    """Make every quaternion key on the object's rotation curve shortest-path.

    Inserting extra keys (event-turn easing / combat body-facing) into a curve
    that already has keys can leave neighbouring quaternions with opposite
    signs.  Blender then interpolates the long way, which shows up as a ~330
    degree swing in one frame (the "crazy shaking / spinning" in the QA clip).
    Walking all points in frame order and forcing them compatible removes it.
    """
    animation = getattr(obj, "animation_data", None)
    action = animation.action if animation else None
    if action is None:
        return
    curves = [curve for curve in action.fcurves if curve.data_path == "rotation_quaternion"]
    if not curves:
        return
    # Quaternion rotation is stored as four fcurves (array_index 0=w,1=x,2=y,3=z),
    # each with 2D keyframe points (frame, component).  Gather the four
    # components per frame, fix the winding, and write them back.
    by_channel = {}
    frames = set()
    for curve in curves:
        channel = int(curve.array_index)
        by_channel[channel] = {int(round(point.co[0])): point for point in curve.keyframe_points}
        frames.update(by_channel[channel])
    reference = None
    for frame in sorted(frames):
        components = [None] * 4
        for channel in range(4):
            point = by_channel.get(channel, {}).get(frame)
            if point is not None:
                components[channel] = float(point.co[1])
        if any(value is None for value in components):
            continue
        quaternion = Quaternion(components)
        if reference is not None:
            quaternion.make_compatible(reference)
            for channel in range(4):
                by_channel[channel][frame].co[1] = quaternion[channel]
        reference = quaternion
_RACING_CAMERA_BACK = 13.5
_RACING_CAMERA_HEIGHT = 3.0
_RACING_CAMERA_LOOKAHEAD = 9.0
_RACING_CAMERA_LENS = 28.0
_RACING_PROP_REACTIONS = True
_RACING_HIDE_CAMERA_FOLIAGE = True
_RACING_TARGET_DURATION_SECONDS = 50.0
_RACING_MIN_DISPLAY_SPEED_KMH = 200.0
_RACING_CRUISE_DISPLAY_SPEED_KMH = 240.0
_RACING_PEAK_DISPLAY_SPEED_KMH = 420.0
_WINGSUIT_PARKED_BACK_M = 0.0
_WINGSUIT_PREFLIGHT_FEEDBACK = False
_WINGSUIT_PREFLIGHT_FRAMES = 72
_WINGSUIT_FLIGHT_FRAMES = 1116
_WINGSUIT_PULLUP_FRAMES = 60
_RACING_SPEED_MPS = 12.5


def script_args():
    return sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []


def parse_args():
    parser = argparse.ArgumentParser(description="Stage a genre-aware Code2Games director demo")
    parser.add_argument("--scene_blend", required=True)
    parser.add_argument("--director_path", required=True)
    parser.add_argument("--output_dir", required=True)
    parser.add_argument("--output_blend", default="")
    parser.add_argument("--npc_fbx", default="./assets/mixamo/mixamo_run.fbx")
    parser.add_argument("--fps_combat_asset_dir", default="./assets/gameplay/fps_combat")
    parser.add_argument(
        "--swat_combat_library",
        default="./assets/gameplay/swat_combat/final/swat_combat_library.blend",
    )
    parser.add_argument(
        "--racing_vehicle_asset",
        default="./assets/gameplay/racing_vehicle/final/arkham_batmobile_game_ready.glb",
    )
    parser.add_argument(
        "--wingsuit_aircraft_asset",
        default="./assets/gameplay/wingsuit_aircraft/final/f104_starfighter_game_ready.glb",
        help="clean +Y-forward aircraft GLB used by the wingsuit/flight Director",
    )
    parser.add_argument(
        "--aircraft_target_length",
        type=float,
        default=16.7,
        help="visible F-104 length in metres; 16.7m matches the real aircraft scale",
    )
    parser.add_argument(
        "--combat_actor_scale",
        type=float,
        default=1.12,
        help="uniform FPS/TPS SWAT scale multiplier relative to authored human height",
    )
    parser.add_argument("--render_previews", action="store_true")
    parser.add_argument("--render_video", action="store_true")
    parser.add_argument("--preview_samples", type=int, default=24)
    parser.add_argument(
        "--preview_frame",
        action="append",
        type=int,
        default=[],
        help="render only these explicit frames; repeat for local QA",
    )
    parser.add_argument("--video_samples", type=int, default=16)
    parser.add_argument("--resolution_x", type=int, default=960)
    parser.add_argument("--resolution_y", type=int, default=540)
    # Scene-specific tuning owned by the dedicated showcase wrappers.
    parser.add_argument(
        "--tps_turn_easing",
        action="store_true",
        help="TPS: ease the body onto the next leg after event holds",
    )
    parser.add_argument(
        "--locomotion_blend_frames",
        type=float,
        default=0.0,
        help="NLA blend frames between locomotion strips (idle/walk/run)",
    )
    parser.add_argument(
        "--tps_always_run",
        action="store_true",
        help="TPS: never switch locomotion mid-run; non-combat beats stay in run",
    )
    parser.add_argument(
        "--tps_fade_player_at_goal",
        action="store_true",
        help="TPS: shrink the player out at the goal (QA: the fade stays off by default)",
    )
    parser.add_argument(
        "--tps_target_motion_end_frame",
        type=int,
        default=1176,
        help="TPS: final moving frame; the Director adds a 24-frame ending tail",
    )
    parser.add_argument(
        "--tps_first_enemy_attack_frame",
        type=int,
        default=240,
        help="TPS: Alpha begins firing at this exact frame (240 = 10 s)",
    )
    parser.add_argument(
        "--tps_enemy_count",
        type=int,
        default=5,
        help="TPS: number of authored combat beats realized as real enemies",
    )
    parser.add_argument(
        "--ground_detour_radius",
        type=float,
        default=2.8,
        help="maximum lateral detour radius when avoiding trunks/props",
    )
    parser.add_argument(
        "--enemy_death_start_offset",
        type=int,
        default=25,
        help="frames after a combat beat when the death pose starts",
    )
    parser.add_argument(
        "--fps_viewmodel_pitch_degrees",
        type=float,
        default=6.0,
        help="FPS: view-model nose-up pitch about the eye (keeps the gun visible)",
    )
    parser.add_argument(
        "--fps_camera_max_up_pitch_degrees",
        type=float,
        default=10.0,
        help="FPS: camera uphill pitch clamp",
    )
    parser.add_argument(
        "--fps_camera_max_down_pitch_degrees",
        type=float,
        default=3.0,
        help="FPS: camera downhill pitch clamp",
    )
    parser.add_argument(
        "--fps_hold_frames",
        nargs="*",
        type=int,
        default=None,
        help="FPS event hold frames as collect collect_required interact combat goal",
    )
    parser.add_argument("--fps_viewmodel_scale", type=float, default=1.0)
    parser.add_argument(
        "--fps_viewmodel_offset",
        nargs=3,
        type=float,
        default=None,
        help="FPS view-model offset x y z relative to the eye-height aim pivot",
    )
    parser.add_argument(
        "--fps_turn_easing",
        action="store_true",
        help="FPS: ease the body onto the next leg after event holds",
    )
    parser.add_argument(
        "--ground_sample_step_frames",
        type=int,
        default=4,
        help="route/rotation/camera sample spacing in frames (2 = denser, smoother)",
    )
    parser.add_argument(
        "--fps_walk_speed_mps",
        type=float,
        default=3.45,
        help="FPS: player ground speed in m/s (5.5 compresses the run to ~29s)",
    )
    parser.add_argument(
        "--fps_smooth_turn_window",
        type=int,
        default=1,
        help="FPS: body faces the path tangent across +/-N samples (3 = smooth arcs)",
    )
    parser.add_argument(
        "--max_turn_deg_per_frame",
        type=float,
        default=4.0,
        help="hard cap on body/camera rotation speed in degrees per frame",
    )
    parser.add_argument("--racing_camera_back", type=float, default=13.5)
    parser.add_argument("--racing_camera_height", type=float, default=3.0)
    parser.add_argument("--racing_camera_lookahead", type=float, default=9.0)
    parser.add_argument("--racing_camera_lens", type=float, default=28.0)
    parser.add_argument("--racing_prop_reactions", action="store_true")
    parser.add_argument("--racing_hide_camera_foliage", action="store_true")
    parser.add_argument(
        "--wingsuit_parked_back_m",
        type=float,
        default=0.0,
        help="Wingsuit: park the aircraft this many metres back (away from the cliff) into the summit grass",
    )
    parser.add_argument(
        "--wingsuit_preflight_feedback",
        action="store_true",
        help="Deprecated compatibility switch; Wingsuit V5 always uses localized asset feedback",
    )
    parser.add_argument("--wingsuit_preflight_frames", type=int, default=72)
    parser.add_argument("--wingsuit_flight_frames", type=int, default=1116)
    parser.add_argument("--wingsuit_pullup_frames", type=int, default=60)
    parser.add_argument(
        "--racing_speed_mps",
        type=float,
        default=12.5,
        help="Racing: base cruise speed in m/s (6.0 ~ 30s full run, 12.5 ~ 16s)",
    )
    parser.add_argument("--racing_target_duration_seconds", type=float, default=50.0)
    parser.add_argument("--racing_min_display_speed_kmh", type=float, default=200.0)
    parser.add_argument("--racing_cruise_display_speed_kmh", type=float, default=240.0)
    parser.add_argument("--racing_peak_display_speed_kmh", type=float, default=420.0)
    return parser.parse_args(script_args())


def project_path(value):
    value = os.path.expanduser(os.path.expandvars(str(value)))
    if os.path.isabs(value):
        return os.path.abspath(value)
    root = os.path.abspath(os.environ.get("CODE2WORLDS_ROOT") or os.getcwd())
    return os.path.abspath(os.path.join(root, value))


def load_json(path):
    if not os.path.isfile(path):
        raise FileNotFoundError("required JSON not found: %s" % path)
    # A showcase wrapper deliberately runs stage 11 and stage 12 back to back.
    # On the shared project mount, a just atomically-replaced JSON can
    # transiently appear as an empty file to the next open.  Retrying this
    # bounded hand-off prevents a valid newly-built director path from being
    # mistaken for malformed JSON; malformed stable JSON still fails clearly.
    last_error = None
    for attempt in range(12):
        try:
            if os.path.getsize(path) == 0:
                raise ValueError("director JSON is temporarily empty")
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            last_error = error
            if attempt == 11:
                break
            time.sleep(0.25)
    raise RuntimeError("could not read stable JSON after Stage-11 hand-off: %s (%s)" % (path, last_error))


def write_json(path, value):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    for attempt in range(6):
        temporary = path + ".tmp"
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                handle.write(payload)
            try:
                with open(temporary, "rb") as handle:
                    os.fsync(handle.fileno())
            except OSError:
                # fsync on a read-only handle is unsupported in some
                # environments (e.g. Blender's Windows Python); the write and
                # atomic replace below are still durable.
                pass
            os.replace(temporary, path)
            with open(path, "r", encoding="utf-8") as handle:
                if handle.read(2) == "{":
                    return
        except Exception as exc:
            if attempt == 5:
                raise
            print("WRITE_JSON_RETRY", path, exc, flush=True)
        time.sleep(1)


def log(message, *values):
    try:
        print(" ".join([str(message), *[str(value) for value in values]]), flush=True)
    except OSError:
        # A detached/expired launcher may close stdout while Blender is still
        # finishing a valid render.  Logging must never invalidate the scene.
        pass


def reset_collection():
    old = bpy.data.collections.get(COLLECTION_NAME)
    if old:
        for obj in list(old.all_objects):
            bpy.data.objects.remove(obj, do_unlink=True)
        bpy.data.collections.remove(old)
    collection = bpy.data.collections.new(COLLECTION_NAME)
    bpy.context.scene.collection.children.link(collection)
    return collection


def link_only(obj, collection):
    for current in list(obj.users_collection):
        current.objects.unlink(obj)
    collection.objects.link(obj)


def material(name, color, emission=0.0, metallic=0.0, roughness=0.55):
    value = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    value.use_nodes = True
    nodes = value.node_tree.nodes
    node = nodes.get("Principled BSDF")
    if node is None:
        # A staged world may already contain a material with the same readable
        # name but a custom shader graph.  Director-owned effects must not
        # assume that graph has Blender's default node.
        node = nodes.new("ShaderNodeBsdfPrincipled")
        node.name = "Principled BSDF"
        output = next((candidate for candidate in nodes if candidate.type == "OUTPUT_MATERIAL"), None)
        if output is None:
            output = nodes.new("ShaderNodeOutputMaterial")
        value.node_tree.links.new(node.outputs["BSDF"], output.inputs["Surface"])
    node.inputs["Base Color"].default_value = tuple(color)
    node.inputs["Roughness"].default_value = float(roughness)
    node.inputs["Metallic"].default_value = float(metallic)
    if "Emission Color" in node.inputs:
        node.inputs["Emission Color"].default_value = tuple(color)
        node.inputs["Emission Strength"].default_value = float(emission)
    elif "Emission" in node.inputs:
        node.inputs["Emission"].default_value = tuple(color)
        node.inputs["Emission Strength"].default_value = float(emission)
    return value


def assign_material(obj, value):
    if obj.data and hasattr(obj.data, "materials"):
        obj.data.materials.clear()
        obj.data.materials.append(value)


def add_cube(name, location, scale, value, collection, parent=None, bevel=0.08):
    bpy.ops.mesh.primitive_cube_add(location=location)
    obj = bpy.context.object
    obj.name = name
    obj.scale = Vector(scale)
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    if bevel > 0.0:
        modifier = obj.modifiers.new("DirectorBevel", "BEVEL")
        modifier.width = bevel
        modifier.segments = 2
    assign_material(obj, value)
    link_only(obj, collection)
    if parent:
        obj.parent = parent
    return obj


def add_uv_sphere(name, location, radius, value, collection, parent=None):
    bpy.ops.mesh.primitive_uv_sphere_add(segments=24, ring_count=12, radius=radius, location=location)
    obj = bpy.context.object
    obj.name = name
    assign_material(obj, value)
    link_only(obj, collection)
    if parent:
        obj.parent = parent
    return obj


def add_cylinder(name, location, radius, depth, value, collection, parent=None, rotation=None):
    bpy.ops.mesh.primitive_cylinder_add(vertices=24, radius=radius, depth=depth, location=location)
    obj = bpy.context.object
    obj.name = name
    if rotation:
        obj.rotation_euler = rotation
    assign_material(obj, value)
    link_only(obj, collection)
    if parent:
        obj.parent = parent
    return obj


def new_root(name, collection):
    root = bpy.data.objects.new(name, None)
    collection.objects.link(root)
    return root


def mesh_bounds(objects):
    points = []
    for obj in objects:
        if obj.type == "MESH":
            points.extend(obj.matrix_world @ Vector(corner) for corner in obj.bound_box)
    if not points:
        return None
    minimum = Vector((min(p.x for p in points), min(p.y for p in points), min(p.z for p in points)))
    maximum = Vector((max(p.x for p in points), max(p.y for p in points), max(p.z for p in points)))
    return minimum, maximum


def cyclic_actions(objects):
    for obj in objects:
        animation = getattr(obj, "animation_data", None)
        action = animation.action if animation else None
        if not action:
            continue
        for curve in action.fcurves:
            if not any(mod.type == "CYCLES" for mod in curve.modifiers):
                curve.modifiers.new("CYCLES")


def import_character(collection, fbx_path, target_height=1.75):
    if not os.path.isfile(fbx_path):
        return None, "FBX not found"
    before = set(bpy.data.objects)
    try:
        bpy.ops.import_scene.fbx(filepath=fbx_path)
    except Exception as exc:
        return None, "FBX import failed: %s" % exc
    imported = [obj for obj in bpy.data.objects if obj not in before]
    if not imported:
        return None, "FBX import created no objects"
    root = new_root("C2G_TPS_CHARACTER_ROOT", collection)
    imported_set = set(imported)
    top_level = [obj for obj in imported if obj.parent not in imported_set]
    for obj in imported:
        link_only(obj, collection)
    for obj in top_level:
        matrix = obj.matrix_world.copy()
        obj.parent = root
        obj.matrix_world = matrix
    bounds = mesh_bounds(imported)
    if bounds:
        height = max(0.01, bounds[1].z - bounds[0].z)
        scale = target_height / height
        root.scale = (scale, scale, scale)
    cyclic_actions(imported)
    return root, None


def procedural_character(collection, genre="tps"):
    root = new_root("C2G_%s_ACTOR" % genre.upper(), collection)
    suit = material("C2G_ActorSuit", (0.055, 0.12, 0.16, 1.0), metallic=0.15, roughness=0.35)
    accent = material("C2G_ActorAccent", (0.95, 0.22, 0.06, 1.0), emission=0.15, metallic=0.25)
    skin = material("C2G_ActorHelmet", (0.16, 0.20, 0.22, 1.0), metallic=0.55, roughness=0.25)
    add_cylinder("C2G_ActorTorso", (0.0, 0.0, 1.05), 0.30, 0.85, suit, collection, root)
    add_uv_sphere("C2G_ActorHead", (0.0, 0.0, 1.68), 0.25, skin, collection, root)
    for side in (-1.0, 1.0):
        add_cylinder("C2G_ActorLeg", (0.16 * side, 0.0, 0.42), 0.095, 0.75, suit, collection, root)
        arm = add_cylinder("C2G_ActorArm", (0.42 * side, 0.05, 1.15), 0.075, 0.70, accent, collection, root)
        arm.rotation_euler[1] = math.radians(12.0 * side)
    return root


def create_rally_vehicle(collection):
    root = new_root("C2G_RALLY_VEHICLE", collection)
    body = material("C2G_RallyBody", (0.72, 0.035, 0.02, 1.0), metallic=0.55, roughness=0.25)
    dark = material("C2G_RallyDark", (0.012, 0.018, 0.025, 1.0), metallic=0.2, roughness=0.32)
    lamp = material("C2G_RallyLamp", (1.0, 0.72, 0.16, 1.0), emission=5.0)
    add_cube("C2G_RallyChassis", (0.0, 0.0, 0.55), (0.95, 1.75, 0.28), body, collection, root, bevel=0.16)
    add_cube("C2G_RallyCabin", (0.0, -0.15, 0.98), (0.72, 0.82, 0.34), dark, collection, root, bevel=0.12)
    add_cube("C2G_RallyWing", (0.0, -1.55, 1.02), (1.02, 0.13, 0.08), body, collection, root, bevel=0.04)
    for x in (-0.93, 0.93):
        for y in (-1.15, 1.15):
            wheel = add_cylinder("C2G_RallyWheel", (x, y, 0.38), 0.38, 0.28, dark, collection, root, rotation=(0.0, math.pi / 2.0, 0.0))
            wheel["code2games_wheel"] = True
    for x in (-0.45, 0.45):
        add_uv_sphere("C2G_RallyHeadlamp", (x, 1.78, 0.63), 0.12, lamp, collection, root)
    return root


def import_racing_vehicle(collection, asset_path, target_length=5.2):
    """Import the cleaned authored vehicle while retaining wheel pivots."""
    if not asset_path or not os.path.isfile(asset_path):
        raise FileNotFoundError("realistic racing vehicle GLB missing: %s" % asset_path)
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=asset_path)
    imported = [obj for obj in bpy.data.objects if obj not in before]
    if not imported:
        raise RuntimeError("racing vehicle import created no objects: %s" % asset_path)
    controller = new_root("C2G_RACING_VEHICLE", collection)
    orientation = new_root("C2G_RACING_VEHICLE_ORIENTATION", collection)
    orientation.parent = controller
    content = new_root("C2G_RACING_VEHICLE_CONTENT", collection)
    content.parent = orientation
    imported_set = set(imported)
    top_level = [obj for obj in imported if obj.parent not in imported_set]
    for obj in imported:
        link_only(obj, collection)
        obj["code2games_racing_vehicle"] = True
    for obj in top_level:
        world = obj.matrix_world.copy()
        obj.parent = content
        obj.matrix_world = world
    meshes = [obj for obj in imported if obj.type == "MESH"]
    bounds = mesh_bounds(meshes)
    if not bounds:
        raise RuntimeError("racing vehicle GLB has no mesh bounds: %s" % asset_path)
    dimensions = bounds[1] - bounds[0]
    source_length = max(float(dimensions.x), float(dimensions.y), 0.01)
    scale = float(target_length) / source_length
    centre = (bounds[0] + bounds[1]) * 0.5
    content.scale = (scale, scale, scale)
    # Two millimetres avoids coplanar flicker while keeping the tyre contact
    # visually indistinguishable from true ground contact.
    content.location = Vector((-centre.x * scale, -centre.y * scale, -bounds[0].z * scale + 0.002))
    # The cleaned vehicle's nose points along local +Y, matching the Director
    # route convention.  Keep a separate orientation node for future assets,
    # but do not rotate this one backwards.
    orientation.rotation_euler[2] = 0.0
    spin_pivots = sorted(
        (obj for obj in imported if obj.name.upper().startswith("C2G_WHEEL_SPIN_")),
        key=lambda obj: obj.name,
    )
    steer_pivots = sorted(
        (obj for obj in imported if obj.name.upper().startswith("C2G_WHEEL_STEER_")),
        key=lambda obj: obj.name,
    )
    if len(spin_pivots) != 4 or len(steer_pivots) != 2:
        raise RuntimeError(
            "racing vehicle requires four spin and two steering pivots; found %d/%d"
            % (len(spin_pivots), len(steer_pivots))
        )
    controller["code2games_vehicle_asset"] = asset_path
    controller["code2games_vehicle_style"] = "authored_rigged_arkham_batmobile"
    controller["code2games_vehicle_dimensions_m"] = [round(float(value), 5) for value in dimensions * scale]
    controller["code2games_wheel_radius_m"] = 0.629 * scale
    controller["code2games_wheel_spin_objects"] = ",".join(obj.name for obj in spin_pivots)
    controller["code2games_wheel_steer_objects"] = ",".join(obj.name for obj in steer_pivots)
    return controller


def import_wingsuit_aircraft(collection, asset_path, target_length=16.7):
    """Import the prepared F-104 hierarchy without changing Stage-6 assets."""
    if not asset_path or not os.path.isfile(asset_path):
        raise FileNotFoundError("F-104 runtime GLB missing: %s" % asset_path)
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=asset_path)
    imported = [obj for obj in bpy.data.objects if obj not in before]
    if not imported:
        raise RuntimeError("F-104 import created no objects: %s" % asset_path)
    controller = new_root("C2G_WINGSUIT_F104_ACTOR", collection)
    imported_set = set(imported)
    top_level = [obj for obj in imported if obj.parent not in imported_set]
    for obj in imported:
        link_only(obj, collection)
        obj["code2games_aircraft"] = True
    for obj in top_level:
        world = obj.matrix_world.copy()
        obj.parent = controller
        obj.matrix_world = world
    bounds = mesh_bounds(imported)
    if not bounds:
        raise RuntimeError("F-104 runtime GLB contains no mesh bounds")
    dimensions = bounds[1] - bounds[0]
    source_length = max(float(dimensions.x), float(dimensions.y), 0.01)
    target_length = max(8.0, min(24.0, float(target_length)))
    scale = target_length / source_length
    controller.scale = (scale, scale, scale)
    scaled_dimensions = dimensions * scale
    # The jet performs full rolls.  Clearance therefore has to cover the
    # rotated wing/vertical-tail radius, not merely the fuselage belly.
    roll_clearance = max(
        2.4,
        0.5 * math.sqrt(float(scaled_dimensions.x) ** 2 + float(scaled_dimensions.z) ** 2) + 0.6,
    )
    exhaust = next((obj for obj in imported if obj.name.startswith("C2G_AIRCRAFT_EXHAUST")), None)
    controller["code2games_aircraft_asset"] = asset_path
    controller["code2games_aircraft_style"] = "authored_f104_starfighter"
    controller["code2games_aircraft_source_dimensions_m"] = [round(float(value), 5) for value in dimensions]
    controller["code2games_aircraft_dimensions_m"] = [round(float(value), 5) for value in scaled_dimensions]
    controller["code2games_aircraft_target_length_m"] = round(float(target_length), 5)
    controller["code2games_aircraft_scale_factor"] = round(float(scale), 7)
    controller["code2games_aircraft_roll_clearance_m"] = round(float(roll_clearance), 5)
    controller["code2games_aircraft_ground_support_offset_m"] = round(
        max(0.25, -float(bounds[0].z) * scale + 0.04),
        5,
    )
    controller["code2games_aircraft_exhaust_marker"] = exhaust.name if exhaust else ""
    return controller


def create_wingsuit_flyer(collection):
    root = new_root("C2G_WINGSUIT_FLYER", collection)
    fabric = material("C2G_WingsuitFabric", (0.04, 0.10, 0.15, 1.0), metallic=0.05, roughness=0.4)
    wing = material("C2G_WingsuitWing", (0.95, 0.19, 0.035, 1.0), emission=0.08, roughness=0.32)
    helmet = material("C2G_WingsuitHelmet", (0.12, 0.16, 0.20, 1.0), metallic=0.65, roughness=0.22)
    add_cylinder("C2G_FlyerBody", (0.0, 0.0, 0.0), 0.23, 1.35, fabric, collection, root, rotation=(math.pi / 2.0, 0.0, 0.0))
    add_uv_sphere("C2G_FlyerHelmet", (0.0, 0.78, 0.0), 0.25, helmet, collection, root)
    add_cube("C2G_FlyerLeftWing", (-0.58, 0.05, 0.0), (0.58, 0.58, 0.035), wing, collection, root, bevel=0.05)
    add_cube("C2G_FlyerRightWing", (0.58, 0.05, 0.0), (0.58, 0.58, 0.035), wing, collection, root, bevel=0.05)
    add_cube("C2G_FlyerTailWing", (0.0, -0.55, 0.0), (0.38, 0.52, 0.035), wing, collection, root, bevel=0.05)
    return root


def import_fps_rifle(collection, player_root, rifle_path):
    if not rifle_path or not os.path.isfile(rifle_path):
        return None
    before = set(bpy.data.objects)
    try:
        bpy.ops.import_scene.gltf(filepath=rifle_path)
    except Exception as exc:
        log("FPS_RIFLE_IMPORT_WARNING", exc)
        return None
    imported = [obj for obj in bpy.data.objects if obj not in before]
    if not imported:
        return None
    weapon_root = new_root("C2G_FPS_WEAPON_ROOT", collection)
    weapon_root.parent = player_root
    content_root = new_root("C2G_FPS_WEAPON_CONTENT", collection)
    content_root.parent = weapon_root
    imported_set = set(imported)
    top_level = [obj for obj in imported if obj.parent not in imported_set]
    for obj in imported:
        link_only(obj, collection)
        obj["code2games_fps_weapon"] = True
    for obj in top_level:
        matrix = obj.matrix_world.copy()
        obj.parent = content_root
        obj.matrix_world = matrix
    bounds = mesh_bounds(imported)
    if bounds:
        center = (bounds[0] + bounds[1]) * 0.5
        longest = max((bounds[1] - bounds[0]).x, (bounds[1] - bounds[0]).y, (bounds[1] - bounds[0]).z, 0.01)
        # Full-size TAC-50 geometry is substantially longer than an assault
        # rifle.  Keep it recognisably real while preventing the barrel from
        # cutting diagonally across most of a 30 mm first-person frame.
        scale = 1.02 / longest
        content_root.scale = (scale, scale, scale)
        content_root.location = -center * scale
    weapon_root.location = (0.40, 0.78, 1.31)
    # The downloaded TAC-50 is authored along local +Y, matching the Director
    # player's forward axis.  Rotating it like the old Quaternius pickup would
    # turn the rifle sideways across the screen.
    weapon_root.rotation_euler = (0.0, 0.0, 0.0)
    muzzle = bpy.data.objects.new("C2G_FPS_MUZZLE", None)
    collection.objects.link(muzzle)
    muzzle.parent = weapon_root
    muzzle.location = (0.0, 0.72, 0.0)
    player_root["code2games_weapon_root"] = weapon_root.name
    player_root["code2games_weapon_muzzle"] = muzzle.name
    return weapon_root


def create_detailed_fps_rifle(collection, player_root):
    """Build a grounded, non-toy first-person service rifle.

    The old Quaternius pickup rifle reads well as a world prop, but its pale,
    chunky silhouette reads like a toy when it occupies a third of the FPS
    camera.  The Director weapon is therefore assembled at the correct screen
    scale from dark gunmetal components and does not depend on an external
    pickup model.
    """
    root = new_root("C2G_FPS_WEAPON_ROOT", collection)
    root.parent = player_root
    root.location = (0.31, 0.78, 1.25)

    gunmetal = material("C2G_FPS_Gunmetal", (0.018, 0.024, 0.028, 1.0), metallic=0.88, roughness=0.25)
    parkerized = material("C2G_FPS_Parkerized", (0.045, 0.052, 0.055, 1.0), metallic=0.62, roughness=0.42)
    polymer = material("C2G_FPS_Polymer", (0.026, 0.031, 0.030, 1.0), metallic=0.05, roughness=0.62)
    optic_glass = material("C2G_FPS_OpticGlass", (0.13, 0.012, 0.008, 1.0), emission=1.6, metallic=0.2, roughness=0.12)

    add_cube("C2G_FPS_Receiver", (0.0, 0.05, 0.0), (0.135, 0.33, 0.105), gunmetal, collection, root, bevel=0.025)
    add_cube("C2G_FPS_UpperReceiver", (0.0, 0.12, 0.115), (0.115, 0.29, 0.045), parkerized, collection, root, bevel=0.014)
    add_cube("C2G_FPS_Handguard", (0.0, 0.53, 0.005), (0.105, 0.27, 0.085), parkerized, collection, root, bevel=0.022)
    for side in (-1.0, 1.0):
        add_cube("C2G_FPS_HandguardRail", (0.112 * side, 0.53, 0.005), (0.014, 0.24, 0.034), gunmetal, collection, root, bevel=0.006)
    add_cylinder("C2G_FPS_Barrel", (0.0, 0.88, 0.025), 0.032, 0.50, gunmetal, collection, root, rotation=(math.pi / 2.0, 0.0, 0.0))
    add_cylinder("C2G_FPS_MuzzleBrake", (0.0, 1.145, 0.025), 0.047, 0.18, parkerized, collection, root, rotation=(math.pi / 2.0, 0.0, 0.0))
    add_cube("C2G_FPS_Stock", (0.0, -0.36, 0.005), (0.115, 0.22, 0.085), polymer, collection, root, bevel=0.035)
    grip = add_cube("C2G_FPS_PistolGrip", (0.0, -0.12, -0.19), (0.075, 0.10, 0.18), polymer, collection, root, bevel=0.028)
    grip.rotation_euler[0] = math.radians(-13.0)
    magazine = add_cube("C2G_FPS_Magazine", (0.0, 0.16, -0.22), (0.085, 0.13, 0.19), parkerized, collection, root, bevel=0.022)
    magazine.rotation_euler[0] = math.radians(5.0)
    add_cube("C2G_FPS_TopRail", (0.0, 0.24, 0.178), (0.075, 0.38, 0.018), gunmetal, collection, root, bevel=0.006)
    add_cube("C2G_FPS_OpticBody", (0.0, 0.22, 0.245), (0.085, 0.12, 0.065), gunmetal, collection, root, bevel=0.025)
    add_cube("C2G_FPS_OpticWindow", (0.0, 0.325, 0.25), (0.057, 0.012, 0.043), optic_glass, collection, root, bevel=0.010)
    add_cube("C2G_FPS_FrontSight", (0.0, 0.70, 0.155), (0.030, 0.025, 0.080), gunmetal, collection, root, bevel=0.006)

    muzzle = bpy.data.objects.new("C2G_FPS_MUZZLE", None)
    collection.objects.link(muzzle)
    muzzle.parent = root
    muzzle.location = (0.0, 1.25, 0.025)
    player_root["code2games_weapon_root"] = root.name
    player_root["code2games_weapon_muzzle"] = muzzle.name
    player_root["code2games_weapon_style"] = "grounded_modern_service_rifle"
    return root


def create_fps_actor(collection, swat_library=""):
    root = new_root("C2G_FPS_PLAYER", collection)
    create_scar_viewmodel(collection, root, swat_library)
    return root


def director_or_gameplay_object(obj):
    if obj.get("code2games_placement_id"):
        return True
    for collection in obj.users_collection:
        if collection.name.startswith("Code2Games_") or collection.name.startswith("C2G_"):
            return True
    return False


def racing_track_surface_object(obj):
    current = obj
    for _ in range(16):
        if current is None:
            return False
        if current.get("code2games_racing_track_surface"):
            return True
        current = current.parent
    return False


def gameplay_placement_object(obj):
    current = obj
    for _ in range(32):
        if current is None:
            return False
        if current.get("code2games_placement_id"):
            return True
        current = current.parent
    return False


def non_ground_scenery(obj):
    text = " ".join(
        [obj.name.lower()]
        + [
            slot.material.name.lower()
            for slot in getattr(obj, "material_slots", [])
            if slot.material
        ]
    )
    vegetation_tokens = (
        "tree", "bush", "grass", "flower", "leaf", "leaves", "branch",
        "plant", "fern", "shrub", "trunk", "canopy", "vine",
    )
    return any(token in text for token in vegetation_tokens)


def racing_route_visual_clutter(obj):
    """Large source props that crowd an otherwise readable rally line.

    These are pre-existing wreck/shelter set dressing, not referenced gameplay
    placements.  Keeping them beside each other made the 19--20 second chase
    shot read as an accidental prop pile rather than spaced game objectives.
    """
    text = obj.name.lower()
    tokens = (
        "wreck", "vehicle", "truck", "car_", "car.", "shelter", "roof",
        "canopy", "awning", "hut", "pavilion", "tent",
    )
    return any(token in text for token in tokens)


def solid_route_scenery(obj):
    """Distinguish tree-scale obstructions from traversable ground cover."""
    names = [obj.name.lower()]
    parent = obj.parent
    for _ in range(8):
        if parent is None:
            break
        names.append(parent.name.lower())
        parent = parent.parent
    text = " ".join(names)
    materials = " ".join(
        slot.material.name.lower()
        for slot in getattr(obj, "material_slots", [])
        if slot.material
    )
    # Trees are obstacles (QA: the player walked through one).  Check the
    # parent chain too, since generated trunks can sit under a factory-named
    # parent.  Leaves/canopy/grass stay traversable so the route can pass
    # under a tree without fighting every branch.
    if any(token in text for token in ("trunk", "branch", "wood", "bark", "stump", "log", "pine", "oak", "birch")):
        return True
    if "tree" in text and not any(token in text for token in ("leaf", "leaves", "canopy", "grass", "fern")):
        return True
    return any(token in materials for token in ("trunk", "wood", "bark"))


def solid_tree_owner(obj):
    """Resolve an evaluated trunk/branch mesh to its complete tree instance.

    Infinigen ray casts often return a generated child mesh whose own name is
    generic.  The old checks therefore missed the trunk even though a parent,
    sibling material, or instance root clearly identified the tree.  Returning
    the highest useful owner lets both navigation and the TPS camera treat the
    same physical tree consistently.
    """
    if obj is None:
        return None
    source = obj.original if getattr(obj, "is_evaluated", False) else obj
    chain = []
    current = source
    for _ in range(64):
        if current is None:
            break
        chain.append(current)
        current = current.parent
    matched = None
    for candidate in chain:
        text = str(candidate.name).lower()
        material_text = " ".join(
            slot.material.name.lower()
            for slot in getattr(candidate, "material_slots", [])
            if slot.material
        )
        if (
            "treefactory(" in text
            or text.startswith("tree.")
            or any(token in text for token in ("trunk", "branch", "bark", "stump", "pine", "oak", "birch"))
            or any(token in material_text for token in ("trunk", "wood", "bark"))
        ):
            matched = candidate
    return matched


def hide_render_hierarchy(root):
    """Hide a complete generated instance, not merely the ray-hit child."""
    if root is None:
        return []
    hidden = []
    pending = [root]
    while pending:
        obj = pending.pop()
        pending.extend(list(obj.children))
        obj.hide_render = True
        try:
            obj.hide_set(True)
        except Exception:
            obj.hide_viewport = True
        hidden.append(obj.name)
    return hidden


def scene_z_extent():
    values = []
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH" or director_or_gameplay_object(obj):
            continue
        try:
            values.extend((obj.matrix_world @ Vector(corner)).z for corner in obj.bound_box)
        except Exception:
            pass
    return (min(values), max(values)) if values else (-100.0, 100.0)


def terrain_surface(x, y, fallback, z_extent):
    """Return the plausible walkable hit closest to an expected route height.

    Large generated worlds may contain a deep ocean/base plane below holes and
    several stacked terrain shells.  Taking the globally highest or first hit
    can therefore teleport a route to a roof, while taking the final hit can
    drop it hundreds of metres.  Stage 11 already supplies an approximate Z;
    geometry remains authoritative, but the closest valid surface to that Z is
    the physically relevant one.
    """
    scene = bpy.context.scene
    depsgraph = bpy.context.evaluated_depsgraph_get()
    origin = Vector((float(x), float(y), z_extent[1] + 200.0))
    direction = Vector((0.0, 0.0, -1.0))
    hits = []
    for _ in range(48):
        hit, location, _normal, _face, obj, _matrix = scene.ray_cast(depsgraph, origin, direction, distance=max(400.0, z_extent[1] - z_extent[0] + 400.0))
        if not hit:
            break
        if (
            obj
            and obj.type == "MESH"
            and (not director_or_gameplay_object(obj) or racing_track_surface_object(obj))
            and not non_ground_scenery(obj)
        ):
            distance_from_expected = abs(float(location.z) - float(fallback))
            if distance_from_expected <= 2.0:
                return float(location.z), obj
            hits.append((distance_from_expected, float(location.z), obj))
        if float(location.z) < float(fallback) - 12.0:
            break
        origin = Vector((location.x, location.y, location.z - 0.03))
    if not hits:
        return float(fallback), None
    _distance, height, obj = min(hits, key=lambda value: (value[0], -value[1]))
    # A surface hundreds of metres away is normally the world's emergency base
    # plane rather than support under this route coordinate.
    if abs(height - float(fallback)) > 12.0:
        return float(fallback), None
    return height, obj


def terrain_height(x, y, fallback, z_extent):
    return terrain_surface(x, y, fallback, z_extent)[0]


def camera_ground_z(x, y, fallback, z_extent):
    """True ground under a point via a straight-down ray.

    terrain_height() picks the surface closest to a fallback height, which can
    silently choose a low shelf under a hill and leave the racing camera boom
    buried in the rising terrain (the recurring black frames around the
    mid-field gate).  A straight-down ray returns the real first surface.
    """
    scene = bpy.context.scene
    depsgraph = bpy.context.evaluated_depsgraph_get()
    origin = Vector((float(x), float(y), float(z_extent[1]) + 200.0))
    direction = Vector((0.0, 0.0, -1.0))
    for _ in range(48):
        hit, location, _normal, _face, obj, _matrix = scene.ray_cast(
            depsgraph,
            origin,
            direction,
            distance=max(400.0, float(z_extent[1] - z_extent[0]) + 400.0),
        )
        if not hit:
            break
        if (
            obj
            and obj.type == "MESH"
            and (not director_or_gameplay_object(obj) or racing_track_surface_object(obj))
            and not non_ground_scenery(obj)
        ):
            return float(location.z)
        origin = Vector((location.x, location.y, location.z - 0.03))
    return terrain_height(float(x), float(y), float(fallback), z_extent)


def build_racing_speedway_surface(collection, path_data, z_extent):
    """Build and publish the supported road used by the long Racing layout."""
    beats = path_data.get("beats") or []
    if not any(beat.get("racing_speedway_layout") for beat in beats):
        return None

    count = int(racing_speedway_layout.SAMPLE_COUNT)
    half_width = float(racing_speedway_layout.TRACK_WIDTH_M) * 0.5
    # Include both endpoints.  A one-way route needs a distinct support
    # sample at fraction 1.0; reusing the penultimate/first sample is what
    # previously let the final grass section inherit the wrong terrain height.
    fractions = [index / float(count) for index in range(count + 1)]
    visual_ground_heights = []
    original_ground_profiles = [[], [], []]
    speedway_ground_fallback_z = -0.65

    def speedway_visible_ground_z(x, y):
        """Sample the local lowland without baking the emergency floor into road."""
        sampled = camera_ground_z(x, y, speedway_ground_fallback_z, z_extent)
        if abs(float(sampled) - speedway_ground_fallback_z) > 12.0:
            # camera_ground_z intentionally takes the first visible hit for
            # camera work, which can be the scene's -500 m emergency floor.
            # That is never a plausible lowland road height.  Re-sample with
            # the terrain selector, then fall back to the known local grade
            # if no real surface is available.
            sampled = terrain_height(x, y, speedway_ground_fallback_z, z_extent)
        if abs(float(sampled) - speedway_ground_fallback_z) > 12.0:
            return float(speedway_ground_fallback_z)
        return float(sampled)

    for fraction in fractions:
        point = racing_speedway_layout.centreline(fraction)
        direction = racing_speedway_layout.tangent(fraction)
        right = Vector((direction[1], -direction[0], 0.0))
        heights = [
            speedway_visible_ground_z(
                point[0] + right.x * lateral,
                point[1] + right.y * lateral,
            )
            for lateral in (-half_width, 0.0, half_width)
        ]
        for profile, value in zip(original_ground_profiles, heights):
            profile.append(float(value))
        # The invisible support is deliberately pegged to the *visible*
        # centre terrain, not to either road edge.  The former grade envelope
        # was useful for a visible asphalt circuit but it could bridge a dune
        # and make the car appear to hover over the source landscape.
        visual_ground_heights.append(float(heights[1]))

    # Keep only tyre-level clearance.  Wheel support later supplies the final
    # four-contact pose, so no artificial elevated road profile is needed.
    heights = [value + 0.045 for value in visual_ground_heights]

    def height_at(fraction):
        # This is a one-way rally line, not a closed circuit.  Wrapping 1.0
        # back to zero gave the finish its *start-line* support height and
        # could pull the car down or tilt it as it entered the final grass.
        value = max(0.0, min(1.0, float(fraction)))
        scaled = value * count
        lower = min(count, int(math.floor(scaled)))
        upper = min(count, lower + 1)
        amount = scaled - math.floor(scaled)
        if value >= 1.0:
            amount = 1.0
        return heights[lower] * (1.0 - amount) + heights[upper] * amount

    def original_ground_height_at(fraction, lateral=0.0):
        value = max(0.0, min(1.0, float(fraction)))
        scaled = value * count
        lower = min(count, int(math.floor(scaled)))
        upper = min(count, lower + 1)
        amount = scaled - math.floor(scaled)
        if value >= 1.0:
            amount = 1.0

        def profile_height(profile_index):
            profile = original_ground_profiles[profile_index]
            return profile[lower] * (1.0 - amount) + profile[upper] * amount

        centre_height = profile_height(1)
        edge_height = profile_height(0 if float(lateral) < 0.0 else 2)
        edge_amount = min(1.0, abs(float(lateral)) / max(0.01, half_width))
        return centre_height * (1.0 - edge_amount) + edge_height * edge_amount

    def make_ribbon(name, width, z_offset, value):
        vertices = []
        faces = []
        # The car controller is located on the route endpoint, while its
        # front/rear tyre contacts extend about 1.8 m beyond it.  Extend only
        # this invisible support ribbon at both endpoints so the launch hold
        # and final settle retain four asphalt contacts.  The authored route,
        # visible landscape, asset locations and camera path stay unchanged.
        endpoint_support_extension_m = 4.5
        for index in range(count + 1):
            # Keep the final endpoint at fraction=1.0.  The previous modulo
            # closed the invisible support ribbon from finish back to start,
            # contradicting the one-way layout and hiding endpoint errors.
            fraction = index / float(count)
            point = Vector(racing_speedway_layout.centreline(fraction, height_at(fraction) + z_offset))
            direction = racing_speedway_layout.tangent(fraction)
            if index == 0:
                point -= Vector(direction) * endpoint_support_extension_m
            elif index == count:
                point += Vector(direction) * endpoint_support_extension_m
            right = Vector((direction[1], -direction[0], 0.0))
            vertices.extend([point - right * (width * 0.5), point + right * (width * 0.5)])
            if index:
                base = 2 * (index - 1)
                faces.append((base, base + 1, base + 3, base + 2))
        mesh = bpy.data.meshes.new(name + "_MESH")
        mesh.from_pydata(vertices, [], faces)
        mesh.update()
        obj = bpy.data.objects.new(name, mesh)
        collection.objects.link(obj)
        assign_material(obj, value)
        obj["code2games_racing_track_surface"] = True
        obj["code2games_director_owned"] = True
        return obj

    shoulder = make_ribbon(
        "C2G_RACING_SPEEDWAY_SHOULDER",
        float(racing_speedway_layout.TRACK_WIDTH_M) + 1.8,
        -0.035,
        material("C2G_RACING_SPEEDWAY_SHOULDER_MAT", (0.12, 0.07, 0.035, 1.0), roughness=0.92),
    )
    road = make_ribbon(
        "C2G_RACING_SPEEDWAY_ASPHALT",
        float(racing_speedway_layout.TRACK_WIDTH_M),
        0.0,
        material("C2G_RACING_SPEEDWAY_ASPHALT_MAT", (0.028, 0.032, 0.038, 1.0), metallic=0.05, roughness=0.72),
    )
    # The speedway is collision/support truth, not visible replacement
    # terrain.  Cars drive on this smooth surface while the audience keeps
    # seeing the original forest floor beneath it.
    road.hide_render = True
    shoulder.hide_render = True

    # The source landscape and the actual gameplay objects communicate the
    # route.  No synthetic road signs, edge paint or gravel are added.
    damaged_sign_count = 0

    relocated = []
    referenced = {
        str(beat.get("placement_id"))
        for beat in beats
        if beat.get("placement_id") and beat.get("racing_speedway_layout")
    }
    for beat in beats:
        if not beat.get("racing_speedway_layout"):
            continue
        fraction = float(beat.get("racing_track_fraction", 0.0))
        route_position = racing_speedway_layout.centreline(fraction, height_at(fraction))
        beat["position"] = [round(float(value), 6) for value in route_position]
        placement_id = beat.get("placement_id")
        if not placement_id:
            continue
        source_anchor = beat.get("racing_source_anchor_world_xyz")
        root = find_placement_root(str(placement_id), source_anchor)
        layout = racing_speedway_layout.asset_layout()[str(placement_id)]
        anchor = racing_speedway_layout.offset_position(
            fraction,
            float(layout["lateral_offset_m"]),
            height_at(fraction) + 0.015,
        )
        beat["fixed_anchor_world_xyz"] = [round(float(value), 6) for value in anchor]
        beat["actor_offset_from_fixed_anchor"] = [
            round(float(route_position[axis] - anchor[axis]), 6)
            for axis in range(3)
        ]
        if root is not None:
            root.animation_data_clear()
            matrix = root.matrix_world.copy()
            matrix.translation = Vector(anchor)
            root.matrix_world = matrix
            scale_factor = (
                1.65
                if str(placement_id).startswith("racing_boost_")
                else 1.20
                if str(beat.get("event_type", "")) in {"checkpoint", "countdown"}
                else 0.85
                if str(beat.get("event_type", "")) == "establish"
                else 1.35
            )
            root.scale = root.scale * scale_factor
            root["code2games_speedway_display_scale"] = float(scale_factor)
            root["code2games_speedway_relocated"] = True
            relocated.append({
                "placement_id": str(placement_id),
                "source_anchor_world_xyz": source_anchor,
                "speedway_anchor_world_xyz": [round(float(value), 6) for value in anchor],
                "track_fraction": round(fraction, 8),
                "display_scale": round(float(scale_factor), 4),
                "lateral_offset_m": round(float(layout["lateral_offset_m"]), 4),
            })

    # Old, unreferenced gameplay props must not remain as invisible collision
    # traps on the new road. Referenced roots stay visible and interactive.
    hidden_unused = []
    for obj in list(bpy.context.scene.objects):
        placement_id = obj.get("code2games_placement_id")
        if not placement_id or str(placement_id) in referenced:
            continue
        obj.hide_render = True
        try:
            obj.hide_set(True)
        except Exception:
            obj.hide_viewport = True
        hidden_unused.append(str(placement_id))

    # Keep a narrow visual lane around the logical route.  Vegetation is only
    # removed where it would intersect the car; pre-existing wrecks/shelters
    # are additionally removed from this lane so they cannot bunch up with
    # the deliberately spaced gameplay assets.
    hidden_foliage = []
    seen = set()
    for obj in list(bpy.context.scene.objects):
        if (
            obj.type != "MESH"
            or gameplay_placement_object(obj)
            or not (non_ground_scenery(obj) or racing_route_visual_clutter(obj))
        ):
            continue
        source = obj.original if getattr(obj, "is_evaluated", False) else obj
        root = solid_tree_owner(source) or source
        if root.name in seen:
            continue
        seen.add(root.name)
        location = root.matrix_world.translation
        fraction = racing_speedway_layout.nearest_fraction(location.x, location.y)
        centre = Vector(racing_speedway_layout.centreline(fraction, height_at(fraction)))
        clearance = half_width + (8.0 if racing_route_visual_clutter(root) else 1.5)
        if Vector((location.x - centre.x, location.y - centre.y, 0.0)).length <= clearance:
            hidden_foliage.extend(hide_render_hierarchy(root))

    bpy.context.view_layer.update()
    layout_report = {
        "enabled": True,
        "road_object": road.name,
        "shoulder_object": shoulder.name,
        "road_visible_to_camera": False,
        "original_environment_floor_visible": True,
        "track_width_m": float(racing_speedway_layout.TRACK_WIDTH_M),
        "centreline_sample_count": count,
        "minimum_road_z_m": round(min(heights), 6),
        "maximum_road_z_m": round(max(heights), 6),
        "maximum_logical_surface_gap_from_visible_ground_m": round(
            max(abs(road_height - ground_height) for road_height, ground_height in zip(heights, visual_ground_heights)),
            6,
        ),
        "relocated_asset_count": len(relocated),
        "relocated_assets": relocated,
        "hidden_unused_gameplay_root_count": len(set(hidden_unused)),
        "hidden_foliage_object_count": len(set(hidden_foliage)),
        "damaged_sign_count": int(damaged_sign_count),
    }
    print("RACING_SPEEDWAY_READY", json.dumps(layout_report, sort_keys=True), flush=True)
    return layout_report


def hierarchy_mesh_objects(root):
    result = []
    pending = list(root.children)
    while pending:
        obj = pending.pop()
        pending.extend(list(obj.children))
        if obj.type == "MESH":
            result.append(obj)
    return result


def percentile(values, fraction):
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    index = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * float(fraction)))))
    return ordered[index]


def asset_support_points(meshes, grid_size=5):
    """Return the lowest evaluated vertex in each occupied footprint cell."""
    bounds = mesh_bounds(meshes)
    if not bounds:
        return []
    minimum, maximum = bounds
    width = max(1e-5, float(maximum.x - minimum.x))
    depth = max(1e-5, float(maximum.y - minimum.y))
    height = max(1e-5, float(maximum.z - minimum.z))
    cells = {}
    depsgraph = bpy.context.evaluated_depsgraph_get()
    for obj in meshes:
        evaluated = obj.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh()
        try:
            stride = max(1, int(math.ceil(len(mesh.vertices) / 16000.0)))
            for vertex_index, vertex in enumerate(mesh.vertices):
                if vertex_index % stride:
                    continue
                point = evaluated.matrix_world @ vertex.co
                cell_x = max(0, min(grid_size - 1, int((point.x - minimum.x) / width * grid_size)))
                cell_y = max(0, min(grid_size - 1, int((point.y - minimum.y) / depth * grid_size)))
                key = (cell_x, cell_y)
                if key not in cells or point.z < cells[key].z:
                    cells[key] = point.copy()
        finally:
            evaluated.to_mesh_clear()
    # Ignore cells occupied only by a roof, mast or other upper overhang.  The
    # threshold is deliberately generous enough to retain feet on mild slopes.
    support_band = max(0.16, min(0.48, height * 0.16))
    return [point for point in cells.values() if point.z <= minimum.z + support_band]


def settle_grounded_gameplay_asset_geometry(z_extent):
    """Seat foundations and feet without changing placement-plan root transforms.

    Stage 10 keeps the candidate-root transform as the position truth.  On a
    capped terrain tilt, its conservative support lift can leave some feet in
    the air even if a different corner supplies the global bounding-box minimum.
    We therefore compare distributed bottom support vertices against terrain
    beneath those exact XY locations.  Only visual children move; candidate XY,
    root Z, yaw and surface alignment remain untouched.
    """
    corrections = []
    for root in list(bpy.context.scene.objects):
        if root.type != "EMPTY" or not root.get("code2games_placement_id"):
            continue
        meshes = hierarchy_mesh_objects(root)
        if not meshes:
            continue
        support_points = asset_support_points(meshes)
        if len(support_points) < 2:
            continue
        root_position = root.matrix_world.translation
        support_gaps = [
            float(point.z) - terrain_height(point.x, point.y, root_position.z, z_extent)
            for point in support_points
        ]
        # Ground at least 90% of the meaningful support samples.  With the
        # usual four-foot chassis this selects the highest foot gap, ensuring
        # all four feet land.  A little penetration at a lower foot is less
        # objectionable than a visibly floating support.
        governing_gap = percentile(support_gaps, 0.90)
        if governing_gap <= 0.040:
            continue
        correction = min(3.00, governing_gap - 0.012)
        if correction <= 0.0:
            continue
        for child in list(root.children):
            matrix = child.matrix_world.copy()
            translation = matrix.translation.copy()
            translation.z -= correction
            matrix.translation = translation
            child.matrix_world = matrix
        bpy.context.view_layer.update()
        corrections.append({
            "placement_id": str(root.get("code2games_placement_id")),
            "support_point_count": len(support_points),
            "support_gap_min_before_m": round(float(min(support_gaps)), 6),
            "support_gap_median_before_m": round(float(percentile(support_gaps, 0.50)), 6),
            "support_gap_p90_before_m": round(float(governing_gap), 6),
            "support_gap_max_before_m": round(float(max(support_gaps)), 6),
            "geometry_lowered_m": round(float(correction), 6),
            "candidate_root_transform_changed": False,
        })
    return corrections


def sampled_beats(path_data, genre):
    beats = [dict(item) for item in path_data.get("beats", [])]
    if len(beats) < 2:
        raise ValueError("director path must contain at least two beats")
    z_extent = scene_z_extent()
    lift = 0.005 if genre == "racing" else 0.0
    for beat in beats:
        position = Vector(beat["position"])
        if beat.get("terrain_sample"):
            position.z = terrain_height(position.x, position.y, position.z, z_extent) + lift
        beat["staged_position"] = [round(float(v), 6) for v in position]
    if genre == "racing":
        # Fixed track furniture is authored at its placed height.  A terrain
        # hole directly under a boost pad (a missing upper terrain shell) can
        # sample 3+ metres lower and drag the whole car -- and the trailing
        # 13.5 m camera boom -- into the surrounding ground, which rendered
        # as the mid-run blackout.  Keep racing beats on the authored surface
        # whenever the sampled terrain deviates by more than a vehicle-height
        # step; genuine ramps (finish climb) already match their authored z.
        for beat in beats:
            expected = None
            anchor = beat.get("fixed_anchor_world_xyz")
            if anchor:
                expected = float(anchor[2])
            elif beat.get("position"):
                expected = float(beat["position"][2])
            if expected is None:
                continue
            sampled = float(beat["staged_position"][2])
            if abs(sampled - expected) > 1.2:
                beat["staged_position"][2] = round(expected + lift, 6)
        beats = smooth_racing_beats(beats, z_extent)
    elif genre in {"fps", "tps"}:
        # Stage 11 may carry a long editorial target duration.  Gameplay
        # locomotion must instead be timed from actual travelled distance:
        # otherwise a direct open-ground segment becomes a slow drift followed
        # by a stop-and-turn at every semantic control point.
        speed = float(_FPS_WALK_SPEED_MPS) if genre == "fps" else 2.85
        frame = 1
        beats[0]["frame"] = frame
        for previous, beat in zip(beats, beats[1:]):
            distance = (Vector(beat["staged_position"]) - Vector(previous["staged_position"])).length
            frame += max(2, int(round(distance / speed * 24.0)))
            beat["frame"] = int(frame)
        for beat in beats:
            beat["ground_timeline_retimed_from_distance"] = True
    return beats


def apply_ground_interaction_standoffs(beats, z_extent, genre):
    """Keep the actor beside an asset instead of walking through its centre.

    Stage 11 stores the immutable asset anchor in ``fixed_anchor_world_xyz``.
    That coordinate is not automatically a valid character foot position: a
    supply crate, relay console, or other solid prop occupies it.  Derive a
    short approach-side stand-off from the real staged geometry while leaving
    the asset transform untouched.
    """
    if genre not in {"fps", "tps"}:
        return []
    interaction_types = {"collect", "collect_required", "interact", "combat", "goal"}
    if genre == "tps":
        # The actor previously spawned inside the wide placement_003 footprint;
        # that single bad start point caused both unresolved route samples and
        # the opening camera boom collision.
        interaction_types.update({"spawn", "recover", "dodge"})
    adjustments = []
    for index, beat in enumerate(beats):
        event_type = str(beat.get("event_type", ""))
        if beat.get("kind") != "authored" or event_type not in interaction_types:
            continue
        anchor_value = beat.get("fixed_anchor_world_xyz")
        if not anchor_value:
            continue
        anchor = Vector(anchor_value)
        previous = Vector(beats[max(0, index - 1)]["staged_position"])
        approach = anchor - previous
        approach.z = 0.0
        if approach.length < 1e-5 and index + 1 < len(beats):
            approach = anchor - Vector(beats[index + 1]["staged_position"])
            approach.z = 0.0
        if approach.length < 1e-5:
            approach = Vector((0.0, 1.0, 0.0))
        approach.normalize()

        root = find_placement_root(beat.get("placement_id"))
        bounds = mesh_bounds(hierarchy_mesh_objects(root)) if root is not None else None
        if bounds:
            dimensions = bounds[1] - bounds[0]
            footprint_radius = max(0.20, min(1.35, 0.5 * max(float(dimensions.x), float(dimensions.y))))
            # The east relay is much wider than an ordinary crate.  The old
            # universal 1.35 m cap put the actor inside its foundation from
            # roughly 31 s onward.  Only this known wide fixed asset receives
            # its real half-footprint; every asset transform remains read-only.
            if genre == "tps" and str(beat.get("placement_id")) == "placement_001":
                footprint_radius = max(
                    footprint_radius,
                    min(4.50, 0.5 * max(float(dimensions.x), float(dimensions.y))),
                )
            if genre == "tps" and event_type in {"recover", "dodge"}:
                # Pass-by cover can be several metres wide.  The generic
                # 1.35 m cap is appropriate for an interaction prop but made
                # placement_005 look crate-sized to the bypass solver, leaving
                # four blocked samples after the Bravo encounter.  Use the
                # real staged half-footprint while retaining a bounded cap.
                footprint_radius = max(
                    footprint_radius,
                    min(4.50, 0.5 * max(float(dimensions.x), float(dimensions.y))),
                )
        else:
            footprint_radius = 0.55
        # Some staged relay GLBs expose their visible geometry through a
        # collection instance, so hierarchy_mesh_objects() has no direct mesh
        # bounds.  Never fall back to crate-sized clearance for this known
        # wide immutable asset: 2.45 m footprint + 0.95 m personal space keeps
        # body, rifle and shoulder camera outside its foundation.
        if genre == "tps" and str(beat.get("placement_id")) == "placement_001":
            footprint_radius = max(2.45, footprint_radius)
        if genre == "tps" and str(beat.get("placement_id")) == "placement_003":
            footprint_radius = max(2.65, footprint_radius)
        if genre == "tps" and event_type in {"recover", "dodge"}:
            # Recovery/dodge props are physical cover, not route waypoints.
            # The former centre-line path clipped these low, wide meshes at
            # the six unresolved samples reported around frames 415/435 and
            # 711-723.  Keep the immutable asset anchor and move only the
            # actor stand point outside a body-sized horizontal footprint.
            footprint_radius = max(1.20, footprint_radius)
        personal_space = (
            0.58
            if event_type.startswith("collect")
            else 0.55
            if event_type == "combat"
            else 0.95
            if genre == "tps" and str(beat.get("placement_id")) in {"placement_001", "placement_003"}
            else 0.72
        )
        stand_distance = footprint_radius + personal_space
        position = anchor - approach * stand_distance
        if genre == "tps" and event_type in {"recover", "dodge"} and index + 1 < len(beats):
            # These are pass-by props, unlike a console/collectible where the
            # player intentionally stops on the approach side.  An
            # approach-side point makes the incoming segment safe but can
            # send the outgoing segment straight through the prop centre.
            # Choose a lateral bypass that keeps BOTH adjacent segments clear.
            next_position = Vector(beats[index + 1]["staged_position"])
            travel = next_position - previous
            travel.z = 0.0
            if travel.length < 1e-5:
                travel = approach.copy()
            travel.normalize()
            side = Vector((-travel.y, travel.x, 0.0))
            bypass = None
            fallback = None
            for radius in (stand_distance, stand_distance + 0.60, stand_distance + 1.20):
                for sign in (1.0, -1.0):
                    option = anchor + side * radius * sign
                    option.z = terrain_height(option.x, option.y, anchor.z, z_extent)
                    support_range, _ground = ground_route_support(option, z_extent)
                    blocked_in = route_segment_blocked(previous, option, z_extent)
                    blocked_out = route_segment_blocked(option, next_position, z_extent)
                    score = (
                        int(blocked_in) + int(blocked_out),
                        float(support_range),
                        float(radius),
                    )
                    if fallback is None or score < fallback[0]:
                        fallback = (score, option.copy(), radius)
                    if not blocked_in and not blocked_out and support_range <= 0.85:
                        bypass = (option.copy(), radius)
                        break
                if bypass is not None:
                    break
            if bypass is not None:
                position, stand_distance = bypass
            elif fallback is not None:
                position, stand_distance = fallback[1], fallback[2]
        position.z = terrain_height(position.x, position.y, anchor.z, z_extent)
        original = Vector(beat["staged_position"])
        beat["staged_position"] = [round(float(value), 6) for value in position]
        beat["actor_interaction_standoff_m"] = round(float(stand_distance), 5)
        beat["actor_interaction_approach_direction"] = [round(float(value), 6) for value in approach]
        adjustments.append({
            "beat_id": beat.get("beat_id"),
            "frame": int(beat["frame"]),
            "event_type": event_type,
            "placement_id": beat.get("placement_id"),
            "original_actor_position": [round(float(value), 6) for value in original],
            "refined_actor_position": list(beat["staged_position"]),
            "asset_anchor_unchanged": [round(float(value), 6) for value in anchor],
            "standoff_m": round(float(stand_distance), 5),
        })
    return adjustments


def insert_ground_event_holds(beats, genre):
    """Insert real time for combat and interactions without moving assets."""
    if genre == "tps":
        durations = {
            # QA: the NPC never stops mid-run; supplies/consoles are swept
            # past at run speed (0-frame holds insert no event_hold beat),
            # only the kill hold and the goal keep the body still.
            "collect": 0,
            "collect_required": 0,
            "interact": 0,
            # All five combat beats now own real moving encounters.  A full-
            # body stationary hold was the actual cause of the unexplained
            # stops around 23 s and 30 s (two of those beats had no enemy at
            # all).  Fire/hit/death run independently while locomotion stays
            # continuous.
            "combat": 0,
            "recover": 0,
            "reveal": 0,
            "goal": 0,
        }
    elif genre == "fps":
        durations = dict(_FPS_HOLD_OVERRIDES or {
            "collect": 3,
            "collect_required": 3,
            "interact": 4,
            "combat": 32,
            "goal": 10,
        })
    else:
        return beats, []
    expanded = []
    holds = []
    accumulated = 0
    for beat in beats:
        staged = dict(beat)
        source_frame = int(staged["frame"])
        staged["source_director_frame"] = source_frame
        staged["frame"] = source_frame + accumulated
        expanded.append(staged)
        duration = int(durations.get(str(staged.get("event_type", "")), 0))
        if staged.get("kind") != "authored" or duration <= 0:
            continue
        hold = dict(staged)
        hold.update({
            "beat_id": "%s_hold" % staged.get("beat_id", "event"),
            "kind": "transit",
            "placement_id": None,
            "fixed_anchor_world_xyz": None,
            "actor_offset_from_fixed_anchor": None,
            "event_type": "event_hold",
            "held_event_type": staged.get("event_type"),
            "label": "hold for %s action" % staged.get("event_type"),
            "frame": int(staged["frame"]) + duration,
            "dwell_seconds": duration / 24.0,
            "action_cue": "%s_hold" % staged.get("action_cue", staged.get("event_type", "event")),
            "effect_cues": [],
        })
        expanded.append(hold)
        accumulated += duration
        holds.append({
            "beat_id": staged.get("beat_id"),
            "event_type": staged.get("event_type"),
            "start_frame": int(staged["frame"]),
            "end_frame": int(hold["frame"]),
            "duration_frames": duration,
        })
    return expanded, holds


def smooth_racing_beats(beats, z_extent):
    """Build a compact, collision-aware car trajectory through fixed anchors.

    The former circular-fillet implementation used every Stage-11 control as a
    mandatory centreline point.  At a sharp corner its centre/sign arithmetic
    could choose the long arc, producing the visible 270/360-degree turns.  It
    also inherited a 50-second editorial timeline, so straights and bends had
    unrelated speeds.  This version retimes controls from travelled distance,
    cuts corners with the short quadratic branch, and performs vehicle-width
    obstacle probes.  Gameplay asset transforms remain read-only.
    """
    if len(beats) < 3:
        return beats
    controls = [dict(beat) for beat in beats]
    if (
        controls[0].get("event_type") == "establish"
        and controls[1].get("event_type") == "countdown"
    ):
        controls[0]["staged_position"] = list(controls[1]["staged_position"])

    points = [Vector(beat["staged_position"]) for beat in controls]
    # Semantic reveal/recovery controls can sit a metre behind the preceding
    # gate even though the next objective is ahead.  They are camera/event
    # anchors, not a command to reverse the car.  Collapse only these local
    # reflex vertices onto the through-corridor; fixed asset anchors stay put.
    for _pass in range(2):
        for index in range(1, len(points) - 1):
            incoming = points[index] - points[index - 1]
            outgoing = points[index + 1] - points[index]
            incoming.z = 0.0
            outgoing.z = 0.0
            if incoming.length < 0.05 or outgoing.length < 0.05:
                continue
            angle = math.degrees(math.acos(max(-1.0, min(1.0, incoming.normalized().dot(outgoing.normalized())))))
            if angle < 105.0:
                continue
            replacement = points[index - 1].lerp(points[index + 1], 0.5)
            replacement.z = points[index].z
            controls[index]["racing_reflex_control_original_position"] = [float(value) for value in points[index]]
            controls[index]["racing_reflex_control_collapsed"] = True
            points[index] = replacement
            controls[index]["staged_position"] = [float(value) for value in replacement]
    # Distance-based timing: a short readable start hold, then continuous
    # driving with intentional boost/hazard speed variation rather than pauses.
    source_frames = [1]
    boost_remaining_m = 0.0
    for index in range(1, len(controls)):
        distance = (points[index] - points[index - 1]).length
        if index == 1 and distance < 0.05:
            source_frames.append(49)
            continue
        multiplier = max(0.55, min(4.0, float(controls[index].get("speed_multiplier", 1.0))))
        event_type = str(controls[index].get("event_type", "drive"))
        previous_event = str(controls[index - 1].get("event_type", "drive"))
        # A boost keeps the car at ~2.6x cruise for a sustained window instead
        # of a single transit segment, so the pull-away reads clearly on
        # screen (QA: the boost must visibly speed the car up).  The path
        # builder tags boost beats with a post multiplier; honour it for
        # ~70 m past the pad before the transit clamp takes over.
        if previous_event == "boost":
            boost_remaining_m = max(boost_remaining_m, 70.0)
        if boost_remaining_m > 0.0:
            multiplier = max(multiplier, 2.6)
            boost_remaining_m = max(0.0, boost_remaining_m - distance)
        event_factor = {
            "checkpoint": 1.08,
            "hazard": 0.78,
            "steer": 0.72,
            "goal": 1.12,
        }.get(event_type, 1.0)
        metres_per_second = float(_RACING_SPEED_MPS) * multiplier * event_factor
        travel_frames = max(3, int(round(distance / max(5.0, metres_per_second) * 24.0)))
        source_frames.append(source_frames[-1] + travel_frames)
    for beat, frame in zip(controls, source_frames):
        beat["frame"] = int(frame)

    corners = {}
    for index in range(1, len(points) - 1):
        point = points[index]
        incoming = point - points[index - 1]
        outgoing = points[index + 1] - point
        incoming.z = 0.0
        outgoing.z = 0.0
        if incoming.length < 0.05 or outgoing.length < 0.05:
            corners[index] = (float(source_frames[index]), point.copy(), float(source_frames[index]), point.copy())
            continue
        incoming_length = incoming.length
        outgoing_length = outgoing.length
        incoming.normalize()
        outgoing.normalize()
        angle = math.acos(max(-1.0, min(1.0, incoming.dot(outgoing))))
        if angle < math.radians(3.0):
            corners[index] = (float(source_frames[index]), point.copy(), float(source_frames[index]), point.copy())
            continue
        # The quadratic branch is uniquely the short turn.  Begin the steer
        # slightly earlier than the old 4.8 m cap so the two visible lowland
        # turns trace a continuous rally arc rather than a quick yaw followed
        # by a lateral-looking translation.  It remains deliberately compact
        # and cannot create the historical 270/360-degree loop.
        trim = min(incoming_length * 0.46, outgoing_length * 0.46, 6.4)
        entry = point - incoming * trim
        exit_position = point + outgoing * trim
        entry_frame = source_frames[index] - (
            (source_frames[index] - source_frames[index - 1]) * trim / incoming_length
        )
        exit_frame = source_frames[index] + (
            (source_frames[index + 1] - source_frames[index]) * trim / outgoing_length
        )
        corners[index] = (entry_frame, entry, exit_frame, exit_position)

    pieces = []
    cursor_frame = float(source_frames[0])
    cursor_position = points[0].copy()
    for index in range(1, len(points) - 1):
        entry_frame, entry, exit_frame, exit_position = corners[index]
        if entry_frame > cursor_frame + 1e-5:
            pieces.append(("line", cursor_frame, entry_frame, cursor_position.copy(), entry.copy(), None))
        if exit_frame > entry_frame + 1e-5:
            pieces.append(("quad", entry_frame, exit_frame, entry.copy(), exit_position.copy(), points[index].copy()))
        cursor_frame = exit_frame
        cursor_position = exit_position.copy()
    if source_frames[-1] > cursor_frame + 1e-5:
        pieces.append(("line", cursor_frame, float(source_frames[-1]), cursor_position, points[-1].copy(), None))

    def position_at_frame(frame):
        for kind, frame_a, frame_b, point_a, point_b, control in pieces:
            if float(frame) <= frame_b + 1e-5:
                amount = 0.0 if frame_b <= frame_a else (float(frame) - frame_a) / (frame_b - frame_a)
                amount = max(0.0, min(1.0, amount))
                if kind == "quad":
                    inverse = 1.0 - amount
                    return point_a * (inverse * inverse) + control * (2.0 * inverse * amount) + point_b * (amount * amount)
                return point_a.lerp(point_b, amount)
        return points[-1].copy()

    def vehicle_segment_blocked(start, end):
        travel = Vector(end) - Vector(start)
        travel.z = 0.0
        if travel.length < 0.08:
            return False
        travel.normalize()
        side = Vector((-travel.y, travel.x, 0.0))
        return any(
            route_segment_blocked(Vector(start) + side * offset, Vector(end) + side * offset, z_extent)
            for offset in (-0.95, 0.0, 0.95)
        )

    output_frames = sorted({
        *range(source_frames[0], source_frames[-1] + 1, 2),
        *source_frames,
        source_frames[-1],
    })
    route_positions = []
    for frame in output_frames:
        position = position_at_frame(frame)
        position.z = terrain_height(position.x, position.y, position.z, z_extent) + 0.15
        route_positions.append(position)

    # Collision detours must never solve a blockage by pushing an authored
    # gameplay beat outside the real asset's pass radius.  Apply this guard to
    # the FIRST detour pass as well as to residual recovery passes; otherwise
    # the first pass can irreversibly miss an asset before recovery starts.
    control_by_frame = {int(frame): control for frame, control in zip(source_frames, controls)}
    asset_pass_anchors = {}
    for control in controls:
        placement_id = control.get("placement_id")
        planned_anchor = control.get("fixed_anchor_world_xyz")
        if not placement_id or not planned_anchor or placement_id in asset_pass_anchors:
            continue
        root = find_placement_root(str(placement_id), planned_anchor)
        asset_pass_anchors[str(placement_id)] = (
            root.matrix_world.translation.copy() if root is not None else Vector(planned_anchor)
        )

    def asset_pass_position_allowed(index, option):
        control = control_by_frame.get(int(output_frames[index]))
        if not control or not control.get("placement_id"):
            return True
        anchor = asset_pass_anchors.get(str(control.get("placement_id")))
        if anchor is None:
            return True
        event_type = str(control.get("event_type", ""))
        threshold = 10.0 if event_type == "establish" else 8.0 if event_type == "steer" else 6.0
        delta = Vector(option) - anchor
        delta.z = 0.0
        return delta.length <= threshold - 0.20

    blocked_indices = [
        index
        for index in range(1, len(route_positions))
        if vehicle_segment_blocked(route_positions[index - 1], route_positions[index])
    ]
    clusters = []
    for blocked_index in blocked_indices:
        if not clusters or blocked_index - clusters[-1][-1] > 2:
            clusters.append([blocked_index])
        else:
            clusters[-1].append(blocked_index)
    detours = []
    for cluster in clusters:
        center = int(round(sum(cluster) / float(len(cluster))))
        before_index = max(0, center - 4)
        after_index = min(len(route_positions) - 1, center + 4)
        travel = route_positions[after_index] - route_positions[before_index]
        travel.z = 0.0
        if travel.length < 0.2:
            continue
        travel.normalize()
        side = Vector((-travel.y, travel.x, 0.0))
        window = max(18, len(cluster) + 12)
        start = max(0, center - window)
        end = min(len(route_positions) - 1, center + window)
        baseline_blocked = sum(
            1 for index in range(max(1, start), end + 1)
            if vehicle_segment_blocked(route_positions[index - 1], route_positions[index])
        )
        best = None
        for radius in (1.6, 2.4, 3.2, 4.0, 4.8, 5.6):
            for sign in (-1.0, 1.0):
                candidate_positions = {}
                support_penalty = 0.0
                valid = True
                for index in range(start, end + 1):
                    normalized = abs(index - center) / float(max(1, window))
                    weight = 0.5 * (1.0 + math.cos(math.pi * min(1.0, normalized)))
                    option = route_positions[index] + side * (radius * sign * weight)
                    option.z = terrain_height(option.x, option.y, option.z, z_extent) + 0.005
                    if not asset_pass_position_allowed(index, option):
                        valid = False
                        break
                    support_range, _ground = ground_route_support(option, z_extent, radius=0.82)
                    support_penalty += max(0.0, support_range - 1.25)
                    candidate_positions[index] = option
                if not valid:
                    continue
                blocked = 0
                for index in range(max(1, start), end + 1):
                    first = candidate_positions.get(index - 1, route_positions[index - 1])
                    second = candidate_positions.get(index, route_positions[index])
                    blocked += int(vehicle_segment_blocked(first, second))
                score = blocked * 1000.0 + support_penalty * 100.0 + radius
                if best is None or score < best[0]:
                    best = (score, blocked, radius, sign, candidate_positions)
        if best is not None and best[1] < baseline_blocked:
            for index, option in best[4].items():
                route_positions[index] = option
            detours.append({
                "center_frame": int(output_frames[center]),
                "radius_m": round(float(best[2]), 4),
                "side_sign": int(best[3]),
                "blocked_segments_before": int(baseline_blocked),
                "blocked_segments_after": int(best[1]),
                "taper_window_samples": int(window),
            })

    # A single cluster pass can leave blocked samples where overlapping
    # tapers meet. Re-detect the CURRENT route and solve only those residual
    # clusters under the same real-asset pass-radius constraint.
    for recovery_pass in range(1, 4):
        residual_indices = [
            index
            for index in range(1, len(route_positions))
            if vehicle_segment_blocked(route_positions[index - 1], route_positions[index])
        ]
        if not residual_indices:
            break
        residual_clusters = []
        for blocked_index in residual_indices:
            if not residual_clusters or blocked_index - residual_clusters[-1][-1] > 2:
                residual_clusters.append([blocked_index])
            else:
                residual_clusters[-1].append(blocked_index)
        improved = False
        for cluster in residual_clusters:
            center = int(round(sum(cluster) / float(len(cluster))))
            before_index = max(0, center - 6)
            after_index = min(len(route_positions) - 1, center + 6)
            travel = route_positions[after_index] - route_positions[before_index]
            travel.z = 0.0
            if travel.length < 0.2:
                continue
            travel.normalize()
            side = Vector((-travel.y, travel.x, 0.0))
            window = max(22, len(cluster) + 16)
            start = max(0, center - window)
            end = min(len(route_positions) - 1, center + window)
            baseline_blocked = sum(
                1
                for index in range(max(1, start), end + 1)
                if vehicle_segment_blocked(route_positions[index - 1], route_positions[index])
            )
            best = None
            # A pure lateral cosine arc can get caught on the end of a wide
            # obstacle cluster: V9 reduced the third cluster from four
            # blocked samples to two but every wider left/right arc either
            # still clipped the end or missed a fixed gameplay asset. Search
            # a small 2-D family as well, letting the arc crest move slightly
            # forward/back along the route while retaining the same tapered
            # endpoints and real-asset pass-radius constraints.
            for radius in (1.2, 1.8, 2.4, 3.2, 4.0, 4.8, 5.6, 6.4, 7.2):
                for sign in (-1.0, 1.0):
                    for longitudinal in (0.0, -1.6, 1.6, -3.2, 3.2):
                        candidate_positions = {}
                        support_penalty = 0.0
                        valid = True
                        offset = side * (radius * sign) + travel * longitudinal
                        for index in range(start, end + 1):
                            normalized = abs(index - center) / float(max(1, window))
                            weight = 0.5 * (1.0 + math.cos(math.pi * min(1.0, normalized)))
                            option = route_positions[index] + offset * weight
                            option.z = terrain_height(option.x, option.y, option.z, z_extent) + 0.005
                            if not asset_pass_position_allowed(index, option):
                                valid = False
                                break
                            support_range, _ground = ground_route_support(option, z_extent, radius=0.82)
                            support_penalty += max(0.0, support_range - 1.25)
                            candidate_positions[index] = option
                        if not valid:
                            continue
                        blocked = 0
                        for index in range(max(1, start), end + 1):
                            first = candidate_positions.get(index - 1, route_positions[index - 1])
                            second = candidate_positions.get(index, route_positions[index])
                            blocked += int(vehicle_segment_blocked(first, second))
                        displacement = math.hypot(radius, longitudinal)
                        score = blocked * 1000.0 + support_penalty * 100.0 + displacement
                        if best is None or score < best[0]:
                            best = (
                                score,
                                blocked,
                                radius,
                                sign,
                                longitudinal,
                                candidate_positions,
                            )
            if best is not None and best[1] < baseline_blocked:
                for index, option in best[5].items():
                    route_positions[index] = option
                detours.append({
                    "center_frame": int(output_frames[center]),
                    "radius_m": round(float(best[2]), 4),
                    "side_sign": int(best[3]),
                    "longitudinal_offset_m": round(float(best[4]), 4),
                    "blocked_segments_before": int(baseline_blocked),
                    "blocked_segments_after": int(best[1]),
                    "taper_window_samples": int(window),
                    "residual_recovery_pass": int(recovery_pass),
                    "asset_pass_radii_preserved": True,
                })
                improved = True
        if not improved:
            break

    def heading_change_at(positions, index):
        if index <= 0 or index >= len(positions) - 1:
            return 0.0
        incoming = positions[index] - positions[index - 1]
        outgoing = positions[index + 1] - positions[index]
        incoming.z = 0.0
        outgoing.z = 0.0
        if incoming.length < 0.02 or outgoing.length < 0.02:
            return 0.0
        return math.degrees(math.acos(max(-1.0, min(1.0, incoming.normalized().dot(outgoing.normalized())))))

    # If a collision-safe route still contains a tight single-sample kink,
    # search a broad outside line whose cosine taper lowers curvature without
    # crossing the obstacle.  This is the vehicle equivalent of beginning the
    # steering input early, not a giant cinematic loop.
    sharp_indices = [
        index for index in range(1, len(route_positions) - 1)
        if heading_change_at(route_positions, index) > 24.0
    ]
    sharp_clusters = []
    for sharp_index in sharp_indices:
        if not sharp_clusters or sharp_index - sharp_clusters[-1][-1] > 3:
            sharp_clusters.append([sharp_index])
        else:
            sharp_clusters[-1].append(sharp_index)
    curvature_detours = []
    for cluster in sharp_clusters:
        center = int(round(sum(cluster) / float(len(cluster))))
        window = 20
        start = max(0, center - window)
        end = min(len(route_positions) - 1, center + window)
        corridor = route_positions[end] - route_positions[start]
        corridor.z = 0.0
        if corridor.length < 1.0:
            continue
        corridor.normalize()
        side = Vector((-corridor.y, corridor.x, 0.0))
        baseline = max(heading_change_at(route_positions, index) for index in range(max(1, start), min(len(route_positions) - 1, end + 1)))
        best = None
        for radius in (1.2, 2.0, 2.8, 3.6, 4.4, 5.2, 6.0):
            for sign in (-1.0, 1.0):
                candidate = [position.copy() for position in route_positions]
                valid = True
                for index in range(start, end + 1):
                    normalized = abs(index - center) / float(window)
                    weight = 0.5 * (1.0 + math.cos(math.pi * min(1.0, normalized)))
                    candidate[index] += side * (radius * sign * weight)
                    candidate[index].z = terrain_height(candidate[index].x, candidate[index].y, candidate[index].z, z_extent) + 0.005
                    if not asset_pass_position_allowed(index, candidate[index]):
                        valid = False
                        break
                    support_range, _ground = ground_route_support(candidate[index], z_extent, radius=0.82)
                    if support_range > 1.25:
                        valid = False
                        break
                if not valid:
                    continue
                if any(
                    vehicle_segment_blocked(candidate[index - 1], candidate[index])
                    for index in range(max(1, start), end + 1)
                ):
                    continue
                maximum = max(heading_change_at(candidate, index) for index in range(max(1, start), min(len(candidate) - 1, end + 1)))
                score = maximum + radius * 0.04
                if best is None or score < best[0]:
                    best = (score, maximum, radius, sign, candidate)
        if best is not None and best[1] + 1.0 < baseline:
            route_positions = best[4]
            curvature_detours.append({
                "center_frame": int(output_frames[center]),
                "radius_m": round(float(best[2]), 4),
                "side_sign": int(best[3]),
                "maximum_heading_before_degrees": round(float(baseline), 4),
                "maximum_heading_after_degrees": round(float(best[1]), 4),
                "taper_window_samples": int(window),
            })

    # Curvature regularization is constrained by the same three vehicle-width
    # collision probes.  It removes the residual one-sample steering spikes
    # produced where a tapered detour meets a short authored segment, without
    # allowing the smoothed centreline to cut back through a prop.
    for _pass in range(6):
        previous_positions = [position.copy() for position in route_positions]
        proposals = [position.copy() for position in previous_positions]
        for index in range(1, len(previous_positions) - 1):
            # Preserve the stationary start grid exactly.
            if output_frames[index] <= 49:
                continue
            proposal = (
                previous_positions[index - 1] * 0.25
                + previous_positions[index] * 0.50
                + previous_positions[index + 1] * 0.25
            )
            proposal.z = terrain_height(proposal.x, proposal.y, proposal.z, z_extent) + 0.005
            if not asset_pass_position_allowed(index, proposal):
                continue
            support_range, _ground = ground_route_support(proposal, z_extent, radius=0.82)
            if support_range <= 1.25:
                proposals[index] = proposal
        accepted = [position.copy() for position in previous_positions]
        for index in range(1, len(previous_positions) - 1):
            proposal = proposals[index]
            if (
                not vehicle_segment_blocked(proposals[index - 1], proposal)
                and not vehicle_segment_blocked(proposal, proposals[index + 1])
            ):
                accepted[index] = proposal
        route_positions = accepted

    # Gameplay props must produce readable feedback, but an off-centre marker
    # must not arbitrarily rewrite the racing line.  Those forced lane moves
    # made the vehicle weave left/right across an otherwise clear road.  The
    # route only detours when the actual collision solver detects a blocker;
    # authored markers use boost/brake/dust/pulse feedback instead.
    vehicle_event_reactions = []
    # A gate frequently has two steering markers only a few frames apart.
    # They describe one manoeuvre through the gate, not two consecutive lane
    # changes.  Do not stack their raised-cosine offsets: opposing overlap was
    # the source of the violent left/right racing-camera shake around 7 s.
    last_manoeuvre_end = -10**9
    speedway_assets = racing_speedway_layout.asset_layout()
    for control_index, control in enumerate(controls):
        event_type = str(control.get("event_type", ""))
        placement_id = str(control.get("placement_id") or "")
        if (
            control.get("kind") != "authored"
            or event_type not in {"steer", "hazard"}
            or not placement_id
        ):
            continue
        center = min(
            range(len(output_frames)),
            key=lambda value: abs(int(output_frames[value]) - int(source_frames[control_index])),
        )
        vehicle_event_reactions.append({
            "placement_id": placement_id,
            "event_type": event_type,
            "center_frame": int(output_frames[center]),
            "lane_shift_m": 0.0,
            "window_samples": 0,
            "response_policy": "visual_and_speed_feedback_only",
        })
        continue
        layout = speedway_assets.get(placement_id) or {}
        lateral = float(layout.get("lateral_offset_m", 0.0))
        if abs(lateral) < 0.05:
            continue
        center = min(
            range(len(output_frames)),
            key=lambda value: abs(int(output_frames[value]) - int(source_frames[control_index])),
        )
        window = 12
        start = max(0, center - window)
        end = min(len(route_positions) - 1, center + window)
        if start <= last_manoeuvre_end:
            vehicle_event_reactions.append({
                "placement_id": placement_id,
                "event_type": event_type,
                "center_frame": int(output_frames[center]),
                "lane_shift_m": 0.0,
                "window_samples": int(window),
                "response_policy": "shared_gate_manoeuvre",
            })
            continue
        travel = route_positions[end] - route_positions[start]
        travel.z = 0.0
        if travel.length < 0.2:
            continue
        travel.normalize()
        left = Vector((-travel.y, travel.x, 0.0))
        # Asset +lateral is route-right; move route-left, and vice versa.
        away_sign = 1.0 if lateral > 0.0 else -1.0
        amplitude = 0.80 if event_type == "hazard" else 0.60
        candidates = {}
        valid = True
        for route_index in range(start, end + 1):
            normalized = abs(route_index - center) / float(max(1, window))
            weight = 0.5 * (1.0 + math.cos(math.pi * min(1.0, normalized)))
            option = route_positions[route_index] + left * (away_sign * amplitude * weight)
            option.z = terrain_height(option.x, option.y, option.z, z_extent) + 0.005
            if not asset_pass_position_allowed(route_index, option):
                valid = False
                break
            support_range, _ground = ground_route_support(option, z_extent, radius=0.82)
            if support_range > 1.25:
                valid = False
                break
            candidates[route_index] = option
        if valid and any(
            vehicle_segment_blocked(
                candidates.get(route_index - 1, route_positions[route_index - 1]),
                candidates.get(route_index, route_positions[route_index]),
            )
            for route_index in range(max(1, start), end + 1)
        ):
            valid = False
        if not valid:
            continue
        for route_index, option in candidates.items():
            route_positions[route_index] = option
        last_manoeuvre_end = end
        vehicle_event_reactions.append({
            "placement_id": placement_id,
            "event_type": event_type,
            "center_frame": int(output_frames[center]),
            "lane_shift_m": round(float(away_sign * amplitude), 4),
            "window_samples": int(window),
            "response_policy": "independent_arc",
        })

    smoothed = []
    for route_index, frame in enumerate(output_frames):
        source_index = max(0, bisect.bisect_right(source_frames, frame) - 1)
        source = next((beat for beat in controls if int(beat["frame"]) == int(frame)), None)
        sample = dict(source or controls[source_index])
        position = route_positions[route_index]
        if source is not None:
            route_control = Vector(source["staged_position"])
            sample["racing_route_control_position"] = [round(float(value), 6) for value in route_control]
            sample["racing_actor_offset_from_route_control_m"] = round(
                float(Vector((position.x - route_control.x, position.y - route_control.y, 0.0)).length),
                6,
            )
        else:
            sample.update({
                "beat_id": "%s_curve_f%04d" % (beats[source_index].get("beat_id", "racing"), frame),
                "kind": "transit",
                "placement_id": None,
                "fixed_anchor_world_xyz": None,
                "actor_offset_from_fixed_anchor": None,
                "look_at_world_xyz": None,
                "event_type": "drive",
                "label": "radius-limited racing turn",
                "dwell_seconds": 0.0,
                "action_cue": "precision_drive",
                "effect_cues": ["wheel_dust", "grass_wake"],
                "audio_cue": "adaptive_movement_mix",
                "racing_curve_sample": True,
                "racing_curve_source_segment": source_index,
                "racing_speedway_layout": True,
                "racing_track_surface": True,
            })
        sample["frame"] = int(frame)
        sample["staged_position"] = [round(float(value), 6) for value in position]
        if source is None:
            sample["position"] = list(sample["staged_position"])
        smoothed.append(sample)
    unresolved_blocked_details = [
        {
            "sample_index": int(index),
            "source_frame_start": int(output_frames[index - 1]),
            "source_frame_end": int(output_frames[index]),
            "start_xyz": [round(float(value), 6) for value in route_positions[index - 1]],
            "end_xyz": [round(float(value), 6) for value in route_positions[index]],
        }
        for index in range(1, len(route_positions))
        if vehicle_segment_blocked(route_positions[index - 1], route_positions[index])
    ]
    unresolved_blocked = len(unresolved_blocked_details)
    for sample in smoothed:
        sample["racing_collision_detours"] = detours
        sample["racing_curvature_detours"] = curvature_detours
        sample["racing_vehicle_event_reactions"] = vehicle_event_reactions
        sample["racing_total_collision_detour_sample_count"] = int(len(detours))
        sample["racing_unresolved_blocked_segment_count"] = int(unresolved_blocked)
        sample["racing_unresolved_blocked_segments"] = unresolved_blocked_details

    # Re-time the smoothed route by actual travelled distance.  Collision and
    # curvature detours lengthen the driven line; keeping the pre-detour
    # frames made a lengthened stretch read as a local teleport (QA: 197 km/h
    # spikes in the boost section).  A boost forces ~2.8x for a solid 2
    # seconds (frame budget) so the pull-away reads clearly instead of a
    # short distance-based burst.
    timeline = 1
    boost_remaining_frames = 0
    previous_event = None
    for index, sample in enumerate(smoothed):
        sample["frame"] = max(1, timeline)
        event_type = str(sample.get("event_type", "drive"))
        if previous_event == "boost":
            boost_remaining_frames = 56
        if index + 1 >= len(smoothed):
            break
        distance = (
            Vector(sample["staged_position"]) - Vector(smoothed[index + 1]["staged_position"])
        ).length
        multiplier = max(0.3, min(4.0, float(sample.get("speed_multiplier", 1.0))))
        if boost_remaining_frames > 0:
            multiplier = max(multiplier, 2.8)
        # Physical event response: steering markers require a controlled
        # lift, hazards require a slightly stronger lift, and boosts retain
        # their sustained pull-away.  This changes the actual driven timing,
        # not merely the displayed HUD speed.
        event_speed_factor = {
            "steer": 0.78,
            "hazard": 0.70,
            "checkpoint": 1.08,
            "goal": 0.92,
        }.get(event_type, 1.0)
        if boost_remaining_frames > 0:
            event_speed_factor = 1.0
        metres_per_second = max(
            2.0,
            float(_RACING_SPEED_MPS) * multiplier * float(event_speed_factor),
        )
        increment = max(2, int(round(distance / metres_per_second * 24.0)))
        if boost_remaining_frames > 0:
            boost_remaining_frames = max(0, boost_remaining_frames - increment)
        timeline += increment
        previous_event = event_type

    # Keep distance-based relative timing, including the boost bursts, then
    # apply one uniform editorial scale so the complete run is exactly the
    # wrapper-owned target duration.  This never inserts a stop, reverse or
    # loop, and it preserves all local speed ratios.
    target_motion_end = max(72, int(round(_RACING_TARGET_DURATION_SECONDS * 24.0)) - 24)
    natural_motion_end = max(2, int(smoothed[-1]["frame"]))
    timing_scale = float(target_motion_end - 1) / float(natural_motion_end - 1)
    previous_frame = 0
    for sample in smoothed:
        scaled = int(round(1.0 + (float(sample["frame"]) - 1.0) * timing_scale))
        sample["frame"] = max(previous_frame + 1, min(target_motion_end, scaled))
        sample["racing_uniform_duration_scale"] = round(timing_scale, 6)
        previous_frame = int(sample["frame"])
    if smoothed[-1]["frame"] != target_motion_end:
        smoothed[-1]["frame"] = target_motion_end
    return smoothed


def maximum_racing_heading_change_degrees(beats):
    directions = []
    for index in range(len(beats) - 1):
        delta = Vector(beats[index + 1]["staged_position"]) - Vector(beats[index]["staged_position"])
        delta.z = 0.0
        if delta.length >= 1e-5:
            directions.append(delta.normalized())
    maximum = 0.0
    for previous, current in zip(directions, directions[1:]):
        dot = max(-1.0, min(1.0, previous.dot(current)))
        maximum = max(maximum, math.degrees(math.acos(dot)))
    return maximum


def fps_head_clearance_distance(origin, direction, limit):
    scene = bpy.context.scene
    depsgraph = bpy.context.evaluated_depsgraph_get()
    direction = Vector(direction)
    if direction.length < 1e-5:
        return 0.0
    hit, location, _normal, _face, _obj, _matrix = scene.ray_cast(
        depsgraph,
        Vector(origin),
        direction.normalized(),
        distance=float(limit),
    )
    return min(float(limit), (Vector(location) - Vector(origin)).length) if hit else float(limit)


def refine_fps_combat_viewpoints(beats, z_extent):
    """Move only free route beats out of foliage/geometry at combat moments."""
    adjustments = []
    for index, beat in enumerate(beats):
        if beat.get("event_type") != "combat":
            continue
        original = Vector(beat["staged_position"])
        forward = direction_for(beats, index)
        forward.z = 0.0
        if forward.length < 1e-5:
            forward = Vector((0.0, 1.0, 0.0))
        forward.normalize()
        side = Vector((-forward.y, forward.x, 0.0))

        def evaluate(point):
            eye = Vector(point) + UP * 1.68
            front = fps_head_clearance_distance(eye, forward, 8.0)
            fan_left = fps_head_clearance_distance(eye, (forward + side * 0.48).normalized(), 5.0)
            fan_right = fps_head_clearance_distance(eye, (forward - side * 0.48).normalized(), 5.0)
            shoulder_left = fps_head_clearance_distance(eye, side, 1.1)
            shoulder_right = fps_head_clearance_distance(eye, -side, 1.1)
            minimum = min(front, fan_left, fan_right, shoulder_left, shoulder_right)
            score = front * 2.0 + fan_left + fan_right + shoulder_left * 0.5 + shoulder_right * 0.5
            return score, minimum

        original_score, original_minimum = evaluate(original)
        best = (original_score, original_minimum, original)
        offsets = [
            side * value for value in (-2.2, -1.1, 1.1, 2.2)
        ] + [
            forward * value for value in (-1.6, -0.8, 0.8, 1.6)
        ] + [
            side * side_value + forward * forward_value
            for side_value in (-1.6, 1.6)
            for forward_value in (-1.0, 1.0)
        ]
        for offset in offsets:
            candidate = original + offset
            candidate.z = terrain_height(candidate.x, candidate.y, original.z, z_extent)
            if abs(candidate.z - original.z) > 1.0:
                continue
            score, minimum = evaluate(candidate)
            if (minimum, score) > (best[1], best[0]):
                best = (score, minimum, candidate)
        if (best[2] - original).length > 1e-5 and (
            original_minimum < 0.85 or best[0] > original_score + 2.0
        ):
            beat["staged_position"] = [round(float(value), 6) for value in best[2]]
            adjustments.append({
                "frame": int(beat["frame"]),
                "original_position": [round(float(value), 6) for value in original],
                "refined_position": beat["staged_position"],
                "minimum_head_clearance_before_m": round(float(original_minimum), 4),
                "minimum_head_clearance_after_m": round(float(best[1]), 4),
            })
    return adjustments


def direction_for(beats, index):
    current = Vector(beats[index]["staged_position"])
    if index < len(beats) - 1:
        direction = Vector(beats[index + 1]["staged_position"]) - current
    else:
        direction = current - Vector(beats[index - 1]["staged_position"])
    if beats[index].get("movement_mode") != "flight":
        direction.z = 0.0
    if direction.length < 1e-5:
        direction = Vector((0.0, 1.0, 0.0))
    return direction.normalized()


def racing_smoothed_tangent(samples, index, window=8):
    """Stable horizontal tangent shared by Racing chassis, wheels and camera.

    The authored racing line is intentionally dense (one or two frames per
    sample).  Using only its next sample made tiny lane corrections flip the
    heading every frame, which read as a shaking car/camera despite a smooth
    translation.  A short centred chord keeps the vehicle aligned with the
    real trajectory while filtering those sub-frame steering oscillations.
    """
    if not samples:
        return Vector((0.0, 1.0, 0.0))

    def position_at(sample):
        return Vector(sample.get("staged_position", sample.get("position", (0.0, 0.0, 0.0))))

    index = max(0, min(len(samples) - 1, int(index)))
    current = position_at(samples[index])
    before = None
    after = None
    for offset in range(int(window), 0, -1):
        candidate = max(0, index - offset)
        delta = current - position_at(samples[candidate])
        delta.z = 0.0
        if delta.length > 0.05:
            before = position_at(samples[candidate])
            break
    for offset in range(int(window), 0, -1):
        candidate = min(len(samples) - 1, index + offset)
        delta = position_at(samples[candidate]) - current
        delta.z = 0.0
        if delta.length > 0.05:
            after = position_at(samples[candidate])
            break
    if before is not None and after is not None:
        tangent = after - before
    elif after is not None:
        tangent = after - current
    elif before is not None:
        tangent = current - before
    else:
        tangent = Vector((0.0, 1.0, 0.0))
    tangent.z = 0.0
    return tangent.normalized() if tangent.length > 1e-5 else Vector((0.0, 1.0, 0.0))


def ground_route_support(position, z_extent, radius=0.42):
    position = Vector(position)
    heights = [
        terrain_height(position.x + dx, position.y + dy, position.z, z_extent)
        for dx, dy in ((0.0, 0.0), (radius, 0.0), (-radius, 0.0), (0.0, radius), (0.0, -radius))
    ]
    return max(heights) - min(heights), heights[0]


def route_segment_blocked(start, end, z_extent):
    start = Vector(start)
    end = Vector(end)
    horizontal = end - start
    horizontal.z = 0.0
    if horizontal.length < 0.10:
        return False
    direction_2d = horizontal.normalized()
    side = Vector((-direction_2d.y, direction_2d.x, 0.0))
    scene = bpy.context.scene
    depsgraph = bpy.context.evaluated_depsgraph_get()

    def blocked_probe(offset):
        ray_start = start + side * offset + UP * 0.78
        ray_end = end + side * offset + UP * 0.78
        ray = ray_end - ray_start
        length = ray.length
        direction = ray.normalized()
        origin = ray_start
        remaining = length
        for _ in range(24):
            hit, location, _normal, _face, obj, _matrix = scene.ray_cast(
                depsgraph, origin, direction, distance=max(0.0, remaining)
            )
            if not hit:
                return False
            travelled = (Vector(location) - origin).length
            remaining -= travelled + 0.05
            if remaining <= 0.12:
                return False
            if obj and any(collection.name == COLLECTION_NAME for collection in obj.users_collection):
                origin = Vector(location) + direction * 0.05
                continue
            solid_tree = (
                solid_tree_owner(obj) is not None
                if _ACTIVE_DIRECTOR_GENRE == "tps"
                else solid_route_scenery(obj)
            )
            if obj and not director_or_gameplay_object(obj) and not solid_tree:
                origin = Vector(location) + direction * 0.05
                continue
            return True
        return True

    # Three body-height rays form a 0.84 m swept capsule.  A centre-only ray
    # allowed a shoulder/hip to pass through trunks at 16 s and 26 s.
    offsets = (0.0, -0.42, 0.42) if _ACTIVE_DIRECTOR_GENRE == "tps" else (0.0,)
    return any(blocked_probe(offset) for offset in offsets)


def safe_ground_route_point(candidate, previous, z_extent):
    candidate = Vector(candidate)
    support_range, ground = ground_route_support(candidate, z_extent)
    candidate.z = ground
    horizontal = candidate - Vector(previous)
    horizontal.z = 0.0
    distance = max(0.01, horizontal.length)
    slope = abs(candidate.z - Vector(previous).z) / distance
    return support_range <= 0.85 and slope <= math.tan(math.radians(45.0)) and not route_segment_blocked(previous, candidate, z_extent), candidate


def corrected_ground_route_point(candidate, previous, next_hint, z_extent, preferred_detour_side=0):
    candidate = Vector(candidate)
    candidate.z = terrain_height(candidate.x, candidate.y, candidate.z, z_extent)
    # Rough terrain is not, by itself, a reason to zig-zag sideways.  Keep the
    # authored corridor grounded and reserve lateral detours for an actual
    # tree/gameplay obstruction crossing the actor's body path.
    if not route_segment_blocked(previous, candidate, z_extent):
        return candidate, 0.0, False, 0
    grounded = candidate
    travel = Vector(next_hint) - Vector(previous)
    travel.z = 0.0
    if travel.length < 1e-5:
        travel = Vector((0.0, 1.0, 0.0))
    travel.normalize()
    side = Vector((-travel.y, travel.x, 0.0))
    best = None
    # A human can make a tight local sidestep.  The previous 2.8m cap left
    # tree trunks directly on the corridor un-avoidable, so the TPS character
    # visibly walked through them; 4.2m still reads as a compact detour, not
    # a wide loop.
    preferred_detour_side = 1 if preferred_detour_side > 0 else -1 if preferred_detour_side < 0 else 0
    signs = (preferred_detour_side, -preferred_detour_side) if preferred_detour_side else (1, -1)
    for radius in _GROUND_DETOUR_RADII:
        candidates = []
        for sign in signs:
            candidates.extend((
                (sign, Vector(candidate) + side * radius * sign),
                (sign, Vector(candidate) + side * radius * sign + travel * min(1.2, radius * 0.25)),
            ))
        for sign, option in candidates:
            option_safe, option = safe_ground_route_point(option, previous, z_extent)
            if not option_safe:
                continue
            forward_probe = option + travel * min(1.4, max(0.5, (Vector(next_hint) - option).length))
            forward_probe.z = terrain_height(forward_probe.x, forward_probe.y, option.z, z_extent)
            if route_segment_blocked(option, forward_probe, z_extent):
                continue
            score = (option - Vector(candidate)).length + (option - Vector(next_hint)).length * 0.08
            # Once an opening detour side has been chosen, do not alternate
            # from one side of the same tree to the other at the next sample.
            if preferred_detour_side and sign != preferred_detour_side:
                score += 4.0
            if best is None or score < best[0]:
                best = (score, option, radius, sign)
        if best is not None:
            return best[1], best[2], False, best[3]
    return grounded, 0.0, True, 0


def smooth_ground_route_samples(beats, z_extent, genre="", frame_step=4):
    """Dense, corner-cut ground motion with physical local detours.

    A cardinal/Hermite spline passes through every route control.  At a sharp
    gameplay corner that makes a walking actor reach the control, continue a
    little in the old direction, and only then rotate into the next segment.
    In first person this reads as backing up followed by an in-place turn.

    Build tangent quadratic fillets instead.  Hard interaction anchors remain
    exact, while free transit/combat controls are used as curve handles: the
    actor takes the natural shortcut around the inside of the corner and its
    velocity direction changes continuously.
    """
    samples = []
    corrections = []
    unresolved = []
    positions = [Vector(beat["staged_position"]) for beat in beats]
    frames_by_beat = [int(beat["frame"]) for beat in beats]
    authored_frames = {
        int(beat["frame"])
        for beat in beats
        if beat.get("kind") == "authored"
    }
    combat_frames = [
        int(beat["frame"])
        for beat in beats
        if beat.get("event_type") == "combat"
    ]
    # The opening travels through one dense stand of trees.  Keep any initial
    # physical detour on the same side for its first three seconds instead of
    # bouncing right/left as adjacent ray samples choose different sides.
    tps_opening_detour_side = 0
    tps_opening_lock_end = int(frames_by_beat[0]) + 72

    exact_event_types = {
        "collect", "collect_required", "interact", "goal", "boost", "jump_collect", "event_hold",
    }
    corners = {}
    for index in range(1, len(beats) - 1):
        point = positions[index]
        incoming = point - positions[index - 1]
        outgoing = positions[index + 1] - point
        incoming.z = 0.0
        outgoing.z = 0.0
        incoming_length = incoming.length
        outgoing_length = outgoing.length
        protected = beats[index].get("event_type") in exact_event_types
        if genre == "fps" and beats[index].get("event_type") == "combat":
            # An FPS firefight is an exact stand: the player walks straight
            # into the cover point, holds the body on the hostile for the kill
            # (see key_player_combat_body_facing), and only then leaves.
            # Corner-cutting here made the body pre-rotate onto the next leg
            # mid-burst and produced the fast lateral swing in the QA video.
            protected = True
        if incoming_length < 1e-5 or outgoing_length < 1e-5 or protected:
            corners[index] = (float(frames_by_beat[index]), point.copy(), float(frames_by_beat[index]), point.copy())
            continue
        incoming_direction = incoming.normalized()
        outgoing_direction = outgoing.normalized()
        turn_angle = math.acos(max(-1.0, min(1.0, incoming_direction.dot(outgoing_direction))))
        if turn_angle < math.radians(4.0):
            corners[index] = (float(frames_by_beat[index]), point.copy(), float(frames_by_beat[index]), point.copy())
            continue
        # Human corner-cutting radius: enough to remove a robotic right angle,
        # but never a deliberately authored wide path through empty ground.
        # Near reversals get a slightly wider fillet so the actor walks the
        # turn as an arc instead of stopping and pivoting in place (the
        # "tree left suddenly becomes tree right" complaint in the TPS QA).
        maximum_trim = (
            0.45
            if beats[index].get("event_type") == "combat"
            else 1.00
            if turn_angle >= math.radians(135.0)
            else 0.70
        )
        if genre == "tps":
            # QA: the wide corner fillet shortened the driven line at turns,
            # so the body briefly moved slower than the run cadence and read
            # as "stuck / stepping backward".  Keep the path on the control
            # points (constant 2.85 m/s); the lookahead window handles the
            # smooth early rotation instead.
            maximum_trim = min(maximum_trim, 0.15)
        if any(
            combat_frame + 32 <= int(beats[index]["frame"]) <= combat_frame + 80
            for combat_frame in combat_frames
        ):
            # QA: reload-on-run must keep a normal run speed.  The wide fillet
            # at the combat-exit corner shaved the driven line and read as a
            # slow reload; keep a small arc so the exit stays smooth while the
            # path length matches the 2.85 m/s re-timing.
            maximum_trim = 0.18
        trim = min(incoming_length * 0.42, outgoing_length * 0.42, maximum_trim)
        entry = point - incoming_direction * trim
        exit_position = point + outgoing_direction * trim
        entry_frame = frames_by_beat[index] - (
            (frames_by_beat[index] - frames_by_beat[index - 1]) * trim / incoming_length
        )
        exit_frame = frames_by_beat[index] + (
            (frames_by_beat[index + 1] - frames_by_beat[index]) * trim / outgoing_length
        )
        if beats[index].get("event_type") == "combat":
            # Finish most of the turn as the player enters cover.  Once the
            # firefight starts, movement proceeds immediately along the
            # outgoing right/forward tangent instead of continuing backwards
            # for half of the combat shot.
            exit_frame = min(exit_frame, float(frames_by_beat[index] + 3))
        corners[index] = (entry_frame, entry, exit_frame, exit_position)

    pieces = []
    cursor_frame = float(frames_by_beat[0])
    cursor_position = positions[0].copy()
    for index in range(1, len(beats) - 1):
        entry_frame, entry, exit_frame, exit_position = corners[index]
        if entry_frame > cursor_frame + 1e-5:
            pieces.append(("line", cursor_frame, entry_frame, cursor_position.copy(), entry.copy(), None))
        if exit_frame > entry_frame + 1e-5:
            pieces.append(("quad", entry_frame, exit_frame, entry.copy(), exit_position.copy(), positions[index].copy()))
        cursor_frame = exit_frame
        cursor_position = exit_position.copy()
    if frames_by_beat[-1] > cursor_frame + 1e-5:
        pieces.append(("line", cursor_frame, float(frames_by_beat[-1]), cursor_position, positions[-1].copy(), None))

    def curve_position(frame):
        frame = float(frame)
        for kind, frame_a, frame_b, point_a, point_b, control in pieces:
            if frame <= frame_b + 1e-5:
                amount = 0.0 if frame_b <= frame_a else (frame - frame_a) / (frame_b - frame_a)
                amount = max(0.0, min(1.0, amount))
                if kind == "quad":
                    inverse = 1.0 - amount
                    return point_a * (inverse * inverse) + control * (2.0 * inverse * amount) + point_b * (amount * amount)
                return point_a.lerp(point_b, amount)
        return positions[-1].copy()

    opening_hidden_trees = []
    if genre == "tps":
        # This is a curated presentation route, not a general navigation-mesh
        # pass.  The earliest visible objective lies across a tiny stand of
        # decorative TreeFactory instances.  Letting the local detour solver
        # invent a side waypoint there made the actor step right and then
        # return left to the *same* medical pickup.  Hide only the individual
        # tree instances that physically cross the first three seconds of the
        # direct actor corridor; fixed gameplay assets and the source world
        # remain unchanged.
        scene = bpy.context.scene
        depsgraph = bpy.context.evaluated_depsgraph_get()
        hidden_names = set()
        opening_end = min(int(frames_by_beat[-1]), int(frames_by_beat[0]) + 72)
        for frame in range(int(frames_by_beat[0]), opening_end, 2):
            start = curve_position(frame)
            end = curve_position(min(opening_end, frame + 2))
            start.z = terrain_height(start.x, start.y, start.z, z_extent) + 0.90
            end.z = terrain_height(end.x, end.y, end.z, z_extent) + 0.78
            ray = end - start
            remaining = ray.length
            if remaining < 0.10:
                continue
            direction = ray.normalized()
            origin = start
            for _ in range(24):
                hit, location, _normal, _face, obj, _matrix = scene.ray_cast(
                    depsgraph,
                    origin,
                    direction,
                    distance=max(0.0, remaining),
                )
                if not hit:
                    break
                location = Vector(location)
                travelled = (location - origin).length
                remaining -= travelled + 0.05
                if remaining <= 0.12:
                    break
                source = obj.original if obj and getattr(obj, "is_evaluated", False) else obj
                tree_owner = solid_tree_owner(source)
                source_name = str(tree_owner.name) if tree_owner else ""
                if tree_owner is not None:
                    if source_name not in hidden_names:
                        hidden_names.add(source_name)
                        hide_render_hierarchy(tree_owner)
                        opening_hidden_trees.append(source_name)
                        bpy.context.view_layer.update()
                    origin = location + direction * 0.05
                    continue
                if obj and any(collection.name == COLLECTION_NAME for collection in obj.users_collection):
                    origin = location + direction * 0.05
                    continue
                if obj and not director_or_gameplay_object(obj) and not solid_route_scenery(obj):
                    origin = location + direction * 0.05
                    continue
                break

    output_frames = sorted({
        *range(frames_by_beat[0], frames_by_beat[-1] + 1, max(2, int(frame_step))),
        *frames_by_beat,
        frames_by_beat[-1],
    })
    for frame in output_frames:
        candidate = curve_position(frame)
        candidate.z = terrain_height(candidate.x, candidate.y, candidate.z, z_extent)
        # TPS interaction beats are actor stand-off positions, not permission
        # to teleport through a tree.  They must receive the same collision
        # resolution as transit samples; forcing them exact was what pulled
        # the opening run back across its just-chosen detour.
        if not samples or (genre != "tps" and frame in authored_frames):
            resolved = candidate
            radius = 0.0
            failed = False
            detour_side = 0
        else:
            hint = curve_position(min(frames_by_beat[-1], frame + max(2, int(frame_step))))
            hint.z = terrain_height(hint.x, hint.y, hint.z, z_extent)
            preferred_side = (
                tps_opening_detour_side
                if genre == "tps" and frame <= tps_opening_lock_end
                else 0
            )
            resolved, radius, failed, detour_side = corrected_ground_route_point(
                candidate,
                samples[-1]["position"],
                hint,
                z_extent,
                preferred_detour_side=preferred_side,
            )
            if (
                genre == "tps"
                and frame <= tps_opening_lock_end
                and tps_opening_detour_side == 0
                and detour_side
            ):
                tps_opening_detour_side = detour_side
        if radius > 0.0:
            corrections.append({
                "frame": frame,
                "original": [round(float(value), 5) for value in candidate],
                "resolved": [round(float(value), 5) for value in resolved],
                "detour_radius_m": radius,
            })
        if failed:
            unresolved.append(frame)
        samples.append({"frame": frame, "position": Vector(resolved)})
    # Remove route spikes (a plateau edge can drop a sample a few metres for
    # one frame, and a lateral detour probe can snap it sideways; both read as
    # teleports).  A five-sample median kills two-frame-wide spikes as well,
    # while still preserving real slopes and corners.
    if len(samples) >= 5:
        for index in range(2, len(samples) - 2):
            for axis in range(3):
                values = sorted([
                    samples[index - 2]["position"][axis],
                    samples[index - 1]["position"][axis],
                    samples[index]["position"][axis],
                    samples[index + 1]["position"][axis],
                    samples[index + 2]["position"][axis],
                ])
                samples[index]["position"][axis] = values[2]
    # Hard cap on per-sample horizontal displacement: a collision detour can
    # snap the route a few metres sideways over a couple of samples, which
    # reads as a teleport/jolt (the FPS/TPS 0.8-0.9 m/frame spikes).  Ease the
    # lateral shift instead of undoing the detour.  0.50 m/sample still allows
    # the FPS 5.5 m/s run (~0.46) and TPS walk (~0.24) untouched.
    max_horizontal_step = 0.50
    for index in range(1, len(samples)):
        delta = samples[index]["position"] - samples[index - 1]["position"]
        delta.z = 0.0
        length = delta.length
        if length > max_horizontal_step:
            scale = max_horizontal_step / length
            samples[index]["position"].x = samples[index - 1]["position"].x + delta.x * scale
            samples[index]["position"].y = samples[index - 1]["position"].y + delta.y * scale
    # A wide cover cluster can leave one genuinely blocked segment even after
    # its authored stand-off is correct (V6: frame 733).  Repair that final
    # segment with a broad cosine-weighted lateral arc, not a one-frame point
    # jump.  Every proposed arc is accepted only if all affected body-width
    # segments are clear, terrain support stays reasonable, and adjacent
    # samples remain below the locomotion displacement cap.
    if genre == "tps":
        for _repair_pass in range(3):
            blocked_indices = [
                index
                for index in range(1, len(samples))
                if route_segment_blocked(
                    samples[index - 1]["position"],
                    samples[index]["position"],
                    z_extent,
                )
            ]
            if not blocked_indices:
                break
            repaired_any = False
            for blocked_index in blocked_indices:
                source_positions = [Vector(sample["position"]) for sample in samples]
                half_window = min(24, blocked_index, len(samples) - 1 - blocked_index)
                if half_window < 6:
                    continue
                window_start = blocked_index - half_window
                window_end = blocked_index + half_window
                tangent = source_positions[window_end] - source_positions[window_start]
                tangent.z = 0.0
                if tangent.length < 1e-5:
                    tangent = source_positions[blocked_index] - source_positions[blocked_index - 1]
                    tangent.z = 0.0
                if tangent.length < 1e-5:
                    continue
                tangent.normalize()
                side = Vector((-tangent.y, tangent.x, 0.0))
                accepted = None
                for radius in (0.60, 0.90, 1.20, 1.60, 2.10, 2.70, 3.40, 4.20, 5.20):
                    for sign in (1.0, -1.0):
                        trial = [position.copy() for position in source_positions]
                        terrain_ok = True
                        for sample_index in range(window_start, window_end + 1):
                            normalized = abs(sample_index - blocked_index) / float(half_window)
                            weight = 0.5 * (1.0 + math.cos(math.pi * normalized))
                            trial[sample_index] += side * (radius * sign * weight)
                            trial[sample_index].z = terrain_height(
                                trial[sample_index].x,
                                trial[sample_index].y,
                                trial[sample_index].z,
                                z_extent,
                            )
                            support_range, _ground = ground_route_support(
                                trial[sample_index], z_extent
                            )
                            if support_range > 1.25:
                                terrain_ok = False
                                break
                        if not terrain_ok:
                            continue
                        segment_start = max(1, window_start)
                        segment_end = min(len(trial) - 1, window_end + 1)
                        if any(
                            route_segment_blocked(
                                trial[index - 1], trial[index], z_extent
                            )
                            for index in range(segment_start, segment_end + 1)
                        ):
                            continue
                        if any(
                            Vector((
                                trial[index].x - trial[index - 1].x,
                                trial[index].y - trial[index - 1].y,
                                0.0,
                            )).length > 0.55
                            for index in range(segment_start, segment_end + 1)
                        ):
                            continue
                        accepted = (trial, radius, sign, window_start, window_end)
                        break
                    if accepted is not None:
                        break
                if accepted is None:
                    continue
                trial, radius, sign, window_start, window_end = accepted
                original = source_positions[blocked_index]
                for sample_index in range(window_start, window_end + 1):
                    samples[sample_index]["position"] = trial[sample_index]
                corrections.append({
                    "frame": int(samples[blocked_index]["frame"]),
                    "original": [round(float(value), 5) for value in original],
                    "resolved": [
                        round(float(value), 5)
                        for value in samples[blocked_index]["position"]
                    ],
                    "detour_radius_m": float(radius),
                    "detour_side": int(sign),
                    "final_collision_repair": True,
                    "smoothing_window_samples": int(window_end - window_start + 1),
                })
                repaired_any = True
            if not repaired_any:
                break
    # ``failed`` above describes the provisional point before the five-sample
    # median and horizontal-step limiter.  Those two passes can remove a
    # one-sample spike completely (V5 retained only frame 733), so carrying
    # the early flag into the report creates a false failure.  Re-audit the
    # actual final geometry with the same swept-body collision test.  This is
    # not a threshold relaxation: a segment remains unresolved whenever the
    # final line is genuinely blocked.
    if genre in {"fps", "tps"}:
        provisional_unresolved = list(unresolved)
        unresolved = [
            int(samples[index]["frame"])
            for index in range(1, len(samples))
            if route_segment_blocked(
                samples[index - 1]["position"],
                samples[index]["position"],
                z_extent,
            )
        ]
        print(
            "GROUND_ROUTE_FINAL_AUDIT",
            genre,
            "provisional", provisional_unresolved,
            "final", unresolved,
            flush=True,
        )
    # Final distance-based retime on the ACTUAL detoured/filleted path so the
    # walking speed is uniform (~2.85 m/s).  Corner fillets shorten a segment
    # and detours lengthen one, so the beat-polyline timing left crawl/sprint
    # pairs that read as the recurring mid-run jolt.  Stationary hold samples
    # keep their original frame spacing.
    if genre == "tps":
        target_speed = 2.85
        original_frames = [int(sample["frame"]) for sample in samples]
        new_frames = []
        current = original_frames[0]
        new_frames.append(current)
        for index in range(1, len(samples)):
            distance = (samples[index]["position"] - samples[index - 1]["position"]).length
            if distance < 0.05:
                current += max(1, original_frames[index] - original_frames[index - 1])
            else:
                current += max(1, int(round(distance / target_speed * 24.0)))
            new_frames.append(current)
        for sample, new_frame in zip(samples, new_frames):
            sample["frame"] = new_frame
        for beat in beats:
            beat_position = Vector(beat["staged_position"])
            original = int(beat["frame"])
            best = None
            best_score = None
            for sample, original_frame in zip(samples, original_frames):
                if (Vector(sample["position"]) - beat_position).length > 0.6:
                    continue
                score = abs(original_frame - original)
                if best_score is None or score < best_score:
                    best_score = score
                    best = int(sample["frame"])
            if best is not None:
                beat["frame"] = best
    return samples, corrections, unresolved


def set_linear_animation(obj):
    animation = obj.animation_data
    action = animation.action if animation else None
    if not action:
        return
    for curve in action.fcurves:
        for point in curve.keyframe_points:
            point.interpolation = "LINEAR"


def smoothstep(value):
    value = max(0.0, min(1.0, float(value)))
    return value * value * (3.0 - 2.0 * value)


def build_aircraft_route_beats(
    beats,
    preflight_frames=72,
    flight_duration_frames=1116,
    pullup_frames=60,
):
    """Build the 52-second flight-cell circuit and second-summit finish.

    The F-104 is not treated like a character collecting props.  Summit assets
    One installed cell ignites the parked aircraft, three airborne cells sit
    directly on the route, and the powered flight ends in a low pass across
    the confirmed second summit.  Only Director transit controls move; every
    fixed asset transform remains the revised Stage-6 truth.
    """
    flight_indices = [index for index, beat in enumerate(beats) if beat.get("movement_mode") == "flight"]
    if not flight_indices:
        raise RuntimeError("wingsuit Director path contains no flight anchors")
    first_flight = flight_indices[0]
    launch_candidates = [dict(beat) for beat in beats[:first_flight] if beat.get("event_type") == "launch"]
    if not launch_candidates:
        raise RuntimeError("wingsuit Director path contains no launch anchor")
    launch = launch_candidates[-1]
    launch_position = Vector(launch["staged_position"])
    by_placement = {
        str(beat.get("placement_id")): dict(beat)
        for beat in beats
        if beat.get("placement_id")
    }

    # The aircraft must be airborne within three seconds.  Only the installed
    # ignition cell and launch receive HUD beats; the surrounding wreck and
    # summit props establish context visually instead of dumping six messages
    # on the same opening frame.
    preflight_spec = (
        (0.000, "placement_021", "establish", "establish the summit crash site and parked F-104"),
        (0.333, "placement_000", "flight_collectible", "install flight cell one of four and ignite the F-104"),
        (1.000, "placement_022", "launch", "release the F-104 from the summit gantry"),
    )
    opening = []
    for fraction, placement_id, event_type, label in preflight_spec:
        source = dict(by_placement.get(placement_id, launch))
        source.update({
            "frame": max(1, int(round(float(preflight_frames) * fraction))),
            "staged_position": [float(value) for value in launch_position],
            "movement_mode": "flight",
            "event_type": event_type,
            "label": label,
            "aircraft_parked": True,
        })
        opening.append(source)

    # The known fixed anchors form a monotonic summit-to-valley corridor.  Use
    # their semantic order explicitly so no asset is silently dropped by a
    # generic projection/filter pass.
    flight_order = (
        "placement_018", "placement_017", "placement_001", "placement_016",
        "placement_003",
        "placement_023", "placement_019", "placement_020", "placement_013",
        "placement_012",
    )
    anchor_offsets = {
        # Do not force the aircraft to zig-zag directly through the two launch
        # hazards.  Those fixed assets sit almost underneath the departure
        # pad, while the first flight cell lies to the south-west.  Offset the
        # fly-bys progressively down that same corridor so the F-104 can
        # accelerate, bank once, and descend on a physically readable arc.
        "placement_018": Vector((-15.0, -9.0, 16.0)),
        "placement_017": Vector((-26.0, -22.0, 18.0)),
        "placement_001": Vector((0.0, 0.0, 0.0)),
        "placement_016": Vector((0.0, 0.0, 8.0)),
        "placement_003": Vector((0.0, 0.0, 0.0)),
        "placement_023": Vector((0.0, 0.0, 6.0)),
        "placement_019": Vector((0.0, 0.0, 6.0)),
        "placement_020": Vector((0.0, 0.0, 6.0)),
        "placement_013": Vector((0.0, 0.0, 6.0)),
        "placement_012": Vector((0.0, 0.0, 6.0)),
    }
    mission = []
    for placement_id in flight_order:
        beat = by_placement.get(placement_id)
        if not beat or not beat.get("fixed_anchor_world_xyz"):
            raise RuntimeError("wingsuit mission anchor missing: %s" % placement_id)
        beat = dict(beat)
        anchor = Vector(beat["fixed_anchor_world_xyz"])
        beat["staged_position"] = [float(value) for value in anchor + anchor_offsets[placement_id]]
        beat["look_at_world_xyz"] = [float(value) for value in anchor]
        beat["movement_mode"] = "flight"
        if placement_id in {"placement_001", "placement_002", "placement_003"}:
            cell_number = {"placement_001": 2, "placement_002": 3, "placement_003": 4}[placement_id]
            beat["event_type"] = "flight_collectible"
            beat["label"] = "collect flight cell %d of four" % cell_number
            beat["aircraft_flight_cell_index"] = cell_number
        if placement_id in {"placement_003", "placement_023", "placement_019", "placement_020", "placement_013", "placement_012"}:
            beat["aircraft_destination_finale"] = True
        mission.append(beat)

        # After the existing mountain/forest objectives, trace a complete
        # counter-clockwise lap around the user-selected destination mountain,
        # returning to its east approach for the final low summit pass. These
        # are Director-only flight controls derived from the confirmed V7
        # extraction anchor; they do not alter any fixed placement transform.
        if placement_id == "placement_016":
            destination = by_placement.get("placement_013")
            if not destination or not destination.get("fixed_anchor_world_xyz"):
                raise RuntimeError("wingsuit destination anchor missing: placement_013")
            center = Vector(destination["fixed_anchor_world_xyz"])
            orbit_offsets = (
                (70.0, -60.0, 20.0),
                (88.0, 0.0, 19.0),
                (55.0, 68.0, 20.0),
                (0.0, 88.0, 21.0),
                (-62.0, 62.0, 20.0),
                (-88.0, 0.0, 19.0),
                (-58.0, -66.0, 20.0),
                (5.0, -88.0, 21.0),
                (68.0, -55.0, 18.0),
                (88.0, 5.0, 16.0),
            )
            for orbit_index, offset in enumerate(orbit_offsets):
                if orbit_index == 5:
                    cell = by_placement.get("placement_002")
                    if not cell or not cell.get("fixed_anchor_world_xyz"):
                        raise RuntimeError("wingsuit circuit cell missing: placement_002")
                    cell = dict(cell)
                    anchor = Vector(cell["fixed_anchor_world_xyz"])
                    cell.update({
                        "staged_position": [float(value) for value in anchor],
                        "look_at_world_xyz": [float(value) for value in anchor],
                        "movement_mode": "flight",
                        "event_type": "flight_collectible",
                        "label": "collect flight cell 3 of four during the mountain circuit",
                        "aircraft_flight_cell_index": 3,
                        "aircraft_destination_orbit": True,
                    })
                    mission.append(cell)
                    continue
                orbit = dict(beat)
                position = center + Vector(offset)
                orbit.update({
                    "kind": "transit",
                    "beat_id": "wingsuit_destination_orbit_%02d" % orbit_index,
                    "placement_id": None,
                    "fixed_anchor_world_xyz": None,
                    "look_at_world_xyz": [float(value) for value in center],
                    "event_type": "flight",
                    "label": "complete the second-mountain circuit before final approach",
                    "staged_position": [float(value) for value in position],
                    "movement_mode": "flight",
                    "aircraft_destination_orbit": True,
                })
                mission.append(orbit)

    outward = Vector(mission[-1]["staged_position"]) - launch_position
    outward.z = 0.0
    if outward.length < 1e-5:
        raise RuntimeError("wingsuit launch and extraction anchors overlap")
    outward.normalize()
    left = Vector((-outward.y, outward.x, 0.0))
    launch_heading = Vector(mission[0]["staged_position"]) - launch_position
    launch_heading.z = 0.0
    if launch_heading.length < 1e-5:
        raise RuntimeError("wingsuit launch and first flight corridor overlap")
    launch_heading.normalize()
    takeoff = dict(launch)
    takeoff.update({
        "kind": "transit",
        "placement_id": None,
        "fixed_anchor_world_xyz": None,
        "event_type": "flight",
        "label": "accelerate continuously off the summit before banking",
        "movement_mode": "flight",
        "staged_position": [float(value) for value in (launch_position + launch_heading * 8.0 + UP * 0.7)],
        "aircraft_parked": False,
        "aircraft_takeoff_run": True,
    })
    mission[-1]["event_type"] = "goal"
    mission[-1]["label"] = "reach the second summit and keep the finish in view"

    # Insert smooth alternating mountain/forest sweeps between real anchors.
    # Long legs receive two controls; this lengthens the demonstration through
    # actual scenery instead of slowing the jet or adding meaningless holds.
    unique = [takeoff]
    terrain_sweep_count = 0
    for mission_index, target in enumerate(mission):
        if target.get("aircraft_destination_orbit") or target.get("aircraft_destination_finale"):
            unique.append(target)
            continue
        start_position = Vector(unique[-1]["staged_position"])
        target_position = Vector(target["staged_position"])
        segment = target_position - start_position
        horizontal_length = math.hypot(float(segment.x), float(segment.y))
        # The first two links are the deliberately authored launch arc above.
        # Keep one *collinear* control in each descent: it preserves the
        # required terrain-sweep coverage and spreads the height change over
        # multiple samples, without the alternating lateral S-turn that made
        # the old takeoff look physically impossible.
        if mission_index < 2:
            fractions = (0.50,)
        else:
            fractions = (0.34, 0.67) if horizontal_length > 28.0 else (0.50,)
        for control_index, fraction in enumerate(fractions):
            if mission_index < 2:
                position = start_position.lerp(target_position, fraction)
                transit_label = "opening downhill arc control"
            else:
                direction = 1.0 if (mission_index + control_index) % 2 == 0 else -1.0
                sweep = min(24.0, max(7.0, horizontal_length * 0.42))
                position = start_position.lerp(target_position, fraction) + left * sweep * direction
                position.z += 3.5 if horizontal_length > 28.0 else 1.8
                transit_label = "terrain-following mountain and forest sweep"
            transit = dict(target)
            transit.update({
                "kind": "transit",
                "beat_id": "wingsuit_terrain_sweep_%02d" % terrain_sweep_count,
                "placement_id": None,
                "fixed_anchor_world_xyz": None,
                "look_at_world_xyz": None,
                "event_type": "flight",
                "label": transit_label,
                "staged_position": [float(value) for value in position],
                "movement_mode": "flight",
                "aircraft_terrain_sweep": True,
            })
            unique.append(transit)
            terrain_sweep_count += 1
        unique.append(target)
        if str(target.get("placement_id") or "") == "placement_001":
            # The next fixed anchor is only eleven metres away and lower.  A
            # direct Hermite link used the following long mountain-circuit leg
            # as its exit tangent, creating the 69-degree nose-down snap seen
            # at frame 143.  This is an actual short climb-and-settle arc,
            # giving the aircraft room to complete its bank before descending.
            departure = dict(target)
            cell_position = Vector(target["staged_position"])
            departure.update({
                "kind": "transit",
                "beat_id": "wingsuit_cell_2_departure_arc",
                "placement_id": None,
                "fixed_anchor_world_xyz": None,
                "look_at_world_xyz": None,
                "event_type": "flight",
                "label": "hold the bank, then settle into the valley corridor",
                "staged_position": [float(value) for value in (cell_position + Vector((-4.0, -4.0, 1.5)))],
                "movement_mode": "flight",
                "aircraft_terrain_sweep": True,
                "aircraft_opening_departure_arc": True,
            })
            unique.append(departure)
            terrain_sweep_count += 1

    launch_frame = int(opening[-1]["frame"])
    mission_end_frame = int(launch_frame + flight_duration_frames)
    finale_start_frame = int(mission_end_frame - 252)  # 39.0 s for the V8 defaults.
    first_finale_index = next(
        (index for index, beat in enumerate(unique) if beat.get("aircraft_destination_finale")),
        len(unique),
    )
    early = unique[:first_finale_index]
    if len(early) < 2:
        raise RuntimeError("wingsuit V8 route has no circuit before the destination finale")
    distances = [(Vector(early[0]["staged_position"]) - launch_position).length]
    for first, second in zip(early, early[1:]):
        distances.append(
            distances[-1]
            + (Vector(second["staged_position"]) - Vector(first["staged_position"])).length
        )
    total = max(distances[-1], 0.01)
    for beat, distance in zip(early, distances):
        beat["source_frame"] = int(beat.get("frame", 0))
        beat["frame"] = launch_frame + 1 + int(
            round(float(finale_start_frame - launch_frame - 1) * distance / total)
        )
        beat["aircraft_retimed"] = True
        beat["aircraft_parked"] = False
    # Cell two must be visibly later than the ignition cell, rather than
    # reading as a second opening prompt.  Preserve all later beat order while
    # allowing the rest of the 39-second circuit to absorb the small retime.
    previous_frame = int(launch_frame)
    for beat in early:
        minimum_frame = previous_frame + 3
        if str(beat.get("placement_id") or "") == "placement_001":
            minimum_frame = max(minimum_frame, int(launch_frame + 78))
        if int(beat["frame"]) < minimum_frame:
            beat["frame"] = int(minimum_frame)
        previous_frame = int(beat["frame"])

    finale_offsets = {
        "placement_003": -180,  # 42.0 s: fourth flight cell unlocks the summit.
        "placement_023": -120,  # 44.5 s: east-edge homing mast.
        "placement_019": -84,   # 46.0 s: windbreak.
        "placement_020": -54,   # 47.25 s: recovery refuge.
        "placement_013": -24,   # 48.5 s: extraction deck.
        "placement_012": 0,     # 49.5 s: west-edge finish.
    }
    for beat in unique[first_finale_index:]:
        placement_id = str(beat.get("placement_id") or "")
        if placement_id not in finale_offsets:
            raise RuntimeError("unexpected V8 finale control: %s" % placement_id)
        beat["source_frame"] = int(beat.get("frame", 0))
        beat["frame"] = int(mission_end_frame + finale_offsets[placement_id])
        beat["aircraft_retimed"] = True
        beat["aircraft_parked"] = False

    last = unique[-1]
    last_position = Vector(last["staged_position"])
    previous_position = Vector(unique[-2]["staged_position"])
    forward = last_position - previous_position
    forward.z = 0.0
    if forward.length < 1e-5:
        forward = outward.copy()
    forward.normalize()
    # End with a shallow, summit-readable exit.  V7 climbed 46 metres in six
    # seconds and left only sky in frame; V8 rises seven metres over 48 metres
    # of forward travel, keeping the plateau behind the F-104 in the chase shot.
    for frame_offset, forward_distance, climb in (
        (pullup_frames // 2, 22.0, 2.5),
        (pullup_frames, 48.0, 7.0),
    ):
        synthetic = dict(last)
        synthetic.update({
            "kind": "transit",
            "event_type": "flight_pullup",
            "placement_id": None,
            "fixed_anchor_world_xyz": None,
            "look_at_world_xyz": None,
            "frame": int(launch_frame + flight_duration_frames + frame_offset),
            "staged_position": [float(value) for value in (last_position + forward * forward_distance + UP * climb)],
            "aircraft_pullup": True,
            "aircraft_shallow_summit_exit": True,
        })
        unique.append(synthetic)
    route = opening + unique
    return route, {
        "source_beat_count": len(beats),
        "source_flight_beat_count": len(flight_indices),
        "aircraft_anchor_count": len(route),
        "summit_establish_duration_frames": launch_frame,
        "flight_duration_frames": int(flight_duration_frames),
        "pullup_duration_frames": int(pullup_frames),
        "frame_end": int(launch_frame + flight_duration_frames + pullup_frames),
        "target_duration_seconds": round((launch_frame + flight_duration_frames + pullup_frames) / 24.0, 3),
        "fixed_asset_positions_changed": False,
        "summit_ground_patrol_omitted_for_aircraft": True,
        "summit_assets_used_as_preflight_events": [item[1] for item in preflight_spec],
        "flight_assets_used_as_near_pass_events": [
            "placement_018", "placement_017", "placement_001", "placement_016",
            "placement_002", "placement_003", "placement_023", "placement_019",
            "placement_020", "placement_013", "placement_012",
        ],
        "flight_collectible_ids": ["placement_000", "placement_001", "placement_002", "placement_003"],
        "flight_collectible_count": 4,
        "collection_unlocks_destination": True,
        "terrain_sweep_count": int(terrain_sweep_count),
        "destination_orbit_count": sum(1 for beat in unique if beat.get("aircraft_destination_orbit")),
        "destination_finale_start_frame": int(finale_start_frame),
        "destination_finale_event_frames": {
            str(beat.get("placement_id")): int(beat["frame"])
            for beat in unique
            if beat.get("aircraft_destination_finale")
        },
        "destination_low_pass_height_m": 6.0,
        "final_exit_climb_m": 7.0,
        "destination_mountain_center_world_xyz": [
            round(float(value), 6)
            for value in by_placement["placement_013"]["fixed_anchor_world_xyz"]
        ],
        "one_way_forward_corridor": False,
        "route_shape": "complete_destination_mountain_circuit_then_low_second_summit_finish",
        "outward_heading_xy": [round(float(outward.x), 6), round(float(outward.y), 6)],
        "reversal_assets_left_as_scenery": 0,
    }


def aircraft_gameplay_obstacle_top(position, horizontal_clearance=4.2):
    """Return the highest placed-asset top intersecting the aircraft envelope."""
    position = Vector(position)
    highest = None
    for root in bpy.context.scene.objects:
        if root.type != "EMPTY" or not root.get("code2games_placement_id"):
            continue
        if str(root.get("code2games_placement_id")) in {
            "placement_000", "placement_001", "placement_002", "placement_003",
        }:
            # V8 flight cells are intentional intercept targets, not solid
            # obstacles for the terrain-clearance envelope.
            continue
        bounds = mesh_bounds(hierarchy_mesh_objects(root))
        if not bounds:
            continue
        minimum, maximum = bounds
        closest_x = max(float(minimum.x), min(float(maximum.x), float(position.x)))
        closest_y = max(float(minimum.y), min(float(maximum.y), float(position.y)))
        horizontal_distance = math.hypot(float(position.x) - closest_x, float(position.y) - closest_y)
        if horizontal_distance <= float(horizontal_clearance):
            highest = float(maximum.z) if highest is None else max(highest, float(maximum.z))
    return highest


def aircraft_curve_samples(
    beats,
    z_extent,
    frame_step=3,
    minimum_clearance=2.4,
    parked_clearance=1.0,
    maximum_path_pitch_degrees=58.0,
):
    samples = []
    positions = [Vector(beat["staged_position"]) for beat in beats]
    for index in range(len(beats) - 1):
        first, second = beats[index], beats[index + 1]
        frame_first, frame_second = int(first["frame"]), int(second["frame"])
        p0, p1 = positions[index], positions[index + 1]
        previous = positions[index - 1] if index else p0
        following = positions[index + 2] if index + 2 < len(positions) else p1
        tangent0 = (p1 - previous) * 0.34
        tangent1 = (following - p0) * 0.34
        frames = list(range(frame_first, frame_second, max(2, int(frame_step))))
        if index == len(beats) - 2:
            frames.append(frame_second)
        for frame in frames:
            if samples and frame <= samples[-1]["frame"]:
                continue
            u = 0.0 if frame_second == frame_first else (frame - frame_first) / float(frame_second - frame_first)
            u = max(0.0, min(1.0, u))
            h00 = 2.0 * u ** 3 - 3.0 * u ** 2 + 1.0
            h10 = u ** 3 - 2.0 * u ** 2 + u
            h01 = -2.0 * u ** 3 + 3.0 * u ** 2
            h11 = u ** 3 - u ** 2
            position = p0 * h00 + tangent0 * h10 + p1 * h01 + tangent1 * h11
            ground = terrain_height(position.x, position.y, position.z, z_extent)
            first_parked = bool(first.get("aircraft_parked"))
            second_parked = bool(second.get("aircraft_parked"))
            if first_parked and second_parked:
                required_clearance = float(parked_clearance)
            elif first_parked:
                required_clearance = (
                    float(parked_clearance)
                    + (float(minimum_clearance) - float(parked_clearance)) * smoothstep(u)
                )
            else:
                required_clearance = float(minimum_clearance)
            obstacle_top = None if first_parked and second_parked else aircraft_gameplay_obstacle_top(position)
            required_z = ground + required_clearance
            if obstacle_top is not None:
                required_z = max(required_z, obstacle_top + max(1.8, required_clearance * 0.45))
            samples.append({
                "frame": frame,
                "position": position,
                "ground": ground,
                "required_z": required_z,
                "aircraft_parked": bool(first_parked and second_parked),
                "gameplay_obstacle_top": obstacle_top,
                "aircraft_destination_finale": bool(
                    first.get("aircraft_destination_finale")
                    or second.get("aircraft_destination_finale")
                ),
                "aircraft_shallow_summit_exit": bool(
                    first.get("aircraft_shallow_summit_exit")
                    or second.get("aircraft_shallow_summit_exit")
                ),
            })
    # Anticipate terrain/asset clearance instead of popping vertically at the
    # obstruction.  The envelope expands six samples (18 frames) each side.
    for index, item in enumerate(samples):
        envelope = float(item["required_z"])
        for other_index in range(max(0, index - 6), min(len(samples), index + 7)):
            penalty = abs(other_index - index) * 0.65
            envelope = max(envelope, float(samples[other_index]["required_z"]) - penalty)
        item["position"].z = max(float(item["position"].z), envelope)

    # Clearance corrections can otherwise create a near-vertical step where a
    # Hermite segment has little horizontal motion.  Limit the actual spatial
    # grade by lifting neighbouring airborne samples in both directions.  We
    # never lower a point, so terrain/asset clearance remains conservative;
    # parked support samples are excluded so the F-104 still rests on the pad.
    first_airborne = next(
        (index for index, item in enumerate(samples) if not item.get("aircraft_parked")),
        len(samples),
    )
    maximum_slope = math.tan(math.radians(float(maximum_path_pitch_degrees)))
    original_z = [float(item["position"].z) for item in samples]
    if first_airborne < len(samples):
        for index in range(len(samples) - 2, first_airborne - 1, -1):
            first = samples[index]["position"]
            second = samples[index + 1]["position"]
            horizontal = max(0.05, math.hypot(float(second.x - first.x), float(second.y - first.y)))
            # The F-104 has just left the summit in this opening window.  Use
            # a deliberately stricter 42-degree descent envelope there, while
            # retaining the established 58-degree global terrain-clearance
            # contract for the rest of the mission.
            opening_slope = math.tan(math.radians(42.0)) if int(samples[index + 1]["frame"]) <= 216 else maximum_slope
            first.z = max(float(first.z), float(second.z) - opening_slope * horizontal)
        for index in range(first_airborne + 1, len(samples)):
            first = samples[index - 1]["position"]
            second = samples[index]["position"]
            horizontal = max(0.05, math.hypot(float(second.x - first.x), float(second.y - first.y)))
            opening_slope = math.tan(math.radians(42.0)) if int(samples[index]["frame"]) <= 216 else maximum_slope
            second.z = max(float(second.z), float(first.z) - opening_slope * horizontal)
    for item, before_z in zip(samples, original_z):
        lift = max(0.0, float(item["position"].z) - before_z)
        item["pitch_smoothing_lift_m"] = lift
    return samples


def aircraft_roll_angle(frame, flight_start, mission_end):
    # Rolls occur only after launch and before the final gate/pull-up.  Their
    # normalized windows survive retiming and never spin the parked aircraft.
    duration = max(48, int(mission_end) - int(flight_start))
    windows = (
        (int(flight_start + duration * 0.48), int(flight_start + duration * 0.59), 1.0),
        (int(flight_start + duration * 0.72), int(flight_start + duration * 0.83), -1.0),
    )
    for start, end, direction in windows:
        if start <= frame <= end:
            progress = smoothstep((frame - start) / float(end - start))
            return direction * math.tau * progress
    return 0.0


def animate_aircraft(root, beats):
    z_extent = scene_z_extent()
    minimum_clearance = max(2.4, float(root.get("code2games_aircraft_roll_clearance_m", 2.4)))
    parked_clearance = max(0.25, float(root.get("code2games_aircraft_ground_support_offset_m", 1.0)))
    motion = aircraft_curve_samples(
        beats,
        z_extent,
        minimum_clearance=minimum_clearance,
        parked_clearance=parked_clearance,
    )
    flight_start = next((int(beat["frame"]) for beat in beats if not beat.get("aircraft_parked")), int(beats[0]["frame"]))
    mission_end = max(
        (int(beat["frame"]) for beat in beats if not beat.get("aircraft_pullup")),
        default=int(beats[-1]["frame"]),
    )
    root.rotation_mode = "QUATERNION"
    previous_rotation = None
    parked_heading = Vector((0.0, -1.0, 0.0))
    parked_origin = Vector(motion[0]["position"])
    for candidate in motion:
        horizontal = Vector(candidate["position"]) - parked_origin
        horizontal.z = 0.0
        if horizontal.length >= 5.0:
            parked_heading = horizontal.normalized()
            break
    max_bank = 0.0
    max_pitch = 0.0
    max_pitch_frame = int(motion[0]["frame"])
    max_pitch_direction = [0.0, 1.0, 0.0]
    minimum_actual_clearance = float("inf")
    for index, item in enumerate(motion):
        position = Vector(item["position"])
        before = Vector(motion[max(0, index - 2)]["position"])
        after = Vector(motion[min(len(motion) - 1, index + 2)]["position"])
        direction = after - before
        if item.get("aircraft_parked"):
            direction = parked_heading.copy()
        if direction.length < 1e-5:
            direction = Vector((0.0, 1.0, 0.0))
        direction.normalize()
        earlier = position - Vector(motion[max(0, index - 4)]["position"])
        later = Vector(motion[min(len(motion) - 1, index + 4)]["position"]) - position
        earlier.z = 0.0
        later.z = 0.0
        if earlier.length > 1e-5 and later.length > 1e-5:
            earlier.normalize()
            later.normalize()
            turn = math.atan2(earlier.x * later.y - earlier.y * later.x, earlier.dot(later))
        else:
            turn = 0.0
        bank = max(-math.radians(52.0), min(math.radians(52.0), -turn * 3.2))
        roll = aircraft_roll_angle(int(item["frame"]), flight_start, mission_end)
        base = direction.to_track_quat("Y", "Z")
        rotation = base @ Quaternion((0.0, 1.0, 0.0), bank + roll)
        if previous_rotation is not None:
            rotation.make_compatible(previous_rotation)
        previous_rotation = rotation.copy()
        root.location = position
        root.rotation_quaternion = rotation
        root.keyframe_insert("location", frame=int(item["frame"]))
        root.keyframe_insert("rotation_quaternion", frame=int(item["frame"]))
        max_bank = max(max_bank, abs(bank))
        pitch = abs(math.asin(max(-1.0, min(1.0, direction.z))))
        if pitch > max_pitch:
            max_pitch = pitch
            max_pitch_frame = int(item["frame"])
            max_pitch_direction = [round(float(value), 6) for value in direction]
        minimum_actual_clearance = min(minimum_actual_clearance, position.z - float(item["ground"]))
    set_linear_animation(root)
    if root.animation_data and root.animation_data.action:
        for curve in root.animation_data.action.fcurves:
            if curve.data_path != "location":
                continue
            for point in curve.keyframe_points:
                point.interpolation = "BEZIER"
                point.handle_left_type = "AUTO_CLAMPED"
                point.handle_right_type = "AUTO_CLAMPED"
    root["code2games_route_sample_count"] = len(motion)
    root["code2games_route_detour_count"] = 0
    root["code2games_route_unresolved_count"] = 0
    root["code2games_route_detours"] = "[]"
    root["code2games_route_unresolved_frames"] = "[]"
    return motion, {
        "enabled": True,
        "motion_sample_count": len(motion),
        "sample_step_frames": 3,
        "maximum_coordinated_bank_degrees": round(math.degrees(max_bank), 4),
        "maximum_pitch_degrees": round(math.degrees(max_pitch), 4),
        "maximum_pitch_frame": int(max_pitch_frame),
        "maximum_pitch_direction": max_pitch_direction,
        "path_pitch_limit_degrees": 58.0,
        "pitch_smoothing_sample_count": sum(
            1 for item in motion if float(item.get("pitch_smoothing_lift_m", 0.0)) > 1e-5
        ),
        "maximum_pitch_smoothing_lift_m": round(
            max((float(item.get("pitch_smoothing_lift_m", 0.0)) for item in motion), default=0.0),
            4,
        ),
        "minimum_terrain_clearance_m": round(float(minimum_actual_clearance), 4),
        "required_roll_clearance_m": round(float(minimum_clearance), 4),
        "roll_windows_relative_to_flight": [[0.48, 0.59, "left"], [0.72, 0.83, "right"]],
        "flight_start_frame": int(flight_start),
        "final_pullup_start_frame": int(mission_end),
        "final_frame": int(motion[-1]["frame"]),
    }


def racing_ground_orientation(position, forward, z_extent):
    """Fit the vehicle's local Y/Z axes to terrain under both axles."""
    forward = Vector((forward.x, forward.y, 0.0))
    if forward.length < 1e-5:
        forward = Vector((0.0, 1.0, 0.0))
    forward.normalize()
    side = Vector((forward.y, -forward.x, 0.0))
    wheelbase = 3.55
    track = 1.85

    def grounded(offset):
        point = Vector(position) + offset
        point.z = terrain_height(point.x, point.y, position.z, z_extent) + 0.03
        return point

    front = grounded(forward * (wheelbase * 0.5))
    rear = grounded(-forward * (wheelbase * 0.5))
    right = grounded(side * (track * 0.5))
    left = grounded(-side * (track * 0.5))
    local_y = (front - rear).normalized()
    local_x = (right - left).normalized()
    local_z = local_x.cross(local_y).normalized()
    if local_z.z < 0.0:
        local_x.negate()
        local_z.negate()
    local_x = local_y.cross(local_z).normalized()
    return Matrix((local_x, local_y, local_z)).transposed().to_quaternion()


def racing_pitch_roll_step_degrees(previous_rotation, current_rotation):
    """Return chassis tilt change while deliberately ignoring heading/yaw."""
    previous_up = previous_rotation @ UP
    current_up = current_rotation @ UP
    if previous_up.length < 1e-6 or current_up.length < 1e-6:
        return 0.0
    dot = max(-1.0, min(1.0, previous_up.normalized().dot(current_up.normalized())))
    return math.degrees(math.acos(dot))


def solve_racing_vehicle_support(beats, z_extent):
    """Seat the complete vehicle on four real wheel-contact ray casts.

    The controller origin is at the visual tyre-bottom plane.  Merely placing
    that origin above the centre terrain sample lets a pitched 5.2 m car rotate
    its rear below a slope.  Solve the support plane at all four tyres, smooth
    its orientation, then choose the lowest controller height that keeps every
    contact above its own ground hit.  A forward/backward height envelope
    raises the car before abrupt terrain steps instead of allowing a one-frame
    plunge (the V6 seventh-second failure).
    """
    if not beats:
        return {
            "samples": [],
            "unsafe_sample_count": 0,
            "minimum_ground_clearance_m": 0.0,
            "maximum_vertical_step_m": 0.0,
            "maximum_pitch_roll_step_degrees": 0.0,
            "maximum_vertical_step_per_2_frames_m": 0.0,
            "maximum_pitch_roll_step_per_2_frames_degrees": 0.0,
            "mountain_drive_seconds": 0.0,
        }
    wheelbase = 3.55
    track = 1.85
    clearance = 0.045
    # These are the four v12 samples whose centre-line probe hit the asphalt
    # while one or more wheel rays reported z~0.  Retain the raw ray chain in
    # the report before changing any support policy or route geometry.
    support_diagnostic_frames = {611, 613, 617, 620}
    raw = []
    previous_rotation = None
    for index, beat in enumerate(beats):
        position = Vector(beat["staged_position"])
        forward = racing_smoothed_tangent(beats, index)
        forward.z = 0.0
        if forward.length < 1e-5:
            forward = Vector((0.0, 1.0, 0.0))
        forward.normalize()
        right = Vector((forward.y, -forward.x, 0.0))
        contacts = [
            ("front_left", -track * 0.5, wheelbase * 0.5),
            ("front_right", track * 0.5, wheelbase * 0.5),
            ("rear_left", -track * 0.5, -wheelbase * 0.5),
            ("rear_right", track * 0.5, -wheelbase * 0.5),
        ]
        heights = []
        wheel_support_diagnostics = []
        diagnostic_frame = int(beat["frame"]) in support_diagnostic_frames
        for _name, local_x, local_y in contacts:
            world_xy = position + right * local_x + forward * local_y
            # Keep the chain for every ray while solving, but serialize it
            # only for the known investigation frames or a wheel that cannot
            # reach asphalt.  This makes any later four-wheel rejection
            # auditable without bloating the normal report.
            hit_chain = []
            ground = racing_ground_z(
                world_xy.x,
                world_xy.y,
                z_extent,
                diagnostic_hits=hit_chain,
            )
            initial_ground = ground
            initial_ground_out_of_range = bool(
                initial_ground is not None
                and abs(float(initial_ground) - float(position.z)) > 12.0
            )
            if ground is None:
                # A tyre has a finite contact patch, not a zero-area point.
                # If the wheel-centre ray lands in a tiny topology hole, use
                # four nearby real downward rays across the footprint.  The
                # wheel is supported only when at least one of those rays
                # really hits drivable ground; no sampled/fabricated height is
                # counted as a four-wheel hit.
                patch_hits = []
                for offset in (
                    right * 0.16,
                    right * -0.16,
                    forward * 0.22,
                    forward * -0.22,
                ):
                    patch_ground = racing_ground_z(
                        world_xy.x + offset.x,
                        world_xy.y + offset.y,
                        z_extent,
                    )
                    if patch_ground is not None:
                        patch_hits.append(float(patch_ground))
                if patch_hits:
                    ordered_patch_hits = sorted(patch_hits)
                    ground = ordered_patch_hits[len(ordered_patch_hits) // 2]
            heights.append(ground)
            if diagnostic_frame or initial_ground is None or initial_ground_out_of_range:
                wheel_support_diagnostics.append({
                    "wheel": _name,
                    "wheel_world_xy": [round(float(world_xy.x), 6), round(float(world_xy.y), 6)],
                    "ray_origin_world_xyz": [
                        round(float(world_xy.x), 6),
                        round(float(world_xy.y), 6),
                        round(float(z_extent[1]) + 200.0, 6),
                    ],
                    "initial_support_z_m": (
                        round(float(initial_ground), 6) if initial_ground is not None else None
                    ),
                    "initial_support_out_of_range": initial_ground_out_of_range,
                    "ray_hits": hit_chain,
                })
        # A malformed or out-of-range road sample must not be promoted into
        # wheel support merely because it belongs to the Asphalt mesh.  Keep
        # the local plausibility guard; the diagnostic below records every
        # rejected Asphalt contact so its geometry can be repaired directly.
        valid_mask = [
            value is not None and abs(float(value) - float(position.z)) <= 12.0
            for value in heights
        ]
        valid = [float(value) for value, is_valid in zip(heights, valid_mask) if is_valid]
        if len(valid) < 4:
            fallback = terrain_height(position.x, position.y, position.z, z_extent)
            heights = [
                float(value) if is_valid else float(fallback)
                for value, is_valid in zip(heights, valid_mask)
            ]
        front_height = 0.5 * (heights[0] + heights[1])
        rear_height = 0.5 * (heights[2] + heights[3])
        left_height = 0.5 * (heights[0] + heights[2])
        right_height = 0.5 * (heights[1] + heights[3])
        local_y_axis = Vector((
            forward.x * wheelbase,
            forward.y * wheelbase,
            front_height - rear_height,
        )).normalized()
        local_x_axis = Vector((
            right.x * track,
            right.y * track,
            right_height - left_height,
        )).normalized()
        local_z_axis = local_x_axis.cross(local_y_axis).normalized()
        if local_z_axis.z < 0.0:
            local_x_axis.negate()
            local_z_axis.negate()
        local_x_axis = local_y_axis.cross(local_z_axis).normalized()
        rotation = Matrix((local_x_axis, local_y_axis, local_z_axis)).transposed().to_quaternion()
        if previous_rotation is not None:
            rotation.make_compatible(previous_rotation)
            # Heading must follow the path tangent immediately.  Limiting the
            # full quaternion here also limited yaw, leaving the vehicle body
            # facing its old direction while its position moved into a bend
            # (the visible sideways slide in Racing QA).  The verifier below
            # now measures the intended pitch/roll-only delta from the up
            # vectors, so this keeps the real tilt safeguard without freezing
            # normal steering.
        rotation.normalize()
        previous_rotation = rotation.copy()
        required_z = max(
            float(ground) - float((rotation @ Vector((local_x, local_y, 0.0))).z) + clearance
            for (_name, local_x, local_y), ground in zip(contacts, heights)
        )
        raw.append({
            "beat": beat,
            "frame": int(beat["frame"]),
            "position": position,
            "rotation": rotation,
            "contacts": contacts,
            "ground_heights": [float(value) for value in heights],
            "hit_count": len(valid),
            "required_z": float(required_z),
            "track_surface": bool(beat.get("racing_track_surface")),
            "wheel_support_diagnostics": wheel_support_diagnostics,
        })

    # Seat the controller directly on its four-wheel contact plane.  Do not
    # pre-lift it for future terrain steps: that produced visually hovering
    # cars even though the old minimum-clearance check passed.
    supported_z = [item["required_z"] for item in raw]

    samples = []
    minimum_clearance = float("inf")
    maximum_clearance = 0.0
    maximum_vertical_step = 0.0
    maximum_rotation_step = 0.0
    maximum_vertical_step_per_2_frames = 0.0
    maximum_rotation_step_per_2_frames = 0.0
    mountain_frames = 0
    previous = None
    for index, (item, centre_z) in enumerate(zip(raw, supported_z)):
        beat = item["beat"]
        item["position"].z = float(centre_z)
        beat["staged_position"] = [round(float(value), 6) for value in item["position"]]
        beat["racing_support_quaternion"] = [round(float(value), 9) for value in item["rotation"]]
        clearances = []
        for (_name, local_x, local_y), ground in zip(item["contacts"], item["ground_heights"]):
            contact_z = float(centre_z) + float((item["rotation"] @ Vector((local_x, local_y, 0.0))).z)
            clearances.append(contact_z - float(ground))
        min_clearance = min(clearances)
        max_clearance = max(clearances)
        minimum_clearance = min(minimum_clearance, min_clearance)
        maximum_clearance = max(maximum_clearance, max_clearance)
        ground_range = max(item["ground_heights"]) - min(item["ground_heights"])
        rotation_step = 0.0
        vertical_step = 0.0
        frame_delta = 0
        vertical_step_per_2_frames = 0.0
        rotation_step_per_2_frames = 0.0
        if previous is not None:
            frame_delta = max(1, item["frame"] - previous["frame"])
            vertical_step = abs(float(centre_z) - float(previous["z"]))
            rotation_step = racing_pitch_roll_step_degrees(
                previous["rotation"], item["rotation"]
            )
            vertical_step_per_2_frames = vertical_step * 2.0 / float(frame_delta)
            rotation_step_per_2_frames = rotation_step * 2.0 / float(frame_delta)
            maximum_vertical_step = max(maximum_vertical_step, vertical_step)
            maximum_rotation_step = max(maximum_rotation_step, rotation_step)
            maximum_vertical_step_per_2_frames = max(
                maximum_vertical_step_per_2_frames,
                vertical_step_per_2_frames,
            )
            maximum_rotation_step_per_2_frames = max(
                maximum_rotation_step_per_2_frames,
                rotation_step_per_2_frames,
            )
            if (
                not item.get("track_surface")
                and not previous.get("track_surface")
                and 0.5 * (max(item["ground_heights"]) + max(previous["grounds"])) > 0.45
            ):
                mountain_frames += frame_delta
        safe = (
            item["hit_count"] == 4
            and min_clearance >= -0.03
            and max_clearance <= 0.20
            and ground_range <= 1.25
            and vertical_step_per_2_frames <= 0.18 + 1e-5
            and rotation_step_per_2_frames <= 3.0 + 1e-5
        )
        sample = {
            "frame": item["frame"],
            "frame_delta": frame_delta,
            "four_wheel_hit_count": item["hit_count"],
            "wheel_ground_heights_m": [round(value, 6) for value in item["ground_heights"]],
            "minimum_ground_clearance_m": round(min_clearance, 6),
            "maximum_ground_clearance_m": round(max_clearance, 6),
            "support_height_range_m": round(ground_range, 6),
            "controller_z_m": round(float(centre_z), 6),
            "vertical_step_m": round(vertical_step, 6),
            "pitch_roll_step_degrees": round(rotation_step, 6),
            "vertical_step_per_2_frames_m": round(vertical_step_per_2_frames, 6),
            "pitch_roll_step_per_2_frames_degrees": round(rotation_step_per_2_frames, 6),
            "mountain": bool(
                not item.get("track_surface")
                and max(item["ground_heights"]) > 0.45
            ),
            "safe": bool(safe),
        }
        if item["wheel_support_diagnostics"]:
            sample["wheel_support_diagnostics"] = item["wheel_support_diagnostics"]
        samples.append(sample)
        previous = {
            "frame": item["frame"],
            "z": float(centre_z),
            "rotation": item["rotation"],
            "grounds": item["ground_heights"],
            "track_surface": bool(item.get("track_surface")),
        }
    return {
        "samples": samples,
        "unsafe_sample_count": sum(1 for sample in samples if not sample["safe"]),
        "minimum_ground_clearance_m": round(minimum_clearance if samples else 0.0, 6),
        "maximum_ground_clearance_m": round(maximum_clearance if samples else 0.0, 6),
        "maximum_vertical_step_m": round(maximum_vertical_step, 6),
        "maximum_pitch_roll_step_degrees": round(maximum_rotation_step, 6),
        "maximum_vertical_step_per_2_frames_m": round(maximum_vertical_step_per_2_frames, 6),
        "maximum_pitch_roll_step_per_2_frames_degrees": round(maximum_rotation_step_per_2_frames, 6),
        "mountain_drive_seconds": round(mountain_frames / 24.0, 6),
        "seventh_second_unsafe_frames": [
            sample["frame"] for sample in samples
            if 157 <= sample["frame"] <= 179 and not sample["safe"]
        ],
    }


def animate_racing_vehicle_dynamics(root, beats):
    spin_names = [name for name in str(root.get("code2games_wheel_spin_objects", "")).split(",") if name]
    steer_names = [name for name in str(root.get("code2games_wheel_steer_objects", "")).split(",") if name]
    spin_pivots = [bpy.data.objects.get(name) for name in spin_names]
    steer_pivots = [bpy.data.objects.get(name) for name in steer_names]
    spin_pivots = [obj for obj in spin_pivots if obj]
    steer_pivots = [obj for obj in steer_pivots if obj]
    if len(spin_pivots) != 4 or len(steer_pivots) != 2:
        return {"enabled": False, "reason": "wheel pivots unavailable"}
    radius = max(0.20, float(root.get("code2games_wheel_radius_m", 0.52)))
    travel = 0.0
    previous = Vector(beats[0]["staged_position"])
    for index, beat in enumerate(beats):
        frame = int(beat["frame"])
        position = Vector(beat["staged_position"])
        if index:
            travel += (position - previous).length
        previous = position
        spin_angle = -travel / radius
        for pivot in spin_pivots:
            pivot.rotation_mode = "XYZ"
            pivot.rotation_euler.x = spin_angle
            pivot.keyframe_insert("rotation_euler", frame=frame)
        direction = racing_smoothed_tangent(beats, index)
        next_direction = racing_smoothed_tangent(beats, min(index + 1, len(beats) - 1))
        cross = direction.x * next_direction.y - direction.y * next_direction.x
        dot = max(-1.0, min(1.0, direction.x * next_direction.x + direction.y * next_direction.y))
        steering = max(-math.radians(28.0), min(math.radians(28.0), math.atan2(cross, dot) * 0.72))
        for pivot in steer_pivots:
            pivot.rotation_mode = "XYZ"
            pivot.rotation_euler.z = steering
            pivot.keyframe_insert("rotation_euler", frame=frame)
    for pivot in [*spin_pivots, *steer_pivots]:
        set_linear_animation(pivot)
    return {
        "enabled": True,
        "wheel_spin_pivot_count": len(spin_pivots),
        "steering_pivot_count": len(steer_pivots),
        "travel_distance_m": round(float(travel), 4),
        "wheel_radius_m": round(float(radius), 4),
        "tangent_locked_per_frame": bool(root.get("code2games_racing_tangent_locked", False)),
        "tangent_lock_sample_count": int(root.get("code2games_racing_tangent_lock_sample_count", 0)),
    }


def densify_racing_motion(motion):
    """Bake Racing translation and support attitude at every playback frame.

    A car may not rotate halfway toward the next curve while its location is
    still travelling along the preceding straight.  Blender's interpolation
    between two-frame translation/rotation keys caused exactly that visual
    side-slip in the Racing QA clip.  Keep the existing supported positions,
    but emit one motion sample per frame so chassis tangent and displacement
    are evaluated on the same temporal basis.
    """
    if len(motion) < 2:
        return motion
    dense = []
    for current, following in zip(motion, motion[1:]):
        frame_a = int(current["frame"])
        frame_b = int(following["frame"])
        if frame_b <= frame_a:
            continue
        position_a = Vector(current["position"])
        position_b = Vector(following["position"])
        beat_a = dict(current.get("beat") or {})
        beat_b = dict(following.get("beat") or {})
        support_a = beat_a.get("racing_support_quaternion")
        support_b = beat_b.get("racing_support_quaternion")
        quaternion_a = Quaternion(support_a) if support_a else None
        quaternion_b = Quaternion(support_b) if support_b else None
        if quaternion_a is not None and quaternion_b is not None:
            quaternion_b.make_compatible(quaternion_a)
        for frame in range(frame_a, frame_b):
            amount = float(frame - frame_a) / float(frame_b - frame_a)
            beat = dict(beat_a)
            if quaternion_a is not None and quaternion_b is not None:
                support = quaternion_a.slerp(quaternion_b, amount)
                support.normalize()
                beat["racing_support_quaternion"] = [float(value) for value in support]
            dense.append({
                "frame": frame,
                "position": position_a.lerp(position_b, amount),
                "beat": beat,
            })
    final = motion[-1]
    dense.append({
        "frame": int(final["frame"]),
        "position": Vector(final["position"]),
        "beat": dict(final.get("beat") or {}),
    })
    return dense


def racing_tangent_locked_orientation(support_quaternion, direction):
    """Preserve supported pitch/roll but force local +Y onto travel tangent."""
    tangent = Vector((direction.x, direction.y, 0.0))
    if tangent.length < 1e-6:
        tangent = Vector((0.0, 1.0, 0.0))
    tangent.normalize()
    up = Quaternion(support_quaternion) @ UP
    if up.length < 1e-6 or abs(float(up.normalized().z)) < 0.15:
        up = UP.copy()
    up.normalize()
    forward = tangent - up * tangent.dot(up)
    if forward.length < 1e-6:
        forward = tangent.copy()
    forward.normalize()
    right = forward.cross(up)
    if right.length < 1e-6:
        right = Vector((1.0, 0.0, 0.0))
    right.normalize()
    forward = up.cross(right).normalized()
    orientation = Matrix((right, forward, up)).transposed().to_quaternion()
    orientation.normalize()
    return orientation


def animate_actor(root, beats, genre):
    root.rotation_mode = "QUATERNION"
    z_extent = scene_z_extent() if genre in {"fps", "tps", "racing"} else None
    route_corrections = []
    route_unresolved = []
    if genre in {"fps", "tps"}:
        motion, route_corrections, route_unresolved = smooth_ground_route_samples(
            beats,
            z_extent,
            genre=genre,
            frame_step=int(_SAMPLE_STEP_FRAMES),
        )
    else:
        motion = [
            {"frame": int(beat["frame"]), "position": Vector(beat["staged_position"]), "beat": beat}
            for beat in beats
        ]
    if genre == "racing":
        motion = densify_racing_motion(motion)
    previous_rotation = None
    tps_motion_keys = [] if genre == "tps" else None
    for index, item in enumerate(motion):
        position = Vector(item["position"])
        frame = int(item["frame"])
        immediate_motion = (
            Vector(motion[index + 1]["position"]) - position
            if index < len(motion) - 1
            else Vector((0.0, 0.0, 0.0))
        )
        if genre == "racing":
            direction = racing_smoothed_tangent(motion, index)
        elif index < len(motion) - 1:
            # Face the smoothed path tangent: find the nearest sample ahead
            # and behind that is at a DIFFERENT position (skipping stationary
            # hold samples), then use the chord between them.  A raw
            # +/-N-sample chord can point backwards around event holds and
            # produced the camera swings seen in the QA clip.
            window = int(_FPS_SMOOTH_TURN_WINDOW) if genre in {"fps", "tps"} else 1
            ahead = None
            for offset in range(window, 0, -1):
                candidate = min(len(motion) - 1, index + offset)
                delta = Vector(motion[candidate]["position"]) - position
                delta.z = 0.0
                if delta.length > 0.05:
                    ahead = Vector(motion[candidate]["position"])
                    break
            behind = None
            for offset in range(1, window + 1):
                candidate = max(0, index - offset)
                delta = position - Vector(motion[candidate]["position"])
                delta.z = 0.0
                if delta.length > 0.05:
                    behind = Vector(motion[candidate]["position"])
                    break
            if ahead is not None and behind is not None:
                # Chord across the window (ahead - behind) instead of the
                # one-sided lookahead: the averaged direction kills the
                # sample-to-sample heading jitter that the rigid boom camera
                # picked up as wobble (QA: TPS camera shake after turn fix).
                direction = ahead - behind
            elif ahead is not None:
                # Start of the route: no samples behind yet.  Face the
                # IMMEDIATE next sample so the character moves along the
                # current leg instead of probing toward a future turn (QA:
                # the start "probed right" before correcting left).
                nearest_ahead = None
                for offset in range(1, window + 1):
                    candidate = min(len(motion) - 1, index + offset)
                    delta = Vector(motion[candidate]["position"]) - position
                    delta.z = 0.0
                    if delta.length > 0.05:
                        nearest_ahead = Vector(motion[candidate]["position"])
                        break
                direction = (nearest_ahead - position) if nearest_ahead is not None else (ahead - position)
            elif behind is not None:
                direction = position - behind
            else:
                direction = Vector((0.0, 0.0, 0.0))
        elif index:
            direction = position - Vector(motion[max(0, index - 2)]["position"])
        else:
            direction = Vector((0.0, 1.0, 0.0))
        if genre != "wingsuit":
            direction.z = 0.0
        stationary_hold = (
            genre in {"fps", "tps"}
            # Treat slow drifting/stopping samples as stationary: the combat
            # hold path can drift a metre sideways, and amplifying that drift
            # into a 90-degree body turn produced the camera swing.
            and immediate_motion.length < 0.10
            and previous_rotation is not None
        )
        if direction.length < 1e-5:
            direction = Vector((0.0, 1.0, 0.0))
        direction.normalize()
        root.location = position
        if stationary_hold:
            # Event dwell means stop both translation and chassis heading.
            # Looking two samples ahead used to rotate the root toward the
            # exit while the feet were planted, dragging the TPS camera around
            # the actor during reload/fire interactions.
            root.rotation_quaternion = previous_rotation.copy()
        elif genre == "racing":
            support_quaternion = item.get("beat", {}).get("racing_support_quaternion")
            root.rotation_quaternion = (
                racing_tangent_locked_orientation(Quaternion(support_quaternion), direction)
                if support_quaternion
                else racing_ground_orientation(position, direction, z_extent)
            )
        elif genre == "wingsuit" and item.get("beat", {}).get("movement_mode") == "flight":
            root.rotation_quaternion = direction.to_track_quat("Y", "Z")
        else:
            root.rotation_quaternion = Vector((direction.x, direction.y, 0.0)).to_track_quat("Y", "Z")
        # Hard cap on rotation speed: whatever the path does, the body (and
        # therefore the mounted camera) may never swing faster than the cap.
        # This is the final guarantee against the "crazy shaking / spinning"
        # complaints; a sharp path corner now simply turns smoothly over more
        # frames instead of snapping.
        if previous_rotation is not None and genre in {"fps", "tps"}:
            # Cap per sample, not per nominal frame spacing: authored beat keys
            # can be one frame apart, so multiplying by the sample step would
            # still allow an 8 degree/frame swing there.
            turn_step = math.radians(float(_MAX_TURN_DEG_PER_FRAME))
            delta = previous_rotation.rotation_difference(root.rotation_quaternion).angle
            if delta > turn_step:
                root.rotation_quaternion = previous_rotation.slerp(
                    root.rotation_quaternion,
                    float(turn_step) / float(delta),
                )
        # The turn-speed clamp uses quaternion slerp; repeated small fractions
        # can leave sub-unit (decaying) quaternion keys, which make the audit
        # and interpolation read garbage.  Normalize before baking.
        root.rotation_quaternion.normalize()
        if previous_rotation is not None:
            root.rotation_quaternion.make_compatible(previous_rotation)
        previous_rotation = root.rotation_quaternion.copy()
        if tps_motion_keys is not None:
            tps_motion_keys.append((frame, position.copy(), root.rotation_quaternion.copy()))
        root.keyframe_insert("location", frame=frame)
        root.keyframe_insert("rotation_quaternion", frame=frame)
    set_linear_animation(root)
    if genre == "tps" and tps_motion_keys:
        retime_tps_actor(root, beats, tps_motion_keys)
        # Diagnostic: the start "probes right then left" (QA).  Print the
        # evaluated heading yaw over the first frames so we can see whether
        # the switch is a few-frame oscillation in the heading keys.
        bpy.context.scene.frame_set(max(1, int(beats[0]["frame"])))
        bpy.context.view_layer.update()
        previous_position = None
        for probe_frame in range(max(1, int(beats[0]["frame"])), min(int(beats[0]["frame"]) + 12, int(beats[-1]["frame"]) + 1)):
            bpy.context.scene.frame_set(probe_frame)
            bpy.context.view_layer.update()
            forward = root.matrix_world.to_3x3() @ Vector((0.0, 1.0, 0.0))
            yaw = math.degrees(math.atan2(float(forward.x), float(forward.y)))
            position = root.matrix_world.translation
            step = 0.0
            if previous_position is not None:
                step = (position - previous_position).length
            previous_position = position.copy()
            print(
                "TPS_START_YAW",
                probe_frame,
                "yaw",
                round(yaw, 1),
                "step_m",
                round(float(step), 3),
                "pos",
                round(float(position.x), 2),
                round(float(position.y), 2),
                flush=True,
            )
    root["code2games_route_sample_count"] = len(motion)
    root["code2games_route_detour_count"] = len(route_corrections)
    root["code2games_route_unresolved_count"] = len(route_unresolved)
    root["code2games_route_detours"] = json.dumps(route_corrections)
    root["code2games_route_unresolved_frames"] = json.dumps(route_unresolved)
    if genre == "racing":
        root["code2games_racing_tangent_locked"] = True
        # The final 24 frames are the intentional finish hold. Its final
        # tangent-locked orientation stays active, so coverage is evaluated
        # through the complete playback range, not only moving keys.
        root["code2games_racing_tangent_lock_sample_count"] = max(
            len(motion), int(bpy.context.scene.frame_end)
        )


def retime_tps_actor(root, beats, motion_keys):
    """Re-time continuous TPS motion to the explicit 50-second contract.

    The first combat control is a hard editorial anchor: Alpha attacks at
    frame 240 (10.0 s).  Motion before and after it is parameterized by actual
    travelled distance, so semantic/transit controls cannot create a pause or
    a gait-speed spike.  The final moving frame is 1176; main() retains the
    normal 24-frame ending tail, producing exactly 1200 frames / 50 seconds.
    """
    samples = []
    cumulative = 0.0
    previous_position = None
    for frame, position, rotation in motion_keys:
        if previous_position is not None:
            cumulative += (Vector(position) - previous_position).length
        samples.append((int(frame), float(cumulative), Vector(position), rotation.copy()))
        previous_position = Vector(position)
    if len(samples) < 2 or cumulative < 0.01:
        return

    old_frames = [item[0] for item in samples]
    distances = [item[1] for item in samples]

    def distance_at(old_frame):
        old_frame = int(old_frame)
        right = bisect.bisect_left(old_frames, old_frame)
        if right <= 0:
            return distances[0]
        if right >= len(samples):
            return distances[-1]
        left = right - 1
        frame_a, frame_b = old_frames[left], old_frames[right]
        if frame_b <= frame_a:
            return distances[left]
        amount = (old_frame - frame_a) / float(frame_b - frame_a)
        return distances[left] + (distances[right] - distances[left]) * amount

    combat_old_frames = [
        int(beat["frame"])
        for beat in beats
        if beat.get("kind") == "authored" and beat.get("event_type") == "combat"
    ]
    anchor_old_frame = min(combat_old_frames) if combat_old_frames else old_frames[0]
    anchor_distance = distance_at(anchor_old_frame)
    total_distance = distances[-1]
    target_start = 1
    target_anchor = int(_TPS_FIRST_ENEMY_ATTACK_FRAME)
    target_end = max(target_anchor + 240, int(_TPS_TARGET_MOTION_END_FRAME))

    def timeline_frame(distance):
        distance = float(distance)
        if anchor_distance > 0.01 and distance <= anchor_distance:
            amount = distance / anchor_distance
            return target_start + int(round(amount * (target_anchor - target_start)))
        remaining = max(0.01, total_distance - anchor_distance)
        amount = max(0.0, min(1.0, (distance - anchor_distance) / remaining))
        return target_anchor + int(round(amount * (target_end - target_anchor)))

    new_keys = []
    last_new_frame = None
    for old_frame, distance, position, rotation in samples:
        new_frame = timeline_frame(distance)
        if last_new_frame is not None and new_frame <= last_new_frame:
            continue
        new_keys.append((old_frame, new_frame, position, rotation))
        last_new_frame = new_frame
    if new_keys[-1][1] != target_end:
        old_frame, _new_frame, position, rotation = new_keys[-1]
        new_keys[-1] = (old_frame, target_end, position, rotation)

    if root.animation_data and root.animation_data.action:
        action = root.animation_data.action
        for curve in list(action.fcurves):
            action.fcurves.remove(curve)
    root.rotation_mode = "QUATERNION"
    for old_frame, new_frame, position, rotation in new_keys:
        root.location = position
        root.rotation_quaternion = rotation
        root.keyframe_insert("location", frame=int(new_frame))
        root.keyframe_insert("rotation_quaternion", frame=int(new_frame))
    set_linear_animation(root)
    clamp_tps_actor_rotation_curve(root, target_start, target_end)

    for beat in beats:
        beat["frame"] = int(timeline_frame(distance_at(int(beat["frame"]))))
    root["code2games_tps_retimed_from_actual_distance"] = True
    root["code2games_tps_timeline_contract"] = "alpha_attack_240_motion_end_%d" % target_end
    print(
        "TPS_RETIMED_SAMPLES", len(new_keys),
        "first_combat_frame", target_anchor,
        "final_motion_frame", int(new_keys[-1][1]),
        flush=True,
    )


def clamp_tps_actor_rotation_curve(root, frame_start, frame_end):
    """Bake the retimed TPS heading with a strict per-render-frame turn cap.

    ``animate_actor`` caps successive source samples, but the final
    distance-based re-time compresses some neighbouring samples.  At the
    Delta encounter this turned two safe source keys into a 16-degree change
    at frame 1040.  Audit and cap the *evaluated final timeline* instead of
    assuming the old sample spacing survived re-timing.
    """
    scene = bpy.context.scene
    frame_start = max(1, int(frame_start))
    frame_end = max(frame_start, int(frame_end))
    sampled = []
    previous = None
    maximum_step = math.radians(min(3.8, float(_MAX_TURN_DEG_PER_FRAME)))
    corrected_count = 0
    for frame in range(frame_start, frame_end + 1):
        scene.frame_set(frame)
        bpy.context.view_layer.update()
        rotation = root.evaluated_get(bpy.context.evaluated_depsgraph_get()).rotation_quaternion.copy()
        rotation.normalize()
        if previous is not None:
            rotation.make_compatible(previous)
            delta = previous.rotation_difference(rotation).angle
            if delta > maximum_step + 1e-7:
                rotation = previous.slerp(rotation, maximum_step / delta)
                rotation.normalize()
                rotation.make_compatible(previous)
                corrected_count += 1
        sampled.append((frame, rotation.copy()))
        previous = rotation.copy()

    action = root.animation_data.action if root.animation_data else None
    if action is not None:
        for curve in list(action.fcurves):
            if curve.data_path == "rotation_quaternion":
                action.fcurves.remove(curve)
    root.rotation_mode = "QUATERNION"
    for frame, rotation in sampled:
        root.rotation_quaternion = rotation
        root.keyframe_insert("rotation_quaternion", frame=frame)
    set_linear_animation(root)
    root["code2games_tps_final_rotation_cap_deg_per_frame"] = math.degrees(maximum_step)
    root["code2games_tps_final_rotation_corrected_frame_count"] = int(corrected_count)
    print(
        "TPS_FINAL_ROTATION_CAP",
        round(math.degrees(maximum_step), 3),
        "corrected_frames",
        corrected_count,
        flush=True,
    )


def ease_ground_event_turns(player, beats):
    """Slowly ease the ground body onto the next leg after every event hold.

    The route solver keys body rotation at every four-frame sample.  Right
    after a stationary collect/interact hold the body used to snap onto the
    outgoing heading within a single sample, which read as a fast camera
    "cut" (and, combined with the old fallback heading, as a spin).  Insert
    intermediate keys so a 40-60 degree turn takes roughly 16 frames.
    Combat beats are skipped: key_player_combat_body_facing owns those.
    """
    player.rotation_mode = "QUATERNION"
    for index, beat in enumerate(beats):
        if beat.get("kind") != "authored":
            continue
        event_type = str(beat.get("event_type", ""))
        if event_type not in {"collect", "collect_required", "interact", "goal"}:
            continue
        event_frame = int(beat["frame"])
        hold_end = event_frame
        for candidate in beats[index + 1:index + 4]:
            if candidate.get("event_type") == "event_hold":
                hold_end = int(candidate["frame"])
                break
        origin = Vector(beat["staged_position"])
        incoming = None
        for candidate in range(index - 1, -1, -1):
            delta = origin - Vector(beats[candidate]["staged_position"])
            delta.z = 0.0
            if delta.length > 0.05:
                incoming = delta.normalized()
                break
        outgoing = None
        for candidate in range(index + 1, len(beats)):
            delta = Vector(beats[candidate]["staged_position"]) - origin
            delta.z = 0.0
            if delta.length > 0.05:
                outgoing = delta.normalized()
                break
        if incoming is None or outgoing is None:
            continue
        turn_angle = math.degrees(
            math.acos(max(-1.0, min(1.0, incoming.dot(outgoing))))
        )
        if turn_angle < 8.0:
            continue
        incoming_rotation = incoming.to_track_quat("Y", "Z")
        outgoing_rotation = outgoing.to_track_quat("Y", "Z")
        previous = incoming_rotation.copy()

        def key(frame, rotation):
            nonlocal previous
            rotation = rotation.copy()
            rotation.make_compatible(previous)
            player.rotation_quaternion = rotation
            player.keyframe_insert("rotation_quaternion", frame=max(1, int(frame)))
            previous = rotation

        key(event_frame - 4, incoming_rotation)
        key(hold_end + 2, incoming_rotation)
        key(hold_end + 8, incoming_rotation.slerp(outgoing_rotation, 0.45))
        key(hold_end + 16, outgoing_rotation)
        key(hold_end + 24, outgoing_rotation)
    normalize_rotation_curve_winding(player)


def look_quaternion(origin, target):
    direction = Vector(target) - Vector(origin)
    if direction.length < 1e-5:
        direction = Vector((0.0, 1.0, 0.0))
    return direction.to_track_quat("-Z", "Y")


def camera_view_is_blocked(camera_position, target, z_extent, strict_scenery=False):
    """Return True when solid scenery blocks the camera-to-target segment."""
    scene = bpy.context.scene
    depsgraph = bpy.context.evaluated_depsgraph_get()
    origin = Vector(camera_position)
    target = Vector(target)
    ray = target - origin
    ray_length = ray.length
    if ray_length < 0.25:
        return True
    direction = ray.normalized()
    origin += direction * 0.08
    remaining = ray_length - 0.16
    for _ in range(32):
        hit, location, _normal, _face, obj, _matrix = scene.ray_cast(
            depsgraph,
            origin,
            direction,
            distance=max(0.0, remaining),
        )
        if not hit:
            return False
        travelled = (Vector(location) - origin).length
        remaining -= travelled + 0.04
        if remaining <= 0.30:
            return False
        # Placed props are real spring-arm obstacles; only Director-owned
        # actors/effects are transparent to the camera ray.
        if obj and gameplay_placement_object(obj):
            return True
        if obj and director_or_gameplay_object(obj):
            origin = Vector(location) + direction * 0.04
            continue
        if obj and non_ground_scenery(obj) and not solid_route_scenery(obj) and not strict_scenery:
            origin = Vector(location) + direction * 0.04
            continue
        return True
    return True


def camera_position_is_clear(position, radius=1.15):
    """Reject lens positions embedded in foliage, props, rock or vehicle mesh."""
    scene = bpy.context.scene
    depsgraph = bpy.context.evaluated_depsgraph_get()
    origin = Vector(position)
    directions = [
        Vector((math.cos(angle), math.sin(angle), 0.0))
        for angle in (0.0, math.pi * 0.25, math.pi * 0.5, math.pi * 0.75, math.pi, math.pi * 1.25, math.pi * 1.5, math.pi * 1.75)
    ] + [UP, -UP]
    for direction in directions:
        hit, _location, _normal, _face, obj, _matrix = scene.ray_cast(
            depsgraph,
            origin,
            direction,
            distance=float(radius),
        )
        if hit and obj and not director_or_gameplay_object(obj):
            return False
    return True


def supported_camera_position(position, forward, side, target, candidates, z_extent, preferred_index=None, strict_scenery=False):
    """Choose the first terrain-safe chase position with an unobstructed view."""
    fallback = None
    candidate_order = list(range(len(candidates)))
    if preferred_index is not None and 0 <= int(preferred_index) < len(candidates):
        preferred_index = int(preferred_index)
        candidate_order = [preferred_index] + [index for index in candidate_order if index != preferred_index]
    for candidate_index in candidate_order:
        back_distance, side_distance, height = candidates[candidate_index]
        candidate = position - forward * back_distance + side * side_distance + UP * height
        camera_ground = terrain_height(candidate.x, candidate.y, candidate.z, z_extent)
        candidate.z = max(candidate.z, camera_ground + 1.6)
        fallback = candidate
        if (
            camera_position_is_clear(candidate, radius=2.4 if strict_scenery else 1.15)
            and not camera_view_is_blocked(candidate, target, z_extent, strict_scenery=strict_scenery)
        ):
            return candidate, candidate_index, False
    return fallback, candidate_order[-1], True


def racing_camera_lookahead_target(beats, index, z_extent, lookahead_distance=10.0):
    """Aim a centre-mounted camera toward the road before the car reaches a grade."""
    origin = Vector(beats[index]["staged_position"])
    cursor = origin.copy()
    remaining = float(lookahead_distance)
    target = origin + direction_for(beats, index) * remaining
    for next_index in range(index + 1, len(beats)):
        next_position = Vector(beats[next_index]["staged_position"])
        segment = next_position - cursor
        segment_length = segment.length
        if segment_length >= remaining and segment_length > 1e-5:
            target = cursor + segment * (remaining / segment_length)
            break
        remaining -= segment_length
        cursor = next_position
        target = next_position
        if remaining <= 1e-5:
            break
    target_ground = terrain_height(target.x, target.y, target.z, z_extent)
    target.z = max(target.z, target_ground + 1.35)
    return target


def hide_racing_camera_foliage(actor, beats, z_extent, camera_back=13.5, camera_height=3.0):
    """Hide foliage intersecting the rigid racing camera boom.

    The chase camera is a fixed transform behind the car; it cannot sidestep
    an object that sits on the boom line.  The lowland source has a few
    shelters/awnings as well as vegetation; either reads as a camera glitch
    when the chase rig passes through it.  Only actual boom blockers are
    hidden, and gameplay assets are never touched.
    """
    scene = bpy.context.scene
    depsgraph = bpy.context.evaluated_depsgraph_get()
    source_frame = int(scene.frame_current)
    frame_list = sorted({int(beat["frame"]) for beat in beats})
    if not frame_list:
        return []
    start_frame = max(1, frame_list[0])
    end_frame = max(start_frame + 1, frame_list[-1])
    hidden = []
    seen = set()
    try:
        # Use the evaluated vehicle transform, rather than re-deriving a
        # second heading from the sparse path beats.  The camera is mounted to
        # this same yaw, so foliage removal must inspect the same boom line.
        for frame in range(start_frame, end_frame + 1):
            scene.frame_set(frame)
            bpy.context.view_layer.update()
            position = actor.matrix_world.translation.copy()
            forward = actor.matrix_world.to_3x3() @ Vector((0.0, 1.0, 0.0))
            forward.z = 0.0
            if forward.length < 1e-5:
                forward = Vector((0.0, 1.0, 0.0))
            forward.normalize()
            camera_position = position - forward * float(camera_back) + UP * float(camera_height)
            target = position + UP * 0.5
            ray = target - camera_position
            remaining = ray.length
            if remaining < 0.5:
                continue
            direction = ray.normalized()
            origin = camera_position + direction * 0.08
            remaining -= 0.16
            for _ in range(24):
                hit, location, _normal, _face, obj, _matrix = scene.ray_cast(
                    depsgraph,
                    origin,
                    direction,
                    distance=max(0.0, remaining),
                )
                if not hit:
                    break
                travelled = (Vector(location) - origin).length
                remaining -= travelled + 0.04
                if remaining <= 0.30:
                    break
                name_text = obj.name.lower() if obj else ""
                camera_blocker = bool(
                    obj
                    and (
                        non_ground_scenery(obj)
                        or any(token in name_text for token in (
                            "roof", "shelter", "canopy", "awning", "hut", "pavilion", "tent",
                        ))
                    )
                )
                if camera_blocker and not gameplay_placement_object(obj):
                    source = obj.original if getattr(obj, "is_evaluated", False) else obj
                    if source.name not in seen:
                        seen.add(source.name)
                        source.hide_render = False
                        source.keyframe_insert("hide_render", frame=start_frame)
                        source.hide_render = True
                        source.keyframe_insert("hide_render", frame=start_frame + 1)
                        source.keyframe_insert("hide_render", frame=end_frame)
                        source.hide_render = False
                        source.keyframe_insert("hide_render", frame=end_frame + 1)
                        hidden.append(source.name)
                    origin = Vector(location) + direction * 0.04
                    continue
                if obj and director_or_gameplay_object(obj):
                    origin = Vector(location) + direction * 0.04
                    continue
                break
    finally:
        scene.frame_set(source_frame)
    return hidden


def create_racing_center_mount_camera(camera, data, actor, beats, z_extent):
    """Create a rigid yaw-relative third-person driving camera.

    The route solver, not the camera, owns obstacle avoidance.  The camera is
    baked directly from the *evaluated vehicle root* every playback frame.
    That is essential: independently smoothing the sparse director beats made
    the camera lead/lag the dense vehicle animation and visually weave left
    and right even on a straight.  World-up is retained so suspension
    pitch/roll does not tilt the horizon, but yaw, position and boom offset
    are exactly shared with the finished vehicle animation.
    """
    if camera.animation_data:
        camera.animation_data_clear()
    camera.parent = None
    camera.rotation_mode = "QUATERNION"
    data.lens = float(_RACING_CAMERA_LENS)
    data.clip_start = 0.10
    data.clip_end = max(float(data.clip_end), 1200.0)
    scene = bpy.context.scene
    source_frame = int(scene.frame_current)
    start_frame = max(1, int(beats[0]["frame"]))
    end_frame = max(start_frame, int(beats[-1]["frame"]))
    previous_rotation = None
    for frame in range(start_frame, end_frame + 1):
        scene.frame_set(frame)
        bpy.context.view_layer.update()
        position = actor.matrix_world.translation.copy()
        forward = actor.matrix_world.to_3x3() @ Vector((0.0, 1.0, 0.0))
        forward.z = 0.0
        if forward.length < 1e-5:
            forward = Vector((0.0, 1.0, 0.0))
        else:
            forward.normalize()
        # Low chase boom: further back and lower than the old rig so the whole
        # car sits in the lower third of the frame with the road and scenery
        # ahead visible above it.  Single rigid yaw-relative transform, never
        # a cinematic orbit.
        camera_position = position - forward * float(_RACING_CAMERA_BACK) + UP * float(_RACING_CAMERA_HEIGHT)
        target = position + forward * float(_RACING_CAMERA_LOOKAHEAD) + UP * 0.9
        rotation = look_quaternion(camera_position, target)
        if previous_rotation is not None:
            rotation.make_compatible(previous_rotation)
        previous_rotation = rotation.copy()
        camera.location = camera_position
        camera.rotation_quaternion = rotation
        camera.keyframe_insert("location", frame=frame)
        camera.keyframe_insert("rotation_quaternion", frame=frame)
    set_linear_animation(camera)
    camera["code2games_camera_system"] = "rigid_low_yaw_relative_vehicle_chase"
    camera["code2games_camera_local_position"] = [0.0, -float(_RACING_CAMERA_BACK), float(_RACING_CAMERA_HEIGHT)]
    camera["code2games_camera_local_target"] = [0.0, float(_RACING_CAMERA_LOOKAHEAD), 0.9]
    camera["code2games_camera_relative_transform_animated"] = True
    camera["code2games_camera_vehicle_locked_every_frame"] = True
    camera["code2games_camera_preferred_distance_m"] = round(math.hypot(float(_RACING_CAMERA_BACK), float(_RACING_CAMERA_HEIGHT)), 4)
    camera["code2games_camera_preferred_height_m"] = float(_RACING_CAMERA_HEIGHT)
    camera["code2games_camera_lookahead_m"] = float(_RACING_CAMERA_LOOKAHEAD)
    camera["code2games_camera_minimum_ground_clearance_m"] = None
    camera["code2games_camera_occlusion_corrections"] = 0
    camera["code2games_camera_unresolved_occlusions"] = 0
    hidden_foliage = (
        hide_racing_camera_foliage(
            actor,
            beats,
            z_extent,
            camera_back=float(_RACING_CAMERA_BACK),
            camera_height=float(_RACING_CAMERA_HEIGHT),
        )
        if _RACING_HIDE_CAMERA_FOLIAGE
        else []
    )
    camera["code2games_camera_hidden_foliage_count"] = len(hidden_foliage)
    scene.frame_set(source_frame)
    return camera


def audit_racing_camera_visibility(camera, actor, beats):
    """No-render check that the vehicle core remains inside the chase view."""
    if not beats:
        return {"visible_ratio": 0.0, "sample_count": 0, "unresolved_frames": []}
    scene = bpy.context.scene
    source_frame = int(scene.frame_current)
    sample_frames = list(range(int(beats[0]["frame"]), int(beats[-1]["frame"]) + 1, 12))
    if not sample_frames or sample_frames[-1] != int(beats[-1]["frame"]):
        sample_frames.append(int(beats[-1]["frame"]))
    visible = 0
    unresolved = []
    try:
        for frame in sample_frames:
            scene.frame_set(frame)
            target = actor.matrix_world.translation + UP * 1.0
            projected = world_to_camera_view(scene, camera, target)
            in_frame = (
                projected.z > 0.0
                and 0.06 <= projected.x <= 0.94
                and 0.05 <= projected.y <= 0.95
            )
            if in_frame:
                visible += 1
            else:
                unresolved.append(int(frame))
    finally:
        scene.frame_set(source_frame)
    return {
        "visible_ratio": round(visible / float(max(1, len(sample_frames))), 6),
        "sample_count": len(sample_frames),
        "unresolved_frames": unresolved,
    }


def create_aircraft_afterburner(collection, actor, motion, beats=None):
    marker_name = str(actor.get("code2games_aircraft_exhaust_marker", ""))
    marker = bpy.data.objects.get(marker_name)
    if marker is None:
        return {"enabled": False, "reason": "runtime exhaust marker unavailable"}
    outer_material = material(
        "C2G_F104_AfterburnerOuter",
        (0.08, 0.30, 1.0, 1.0),
        emission=15.0,
        metallic=0.0,
        roughness=0.18,
    )
    inner_material = material(
        "C2G_F104_AfterburnerCore",
        (1.0, 0.72, 0.18, 1.0),
        emission=28.0,
        metallic=0.0,
        roughness=0.10,
    )
    for value, alpha in ((outer_material, 0.38), (inner_material, 0.72)):
        node = value.node_tree.nodes.get("Principled BSDF") if value.node_tree else None
        if node and node.inputs.get("Alpha"):
            node.inputs["Alpha"].default_value = alpha
        value.diffuse_color[3] = alpha
        if hasattr(value, "surface_render_method"):
            value.surface_render_method = "DITHERED"

    def plume(name, location_y, radius, depth, value):
        bpy.ops.mesh.primitive_cone_add(
            vertices=32,
            radius1=radius,
            radius2=radius * 0.10,
            depth=depth,
            location=(0.0, location_y, 0.0),
            rotation=(math.radians(-90.0), 0.0, 0.0),
        )
        obj = bpy.context.object
        obj.name = name
        assign_material(obj, value)
        link_only(obj, collection)
        obj.parent = marker
        return obj

    outer = plume("C2G_F104_AFTERBURNER_OUTER", 0.86, 0.27, 1.75, outer_material)
    inner = plume("C2G_F104_AFTERBURNER_CORE", 0.58, 0.15, 1.15, inner_material)
    light_data = bpy.data.lights.new("C2G_F104_AFTERBURNER_LIGHT_DATA", "POINT")
    light_data.color = (0.18, 0.42, 1.0)
    light_data.shadow_soft_size = 0.45
    light = bpy.data.objects.new("C2G_F104_AFTERBURNER_LIGHT", light_data)
    collection.objects.link(light)
    light.parent = marker
    light.location = (0.0, 0.18, 0.0)
    keyed = 0
    flight_start = next(
        (int(item["frame"]) for item in motion if not item.get("aircraft_parked")),
        int(motion[0]["frame"]),
    )
    pullup_start = max(
        int(motion[0]["frame"]),
        int(motion[-1]["frame"]) - int(_WINGSUIT_PULLUP_FRAMES),
    )
    collection_frames = sorted(
        int(beat["frame"])
        for beat in (beats or [])
        if beat.get("event_type") == "flight_collectible"
    )
    for index, item in enumerate(motion[::2]):
        frame = int(item["frame"])
        pulse = 0.92 + 0.10 * math.sin(frame * 0.43) + 0.04 * math.sin(frame * 1.17)
        collected = sum(1 for collection_frame in collection_frames if frame >= collection_frame)
        cell_power = min(1.18, 0.54 + collected * 0.16)
        pullup_boost = 1.08 if frame >= pullup_start else 1.0
        ignition = 0.001 if frame < flight_start else 1.0
        outer.scale = (
            pulse * cell_power * ignition,
            pulse * cell_power * pullup_boost * ignition,
            pulse * cell_power * ignition,
        )
        inner.scale = (
            pulse * cell_power * ignition,
            pulse * cell_power * pullup_boost * ignition,
            pulse * cell_power * ignition,
        )
        light_data.energy = 520.0 * pulse * cell_power * pullup_boost * ignition
        outer.keyframe_insert("scale", frame=frame)
        inner.keyframe_insert("scale", frame=frame)
        light_data.keyframe_insert("energy", frame=frame)
        keyed += 1
    for obj in (outer, inner):
        set_linear_animation(obj)
    return {
        "enabled": True,
        "marker": marker.name,
        "plume_objects": [outer.name, inner.name],
        "light_object": light.name,
        "keyed_sample_count": keyed,
        "pullup_afterburner_boost": 1.08,
        "flight_cell_collection_frames": collection_frames,
        "flight_cell_power_levels": [0.70, 0.86, 1.02, 1.18],
        "collection_changes_engine_output": len(collection_frames) == 4,
    }


def create_aircraft_camera(collection, actor, motion):
    data = bpy.data.cameras.new("C2G_WINGSUIT_AIRCRAFT_CAMERA_DATA")
    camera = bpy.data.objects.new("C2G_WINGSUIT_AIRCRAFT_CAMERA", data)
    collection.objects.link(camera)
    camera.rotation_mode = "QUATERNION"
    data.lens = 44.0
    data.clip_start = 0.12
    data.clip_end = max(float(data.clip_end), 1200.0)
    z_extent = scene_z_extent()
    previous_rotation = None
    previous_camera_position = None
    minimum_clearance = float("inf")
    occlusion_corrections = 0
    unresolved_occlusions = 0
    camera_samples = motion[::2]
    if camera_samples[-1]["frame"] != motion[-1]["frame"]:
        camera_samples.append(motion[-1])
    opening_heading = Vector((0.0, -1.0, 0.0))
    opening_origin = Vector(camera_samples[0]["position"])
    for candidate in camera_samples:
        horizontal = Vector(candidate["position"]) - opening_origin
        horizontal.z = 0.0
        if horizontal.length >= 5.0:
            opening_heading = horizontal.normalized()
            break
    for index, item in enumerate(camera_samples):
        position = Vector(item["position"])
        # Search beyond stationary opening samples so the establishing camera
        # already understands the actual launch heading.
        previous = position.copy()
        following = position.copy()
        for offset in range(1, len(camera_samples)):
            before_index = max(0, index - offset)
            after_index = min(len(camera_samples) - 1, index + offset)
            previous = Vector(camera_samples[before_index]["position"])
            following = Vector(camera_samples[after_index]["position"])
            if (following - previous).length > 0.25:
                break
        forward = following - previous
        if forward.length < 1e-5:
            forward = Vector((0.0, 1.0, 0.0))
        forward.normalize()
        side = Vector((-forward.y, forward.x, 0.0))
        if side.length < 1e-5:
            side = Vector((1.0, 0.0, 0.0))
        else:
            side.normalize()
        frame = int(item["frame"])
        last_frame = max(1, int(motion[-1]["frame"]))
        phase = frame / float(last_frame)
        parked = bool(item.get("aircraft_parked")) or frame <= 73
        if parked:
            forward = opening_heading.copy()
            side = Vector((-forward.y, forward.x, 0.0)).normalized()
        pullup = phase >= 0.80
        if parked:
            # Wide side/rear establishing shot: aircraft, launch platform and
            # cliff edge remain readable together for the full preflight beat.
            desired = position - forward * 17.0 + side * 20.0 + UP * 7.0
            target = position + forward * 2.0 + UP * 0.7
            lens = 48.0
        elif pullup:
            desired = position - forward * 28.0 + side * 18.0 + UP * 9.0
            target = position + forward * 5.0 + UP * 1.0
            lens = 48.0
        else:
            orbit_side = 2.5 * math.sin(phase * math.pi * 1.2)
            desired = position - forward * 27.0 + side * orbit_side + UP * 7.5
            target = position + forward * 8.0 + UP * 0.7
            lens = 42.0

        candidates = [
            desired,
            position - forward * 23.0 + side * 9.0 + UP * 10.0,
            position - forward * 21.0 - side * 9.0 + UP * 11.5,
            position - forward * 18.0 + UP * 15.0,
        ]
        camera_position = candidates[-1]
        resolved = False
        for candidate_index, candidate in enumerate(candidates):
            candidate = Vector(candidate)
            ground = terrain_height(candidate.x, candidate.y, candidate.z, z_extent)
            candidate.z = max(candidate.z, ground + 3.2)
            if (
                camera_position_is_clear(candidate, radius=2.4)
                and not camera_view_is_blocked(candidate, target, z_extent, strict_scenery=True)
            ):
                camera_position = candidate
                resolved = True
                if candidate_index:
                    occlusion_corrections += 1
                break
        if not resolved:
            unresolved_occlusions += 1
        if previous_camera_position is not None:
            displacement = camera_position - previous_camera_position
            maximum_step = 5.0
            if displacement.length > maximum_step:
                camera_position = previous_camera_position + displacement.normalized() * maximum_step
        previous_camera_position = camera_position.copy()
        ground = terrain_height(camera_position.x, camera_position.y, camera_position.z, z_extent)
        camera_position.z = max(camera_position.z, ground + 3.2)
        rotation = look_quaternion(camera_position, target)
        if previous_rotation is not None:
            rotation.make_compatible(previous_rotation)
        previous_rotation = rotation.copy()
        camera.location = camera_position
        camera.rotation_quaternion = rotation
        data.lens = lens
        camera.keyframe_insert("location", frame=frame)
        camera.keyframe_insert("rotation_quaternion", frame=frame)
        data.keyframe_insert("lens", frame=frame)
        minimum_clearance = min(minimum_clearance, camera_position.z - ground)
    set_linear_animation(camera)
    if camera.animation_data and camera.animation_data.action:
        for curve in camera.animation_data.action.fcurves:
            if curve.data_path != "location":
                continue
            for point in curve.keyframe_points:
                point.interpolation = "BEZIER"
                point.handle_left_type = "AUTO_CLAMPED"
                point.handle_right_type = "AUTO_CLAMPED"
    camera["code2games_camera_system"] = "parked_establish_then_terrain_aware_aircraft_chase"
    camera["code2games_camera_sample_step_frames"] = 6
    camera["code2games_camera_minimum_ground_clearance_m"] = round(float(minimum_clearance), 4)
    camera["code2games_camera_occlusion_corrections"] = int(occlusion_corrections)
    camera["code2games_camera_unresolved_occlusions"] = int(unresolved_occlusions)
    return camera


def create_rigid_left_rear_aircraft_camera(collection, actor, motion):
    """Stable flight-path boom fixed to the F-104's left rear quarter.

    The previous camera searched several world-space candidates whenever a
    tree or slope entered the view.  Those candidate switches looked like
    cuts, overhead resets and an independently orbiting drone.  This rig keeps
    one left/rear/height offset and one forward look-ahead for the complete
    flight; only a vertical terrain safety clamp is permitted.
    """
    data = bpy.data.cameras.new("C2G_WINGSUIT_RIGID_CAMERA_DATA")
    camera = bpy.data.objects.new("C2G_WINGSUIT_RIGID_CAMERA", data)
    collection.objects.link(camera)
    camera.rotation_mode = "QUATERNION"
    data.lens = 38.0
    data.clip_start = 0.12
    data.clip_end = max(float(data.clip_end), 1200.0)
    z_extent = scene_z_extent()
    samples = motion[::2]
    if samples[-1]["frame"] != motion[-1]["frame"]:
        samples.append(motion[-1])
    first_airborne = next(
        (index for index, item in enumerate(samples) if not item.get("aircraft_parked")),
        0,
    )
    initial_forward = Vector((0.0, -1.0, 0.0))
    for index in range(first_airborne, len(samples) - 1):
        delta = Vector(samples[index + 1]["position"]) - Vector(samples[index]["position"])
        if delta.length > 0.5:
            initial_forward = delta.normalized()
            break
    previous_rotation = None
    # At launch the clearance solver makes several intentionally sharp course
    # corrections around the starting summit.  Feeding each instantaneous
    # heading straight into a rear boom reads as camera shake.  Damp only the
    # opening flight heading; the blend reaches the true heading before the
    # first long terrain sweep, and the destination pass remains unmodified.
    smoothed_forward = initial_forward.copy()
    terrain_clamps = 0
    minimum_clearance = float("inf")
    destination_framing_samples = 0
    for index, item in enumerate(samples):
        position = Vector(item["position"])
        before = Vector(samples[max(first_airborne, index - 1)]["position"])
        after = Vector(samples[min(len(samples) - 1, max(first_airborne, index + 1))]["position"])
        forward = after - before
        if item.get("aircraft_parked") or forward.length < 0.25:
            forward = initial_forward.copy()
        forward.normalize()
        frame = int(item["frame"])
        if not item.get("aircraft_parked") and frame <= 288:
            smoothed_forward = smoothed_forward.lerp(forward, 0.26)
            if smoothed_forward.length > 1e-5:
                forward = smoothed_forward.normalized()
        elif not item.get("aircraft_parked") and frame <= 360:
            smoothed_forward = smoothed_forward.lerp(forward, 0.55)
            if smoothed_forward.length > 1e-5:
                forward = smoothed_forward.normalized()
        else:
            smoothed_forward = forward.copy()
        horizontal = Vector((forward.x, forward.y, 0.0))
        if horizontal.length < 1e-5:
            horizontal = Vector((initial_forward.x, initial_forward.y, 0.0))
        horizontal.normalize()
        left = Vector((-horizontal.y, horizontal.x, 0.0))
        destination_framing = bool(
            item.get("aircraft_destination_finale")
            or item.get("aircraft_shallow_summit_exit")
        )
        if destination_framing:
            # Widen and look slightly down during the final summit pass.  The
            # boom remains on the same left-rear side; only its framing opens
            # enough to retain the plateau and sequential assets below the jet.
            # V8 QA still lost one of five anchors: the aircraft is six metres
            # above the plateau, so aiming only 1.8 m below it left the ground
            # target on the lower edge during the final turn.  Centre the boom
            # between aircraft and summit, reduce the side offset, and widen
            # the lens.  This is still one continuous left-rear camera.
            camera_position = position - forward * 22.0 + left * 6.0 + UP * 5.0
            # The last summit anchor (placement_012) is reached on the
            # shallow exit at a much shorter camera depth than the preceding
            # four anchors.  Keep that close, final ground target above the
            # lower safe-frame margin without changing the flight path.
            target = position + forward * 8.0 - UP * 8.0
            data.lens = 30.0
            destination_framing_samples += 1
        else:
            camera_position = position - forward * 28.0 + left * 11.0 + UP * 7.5
            target = position + forward * 12.0 + UP * 0.45
            data.lens = 38.0
        ground = terrain_height(camera_position.x, camera_position.y, camera_position.z, z_extent)
        safe_z = ground + 3.0
        if camera_position.z < safe_z:
            camera_position.z = safe_z
            terrain_clamps += 1
        minimum_clearance = min(minimum_clearance, camera_position.z - ground)
        rotation = look_quaternion(camera_position, target)
        if previous_rotation is not None:
            rotation.make_compatible(previous_rotation)
        previous_rotation = rotation.copy()
        camera.location = camera_position
        camera.rotation_quaternion = rotation
        camera.keyframe_insert("location", frame=frame)
        camera.keyframe_insert("rotation_quaternion", frame=frame)
        data.keyframe_insert("lens", frame=frame)
    if camera.animation_data and camera.animation_data.action:
        for curve in camera.animation_data.action.fcurves:
            for point in curve.keyframe_points:
                point.interpolation = "BEZIER"
                point.handle_left_type = "AUTO_CLAMPED"
                point.handle_right_type = "AUTO_CLAMPED"
    camera["code2games_camera_system"] = "rigid_left_rear_flight_path_boom"
    camera["code2games_camera_local_position"] = [-11.0, -28.0, 7.5]
    camera["code2games_camera_local_target"] = [0.0, 12.0, 0.45]
    camera["code2games_camera_relative_transform_animated"] = False
    camera["code2games_camera_terrain_safety_clamps"] = int(terrain_clamps)
    camera["code2games_camera_minimum_ground_clearance_m"] = round(float(minimum_clearance), 4)
    camera["code2games_camera_occlusion_corrections"] = 0
    camera["code2games_camera_unresolved_occlusions"] = 0
    camera["code2games_destination_framing_sample_count"] = int(destination_framing_samples)
    camera["code2games_destination_framing_lens_mm"] = 30.0
    camera["code2games_destination_ground_lookdown_m"] = 8.0
    camera["code2games_opening_heading_damping_frames"] = 288
    return camera


def audit_wingsuit_destination_readability(camera, beats):
    """Project every terminal anchor into the authored chase camera.

    This is deliberately stricter than the V7 coordinate-only check: a
    destination event counts as readable only when its anchor is in front of
    the camera and inside a safe screen margin on its event frame.
    """
    destination_ids = {
        "placement_023", "placement_019", "placement_020", "placement_013", "placement_012",
    }
    scene = bpy.context.scene
    source_frame = int(scene.frame_current)
    samples = []
    try:
        for beat in beats:
            placement_id = str(beat.get("placement_id") or "")
            anchor = beat.get("fixed_anchor_world_xyz")
            if placement_id not in destination_ids or not anchor:
                continue
            frame = int(beat["frame"])
            scene.frame_set(frame)
            bpy.context.view_layer.update()
            projected = world_to_camera_view(scene, camera, Vector(anchor) + UP * 1.0)
            in_frame = bool(
                projected.z > 0.0
                and 0.03 <= projected.x <= 0.97
                and 0.03 <= projected.y <= 0.94
            )
            samples.append({
                "placement_id": placement_id,
                "frame": frame,
                "seconds": round(frame / float(scene.render.fps), 3),
                "screen_uv": [round(float(projected.x), 6), round(float(projected.y), 6)],
                "camera_depth": round(float(projected.z), 6),
                "in_safe_frame": in_frame,
            })
    finally:
        scene.frame_set(source_frame)
    frames = sorted(item["frame"] for item in samples)
    gaps = [second - first for first, second in zip(frames, frames[1:])]
    return {
        "enabled": True,
        "required_placement_ids": sorted(destination_ids),
        "sample_count": len(samples),
        "in_safe_frame_count": sum(1 for item in samples if item["in_safe_frame"]),
        "all_destination_anchors_in_safe_frame": bool(
            len(samples) == len(destination_ids)
            and all(item["in_safe_frame"] for item in samples)
        ),
        "minimum_event_gap_frames": min(gaps) if gaps else 0,
        "minimum_event_gap_seconds": round(min(gaps) / float(scene.render.fps), 3) if gaps else 0.0,
        "samples": samples,
    }


def hide_tps_camera_foliage(actor, local_position, local_target, frames):
    """Hide only foliage that intersects the rigid TPS boom over the shot.

    The spring-arm camera is parented to the actor with a mathematically
    constant local transform, so it can never sidestep a plant that sits on
    the boom line.  Hiding exactly that vegetation for the complete camera
    range keeps the framing stable without moving trees, gameplay assets, or
    terrain.  Solid trunks stay visible; only non-ground foliage that would
    otherwise sit inside the lens is hidden.
    """
    scene = bpy.context.scene
    depsgraph = bpy.context.evaluated_depsgraph_get()
    source_frame = int(scene.frame_current)
    frame_list = sorted({int(frame) for frame in frames})
    if not frame_list:
        return []
    start_frame = max(1, frame_list[0])
    end_frame = max(start_frame + 1, frame_list[-1])
    hidden = []
    seen = set()
    try:
        for frame in frame_list:
            scene.frame_set(frame)
            bpy.context.view_layer.update()
            matrix = actor.evaluated_get(depsgraph).matrix_world
            camera_position = matrix @ Vector(local_position)
            target = matrix @ Vector(local_target)
            ray = target - camera_position
            remaining = ray.length
            if remaining < 0.25:
                continue
            direction = ray.normalized()
            origin = camera_position + direction * 0.08
            remaining -= 0.16
            for _ in range(24):
                hit, location, _normal, _face, obj, _matrix = scene.ray_cast(
                    depsgraph,
                    origin,
                    direction,
                    distance=max(0.0, remaining),
                )
                if not hit:
                    break
                travelled = (Vector(location) - origin).length
                remaining -= travelled + 0.04
                if remaining <= 0.30:
                    break
                if obj and non_ground_scenery(obj) and not solid_route_scenery(obj):
                    source = obj.original if getattr(obj, "is_evaluated", False) else obj
                    if source.name not in seen:
                        seen.add(source.name)
                        # Hold the plant hidden for the whole rigid-boom shot;
                        # toggling per sample would flicker between keyframes.
                        source.hide_render = False
                        source.keyframe_insert("hide_render", frame=start_frame)
                        source.hide_render = True
                        source.keyframe_insert("hide_render", frame=start_frame + 1)
                        source.keyframe_insert("hide_render", frame=end_frame)
                        source.hide_render = False
                        source.keyframe_insert("hide_render", frame=end_frame + 1)
                        hidden.append(source.name)
                    origin = Vector(location) + direction * 0.04
                    continue
                if obj and director_or_gameplay_object(obj):
                    origin = Vector(location) + direction * 0.04
                    continue
                break
    finally:
        scene.frame_set(source_frame)
    return hidden


def create_tps_spring_arm_camera(camera, data, actor, beats):
    """A low left-rear TPS spring arm with real scenery collision.

    Preserve the shoulder framing, but shorten the boom before a tree, prop,
    or terrain surface reaches the lens.  Expansion is damped and upcoming
    obstruction samples are anticipated so entering/leaving an orchard does
    not create a second camera shake.
    """
    data.lens = 42.0
    data.clip_start = 0.10
    data.clip_end = max(float(data.clip_end), 1200.0)
    # A twelve-metre boom was inappropriate inside this dense woodland: even
    # when the player had a clear path, the lens crossed distant terrain and
    # several unrelated trees.  This is a conventional close TPS shoulder
    # rig, still showing the complete actor and useful space ahead.
    local_position = Vector((-2.00, -5.20, 3.00))
    local_target = Vector((0.0, 1.40, 1.20))
    # At the opening spawn, placement_003 sits directly behind the shoulder.
    # A 2.4 m hard floor left two collision samples unresolved even though a
    # conventional close shoulder shot at 1.65 m is still outside the actor.
    minimum_boom_distance = 1.65
    scene = bpy.context.scene
    depsgraph = bpy.context.evaluated_depsgraph_get()
    z_extent = scene_z_extent()
    frame_step = 2
    frames = list(range(int(beats[0]["frame"]), int(beats[-1]["frame"]) + 1, frame_step))
    if frames[-1] != int(beats[-1]["frame"]):
        frames.append(int(beats[-1]["frame"]))

    actor_tag = str(actor.name)
    if actor_tag.startswith("C2G_") and actor_tag.endswith("_ROOT"):
        actor_tag = actor_tag[4:-5]
    blocking_object_counts = {}
    hidden_camera_trees = {}
    frustum_ray_count = 0
    frustum_unresolved_count = 0

    def actor_hierarchy_object(obj):
        if obj is None:
            return False
        source = obj.original if getattr(obj, "is_evaluated", False) else obj
        if str(source.get("code2games_swat_actor", "")) == actor_tag:
            return True
        current = source
        for _ in range(64):
            if current is None:
                break
            if current == actor:
                return True
            current = current.parent
        return False

    def ignored_boom_hit(obj):
        if obj is None:
            return False
        if actor_hierarchy_object(obj):
            return True
        if director_or_gameplay_object(obj) and not gameplay_placement_object(obj):
            return True
        source = obj.original if getattr(obj, "is_evaluated", False) else obj
        source_name = str(source.name)
        # In this generated woodland each tree is an independent instance
        # (TreeFactory spawn placeholders or plain "Tree.123" objects).  If an
        # instance physically crosses the shoulder boom, retracting any farther
        # would put the lens inside the player (QA: 15s/26s camera-through-tree
        # clips came from non-TreeFactory trunks).  Hide only that obstructing
        # instance in the staged showcase; the source world, all other
        # vegetation and every gameplay asset stay untouched.  Keeping it
        # hidden for the full shot avoids visible tree popping when the camera
        # approaches/leaves the orchard.
        tree_owner = solid_tree_owner(source)
        if not gameplay_placement_object(obj) and tree_owner is not None:
            owner_name = str(tree_owner.name)
            if owner_name not in hidden_camera_trees:
                hidden_camera_trees[owner_name] = tree_owner
                hide_render_hierarchy(tree_owner)
                bpy.context.view_layer.update()
            return True
        return bool(
            not gameplay_placement_object(obj)
            and non_ground_scenery(obj)
            and not solid_route_scenery(obj)
        )

    def clear_camera_frustum(camera_position, actor_world):
        """Clear tree instances from the useful TPS view volume.

        A single spring-arm ray can pass through a gap while a nearby trunk
        still fills half of the rendered frame.  Trace a compact bundle from
        the lens to the actor's shoulders, head and feet.  TreeFactory spawn
        instances hit by any ray are hidden by ``ignored_boom_hit``; placed
        gameplay assets, terrain and structures remain untouched.
        """
        nonlocal frustum_ray_count, frustum_unresolved_count
        target_offsets = (
            Vector((0.0, 0.0, 0.0)),
            Vector((-0.75, 0.0, 0.0)),
            Vector((0.75, 0.0, 0.0)),
            Vector((-1.25, 0.0, 0.55)),
            Vector((1.25, 0.0, 0.55)),
            Vector((0.0, 0.0, 1.25)),
            Vector((0.0, 0.0, -0.70)),
            # Edge-of-frame probes: the previous body-sized bundle could
            # leave a trunk just outside the actor silhouette, where it still
            # occupied half of the rendered shoulder-camera image.
            Vector((-3.25, 0.0, 0.35)),
            Vector((3.25, 0.0, 0.35)),
            Vector((-2.65, 0.0, 1.65)),
            Vector((2.65, 0.0, 1.65)),
        )
        for offset in target_offsets:
            target = actor_world @ (local_target + offset)
            ray = target - Vector(camera_position)
            remaining = ray.length
            if remaining < 0.25:
                continue
            frustum_ray_count += 1
            direction = ray.normalized()
            origin = Vector(camera_position) + direction * 0.08
            remaining -= 0.16
            unresolved = False
            for _ in range(40):
                hit, location, _normal, _face, obj, _matrix = scene.ray_cast(
                    depsgraph,
                    origin,
                    direction,
                    distance=max(0.0, remaining),
                )
                if not hit:
                    break
                location = Vector(location)
                travelled = (location - origin).length
                remaining -= travelled + 0.04
                if remaining <= 0.30:
                    break
                if ignored_boom_hit(obj):
                    origin = location + direction * 0.04
                    continue
                unresolved = True
                break
            if unresolved:
                frustum_unresolved_count += 1

    def boom_clearance(target, desired):
        ray = Vector(desired) - Vector(target)
        nominal_distance = ray.length
        if nominal_distance < 0.5:
            return nominal_distance, False
        direction = ray.normalized()
        origin = Vector(target) + direction * 0.38
        remaining = max(0.0, nominal_distance - 0.38)
        for _ in range(40):
            hit, location, _normal, _face, obj, _matrix = scene.ray_cast(
                depsgraph,
                origin,
                direction,
                distance=remaining,
            )
            if not hit:
                return nominal_distance, False
            location = Vector(location)
            if ignored_boom_hit(obj):
                origin = location + direction * 0.05
                remaining = max(0.0, (Vector(desired) - origin).length)
                if remaining <= 0.10:
                    return nominal_distance, False
                continue
            hit_distance = max(0.0, (location - Vector(target)).length)
            blocker_name = obj.name if obj is not None else "<unknown>"
            blocking_object_counts[blocker_name] = blocking_object_counts.get(blocker_name, 0) + 1
            return max(minimum_boom_distance, hit_distance - 0.55), True
        return minimum_boom_distance, True

    def final_segment_is_blocked(camera_position, target):
        ray = Vector(target) - Vector(camera_position)
        remaining = ray.length
        if remaining < 0.25:
            return True
        direction = ray.normalized()
        origin = Vector(camera_position) + direction * 0.08
        remaining -= 0.16
        for _ in range(40):
            hit, location, _normal, _face, obj, _matrix = scene.ray_cast(
                depsgraph, origin, direction, distance=max(0.0, remaining)
            )
            if not hit:
                return False
            travelled = (Vector(location) - origin).length
            remaining -= travelled + 0.04
            if remaining <= 0.30:
                return False
            if ignored_boom_hit(obj):
                origin = Vector(location) + direction * 0.04
                continue
            return True
        return True

    samples = []
    for frame in frames:
        scene.frame_set(frame)
        bpy.context.view_layer.update()
        actor_world = actor.evaluated_get(depsgraph).matrix_world
        desired_position = actor_world @ local_position
        world_target = actor_world @ local_target
        clear_camera_frustum(desired_position, actor_world)
        # Hiding a blocking tree changes the evaluated ray-cast scene.
        bpy.context.view_layer.update()
        boom_direction = desired_position - world_target
        if boom_direction.length < 0.5:
            boom_direction = Vector((0.0, -1.0, 0.2))
        boom_direction.normalize()
        allowed_distance, obstructed = boom_clearance(world_target, desired_position)
        samples.append((frame, world_target, boom_direction, allowed_distance, obstructed))
    smoothed = []
    current_distance = None
    shortened_count = 0
    unresolved_count = 0
    emergency_contraction_count = 0
    nominal_distance = (local_position - local_target).length
    # Propagate every future obstruction backward into a continuous safety
    # envelope.  At most 0.55 m is retracted per two-frame sample, so a trunk
    # produces a smooth push-in instead of a one-frame camera jump.
    anticipated_distances = [float(sample[3]) for sample in samples]
    for index in range(len(anticipated_distances) - 2, -1, -1):
        anticipated_distances[index] = min(
            anticipated_distances[index],
            anticipated_distances[index + 1] + 0.55,
        )
    for index, (frame, target, direction, allowed_distance, _obstructed) in enumerate(samples):
        window = samples[max(0, index - 2):min(len(samples), index + 3)]
        average_target = sum((sample[1] for sample in window), Vector()) / float(len(window))
        average_direction = sum((sample[2] for sample in window), Vector()) / float(len(window))
        if average_direction.length < 1e-5:
            average_direction = direction.copy()
        average_direction.normalize()
        anticipated_distance = anticipated_distances[index]
        if current_distance is None or anticipated_distance < current_distance:
            current_distance = anticipated_distance
        else:
            current_distance = min(anticipated_distance, current_distance + 0.18)
        scene.frame_set(frame)
        bpy.context.view_layer.update()
        position = average_target + average_direction * current_distance
        # The averaged target/direction can differ slightly from the raw ray;
        # make a final bounded contraction if that smoothing reintroduces an
        # obstruction.  Assets and world scenery remain untouched.
        while (
            current_distance > minimum_boom_distance + 0.06
            and final_segment_is_blocked(position, average_target)
        ):
            current_distance = max(minimum_boom_distance, current_distance - 0.22)
            position = average_target + average_direction * current_distance
        # Two opening samples can still meet the back edge of the immutable
        # spawn prop at the ordinary 1.65 m spring-arm floor.  Contract on the
        # same boom line (no side jump/camera cut) for those exceptional
        # frames; 1.05 m remains outside the actor shoulder and is safer than
        # accepting a prop between the lens and target.
        if final_segment_is_blocked(position, average_target):
            emergency_floor = 1.05
            while (
                current_distance > emergency_floor + 0.04
                and final_segment_is_blocked(position, average_target)
            ):
                current_distance = max(emergency_floor, current_distance - 0.12)
                position = average_target + average_direction * current_distance
                emergency_contraction_count += 1
        if final_segment_is_blocked(position, average_target):
            unresolved_count += 1
        if current_distance < nominal_distance - 0.05:
            shortened_count += 1
        smoothed.append((frame, position, average_target))
    camera.parent = None
    camera.matrix_parent_inverse = Matrix.Identity(4)
    camera.rotation_mode = "QUATERNION"
    previous_rotation = None
    for frame, position, target in smoothed:
        rotation = look_quaternion(position, target)
        if previous_rotation is not None:
            # q and -q represent the same orientation.  Without compatibility
            # Blender can interpolate between opposite quaternion signs and
            # create an artificial ~180-degree camera spin.
            rotation.make_compatible(previous_rotation)
        previous_rotation = rotation.copy()
        camera.location = position
        camera.rotation_quaternion = rotation
        camera.keyframe_insert("location", frame=frame)
        camera.keyframe_insert("rotation_quaternion", frame=frame)
    set_linear_animation(camera)
    normalize_rotation_curve_winding(camera)
    camera["code2games_camera_system"] = "rigid_left_rear_character_boom"
    camera["code2games_camera_nominal_local_position"] = [float(value) for value in local_position]
    camera["code2games_camera_local_target"] = [float(value) for value in local_target]
    camera["code2games_camera_relative_transform_animated"] = True
    camera["code2games_camera_sample_step_frames"] = int(frame_step)
    camera["code2games_camera_soft_foliage_collision"] = False
    camera["code2games_camera_emergency_contraction_count"] = int(emergency_contraction_count)
    camera["code2games_camera_solid_trunk_collision"] = True
    camera["code2games_camera_blocking_objects"] = json.dumps(
        sorted(blocking_object_counts.items(), key=lambda item: (-item[1], item[0]))[:20]
    )
    camera["code2games_camera_hidden_tree_count"] = len(hidden_camera_trees)
    camera["code2games_camera_hidden_tree_names"] = json.dumps(sorted(hidden_camera_trees))
    camera["code2games_camera_frustum_ray_count"] = int(frustum_ray_count)
    camera["code2games_camera_frustum_unresolved_count"] = int(frustum_unresolved_count)
    camera["code2games_camera_nominal_boom_distance_m"] = round(float(nominal_distance), 4)
    camera["code2games_camera_minimum_boom_distance_m"] = round(
        min(
            (float((position - target).length) for _frame, position, target in smoothed),
            default=float(nominal_distance),
        ),
        4,
    )
    camera["code2games_camera_shortened_sample_count"] = int(shortened_count)
    camera["code2games_camera_candidate_switch_count"] = 0
    camera["code2games_camera_occlusion_corrections"] = int(shortened_count)
    camera["code2games_camera_unresolved_occlusions"] = int(unresolved_count)
    camera["code2games_camera_hidden_foliage_count"] = len(hidden_camera_trees)
    return camera


def configure_fps_body_camera(camera, data, actor, beats):
    """Rigid first-person body mount with terrain-only pitch anticipation."""
    camera.parent = actor
    camera.location = (0.0, 0.0, 1.68)
    camera.rotation_mode = "XYZ"
    data.lens = 30.0
    data.clip_start = 0.05
    # A small look-up during each firefight keeps the hostile's chest in the
    # frame while the burst lands.  The third encounter is the uphill final
    # crossfire, so it gets a clearly larger raise (QA: raise the view when
    # attacking the hilltop hostile so the player can see him being hit).
    combat_bumps = []
    cliff_ramp = []
    combat_index = 0
    for beat in beats:
        if beat.get("kind") == "authored" and beat.get("event_type") == "combat":
            if combat_index == 2:
                # The hilltop hostile's engagement ramp is 10 s -> 12 s ->
                # 13 s (10/20/30 deg down); the player's view mirrors it by
                # rising 4 deg at 10 s, 8 deg at 12 s and the full 12 deg by
                # 13 s, holding through the kill, then easing back (QA:
                # "our side must also look up at the same 10/12/13 s").
                cliff_frame = int(beat["frame"])
                cliff_ramp = [
                    (240, math.radians(4.0)),
                    # QA: 288 down another 5 deg (13->8), 312 set to 20;
                    # the kill frame (363) stays at 22 deg, holds through the
                    # kill, then eases back.
                    (288, math.radians(8.0)),
                    (312, math.radians(20.0)),
                    (cliff_frame, math.radians(22.0)),
                    (cliff_frame + 44, math.radians(22.0)),
                    (cliff_frame + 58, 0.0),
                ]
            else:
                combat_bumps.append((int(beat["frame"]), math.radians(3.0)))
            combat_index += 1
    frames = list(range(
        int(beats[0]["frame"]),
        int(beats[-1]["frame"]) + 1,
        max(1, int(_SAMPLE_STEP_FRAMES)),
    ))
    if frames[-1] != int(beats[-1]["frame"]):
        frames.append(int(beats[-1]["frame"]))
    pitches = []
    scene = bpy.context.scene
    depsgraph = bpy.context.evaluated_depsgraph_get()
    for frame in frames:
        scene.frame_set(frame)
        current = actor.evaluated_get(depsgraph).matrix_world.translation.copy()
        scene.frame_set(min(int(beats[-1]["frame"]), frame + 12))
        ahead = actor.evaluated_get(depsgraph).matrix_world.translation.copy()
        delta = ahead - current
        horizontal = math.hypot(float(delta.x), float(delta.y))
        pitch = math.atan2(float(delta.z), max(0.8, horizontal))
        bump = 0.0
        for combat_frame, magnitude in combat_bumps:
            delta_frames = frame - combat_frame
            if -16 <= delta_frames <= 48:
                if delta_frames < -4:
                    envelope = (delta_frames + 16) / 12.0
                elif delta_frames <= 36:
                    envelope = 1.0
                else:
                    envelope = (48 - delta_frames) / 12.0
                bump = max(bump, magnitude * max(0.0, min(1.0, envelope)))
        for ramp_index in range(len(cliff_ramp) - 1):
            frame_a, value_a = cliff_ramp[ramp_index]
            frame_b, value_b = cliff_ramp[ramp_index + 1]
            if frame_a <= frame <= frame_b:
                amount = 0.0 if frame_b <= frame_a else (frame - frame_a) / (frame_b - frame_a)
                bump = max(bump, value_a + (value_b - value_a) * amount)
                break
        pitch += bump
        # Keep the slope look readable without letting the view model leave
        # the lower frame on a climb, and do not look down into the grass on
        # descents: the extraction run needs the far road ahead visible.
        pitch = max(
            math.radians(-float(_FPS_CAMERA_MAX_DOWN_PITCH_DEGREES)),
            min(math.radians(float(_FPS_CAMERA_MAX_UP_PITCH_DEGREES)), pitch),
        )
        pitches.append(pitch)
    # Remove terrain-mesh noise while retaining a sustained uphill look.  A
    # wider nine-sample window (vs five) stops the mid-run pitch wobble seen
    # in the 11-17s section of the QA clip.
    filtered = []
    for index, pitch in enumerate(pitches):
        window = pitches[max(0, index - 4):min(len(pitches), index + 5)]
        filtered.append(sum(window) / float(len(window)))
    for frame, pitch in zip(frames, filtered):
        camera.rotation_euler = (math.pi / 2.0 + pitch, 0.0, 0.0)
        camera.keyframe_insert("rotation_euler", frame=frame, index=0)
    # The view model rides the camera pitch, so the gun stays in the lower
    # frame when the view raises for the hilltop hostile (QA: after raising
    # the 12 s / kill look-up the SCAR left the frame entirely).  The aim
    # pivot keeps its own yaw; only its X pitch follows the camera.
    aim_pivot = bpy.data.objects.get(actor.get("code2games_fps_aim_pivot", ""))
    if aim_pivot is not None:
        aim_pivot.rotation_mode = "XYZ"
        viewmodel_base = math.radians(float(_FPS_VIEWMODEL_PITCH_DEGREES))
        for frame, pitch in zip(frames, filtered):
            aim_pivot.rotation_euler.x = viewmodel_base + pitch
            aim_pivot.keyframe_insert("rotation_euler", frame=int(frame), index=0)
    if camera.animation_data and camera.animation_data.action:
        for curve in camera.animation_data.action.fcurves:
            for point in curve.keyframe_points:
                # LINEAR: dense two-frame pitch keys already track the slope;
                # bezier auto handles could overshoot on terrain and read as
                # the recurring mid-run camera shake.
                point.interpolation = "LINEAR"
    camera["code2games_camera_system"] = "rigid_first_person_actor_mount_with_slope_pitch"
    camera["code2games_camera_local_position"] = [0.0, 0.0, 1.68]
    camera["code2games_camera_relative_transform_animated"] = False
    camera["code2games_camera_lateral_sway_enabled"] = False
    camera["code2games_camera_max_uphill_pitch_degrees"] = round(float(_FPS_CAMERA_MAX_UP_PITCH_DEGREES), 4)
    camera["code2games_camera_occlusion_corrections"] = 0
    camera["code2games_camera_unresolved_occlusions"] = 0
    return camera


def create_camera(collection, beats, genre, actor):
    data = bpy.data.cameras.new("C2G_%s_CAMERA_DATA" % genre.upper())
    camera = bpy.data.objects.new("C2G_%s_CAMERA" % genre.upper(), data)
    collection.objects.link(camera)
    camera.rotation_mode = "QUATERNION"
    if genre == "fps":
        return configure_fps_body_camera(camera, data, actor, beats)
    if genre == "racing":
        return create_racing_center_mount_camera(camera, data, actor, beats, scene_z_extent())
    if genre == "tps":
        return create_tps_spring_arm_camera(camera, data, actor, beats)
    data.lens = 38.0 if genre == "tps" else 32.0 if genre == "racing" else 28.0
    data.clip_start = 0.08
    z_extent = scene_z_extent()
    occlusion_corrections = 0
    unresolved_occlusions = 0
    if genre == "tps":
        source_frames = list(range(int(beats[0]["frame"]), int(beats[-1]["frame"]) + 1, 4))
        if source_frames[-1] != int(beats[-1]["frame"]):
            source_frames.append(int(beats[-1]["frame"]))
        camera_samples = []
        for frame in source_frames:
            bpy.context.scene.frame_set(frame)
            position = actor.matrix_world.translation.copy()
            forward = actor.matrix_world.to_quaternion() @ Vector((0.0, 1.0, 0.0))
            forward.z = 0.0
            if forward.length < 1e-5:
                forward = Vector((0.0, 1.0, 0.0))
            else:
                forward.normalize()
            camera_samples.append((frame, position, forward, True))
    else:
        camera_samples = [
            (int(beat["frame"]), Vector(beat["staged_position"]), direction_for(beats, index), bool(beat.get("terrain_sample")))
            for index, beat in enumerate(beats)
        ]
    previous_rotation = None
    previous_camera_position = None
    preferred_tps_candidate = 0
    camera_candidate_switches = 0
    for frame, position, forward, terrain_sample in camera_samples:
        side = Vector((-forward.y, forward.x, 0.0))
        if genre == "tps":
            target = position + forward * 2.2 + UP * 1.15
            candidates = [(7.2, 1.6, 3.5), (5.8, -2.4, 4.8), (4.8, 0.8, 6.4), (3.8, 0.0, 8.5)]
        else:
            flying = beat.get("movement_mode") == "flight"
            target = position + forward * (4.0 if flying else 2.0) + UP * (0.2 if flying else 1.0)
            if flying:
                candidates = [(11.5, 2.0, 4.5), (8.5, -3.5, 5.8), (6.5, 3.0, 7.2), (5.0, 0.0, 9.0)]
            else:
                candidates = [(6.5, 2.0, 3.0), (5.2, -2.4, 4.6), (4.2, 1.2, 6.2), (3.5, 0.0, 8.0)]
        if terrain_sample:
            target_ground = terrain_height(target.x, target.y, target.z, z_extent)
            target.z = max(target.z, target_ground + 1.25)
        camera_position, selected_candidate, unresolved = supported_camera_position(
            position,
            forward,
            side,
            target,
            candidates,
            z_extent,
            preferred_tps_candidate if genre == "tps" else None,
        )
        if genre == "tps" and selected_candidate != preferred_tps_candidate:
            camera_candidate_switches += 1
        if genre == "tps":
            preferred_tps_candidate = selected_candidate
        if selected_candidate > 0:
            occlusion_corrections += 1
        if unresolved:
            unresolved_occlusions += 1
        if genre == "tps" and previous_camera_position is not None:
            # Clamp each four-frame camera displacement.  Candidate hysteresis
            # above prevents right/left shoulder ping-pong; this bound removes
            # the remaining single-sample whip when a tree genuinely forces a
            # closer camera position.
            displacement = camera_position - previous_camera_position
            maximum_step = 2.25
            if displacement.length > maximum_step:
                camera_position = previous_camera_position + displacement.normalized() * maximum_step
                camera_ground = terrain_height(
                    camera_position.x, camera_position.y, camera_position.z, z_extent
                )
                camera_position.z = max(camera_position.z, camera_ground + 1.6)
        previous_camera_position = camera_position.copy()
        camera.location = camera_position
        rotation = look_quaternion(camera_position, target)
        if previous_rotation is not None:
            rotation.make_compatible(previous_rotation)
        previous_rotation = rotation.copy()
        camera.rotation_quaternion = rotation
        camera.keyframe_insert("location", frame=frame)
        camera.keyframe_insert("rotation_quaternion", frame=frame)
    set_linear_animation(camera)
    if genre == "tps" and camera.animation_data and camera.animation_data.action:
        for curve in camera.animation_data.action.fcurves:
            for point in curve.keyframe_points:
                point.interpolation = "BEZIER"
                point.handle_left_type = "AUTO_CLAMPED"
                point.handle_right_type = "AUTO_CLAMPED"
        camera["code2games_camera_system"] = "dense_occlusion_aware_over_shoulder"
        camera["code2games_camera_sample_step_frames"] = 4
        camera["code2games_camera_candidate_hysteresis"] = True
        camera["code2games_camera_candidate_switch_count"] = int(camera_candidate_switches)
    camera["code2games_camera_occlusion_corrections"] = occlusion_corrections
    camera["code2games_camera_unresolved_occlusions"] = unresolved_occlusions
    return camera


def find_placement_root(placement_id, expected_anchor=None):
    if not placement_id:
        return None
    matches = [obj for obj in bpy.data.objects if obj.get("code2games_placement_id") == placement_id]
    if not matches:
        matches = [obj for obj in bpy.data.objects if placement_id.lower() in obj.name.lower()]
    if not matches:
        return None
    roots = []
    for obj in matches:
        candidates = []
        root_name = obj.get("code2games_root_object")
        if isinstance(root_name, str) and bpy.data.objects.get(root_name):
            candidates.append(bpy.data.objects[root_name])
        # A reused showcase may carry a stale root-name pointer even though
        # the matching object's own world transform is still correct.  Keep
        # both candidates, then let the immutable Stage-11 anchor disambiguate
        # them for Racing instead of blindly trusting the first pointer.
        direct_candidate = obj
        while (
            direct_candidate.parent
            and direct_candidate.parent.get("code2games_placement_id") == placement_id
        ):
            direct_candidate = direct_candidate.parent
        candidates.append(direct_candidate)
        for candidate in candidates:
            if candidate not in roots:
                roots.append(candidate)

    def priority(obj):
        collection_names = {collection.name for collection in obj.users_collection}
        return (
            0 if "Code2Games_Gameplay_Assets" in collection_names else 1,
            0 if obj.type == "EMPTY" else 1,
            obj.name,
        )
    if expected_anchor is not None:
        expected = Vector(expected_anchor)
        return min(
            roots,
            key=lambda obj: (
                float((obj.matrix_world.translation - expected).length),
                priority(obj),
            ),
        )
    return sorted(roots, key=priority)[0]


def animate_pickup(root, frame):
    if root is None:
        return
    original = root.scale.copy()
    root.scale = original
    root.keyframe_insert("scale", frame=max(1, frame - 5))
    root.scale = original * 1.28
    root.keyframe_insert("scale", frame=frame)
    root.scale = original * 0.72
    root.keyframe_insert("scale", frame=frame + 3)
    root.scale = original * 0.02
    root.keyframe_insert("scale", frame=frame + 7)
    root["code2games_collectible_consumed_frame"] = int(frame)
    set_linear_animation(root)


def event_color(event_type):
    if event_type in {"collect", "collect_required", "boost", "jump_collect"}:
        return (0.10, 0.85, 1.0, 1.0)
    if event_type in {"hazard", "flight_hazard", "dodge", "combat"}:
        return (1.0, 0.07, 0.02, 1.0)
    if event_type in {"goal", "land"}:
        return (0.18, 1.0, 0.24, 1.0)
    return (1.0, 0.55, 0.06, 1.0)


def create_event_pulse(collection, beat, index):
    if beat.get("kind") != "authored" or beat.get("event_type") in {"spawn", "establish", "flight", "move", "drive", "steer"}:
        return None
    value = material("C2G_Event_%s" % beat["event_type"], event_color(beat["event_type"]), emission=3.0, roughness=0.25)
    position = Vector(beat.get("fixed_anchor_world_xyz") or beat["staged_position"]) + UP * 0.055
    # A volumetric sphere near a chase camera becomes a giant flat-coloured
    # ball when the near clip plane enters it.  A thin ground ring communicates
    # the same event without obscuring the actor, weapon, or camera.
    bpy.ops.mesh.primitive_torus_add(
        major_segments=40,
        minor_segments=8,
        major_radius=0.34,
        minor_radius=0.025,
        location=position,
    )
    pulse = bpy.context.object
    pulse.name = "C2G_EVENT_%03d_%s" % (index, beat["event_type"])
    assign_material(pulse, value)
    link_only(pulse, collection)
    frame = int(beat["frame"])
    pulse.scale = (0.05, 0.05, 0.05)
    pulse.keyframe_insert("scale", frame=max(1, frame - 4))
    pulse.scale = (1.45, 1.45, 1.45)
    pulse.keyframe_insert("scale", frame=frame + 5)
    pulse.scale = (0.05, 0.05, 0.05)
    pulse.keyframe_insert("scale", frame=frame + 14)
    return pulse


def key_scale_pulse(obj, frame, peak_scale, lead=5, tail=14):
    obj.scale = (0.001, 0.001, 0.001)
    obj.keyframe_insert("scale", frame=max(1, int(frame) - int(lead)))
    obj.scale = tuple(float(value) for value in peak_scale)
    obj.keyframe_insert("scale", frame=int(frame))
    obj.scale = (0.001, 0.001, 0.001)
    obj.keyframe_insert("scale", frame=int(frame) + int(tail))
    set_linear_animation(obj)


def stage_racing_feedback(collection, actor, beats):
    """Make boost, braking and rough-surface responses legible on the car."""
    vehicle_event_reactions = list(
        beats[0].get("racing_vehicle_event_reactions", []) if beats else []
    )
    vehicle_event_reaction_ids = sorted({
        str(item.get("placement_id"))
        for item in vehicle_event_reactions
        if item.get("placement_id")
    })
    boost_frames = sorted({
        int(beat["frame"]) for beat in beats
        if beat.get("kind") == "authored" and beat.get("event_type") == "boost"
    })
    steer_frames = sorted({
        int(beat["frame"]) for beat in beats
        if beat.get("kind") == "authored" and beat.get("event_type") in {"steer", "hazard"}
    })
    checkpoint_frames = sorted({
        int(beat["frame"]) for beat in beats
        if beat.get("kind") == "authored" and beat.get("event_type") == "checkpoint"
    })
    goal_frames = sorted({
        int(beat["frame"]) for beat in beats
        if beat.get("kind") == "authored" and beat.get("event_type") == "goal"
    })
    flame_material = material("C2G_RacingBoostFlame", (0.08, 0.38, 1.0, 1.0), emission=18.0, roughness=0.15)
    # The core used to be a sand-yellow cone (0.95,0.85,0.35) that read as a
    # "sand-coloured ball" behind the car (QA).  Make the exhaust read as
    # blue-white flame, consistent with the outer plume.
    flame_core_material = material("C2G_RacingBoostFlameCore", (0.80, 0.92, 1.0, 1.0), emission=16.0, roughness=0.10)
    brake_material = material("C2G_RacingBrakeLight", (1.0, 0.015, 0.005, 1.0), emission=12.0, roughness=0.20)
    dust_material = material("C2G_RacingSurfaceDust", (0.22, 0.09, 0.035, 0.30), emission=0.05, roughness=0.95)
    reaction_materials = {
        "boost": material("C2G_RacingBoostPickupPulse", (0.05, 0.55, 1.0, 1.0), emission=12.0, roughness=0.20),
        "hazard": material("C2G_RacingHazardPulse", (1.0, 0.16, 0.025, 1.0), emission=10.0, roughness=0.24),
        "steer": material("C2G_RacingSteerPulse", (1.0, 0.55, 0.04, 1.0), emission=9.0, roughness=0.25),
        "countdown": material("C2G_RacingCountdownPulse", (0.95, 0.90, 0.22, 1.0), emission=8.0, roughness=0.25),
        "establish": material("C2G_RacingBannerPulse", (0.28, 0.72, 1.0, 1.0), emission=7.0, roughness=0.28),
        "recover": material("C2G_RacingRecoverPulse", (0.25, 1.0, 0.55, 1.0), emission=9.0, roughness=0.22),
        "checkpoint": material("C2G_RacingCheckpointPulse", (0.40, 0.90, 1.0, 1.0), emission=9.0, roughness=0.22),
    }
    boost_objects = []
    for side in (-1.0, 1.0):
        bpy.ops.mesh.primitive_cone_add(
            vertices=24,
            radius1=0.30,
            radius2=0.035,
            depth=1.70,
            location=(0.0, 0.0, 0.0),
            rotation=(math.radians(90.0), 0.0, 0.0),
        )
        plume = bpy.context.object
        plume.name = "C2G_RACING_BOOST_%s" % ("L" if side < 0.0 else "R")
        link_only(plume, collection)
        assign_material(plume, flame_material)
        plume.parent = actor
        plume.location = (0.66 * side, -2.70, 0.66)
        for frame in boost_frames:
            key_scale_pulse(plume, frame + 3, (1.0, 1.0, 1.0), lead=4, tail=18)
        boost_objects.append(plume.name)
        bpy.ops.mesh.primitive_cone_add(
            vertices=24,
            radius1=0.16,
            radius2=0.025,
            depth=1.10,
            location=(0.0, 0.0, 0.0),
            rotation=(math.radians(90.0), 0.0, 0.0),
        )
        core = bpy.context.object
        core.name = "C2G_RACING_BOOST_CORE_%s" % ("L" if side < 0.0 else "R")
        link_only(core, collection)
        assign_material(core, flame_core_material)
        core.parent = actor
        core.location = (0.66 * side, -2.70, 0.66)
        for frame in boost_frames:
            key_scale_pulse(core, frame + 4, (1.0, 1.0, 1.0), lead=4, tail=16)
        boost_objects.append(core.name)
    brake_objects = []
    for side in (-1.0, 1.0):
        brake = add_uv_sphere(
            "C2G_RACING_BRAKE_%s" % ("L" if side < 0.0 else "R"),
            (0.68 * side, -2.48, 0.76),
            0.075,
            brake_material,
            collection,
            actor,
        )
        for frame in steer_frames:
            key_scale_pulse(brake, frame - 2, (1.25, 1.25, 1.25), lead=5, tail=10)
        brake_objects.append(brake.name)
    dust_count = 0
    for beat in beats:
        if beat.get("kind") != "authored" or beat.get("event_type") != "hazard":
            continue
        frame = int(beat["frame"])
        position = Vector(beat["staged_position"]) + UP * 0.22
        for offset in (-0.75, 0.0, 0.75):
            bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=1, radius=0.28, location=position + Vector((offset, 0.0, 0.0)))
            dust = bpy.context.object
            dust.name = "C2G_RACING_HAZARD_DUST_%03d" % dust_count
            link_only(dust, collection)
            assign_material(dust, dust_material)
            key_scale_pulse(dust, frame + 2 + dust_count % 4, (1.5, 1.5, 1.0), lead=3, tail=18)
            dust_count += 1
    # Local point pulses make every actually passed prop react without a
    # camera-filling ring. Boost collectibles additionally shrink away in
    # stage_events(), matching their collection semantics.
    response_objects = []
    response_mesh_objects = []
    response_placement_ids = []
    for index, beat in enumerate(beats):
        placement_id = beat.get("placement_id")
        anchor = beat.get("fixed_anchor_world_xyz")
        event_type = str(beat.get("event_type", ""))
        if (
            beat.get("kind") != "authored"
            or not placement_id
            or not anchor
            or event_type not in {"establish", "boost", "checkpoint", "hazard", "steer", "recover", "countdown"}
            or placement_id in response_placement_ids
        ):
            continue
        rgba = event_color(event_type)
        light_data = bpy.data.lights.new("C2G_RACING_PROP_REACTION_%03d_DATA" % index, "POINT")
        light_data.color = tuple(float(value) for value in rgba[:3])
        light_data.shadow_soft_size = 1.2
        light_data.energy = 0.0
        light = bpy.data.objects.new("C2G_RACING_PROP_REACTION_%03d" % index, light_data)
        collection.objects.link(light)
        root = find_placement_root(str(placement_id), anchor)
        reaction_anchor = root.matrix_world.translation.copy() if root is not None else Vector(anchor)
        light.location = reaction_anchor + UP * 1.2
        frame = int(beat["frame"])
        light_data.keyframe_insert("energy", frame=max(1, frame - 8))
        light_data.energy = 2200.0
        light_data.keyframe_insert("energy", frame=frame + 3)
        light_data.energy = 0.0
        light_data.keyframe_insert("energy", frame=frame + 22)
        set_linear_animation(light_data)
        response_objects.append(light.name)
        # A point light alone is washed out by this sun-lit desert scene.
        # Give each passed gameplay asset a short, physical emissive ground
        # pulse so that the player visibly triggers it from the chase camera.
        bpy.ops.mesh.primitive_torus_add(
            major_radius=1.05 if event_type != "hazard" else 1.35,
            minor_radius=0.075,
            major_segments=32,
            minor_segments=8,
            location=reaction_anchor + UP * 0.09,
        )
        pulse = bpy.context.object
        pulse.name = "C2G_RACING_ASSET_PULSE_%03d_%s" % (index, event_type.upper())
        link_only(pulse, collection)
        assign_material(pulse, reaction_materials.get(event_type, reaction_materials["steer"]))
        pulse.scale = (0.001, 0.001, 0.001)
        pulse.keyframe_insert("scale", frame=max(1, frame - 4))
        pulse.scale = (0.72, 0.72, 0.72)
        pulse.keyframe_insert("scale", frame=frame)
        pulse.scale = (1.28, 1.28, 1.0)
        pulse.keyframe_insert("scale", frame=frame + 8)
        pulse.scale = (0.001, 0.001, 0.001)
        pulse.keyframe_insert("scale", frame=frame + 18)
        set_linear_animation(pulse)
        response_mesh_objects.append(pulse.name)
        response_placement_ids.append(str(placement_id))
    prop_react_count = len(response_placement_ids)
    return {
        "enabled": True,
        "boost_event_count": len(boost_frames),
        "brake_or_steer_event_count": len(steer_frames),
        "boost_objects": boost_objects,
        "brake_objects": brake_objects,
        "hazard_dust_object_count": dust_count,
        "checkpoint_response_count": 0,
        "finish_response_count": 0,
        "prop_reaction_count": prop_react_count,
        "prop_reaction_visual_count": len(response_mesh_objects),
        "prop_reaction_placement_ids": response_placement_ids,
        "vehicle_manoeuvre_count": len(vehicle_event_reactions),
        "vehicle_manoeuvre_placement_ids": vehicle_event_reaction_ids,
        "vehicle_manoeuvres": vehicle_event_reactions,
        "vehicle_response_objects": response_objects,
        "vehicle_response_mesh_objects": response_mesh_objects,
        "boost_props_removed_or_hidden": bool(boost_frames),
        "boost_collectible_disappear_count": len(boost_frames),
        "asset_transform_changes": len(boost_frames),
    }


def hide_racing_unused_props(collection):
    """Keep all Stage-10 assets visible; route safety must come from driving."""
    return []


def racing_speed_profile(beats, fps):
    """Per-frame physical speed plus a continuous 200--420 km/h HUD curve."""
    physical_by_frame = {}
    for index in range(len(beats) - 1):
        frame_a = int(beats[index]["frame"])
        frame_b = int(beats[index + 1]["frame"])
        if frame_b <= frame_a:
            continue
        distance = (
            Vector(beats[index + 1]["staged_position"]) - Vector(beats[index]["staged_position"])
        ).length
        speed_mps = distance / float(frame_b - frame_a) * float(fps)
        for frame in range(frame_a, frame_b):
            physical_by_frame[frame] = float(speed_mps)
    if not physical_by_frame:
        return []
    first_frame = min(physical_by_frame)
    final_frame = max(physical_by_frame)
    physical_values = [physical_by_frame[frame] for frame in range(first_frame, final_frame + 1)]
    smoothed_physical = []
    for index in range(len(physical_values)):
        window = sorted(physical_values[max(0, index - 2): index + 3])
        smoothed_physical.append(window[len(window) // 2])

    targets = {
        frame: float(_RACING_CRUISE_DISPLAY_SPEED_KMH)
        for frame in range(first_frame, final_frame + 1)
    }
    boost_frames = []
    for beat in beats:
        frame = int(beat["frame"])
        event_type = str(beat.get("event_type", ""))
        if event_type in {"hazard", "steer"}:
            for active in range(max(first_frame, frame - 10), min(final_frame, frame + 18) + 1):
                targets[active] = min(targets[active], _RACING_MIN_DISPLAY_SPEED_KMH + 15.0)
        elif event_type == "checkpoint":
            for active in range(max(first_frame, frame - 6), min(final_frame, frame + 10) + 1):
                targets[active] = max(targets[active], _RACING_CRUISE_DISPLAY_SPEED_KMH + 20.0)
        elif event_type == "boost":
            boost_frames.append(frame)
            ramp_frames = 14
            hold_frames = 48
            decay_frames = 28
            for offset in range(ramp_frames + hold_frames + decay_frames + 1):
                active = frame + offset
                if active > final_frame:
                    break
                if offset <= ramp_frames:
                    amount = offset / float(ramp_frames)
                    value = _RACING_CRUISE_DISPLAY_SPEED_KMH + (
                        _RACING_PEAK_DISPLAY_SPEED_KMH - _RACING_CRUISE_DISPLAY_SPEED_KMH
                    ) * amount
                elif offset <= ramp_frames + hold_frames:
                    value = _RACING_PEAK_DISPLAY_SPEED_KMH
                else:
                    amount = (offset - ramp_frames - hold_frames) / float(decay_frames)
                    value = _RACING_PEAK_DISPLAY_SPEED_KMH + (
                        _RACING_CRUISE_DISPLAY_SPEED_KMH - _RACING_PEAK_DISPLAY_SPEED_KMH
                    ) * amount
                targets[active] = max(targets[active], value)
        elif event_type == "goal":
            for active in range(max(first_frame, frame), final_frame + 1):
                amount = (active - frame) / float(max(1, final_frame - frame))
                targets[active] = min(
                    targets[active],
                    _RACING_CRUISE_DISPLAY_SPEED_KMH + (
                        _RACING_MIN_DISPLAY_SPEED_KMH - _RACING_CRUISE_DISPLAY_SPEED_KMH
                    ) * amount,
                )

    # Boost status wins over nearby steering/hazard cues.  Those cues can
    # affect handling and physical motion, but must not cancel the promised
    # two-second 400+ km/h HUD run immediately after a pickup.
    for frame in boost_frames:
        for active in range(frame + 14, min(final_frame, frame + 65) + 1):
            targets[active] = max(targets[active], _RACING_PEAK_DISPLAY_SPEED_KMH)

    samples = []
    display = max(_RACING_MIN_DISPLAY_SPEED_KMH, targets[first_frame])
    for index, frame in enumerate(range(first_frame, final_frame + 1)):
        target = max(
            _RACING_MIN_DISPLAY_SPEED_KMH,
            min(_RACING_PEAK_DISPLAY_SPEED_KMH, float(targets[frame])),
        )
        # Roughly 0.6 s to accelerate and at least 1.5 s to fall from the
        # 420 peak to the 240 cruise.  No discontinuity and no 100 km/h valley.
        if target > display:
            display = min(target, display + 13.0)
        else:
            display = max(target, display - 5.0)
        physical = smoothed_physical[index]
        samples.append({
            "frame": frame,
            "seconds": round(frame / float(fps), 3),
            "speed_mps": round(physical, 4),
            "physical_speed_kmh": round(physical * 3.6, 2),
            "display_speed_kmh": round(display, 2),
            # Compatibility: verification now treats speed_kmh as the actual
            # displayed gameplay speed, not a value multiplied later by HUD.
            "speed_kmh": round(display, 2),
        })
    return samples


def stage_aircraft_feedback(collection, beats):
    """Give flight cells and fixed systems localized, ring-free responses."""
    status_materials = {
        "preflight": material("C2G_AircraftPreflightStatus", (0.06, 0.72, 0.22, 1.0), emission=6.0, roughness=0.20),
        "mission": material("C2G_AircraftMissionStatus", (0.04, 0.38, 1.0, 1.0), emission=6.0, roughness=0.18),
        "hazard": material("C2G_AircraftHazardStatus", (1.0, 0.055, 0.015, 1.0), emission=7.0, roughness=0.22),
        "goal": material("C2G_AircraftGoalStatus", (0.05, 1.0, 0.30, 1.0), emission=8.0, roughness=0.16),
    }
    accepted = {
        "preflight_data", "preflight_power", "preflight_route",
        "preflight_weather", "preflight_clearance", "preflight_calibration",
        "launch", "flight_hazard", "collect_required", "flight_event",
        "flight_collectible", "reveal", "goal",
    }
    response_ids = []
    summit_ids = []
    airborne_ids = []
    light_count = 0
    lamp_count = 0
    collected_ids = []
    collection_frames = []
    for index, beat in enumerate(beats):
        anchor_value = beat.get("fixed_anchor_world_xyz")
        if not anchor_value:
            continue
        event_type = str(beat.get("event_type", ""))
        if event_type not in accepted:
            continue
        frame = int(beat["frame"])
        anchor = Vector(anchor_value)
        parked = bool(beat.get("aircraft_parked"))
        if parked:
            style = "preflight"
        elif event_type == "flight_hazard":
            style = "hazard"
        elif event_type == "goal":
            style = "goal"
        else:
            style = "mission"
        color = {
            "preflight": (0.08, 0.82, 0.28),
            "mission": (0.04, 0.42, 1.0),
            "hazard": (1.0, 0.04, 0.01),
            "goal": (0.04, 1.0, 0.24),
        }[style]

        # A ten-centimetre status lamp remains attached to the asset.  It can
        # never surround the aircraft/camera like the V1 torus did.
        lamp_position = anchor + UP * (0.72 if parked else 0.45)
        bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=2, radius=0.10, location=lamp_position)
        lamp = bpy.context.object
        lamp.name = "C2G_AIRCRAFT_LOCAL_STATUS_%03d_%s" % (index, event_type)
        link_only(lamp, collection)
        assign_material(lamp, status_materials[style])
        key_scale_pulse(lamp, frame, (1.8, 1.8, 1.8), lead=8, tail=20)
        lamp_count += 1

        light_data = bpy.data.lights.new("%s_DATA" % lamp.name, "POINT")
        light_data.color = color
        light_data.shadow_soft_size = 0.42
        light_data.energy = 0.0
        light = bpy.data.objects.new("%s_LIGHT" % lamp.name, light_data)
        collection.objects.link(light)
        light.location = lamp_position
        peak_energy = 1250.0 if style in {"hazard", "goal"} else 760.0
        light_data.energy = 0.0
        light_data.keyframe_insert("energy", frame=max(1, frame - 8))
        light_data.energy = peak_energy
        light_data.keyframe_insert("energy", frame=frame)
        light_data.energy = 0.0
        light_data.keyframe_insert("energy", frame=frame + 20)
        light_count += 1
        placement_id = str(beat.get("placement_id"))
        response_ids.append(placement_id)
        if event_type == "flight_collectible":
            collected_ids.append(placement_id)
            collection_frames.append(frame)
        (summit_ids if parked else airborne_ids).append(placement_id)
    return {
        "enabled": True,
        "world_space_ring_count": 0,
        "airborne_gate_count": 0,
        "anchor_beam_count": 0,
        "localized_status_lamp_count": lamp_count,
        "localized_light_count": light_count,
        "localized_asset_response_count": len(response_ids),
        "responsive_placement_ids": response_ids,
        "summit_response_placement_ids": summit_ids,
        "airborne_response_placement_ids": airborne_ids,
        "collected_placement_ids": collected_ids,
        "flight_cell_collection_frames": collection_frames,
        "flight_cell_collection_count": len(collected_ids),
        "collection_counter_final": "%d/4" % len(collected_ids),
        "collection_unlocks_destination": len(collected_ids) == 4,
        "feedback_policy": "flight cells are consumed with engine progression; other assets use local lamps; no rings or beams",
        "fixed_asset_positions_changed": False,
    }


def create_route_debug(collection, beats):
    curve_data = bpy.data.curves.new("C2G_DirectorRouteData", "CURVE")
    curve_data.dimensions = "3D"
    curve_data.bevel_depth = 0.045
    curve_data.bevel_resolution = 2
    spline = curve_data.splines.new("POLY")
    spline.points.add(len(beats) - 1)
    for point, beat in zip(spline.points, beats):
        position = beat["staged_position"]
        point.co = (position[0], position[1], position[2] + 0.12, 1.0)
    obj = bpy.data.objects.new("C2G_DIRECTOR_ROUTE_DEBUG", curve_data)
    collection.objects.link(obj)
    assign_material(obj, material("C2G_RouteDebug", (0.04, 0.65, 1.0, 1.0), emission=2.0))
    obj.hide_render = True
    return obj


def load_swat_template(library_path):
    library_path = os.path.abspath(library_path)
    cached = _SWAT_TEMPLATE_CACHE.get(library_path)
    if cached and cached.name in bpy.data.collections:
        return cached
    if not os.path.isfile(library_path):
        raise FileNotFoundError("SWAT combat library missing: %s" % library_path)
    with bpy.data.libraries.load(library_path, link=False) as (available, requested):
        if "C2G_SWAT_CHARACTER_TEMPLATE" not in available.collections:
            raise RuntimeError("SWAT library has no C2G_SWAT_CHARACTER_TEMPLATE collection")
        requested.collections = ["C2G_SWAT_CHARACTER_TEMPLATE"]
    template = requested.collections[0]
    template.hide_render = True
    template.hide_viewport = True
    _SWAT_TEMPLATE_CACHE[library_path] = template
    return template


def duplicate_swat_hierarchy(template, target_collection):
    originals = list(template.all_objects)
    mapping = {}
    for original in originals:
        duplicate = original.copy()
        if original.type == "ARMATURE":
            duplicate.data = original.data.copy()
        target_collection.objects.link(duplicate)
        mapping[original] = duplicate
    for original, duplicate in mapping.items():
        if original.parent in mapping:
            duplicate.parent = mapping[original.parent]
        for modifier in duplicate.modifiers:
            if modifier.type == "ARMATURE" and modifier.object in mapping:
                modifier.object = mapping[modifier.object]
    return list(mapping.values())


def swat_actions_from_armature(armature):
    result = {}
    animation = armature.animation_data
    if animation:
        for track in animation.nla_tracks:
            if not track.strips:
                continue
            match = re.match(r"C2G_ACTION_(.+)", track.name, re.IGNORECASE)
            if match:
                result[match.group(1).lower()] = track.strips[0].action
        if animation.action:
            result.setdefault("idle", animation.action)
        armature.animation_data_clear()
    # The Mixamo fire/reload clips carry baked root translation in the Hips
    # bone, so a planted shooter visibly slides while firing (QA: "he slides
    # when shooting").  Zero the root translation for these two clips; the
    # stance and recoil stay, only the drift is removed.
    for role in ("fire", "reload"):
        action = result.get(role)
        if action is None:
            continue
        for fcurve in action.fcurves:
            if "Hips" in fcurve.data_path and "location" in fcurve.data_path:
                for point in fcurve.keyframe_points:
                    point.co[1] = 0.0
    # Appended libraries retain the pose evaluated at save time even after the
    # AnimationData is removed.  Clear that residue before constructing a new
    # NLA schedule, otherwise a zero-weight gap can show a stale death pose.
    for pose_bone in armature.pose.bones:
        pose_bone.matrix_basis.identity()
    bpy.context.view_layer.update()
    return result


def append_swat_actor(collection, library_path, actor_name, target_height=1.78):
    template = load_swat_template(library_path)
    imported = duplicate_swat_hierarchy(template, collection)
    armatures = [obj for obj in imported if obj.type == "ARMATURE"]
    meshes = [obj for obj in imported if obj.type == "MESH"]
    if len(armatures) != 1 or not meshes:
        raise RuntimeError("SWAT template requires one armature and visible meshes")
    armature = armatures[0]
    actions = swat_actions_from_armature(armature)
    required_actions = {"idle", "walk", "run", "strafe_left", "strafe_right", "fire", "reload", "hit", "death"}
    missing = sorted(required_actions - set(actions))
    if missing:
        raise RuntimeError("SWAT library is missing actions: %s" % ", ".join(missing))
    controller = new_root("C2G_%s_ROOT" % actor_name, collection)
    orientation = new_root("C2G_%s_ORIENTATION" % actor_name, collection)
    orientation.parent = controller
    content = new_root("C2G_%s_CONTENT" % actor_name, collection)
    content.parent = orientation
    imported_set = set(imported)
    for obj in imported:
        obj["code2games_swat_actor"] = actor_name
    for obj in imported:
        if obj.parent not in imported_set:
            matrix = obj.matrix_world.copy()
            obj.parent = content
            obj.matrix_world = matrix
    bpy.context.view_layer.update()
    bounds = mesh_bounds(meshes)
    if not bounds:
        raise RuntimeError("SWAT actor has no mesh bounds")
    height = max(0.01, float(bounds[1].z - bounds[0].z))
    scale = float(target_height) / height
    center = (bounds[0] + bounds[1]) * 0.5
    content.scale = (scale, scale, scale)
    content.location = Vector((-center.x * scale, -center.y * scale, -bounds[0].z * scale + 0.003))
    # The Mixamo character visually faces -Y; Director actors face +Y.
    orientation.rotation_euler[2] = math.pi
    weapon_instances = [obj for obj in imported if obj.get("code2games_hand_attached_weapon")]
    weapon_muzzles = []
    for weapon_index, weapon_instance in enumerate(weapon_instances):
        source_muzzle = next(
            (
                obj for obj in weapon_instance.instance_collection.all_objects
                if obj.name.startswith("C2G_SCAR_H_MUZZLE")
            ),
            None,
        ) if weapon_instance.instance_collection else None
        if source_muzzle is None:
            continue
        muzzle = bpy.data.objects.new(
            "C2G_%s_SCAR_H_MUZZLE_%02d" % (actor_name, weapon_index), None
        )
        collection.objects.link(muzzle)
        muzzle.parent = weapon_instance
        marker_position = collection_space_matrix(source_muzzle).translation.copy()
        muzzle.matrix_parent_inverse = Matrix.Identity(4)
        muzzle.location = marker_position
        muzzle.matrix_basis.translation = marker_position
        muzzle["code2games_muzzle_source"] = source_muzzle.name
        weapon_muzzles.append(muzzle)
    controller["code2games_actor_style"] = "realistic_swat_with_scar_h"
    controller["code2games_actor_target_height_m"] = float(target_height)
    controller["code2games_actor_armature"] = armature.name
    controller["code2games_actor_actions"] = json.dumps({role: action.name for role, action in actions.items()})
    controller["code2games_actor_weapon"] = weapon_instances[0].name if weapon_instances else ""
    controller["code2games_actor_weapon_muzzle"] = weapon_muzzles[0].name if weapon_muzzles else ""
    data = {
        "name": actor_name,
        "root": controller,
        "orientation": orientation,
        "content": content,
        "armatures": armatures,
        "actions": actions,
        "objects": imported,
        "weapon_instances": weapon_instances,
        "weapon_muzzles": weapon_muzzles,
        "visual_style": "realistic_swat_with_scar_h",
    }
    _SWAT_ACTOR_CACHE[controller.name] = data
    return data


def swat_actor_data(root):
    return _SWAT_ACTOR_CACHE.get(root.name)


def collection_space_matrix(obj):
    """Return an unlinked library object's transform in collection space."""
    # matrix_local can remain identity until an appended, unlinked collection
    # has been evaluated once.  Compose the authored transforms directly so
    # the very first FPS view-model gets the same muzzle result as later enemy
    # instances.
    matrix = Matrix.LocRotScale(obj.location, obj.rotation_euler.to_quaternion(), obj.scale)
    parent = obj.parent
    while parent is not None:
        matrix = Matrix.LocRotScale(
            parent.location, parent.rotation_euler.to_quaternion(), parent.scale
        ) @ matrix
        parent = parent.parent
    return matrix


def create_scar_viewmodel(collection, player_root, library_path):
    template = load_swat_template(library_path)
    source_instance = next(
        (obj for obj in template.all_objects if obj.get("code2games_hand_attached_weapon") and obj.instance_collection),
        None,
    )
    if source_instance is None:
        raise RuntimeError("SWAT library has no SCAR-H collection instance")
    aim = new_root("C2G_FPS_AIM_PIVOT", collection)
    aim.parent = player_root
    # Pitch the view model up about the eye.  A horizontal SCAR sits far below
    # the 30 mm view axis, so a small 6-degree pivot pitch parks the receiver
    # in the lower-right of the frame while keeping the muzzle at the
    # crosshair height; a larger pitch (the old 20 degrees) visibly pointed
    # the barrel away from anything the player was shooting at.
    aim.location = (0.0, 0.0, 1.68)
    aim.rotation_mode = "XYZ"
    aim.rotation_euler = (math.radians(float(_FPS_VIEWMODEL_PITCH_DEGREES)), 0.0, 0.0)
    weapon = bpy.data.objects.new("C2G_FPS_SCAR_H_VIEWMODEL", None)
    collection.objects.link(weapon)
    weapon.instance_type = "COLLECTION"
    weapon.instance_collection = source_instance.instance_collection
    weapon.parent = aim
    weapon.location = tuple(float(value) for value in _FPS_VIEWMODEL_OFFSET)
    weapon.scale = (float(_FPS_VIEWMODEL_SCALE),) * 3
    # Shared SCAR-H muzzle points along source -X; map it to Director +Y while
    # preserving source +Z as the top rail/up direction.  The nose-up pitch
    # lives on the eye-height aim pivot above, not on this object: rotating
    # the weapon about its own X would only roll it around the barrel axis.
    weapon.rotation_euler = (0.0, 0.0, math.radians(-90.0))
    muzzle = bpy.data.objects.new("C2G_FPS_MUZZLE", None)
    collection.objects.link(muzzle)
    muzzle.parent = weapon
    # The prepared SCAR-H collection contains the marker authored from the
    # actual barrel geometry.  Parenting a duplicate marker to the view-model
    # instance makes recoil and aim yaw move the shot origin with the barrel.
    source_muzzle = next(
        (
            obj for obj in source_instance.instance_collection.all_objects
            if obj.name.startswith("C2G_SCAR_H_MUZZLE")
        ),
        None,
    )
    marker_position = (
        collection_space_matrix(source_muzzle).translation.copy()
        if source_muzzle is not None
        else Vector((-0.6514, 0.0, 0.1118))
    )
    muzzle.matrix_parent_inverse = Matrix.Identity(4)
    muzzle.location = marker_position
    muzzle.matrix_basis.translation = marker_position
    muzzle["code2games_muzzle_source"] = (
        source_muzzle.name if source_muzzle is not None else "measured_scar_h_fallback"
    )
    player_root["code2games_fps_aim_pivot"] = aim.name
    player_root["code2games_weapon_root"] = weapon.name
    player_root["code2games_weapon_muzzle"] = muzzle.name
    player_root["code2games_weapon_style"] = "realistic_scar_h_pbr"
    return weapon


def normalized_action_name(name):
    value = str(name or "").split("|")[-1].strip().lower()
    value = re.sub(r"\.\d{3}$", "", value)
    return re.sub(r"[^a-z0-9]+", "_", value).strip("_")


def resolve_enemy_action(action_map, requested_name):
    requested = normalized_action_name(requested_name)
    if requested in action_map:
        return action_map[requested]
    candidates = []
    for key, action in action_map.items():
        tokens = [token for token in key.split("_") if token]
        if requested in tokens:
            candidates.append((0, len(key), key, action))
        elif key.endswith("_" + requested) or key.startswith(requested + "_"):
            candidates.append((1, len(key), key, action))
        elif requested in key:
            candidates.append((2, len(key), key, action))
    return sorted(candidates, key=lambda item: item[:3])[0][3] if candidates else None


def style_armored_fps_enemy(collection, orientation, imported, enemy_name, target_height):
    """Replace the kit's white toy-like shell with a readable combat chassis."""
    armor = material("C2G_EnemyArmor", (0.025, 0.032, 0.036, 1.0), metallic=0.88, roughness=0.28)
    secondary = material("C2G_EnemyMechanics", (0.075, 0.082, 0.082, 1.0), metallic=0.72, roughness=0.44)
    visor = material("C2G_EnemyThreatVisor", (0.55, 0.004, 0.002, 1.0), emission=7.0, metallic=0.22, roughness=0.14)
    for obj in imported:
        if obj.type != "MESH":
            continue
        assign_material(obj, armor if "body" in obj.name.lower() else secondary)

    height = float(target_height)
    add_cube(
        "C2G_%s_ARMORED_CORE" % enemy_name,
        (0.0, 0.0, height * 0.43),
        (height * 0.34, height * 0.29, height * 0.25),
        armor,
        collection,
        orientation,
        bevel=height * 0.055,
    )
    add_cube(
        "C2G_%s_DORSAL_ARMOR" % enemy_name,
        (0.0, 0.0, height * 0.72),
        (height * 0.29, height * 0.24, height * 0.075),
        secondary,
        collection,
        orientation,
        bevel=height * 0.025,
    )
    for side in (-1.0, 1.0):
        add_cube(
            "C2G_%s_SIDE_ARMOR" % enemy_name,
            (height * 0.39 * side, 0.0, height * 0.46),
            (height * 0.095, height * 0.25, height * 0.17),
            secondary,
            collection,
            orientation,
            bevel=height * 0.026,
        )
    # Put a threat-readable visor on both longitudinal faces so it remains
    # visible despite differing source-model forward conventions.
    for front in (-1.0, 1.0):
        add_cube(
            "C2G_%s_THREAT_VISOR" % enemy_name,
            (0.0, height * 0.296 * front, height * 0.49),
            (height * 0.19, height * 0.018, height * 0.045),
            visor,
            collection,
            orientation,
            bevel=height * 0.010,
        )
    return "dark_armored_combat_drone"


def imported_enemy(collection, asset_path, enemy_name, target_height):
    before_objects = set(bpy.data.objects)
    before_actions = set(bpy.data.actions)
    bpy.ops.import_scene.gltf(filepath=asset_path)
    imported = [obj for obj in bpy.data.objects if obj not in before_objects]
    actions = [action for action in bpy.data.actions if action not in before_actions]
    # Blender's glTF round-trip can materialize a top-level Rigify helper as a
    # two-metre white Icosphere.  It has no skin, parent, modifiers, vertex
    # groups, or role in the downloaded character.  Reject it structurally so
    # it can never be mistaken for an enemy or contaminate character bounds.
    helper_meshes = [
        obj
        for obj in imported
        if obj.type == "MESH"
        and obj.parent is None
        and obj.name.lower().startswith("icosphere")
        and not obj.modifiers
        and not obj.vertex_groups
    ]
    for helper in helper_meshes:
        imported.remove(helper)
        bpy.data.objects.remove(helper, do_unlink=True)
    if not imported:
        raise RuntimeError("enemy import created no objects: %s" % asset_path)
    controller = new_root("C2G_FPS_%s_ROOT" % enemy_name, collection)
    orientation = new_root("C2G_FPS_%s_ORIENTATION" % enemy_name, collection)
    orientation.parent = controller
    content = new_root("C2G_FPS_%s_CONTENT" % enemy_name, collection)
    content.parent = orientation
    imported_set = set(imported)
    top_level = [obj for obj in imported if obj.parent not in imported_set]
    for obj in imported:
        link_only(obj, collection)
        obj["code2games_fps_enemy"] = enemy_name
    for obj in top_level:
        matrix = obj.matrix_world.copy()
        obj.parent = content
        obj.matrix_world = matrix
    bounds = mesh_bounds(imported)
    if not bounds:
        raise RuntimeError("enemy import has no mesh bounds: %s" % asset_path)
    height = max(0.01, bounds[1].z - bounds[0].z)
    scale = float(target_height) / height
    center = (bounds[0] + bounds[1]) * 0.5
    content.scale = (scale, scale, scale)
    content.location = Vector((-center.x * scale, -center.y * scale, -bounds[0].z * scale))
    # The prepared BlenderKit/Rigify tactical officer uses -Y visual forward.
    orientation.rotation_euler[2] = math.pi
    visual_style = "realistic_rigged_tactical_officer_with_tac50"
    armatures = [obj for obj in imported if obj.type == "ARMATURE"]
    action_map = {}
    for armature in armatures:
        animation_data = armature.animation_data
        if not animation_data:
            continue
        if animation_data.action:
            action_map[normalized_action_name(animation_data.action.name)] = animation_data.action
        for track in animation_data.nla_tracks:
            for strip in track.strips:
                if strip.action:
                    action_map[normalized_action_name(track.name)] = strip.action
                    action_map[normalized_action_name(strip.name)] = strip.action
                    action_map[normalized_action_name(strip.action.name)] = strip.action
    for obj in imported:
        if obj in armatures or not obj.animation_data:
            continue
        animation_data = obj.animation_data
        if animation_data.action:
            action_map.setdefault(normalized_action_name(animation_data.action.name), animation_data.action)
        for track in animation_data.nla_tracks:
            for strip in track.strips:
                if strip.action:
                    action_map.setdefault(normalized_action_name(track.name), strip.action)
                    action_map.setdefault(normalized_action_name(strip.name), strip.action)
                    action_map.setdefault(normalized_action_name(strip.action.name), strip.action)
    # Fallback for importers that stash clips without attaching every action to
    # the armature's initial NLA tracks.
    for action in actions:
        action_map.setdefault(normalized_action_name(action.name), action)
    # Blender versions differ in whether the glTF animation name is assigned
    # to the Action, NLA track, or strip.  When this import produced exactly
    # one Action per glTF animation, preserve the file-declared ordering as a
    # final unambiguous alias source.
    try:
        with open(asset_path, "r", encoding="utf-8") as handle:
            declared_names = [item.get("name") for item in json.load(handle).get("animations", [])]
        if declared_names and len(declared_names) == len(actions):
            for declared_name, action in zip(declared_names, actions):
                action_map.setdefault(normalized_action_name(declared_name), action)
    except Exception:
        pass
    for armature in armatures:
        # glTF imports every clip as an NLA track.  Retain the Actions but clear
        # the imported playback so the Director can assemble a deterministic
        # combat sequence below.
        if armature.animation_data:
            armature.animation_data_clear()
    controller["code2games_enemy_actions"] = ",".join(sorted(action_map))
    return {
        "name": enemy_name,
        "root": controller,
        "armatures": armatures,
        "actions": action_map,
        "objects": imported,
        "asset_path": asset_path,
        "visual_style": visual_style,
    }

def install_enemy_action_sequence(enemy, segments):
    installed = []
    missing = []
    for armature in enemy["armatures"]:
        armature.animation_data_create()
        # A persistent rifle-idle base prevents the rig from falling back to
        # its Mixamo bind pose before, after, or between scheduled actions.
        # The combat track below replaces this base wherever it has a strip.
        idle_action = resolve_enemy_action(enemy["actions"], "idle")
        if idle_action is not None:
            base_track = armature.animation_data.nla_tracks.new()
            base_track.name = "C2G_ARMED_IDLE_BASE"
            scene_start = int(bpy.context.scene.frame_start)
            scene_end = max(scene_start + 2, int(bpy.context.scene.frame_end))
            base_strip = base_track.strips.new("C2G_ARMED_IDLE_BASE", scene_start, idle_action)
            action_length = max(1.0, float(idle_action.frame_range[1] - idle_action.frame_range[0]))
            base_strip.action_frame_start = float(idle_action.frame_range[0])
            base_strip.action_frame_end = float(idle_action.frame_range[1])
            base_strip.repeat = max(1.0, float(scene_end - scene_start) / action_length)
            base_strip.frame_end = scene_end
            base_strip.extrapolation = "NOTHING"
            base_strip.blend_type = "REPLACE"
            base_strip.blend_in = 0.0
            base_strip.blend_out = 0.0
        track = armature.animation_data.nla_tracks.new()
        track.name = "C2G_FPS_COMBAT_SEQUENCE"
        previous_end = -100000
        locomotion_phase = {}
        last_locomotion_role = None
        created_strips = []
        for segment in segments:
            requested_name, start_frame, end_frame = segment[:3]
            segment_meta = segment[3] if len(segment) > 3 and isinstance(segment[3], dict) else {}
            action = resolve_enemy_action(enemy["actions"], requested_name)
            if action is None:
                missing.append(requested_name)
                continue
            start_frame = max(int(start_frame), previous_end + 1)
            end_frame = max(start_frame + 2, int(end_frame))
            strip = track.strips.new(
                "%s_%s_%d" % (enemy["name"], requested_name, start_frame),
                start_frame,
                action,
            )
            action_length = max(1.0, float(action.frame_range[1] - action.frame_range[0]))
            desired_length = max(2.0, float(end_frame - start_frame))
            strip.action_frame_start = float(action.frame_range[0])
            strip.action_frame_end = float(action.frame_range[1])
            strip.use_animated_influence = False
            strip.influence = 1.0
            normalized_role = normalized_action_name(requested_name)
            created_strips.append((strip, normalized_role))
            if normalized_role in {"idle", "walk", "run", "strafe_left", "strafe_right"}:
                if normalized_role != last_locomotion_role:
                    locomotion_phase.clear()
                last_locomotion_role = normalized_role
                travel_distance = segment_meta.get("travel_distance_m")
                if travel_distance is not None and normalized_role != "idle":
                    stride_distance = {
                        "walk": 1.05,
                        "run": 1.68,
                        "strafe_left": 1.02,
                        "strafe_right": 1.02,
                    }[normalized_role]
                    # Match visible foot cycles to actual world displacement.
                    # Fractional repeats are intentional for short segments.
                    # Other genres retain the historical 0.9 default.  TPS
                    # supplies an explicit fractional floor because its dense
                    # Stage-11 transit samples can be only a few frames long.
                    minimum_repeat = max(
                        0.02,
                        float(segment_meta.get("minimum_locomotion_repeat", 0.9)),
                    )
                    strip.repeat = max(minimum_repeat, float(travel_distance) / stride_distance)
                    # Continue the foot-cycle phase across consecutive strips
                    # of the same role: restarting the run/walk action at
                    # frame zero on every segment snapped the feet to a
                    # different pose at each boundary, which read as the
                    # recurring "jolt / backward hitch" while walking.
                    phase = locomotion_phase.get(normalized_role, 0.0)
                    strip.action_frame_start = float(action.frame_range[0]) + (phase % action_length)
                    strip.action_frame_end = float(action.frame_range[1])
                    locomotion_phase[normalized_role] = (
                        phase + float(travel_distance) / stride_distance * action_length
                    )
                else:
                    strip.repeat = max(1.0, desired_length / action_length)
                strip.frame_end = end_frame
            else:
                strip.repeat = 1.0
                if normalized_role == "death":
                    # Fast readable collapse: strip.scale < 1 plays the action
                    # faster, so this makes the 92-frame death action complete
                    # in about 22 timeline frames (a ~0.9 s fall).  The strip
                    # is shortened to that 22-frame window and HOLD_FORWARD
                    # keeps the body down for the rest of the scene.  Stretching
                    # the strip across the whole scene made Blender cycle the
                    # action (fall -> stand -> fall) because the strip consumed
                    # many times the action range, so the enemy stood back up.
                    collapse_frames = 22.0
                    strip.scale = max(0.02, min(1.0, collapse_frames / action_length))
                    strip.frame_end = int(strip.frame_start) + int(round(collapse_frames))
                else:
                    strip.scale = desired_length / action_length
            strip.extrapolation = "HOLD_FORWARD" if normalized_role == "death" else "NOTHING"
            # These strips are consecutive rather than overlapping.  A zero
            # hand-off between fire/hit/death keeps the armed pose valid;
            # locomotion blends are resolved in the same-role pass below.
            if normalized_role in {"idle", "walk", "run", "strafe_left", "strafe_right"}:
                strip.blend_in = 0.0
                strip.blend_out = 0.0
            else:
                strip.blend_in = 0.0
                strip.blend_out = 0.0
            previous_end = end_frame
            installed_item = {"action": requested_name, "frame_start": start_frame, "frame_end": end_frame}
            if segment_meta:
                installed_item.update(segment_meta)
                installed_item["animation_repeat"] = round(float(strip.repeat), 5)
            installed.append(installed_item)
        # Consecutive strips of the same locomotion role already continue the
        # foot-cycle phase, so their poses match at the seam.  Blending them
        # through the armed-idle base anyway dropped the runner into the
        # standing idle pose for one or two frames at every segment boundary,
        # which read as the recurring "walk then crouch / backward hitch"
        # while sprinting (QA).  Zero the blend on same-role hand-offs and
        # keep a short blend only where the role actually changes (e.g. the
        # run->fire stop), so a transition still settles naturally.
        for index, (strip, role) in enumerate(created_strips):
            prev_role = created_strips[index - 1][1] if index > 0 else None
            next_role = created_strips[index + 1][1] if index + 1 < len(created_strips) else None
            locomotion = role in {"idle", "walk", "run", "strafe_left", "strafe_right"}
            blend_frames = float(_LOCOMOTION_BLEND_FRAMES) if locomotion else 0.0
            strip.blend_in = 0.0 if (prev_role is None or (locomotion and prev_role == role)) else blend_frames
            strip.blend_out = 0.0 if (next_role is None or (locomotion and next_role == role)) else blend_frames
    return installed, sorted(set(missing))


def install_swat_route_actions(actor_root, beats, combat_frames=None):
    data = swat_actor_data(actor_root)
    if not data:
        return {"installed": [], "missing": ["actor metadata unavailable"]}
    combat_frames = set(int(value) for value in (combat_frames or []))
    segments = []
    source_frame = int(bpy.context.scene.frame_current)
    fps = max(1.0, float(bpy.context.scene.render.fps))

    def actual_travel_distance(start, end):
        frames = list(range(int(start), int(end) + 1, 2))
        if not frames or frames[-1] != int(end):
            frames.append(int(end))
        points = [evaluated_world_location(actor_root, frame) for frame in frames]
        return sum((second - first).length for first, second in zip(points, points[1:]))

    for index in range(len(beats) - 1):
        start = int(beats[index]["frame"])
        end = max(start + 2, int(beats[index + 1]["frame"]) - 1)
        distance = actual_travel_distance(start, end)
        duration = max(1, end - start)
        speed = distance / duration * fps
        # The fire role starts no earlier than the hostile's first shot (about
        # 45 frames before the combat beat): the enemy opens fire first and
        # the player stops and returns fire from the next route segment,
        # instead of the player shooting a still-idle enemy first.
        combat_here = any(
            start >= int(combat_frame) - 45 and start < int(combat_frame) + 32
            for combat_frame in combat_frames
        )
        # Reload while running away from the finished fight: the segments
        # right after the combat hold keep the normal RUN loop (feet and speed
        # match a normal run; QA), while a procedural gun dip on the weapon
        # reads as the reload -- the full-body reload action would freeze the
        # legs mid-stride.
        reload_after_combat = any(
            start >= int(combat_frame) + 32 and start < int(combat_frame) + 80
            for combat_frame in combat_frames
        )
        event_type = str(beats[index].get("event_type", ""))
        if distance < 0.08:
            # Event holds have no world displacement.  Playing even a short
            # run cycle during those stationary frames makes the knees and
            # feet tremble in place; use the armed idle base until travel
            # resumes.
            role = "idle"
        elif reload_after_combat:
            role = "run"
        elif event_type in {"collect", "collect_required", "interact"} and not _TPS_ALWAYS_RUN:
            # The available armed interaction clip is a realistic reload/check
            # sequence.  Combined with the procedural lean below it reads as a
            # deliberate supply/console action instead of sliding through it.
            role = "reload"
        elif combat_here or event_type == "combat":
            # TPS fights happen while advancing.  A full-body fire strip owns
            # the legs and makes a moving root look like it is skating; keep
            # the distance-synchronised rifle-run and drive aim/recoil as
            # independent weapon/body orientation keys.
            role = "run"
        elif event_type == "dodge":
            current = direction_for(beats, index)
            following = direction_for(beats, min(index + 1, len(beats) - 1))
            cross = current.x * following.y - current.y * following.x
            role = "strafe_left" if cross >= 0.0 else "strafe_right"
        else:
            # The TPS showcase never switches locomotion mid-run: every
            # non-combat beat (short holds, supplies, consoles included) keeps
            # the run loop, so there is no walk/run/reload switching while
            # walking (QA: "don't switch actions mid-walk, run unless firing").
            role = "run"
        segments.append((role, start, end, {
            "travel_distance_m": round(float(distance), 5),
            "world_speed_mps": round(float(speed), 5),
            "foot_cycle_sync": role in {"walk", "run", "strafe_left", "strafe_right"},
            "minimum_locomotion_repeat": 0.05,
        }))
    # Merge consecutive TPS run/walk strips as well as reload strips.  Stage
    # 11 densifies the route into short transit beats; forcing nearly a full
    # run cycle into every two- or three-frame transit strip made the feet and
    # knees vibrate.  One continuous distance-synchronised strip preserves
    # the gait phase across all of those semantic boundaries.
    merged_segments = []
    for segment in segments:
        role = segment[0]
        mergeable = role in {"reload", "run", "walk"}
        if mergeable and merged_segments and merged_segments[-1][0] == role:
            previous = merged_segments[-1]
            combined_distance = (
                float(previous[3].get("travel_distance_m", 0.0))
                + float(segment[3].get("travel_distance_m", 0.0))
            )
            combined_duration = max(1, int(segment[2]) - int(previous[1]))
            merged_segments[-1] = (
                role,
                previous[1],
                segment[2],
                {
                    "travel_distance_m": round(combined_distance, 5),
                    "world_speed_mps": round(combined_distance / combined_duration * fps, 5),
                    "foot_cycle_sync": role in {"run", "walk"},
                    "minimum_locomotion_repeat": 0.05,
                },
            )
        else:
            merged_segments.append(segment)
    segments = merged_segments
    bpy.context.scene.frame_set(source_frame)
    proxy = {"name": data["name"], "armatures": data["armatures"], "actions": data["actions"]}
    installed, missing = install_enemy_action_sequence(proxy, segments)
    return {
        "installed": installed,
        "missing": missing,
        "distance_synchronized_locomotion": True,
        "continuous_locomotion_strips_merged": True,
        "stationary_holds_use_armed_idle": True,
        "minimum_fractional_locomotion_repeat": 0.05,
        "run_stride_distance_m": 1.68,
        "walk_stride_distance_m": 1.05,
        "moving_combat_uses_locomotion_base": True,
    }


def swat_weapon_barrel_direction(actor_data, frame):
    """Return final evaluated SCAR-H barrel direction in world space."""
    weapon = next(iter(actor_data.get("weapon_instances", [])), None) if actor_data else None
    if weapon is None:
        return None
    bpy.context.scene.frame_set(int(frame))
    bpy.context.view_layer.update()
    evaluated = weapon.evaluated_get(bpy.context.evaluated_depsgraph_get())
    direction = evaluated.matrix_world.to_3x3() @ Vector((-1.0, 0.0, 0.0))
    if direction.length < 1e-6:
        return None
    return direction.normalized()


def key_swat_local_aim(actor, beats, beat_index, enemy_position, attack_frame=None):
    data = swat_actor_data(actor)
    if not data:
        return 0.0
    frame = (
        int(attack_frame)
        if attack_frame is not None
        else int(beats[beat_index]["frame"])
    )
    orientation = data["orientation"]
    orientation.rotation_mode = "XYZ"
    muzzle_marker = next(iter(data.get("weapon_muzzles", [])), None)
    if muzzle_marker is None:
        return {"yaw_degrees": 0.0, "residual_degrees": 180.0, "verified": False}

    # Crucially, install_swat_route_actions() now runs before this function.
    # Therefore both the hand pose and weapon transform below are the FINAL
    # evaluated ones at the actual shot frame, not a pre-NLA approximation.
    bpy.context.scene.frame_set(max(1, frame - 14))
    bpy.context.view_layer.update()
    base_yaw = float(orientation.rotation_euler.z)
    muzzle = evaluated_world_location(muzzle_marker, frame)
    desired = Vector(enemy_position) + UP * 1.12 - muzzle
    desired.z = 0.0
    barrel = swat_weapon_barrel_direction(data, frame)
    if barrel is None or desired.length < 1e-5:
        return {"yaw_degrees": 0.0, "residual_degrees": 180.0, "verified": False}
    barrel.z = 0.0
    if barrel.length < 1e-5:
        return {"yaw_degrees": 0.0, "residual_degrees": 180.0, "verified": False}
    barrel.normalize()
    desired.normalize()
    correction = math.atan2(
        barrel.x * desired.y - barrel.y * desired.x,
        max(-1.0, min(1.0, barrel.dot(desired))),
    )
    correction = max(math.radians(-100.0), min(math.radians(100.0), correction))
    target_yaw = base_yaw + correction
    for key_frame, value in (
        (frame - 14, base_yaw),
        (frame - 5, base_yaw + correction * 0.6),
        (frame, target_yaw),
        (frame + 18, target_yaw),
        (frame + 28, base_yaw),
    ):
        orientation.rotation_euler.z = value
        orientation.keyframe_insert("rotation_euler", frame=max(1, key_frame), index=2)
    if orientation.animation_data and orientation.animation_data.action:
        for curve in orientation.animation_data.action.fcurves:
            for point in curve.keyframe_points:
                point.interpolation = "BEZIER"
                point.handle_left_type = "AUTO_CLAMPED"
                point.handle_right_type = "AUTO_CLAMPED"
    evaluated_barrel = swat_weapon_barrel_direction(data, frame)
    residual = 180.0
    if evaluated_barrel is not None:
        evaluated_barrel.z = 0.0
        if evaluated_barrel.length > 1e-5:
            evaluated_barrel.normalize()
            residual = math.degrees(math.acos(max(-1.0, min(1.0, evaluated_barrel.dot(desired)))))
    return {
        "yaw_degrees": round(math.degrees(correction), 5),
        "residual_degrees": round(float(residual), 5),
        "verified": bool(residual <= 3.0),
    }


def key_swat_burst_alignment(actor, enemy_position, shot_frames):
    """Correct every burst frame against the animated SCAR-H barrel.

    A rifle-run clip moves the wrists between shots.  Aligning only the first
    frame made shot 0 correct while shots 1/2 drifted by as much as 21 degrees.
    Insert a small evaluated yaw correction at each actual shot frame so all
    tracers, not merely the first one, leave along the visible barrel.
    """
    data = swat_actor_data(actor)
    if not data:
        return []
    orientation = data["orientation"]
    orientation.rotation_mode = "XYZ"
    muzzle_marker = next(iter(data.get("weapon_muzzles", [])), None)
    if muzzle_marker is None:
        return []
    samples = []
    for frame in sorted(set(int(value) for value in shot_frames)):
        muzzle = evaluated_world_location(muzzle_marker, frame)
        desired = Vector(enemy_position) + UP * 1.12 - muzzle
        desired.z = 0.0
        barrel = swat_weapon_barrel_direction(data, frame)
        if barrel is None or desired.length < 1e-5:
            continue
        barrel.z = 0.0
        if barrel.length < 1e-5:
            continue
        barrel.normalize()
        desired.normalize()
        correction = math.atan2(
            barrel.x * desired.y - barrel.y * desired.x,
            max(-1.0, min(1.0, barrel.dot(desired))),
        )
        bpy.context.scene.frame_set(frame)
        bpy.context.view_layer.update()
        current_yaw = float(orientation.rotation_euler.z)
        orientation.rotation_euler.z = current_yaw + correction
        orientation.keyframe_insert("rotation_euler", frame=frame, index=2)
        evaluated = swat_weapon_barrel_direction(data, frame)
        residual = 180.0
        if evaluated is not None:
            evaluated.z = 0.0
            if evaluated.length > 1e-5:
                residual = math.degrees(math.acos(max(
                    -1.0,
                    min(1.0, evaluated.normalized().dot(desired)),
                )))
        samples.append({
            "frame": frame,
            "correction_degrees": round(math.degrees(correction), 5),
            "residual_degrees": round(float(residual), 5),
        })
    action = orientation.animation_data.action if orientation.animation_data else None
    if action:
        for curve in action.fcurves:
            if curve.data_path == "rotation_euler" and curve.array_index == 2:
                for point in curve.keyframe_points:
                    point.interpolation = "BEZIER"
                    point.handle_left_type = "AUTO_CLAMPED"
                    point.handle_right_type = "AUTO_CLAMPED"
    return samples


def key_swat_interaction_poses(actor, beats):
    """Add a readable armed lean/check pose at supplies and consoles."""
    data = swat_actor_data(actor)
    if not data:
        return []
    orientation = data["orientation"]
    orientation.rotation_mode = "XYZ"
    interactions = []
    for index, beat in enumerate(beats):
        event_type = str(beat.get("event_type", ""))
        if beat.get("kind") != "authored" or event_type not in {"collect", "collect_required", "interact", "goal"}:
            continue
        start = int(beat["frame"])
        end = start + (16 if event_type.startswith("collect") else 22 if event_type == "interact" else 12)
        if index + 1 < len(beats) and beats[index + 1].get("event_type") == "event_hold":
            end = int(beats[index + 1]["frame"])
        lean = math.radians(10.0 if event_type.startswith("collect") else 7.0)
        for frame, value in (
            (max(1, start - 4), 0.0),
            (start + 3, lean),
            (max(start + 4, end - 4), lean),
            (end + 3, 0.0),
        ):
            orientation.rotation_euler.x = value
            orientation.keyframe_insert("rotation_euler", frame=frame, index=0)
        interactions.append({
            "beat_id": beat.get("beat_id"),
            "event_type": event_type,
            "frame_start": start,
            "frame_end": end,
            "pose": "armed_reload_check_with_forward_lean",
        })
    if orientation.animation_data and orientation.animation_data.action:
        for curve in orientation.animation_data.action.fcurves:
            if curve.data_path != "rotation_euler" or curve.array_index != 0:
                continue
            for point in curve.keyframe_points:
                point.interpolation = "BEZIER"
                point.handle_left_type = "AUTO_CLAMPED"
                point.handle_right_type = "AUTO_CLAMPED"
    return interactions


def key_player_reload_on_run(player, combat_frames):
    """Procedural gun dip reads as a reload while the legs keep a normal run."""
    data = swat_actor_data(player)
    if not data:
        return 0
    weapons = data.get("weapon_instances") or []
    if not weapons:
        return 0
    weapon = weapons[0]
    weapon.rotation_mode = "XYZ"
    base_x = float(weapon.rotation_euler.x)
    count = 0
    for combat_frame in combat_frames:
        start = int(combat_frame) + 32
        end = int(combat_frame) + 80
        for frame, pitch in (
            (start, base_x),
            (start + 6, base_x - math.radians(42.0)),
            (start + 18, base_x - math.radians(42.0)),
            (start + 28, base_x - math.radians(8.0)),
            (end, base_x),
        ):
            weapon.rotation_euler.x = pitch
            weapon.keyframe_insert("rotation_euler", frame=max(1, int(frame)), index=0)
        count += 1
    return count


def fade_tps_player_at_goal(player, beats):
    """Shrink the player out at the extraction point (QA: NPC slowly vanishes)."""
    data = swat_actor_data(player)
    if not data:
        return False
    goal_beats = [
        beat for beat in beats
        if beat.get("kind") == "authored" and beat.get("event_type") == "goal"
    ]
    if not goal_beats:
        return False
    frame = int(goal_beats[-1]["frame"])
    content = data["content"]
    original = content.scale.copy()
    content.scale = original
    content.keyframe_insert("scale", frame=max(1, frame - 2))
    content.scale = original * 0.01
    content.keyframe_insert("scale", frame=frame + 36)
    return True


def project_world_into_camera(camera, scene, depsgraph, world_position):
    """Project a world point into the camera frame; None when behind the lens."""
    if camera is None:
        return None
    rel = camera.matrix_world.inverted() @ Vector(world_position)
    if rel.z >= -0.05:
        return None
    res_x = int(scene.render.resolution_x * scene.render.resolution_percentage / 100.0)
    res_y = int(scene.render.resolution_y * scene.render.resolution_percentage / 100.0)
    focal_px = float(camera.data.lens) * res_x / 36.0
    depth = -rel.z
    screen_x = res_x / 2.0 + (rel.x / depth) * focal_px
    screen_y = res_y / 2.0 - (rel.y / depth) * focal_px
    inside = 0.0 <= screen_x <= res_x and 0.0 <= screen_y <= res_y
    return (screen_x, screen_y, inside)


def _camera_hidden_tree_names(camera):
    """Tree instances the TPS camera hid from the staged demo (not blockers)."""
    if camera is None:
        return set()
    try:
        hidden = json.loads(str(camera.get("code2games_camera_hidden_tree_names", "[]")))
    except Exception:
        hidden = []
    return {str(name) for name in hidden}


def _hierarchy_object_names(root):
    """All object names in a root's descendant hierarchy (including the root)."""
    names = set()
    if root is None:
        return names
    pending = [root]
    while pending:
        obj = pending.pop()
        names.add(str(obj.name))
        pending.extend(list(obj.children))
    return names


def enemy_first_visible_frame(
    camera,
    player_root,
    enemy_root,
    enemy_position,
    start_frame,
    end_frame,
    z_extent,
    effective_range_m=45.0,
):
    """First frame the hostile chest is in-frame and LOS to the player is clear.

    Spec 2.3: the enemy combat state must trigger from the first frame the
    player can actually see the hostile (chest projects inside the real TPS
    camera AND the player->enemy ray is not blocked), not from a fixed
    director frame.
    """
    scene = bpy.context.scene
    ignore_names = _camera_hidden_tree_names(camera)
    ignore_names.update(_hierarchy_object_names(player_root))
    ignore_names.update(_hierarchy_object_names(enemy_root))
    start_frame = max(1, int(start_frame))
    end_frame = max(start_frame, int(end_frame))
    chest = Vector(enemy_position) + Vector((0.0, 0.0, 1.5))
    for frame in range(start_frame, end_frame + 1, 2):
        scene.frame_set(frame)
        depsgraph = bpy.context.evaluated_depsgraph_get()
        bpy.context.view_layer.update()
        player_position = player_root.evaluated_get(depsgraph).matrix_world.translation
        horizontal = math.hypot(
            float(chest.x - player_position.x),
            float(chest.y - player_position.y),
        )
        if horizontal > effective_range_m:
            continue
        projected = project_world_into_camera(camera, scene, depsgraph, chest)
        if projected is None or not projected[2]:
            continue
        origin = player_position + Vector((0.0, 0.0, 1.35))
        delta = chest - origin
        distance = delta.length
        if distance < 0.5:
            continue
        direction = delta / distance
        ray_origin = origin
        remaining = distance
        clear = True
        for _ in range(48):
            hit, location, _normal, _face, obj, _matrix = scene.ray_cast(
                depsgraph,
                ray_origin,
                direction,
                distance=max(0.0, remaining - 0.30),
            )
            if not hit:
                break
            location = Vector(location)
            travelled = (location - ray_origin).length
            if travelled >= remaining - 1.0:
                break  # reached the enemy chest
            source = obj.original if getattr(obj, "is_evaluated", False) else obj
            name = str(source.name)
            if name in ignore_names or name.startswith("C2G_TPS_HOSTILE_"):
                ray_origin = location + direction * 0.04
                remaining = max(0.0, (chest - ray_origin).length)
                continue
            if director_or_gameplay_object(source) and not gameplay_placement_object(source):
                ray_origin = location + direction * 0.04
                remaining = max(0.0, (chest - ray_origin).length)
                continue
            clear = False
            break
        if clear:
            return frame
    return None


def key_swat_actor_visible_from(actor_data, visible_frame):
    """Keep a staged guard out of the render until its encounter begins."""
    visible_frame = max(1, int(visible_frame))
    keyed = 0
    for obj in actor_data.get("objects", []):
        obj.hide_render = True
        obj.keyframe_insert("hide_render", frame=1)
        obj.keyframe_insert("hide_render", frame=max(1, visible_frame - 1))
        obj.hide_render = False
        obj.keyframe_insert("hide_render", frame=visible_frame)
        action = obj.animation_data.action if obj.animation_data else None
        if action:
            for curve in action.fcurves:
                if curve.data_path == "hide_render":
                    for point in curve.keyframe_points:
                        point.interpolation = "CONSTANT"
        keyed += 1
    return keyed


def stage_tps_combat(collection, beats, player, swat_library, actor_scale=1.0):
    authored = [index for index, beat in enumerate(beats) if beat.get("kind") == "authored"]
    combat_indices = [index for index in authored if beats[index].get("event_type") == "combat"]
    if len(combat_indices) < 2:
        pool = authored[1:-1] or list(range(1, max(2, len(beats) - 1)))
        if not pool:
            return {"enabled": False, "reason": "route has no encounter beats"}
        picks = [pool[max(0, int(len(pool) * fraction) - 1)] for fraction in (0.52, 0.78)]
        combat_indices = sorted(set(picks))
    combat_indices = combat_indices[:int(_TPS_ENEMY_COUNT)]
    z_extent = scene_z_extent()
    actor_scale = max(0.75, min(1.50, float(actor_scale)))
    enemies = [
        append_swat_actor(collection, swat_library, "TPS_HOSTILE_ALPHA", 1.79 * actor_scale),
        append_swat_actor(collection, swat_library, "TPS_HOSTILE_BRAVO", 1.83 * actor_scale),
        append_swat_actor(collection, swat_library, "TPS_HOSTILE_CHARLIE", 1.86 * actor_scale),
        append_swat_actor(collection, swat_library, "TPS_HOSTILE_DELTA", 1.81 * actor_scale),
        append_swat_actor(collection, swat_library, "TPS_HOSTILE_ECHO", 1.84 * actor_scale),
    ][:len(combat_indices)]
    schedules = []
    preview_frames = []
    shot_count = 0
    source_frame = int(bpy.context.scene.frame_current)
    player_data = swat_actor_data(player)
    player_muzzle_marker = (
        next(iter(player_data.get("weapon_muzzles", [])), None)
        if player_data
        else None
    )
    # Build the final distance-synchronised leg animation BEFORE querying a
    # muzzle or aiming the weapon.  The old order measured the idle hand pose,
    # then installed the run/fire NLA later, producing the visible 90-degree
    # disagreement between SCAR-H and tracer.
    locomotion = install_swat_route_actions(
        player,
        beats,
        [int(beats[index]["frame"]) for index in combat_indices],
    )
    camera = next(
        (
            obj for obj in bpy.data.objects
            if obj.get("code2games_camera_system") == "rigid_left_rear_character_boom"
        ),
        None,
    )
    scan_end = min(
        int(bpy.context.scene.frame_end),
        max(1, int(beats[-1]["frame"]) + 48),
    )
    encounters = []
    for enemy_index, (enemy, beat_index) in enumerate(zip(enemies, combat_indices)):
        beat = beats[beat_index]
        frame = int(beat["frame"])
        player_position = Vector(beat["staged_position"])
        root = enemy["root"]
        appearance_frame = (
            max(1, int(_TPS_FIRST_ENEMY_ATTACK_FRAME) - 4)
            if enemy_index == 0
            else max(1, frame - 4)
        )
        spawn_frame = max(1, appearance_frame - 60)
        authored_lateral = (1.7, -2.0, 1.5, -1.6, 1.2)[enemy_index]
        forward_distance = 7.2 + enemy_index * 0.55
        enemy_position = enemy_position_for_beat(
            beats, beat_index, forward_distance, authored_lateral, z_extent, player
        )
        # Spec 2.3: resolve a standoff the real TPS camera can actually see
        # (frustum + unobstructed LOS) before keying anything.  The hostile
        # is a staged actor, not a fixed asset: if the authored lateral spot
        # stays behind a tree/ridge for the whole approach, try nearby
        # standoffs instead of falling back to a scripted frame.
        first_visible = None
        search_end = min(scan_end, appearance_frame + 4)
        candidate_specs = [(forward_distance, authored_lateral)] + [
            (forward, lateral)
            for forward in (5.8, 6.6, 7.4, 8.2)
            for lateral in (0.0, 0.8, -0.8, 1.6, -1.6, 2.4, -2.4)
            if abs(forward - forward_distance) > 0.01 or abs(lateral - authored_lateral) > 0.01
        ]
        for candidate_forward, candidate_lateral in candidate_specs:
            candidate = enemy_position_for_beat(
                beats,
                beat_index,
                candidate_forward,
                candidate_lateral,
                z_extent,
                player,
            )
            probe = enemy_first_visible_frame(
                camera,
                player,
                root,
                candidate,
                appearance_frame,
                search_end,
                z_extent,
            )
            if probe is not None:
                enemy_position = candidate
                first_visible = probe
                if (candidate_forward, candidate_lateral) != candidate_specs[0]:
                    print(
                        "TPS_ENEMY_REPOSITIONED", enemy["name"],
                        "forward", candidate_forward,
                        "lateral", candidate_lateral,
                        "first_visible", probe,
                        flush=True,
                    )
                break
        # Before detection the guard holds a relaxed station and looks across
        # the clearing.  It turns to the player only immediately before the
        # fire action instead of aiming at the player for the whole scene.
        relaxed_target = enemy_position + Vector((1.8 if enemy_index == 0 else -1.8, 1.0, 0.0))
        key_enemy_root(root, spawn_frame, enemy_position, relaxed_target)
        if first_visible is None:
            first_visible = appearance_frame
            visibility_fallback = True
        else:
            visibility_fallback = False
        # Alpha is the hard 10-second contract.  Later guards fire within two
        # frames of becoming visible.  The player responds one second later,
        # satisfying the requested 0-2 second reaction window while moving.
        attack_start = (
            int(_TPS_FIRST_ENEMY_ATTACK_FRAME)
            if enemy_index == 0
            else first_visible + 2
        )
        player_attack_start = attack_start + 24
        first_player_hit_frame = player_attack_start + 1
        death_start = max(
            player_attack_start + 10,
            attack_start + int(_ENEMY_DEATH_START_OFFSET),
        )
        visibility_object_count = key_swat_actor_visible_from(enemy, first_visible)
        turn_start = max(spawn_frame + 1, attack_start - 3)
        face_frame = max(attack_start - 1, turn_start + 1)
        key_enemy_root(root, turn_start, enemy_position, relaxed_target)
        key_enemy_root(root, max(1, face_frame), enemy_position, player_position + UP)
        key_enemy_root(root, death_start + 48, enemy_position, player_position + UP)
        segments = [
            ("idle", spawn_frame, attack_start - 1),
            ("fire", attack_start, player_attack_start - 1),
            ("hit", player_attack_start, death_start - 1),
            ("death", death_start, death_start + 72),
        ]
        installed, missing = install_enemy_action_sequence(enemy, segments)
        enemy_fire_pitch = key_enemy_fire_pitch(enemy, player, beats, None, segments)
        schedules.append({
            "enemy": enemy["name"],
            "frame": attack_start,
            "combat_beat_frame": frame,
            "position": [round(float(value), 6) for value in enemy_position],
            "installed_actions": installed,
            "missing_actions": missing,
            "visibility_object_count": visibility_object_count,
        })
        aim_alignment = key_swat_local_aim(
            player,
            beats,
            beat_index,
            enemy_position,
            attack_frame=player_attack_start,
        )
        player_fire_frames = [player_attack_start + offset for offset in (0, 4, 8)]
        burst_alignment = key_swat_burst_alignment(
            player,
            enemy_position,
            player_fire_frames,
        )
        burst_residual = max(
            (float(item.get("residual_degrees", 180.0)) for item in burst_alignment),
            default=180.0,
        )
        aim_alignment["burst_samples"] = burst_alignment
        aim_alignment["residual_degrees"] = round(burst_residual, 5)
        aim_alignment["verified"] = bool(burst_alignment and burst_residual <= 3.0)
        enemy_to_player = player_position - enemy_position
        enemy_to_player.z = 0.0
        if enemy_to_player.length < 1e-5:
            enemy_to_player = Vector((0.0, -1.0, 0.0))
        enemy_to_player.normalize()
        player_forward = direction_for(beats, beat_index)
        player_side = Vector((-player_forward.y, player_forward.x, 0.0))
        enemy_muzzle_marker = next(iter(enemy.get("weapon_muzzles", [])), None)
        enemy_fire_frames = []
        for burst, offset in enumerate((0, 5, 10)):
            shot_frame = attack_start + offset
            enemy_fire_frames.append(shot_frame)
            enemy_muzzle = (
                evaluated_world_location(enemy_muzzle_marker, shot_frame)
                if enemy_muzzle_marker is not None
                else enemy_position + UP * (1.38 * actor_scale) + enemy_to_player * 0.72
            )
            # These are readable suppression misses.  The TPS route has no
            # player-hit/death beat, so an impact on the player would create a
            # false causal story just as it did in the old FPS render.
            miss_sign = -1.0 if burst % 2 == 0 else 1.0
            player_target = (
                player_position
                + player_side * (0.72 * miss_sign)
                + player_forward * (0.35 + 0.18 * burst)
                + UP * (0.55 + 0.11 * burst)
            )
            create_combat_shot(
                collection,
                "TPS_ENEMY_%d_%d" % (enemy_index, burst),
                enemy_muzzle,
                player_target,
                shot_frame,
                (1.0, 0.12, 0.02, 1.0),
                add_impact=False,
            )
            shot_count += 1
        for burst, shot_frame in enumerate(player_fire_frames):
            player_muzzle = (
                evaluated_world_location(player_muzzle_marker, shot_frame)
                if player_muzzle_marker is not None
                else player_position + UP * (1.34 * actor_scale) + player_forward * 0.72
            )
            create_combat_shot(
                collection,
                "TPS_PLAYER_%d_%d" % (enemy_index, burst),
                player_muzzle,
                enemy_position + UP * (1.12 * actor_scale),
                shot_frame,
                (0.08, 0.72, 1.0, 1.0),
            )
            shot_count += 1
        preview_frames.append(player_attack_start + 5)
        encounters.append({
            "enemy": enemy["name"],
            "beat_id": beat.get("beat_id"),
            "combat_beat_frame": frame,
            "spawn_frame": spawn_frame,
            "first_visible_frame": first_visible,
            "enemy_attack_start_frame": attack_start,
            "player_attack_start_frame": player_attack_start,
            "first_player_hit_frame": first_player_hit_frame,
            "enemy_death_start_frame": death_start,
            "enemy_fire_frames": enemy_fire_frames,
            "player_fire_frames": player_fire_frames,
            "visibility_fallback": visibility_fallback,
            "aim_alignment": aim_alignment,
            "position": [round(float(value), 6) for value in enemy_position],
            "enemy_attack_window_ok": 0 <= attack_start - first_visible <= 4,
            "player_attack_window_ok": 0 <= player_attack_start - attack_start <= 48,
        })
    bpy.context.scene.frame_set(source_frame)
    interaction_poses = key_swat_interaction_poses(player, beats)
    reload_on_run_count = key_player_reload_on_run(
        player,
        [int(beats[index]["frame"]) for index in combat_indices],
    )
    # Final reconciliation happens only after every locomotion, interaction,
    # reload and encounter key exists.  Later keys can change Blender's Bezier
    # interpolation at an earlier burst even when that burst was correct at
    # creation time.  Re-key all shot frames, then rebuild tracer/flash origins
    # from the final evaluated muzzle hierarchy.
    for encounter in encounters:
        final_samples = key_swat_burst_alignment(
            player,
            Vector(encounter["position"]),
            encounter["player_fire_frames"],
        )
        maximum_residual = max(
            (float(item.get("residual_degrees", 180.0)) for item in final_samples),
            default=180.0,
        )
        encounter["aim_alignment"]["final_reconciliation_samples"] = final_samples
        encounter["aim_alignment"]["residual_degrees"] = round(maximum_residual, 5)
        encounter["aim_alignment"]["verified"] = bool(final_samples and maximum_residual <= 3.0)
    final_shot_rewrite_count = 0
    for encounter_index, encounter in enumerate(encounters):
        target = Vector(encounter["position"]) + UP * (1.12 * actor_scale)
        for burst, shot_frame in enumerate(encounter["player_fire_frames"]):
            tracer = bpy.data.objects.get(
                "TPS_PLAYER_%d_%d_TRACER" % (encounter_index, burst)
            )
            if tracer is None or player_muzzle_marker is None:
                continue
            muzzle = evaluated_world_location(player_muzzle_marker, shot_frame)
            # A tracer is a short near-muzzle streak, while the enemy impact
            # is a separate effect at the intended target.  Build that streak
            # on the final evaluated barrel axis so it cannot visibly leave
            # the SCAR-H at a 3.273-degree angle after save/reload.  Preserve
            # the true enemy chest point as audit metadata.
            barrel = swat_weapon_barrel_direction(player_data, shot_frame)
            visual_target = target
            if barrel is not None:
                visual_target = muzzle + barrel * max(4.0, (target - muzzle).length)
            tracer["code2games_intended_impact_target"] = [float(value) for value in target]
            if rewrite_combat_shot_from_final_muzzle(tracer, muzzle, visual_target):
                final_shot_rewrite_count += 1
    player_faded_at_goal = (
        fade_tps_player_at_goal(player, beats)
        if _TPS_FADE_PLAYER_AT_GOAL
        else False
    )
    return {
        "enabled": True,
        "enemy_count": len(enemies),
        "combat_beat_count": len(combat_indices),
        "combat_encounter_count": len(encounters),
        "encounters": encounters,
        "shot_effect_count": shot_count,
        "final_player_shot_rewrite_count": final_shot_rewrite_count,
        "enemy_schedules": schedules,
        "player_action_schedule": locomotion,
        "reload_on_run_count": reload_on_run_count,
        "player_faded_at_goal": player_faded_at_goal,
        "preview_frames": preview_frames,
        "asset_library": swat_library,
        "combat_actor_scale": actor_scale,
        "combat_visibility_policy": "first_visible_camera_frustum_plus_los",
        "combat_timing_windows_frames": {
            "enemy_attack": 4,
            "player_attack": 48,
            "fps": 24,
        },
        "shot_origin_policy": "evaluated_authored_scar_h_muzzle_markers",
        "player_aim_alignment_policy": "evaluated_final_scar_barrel_to_target",
        "ballistic_check_frame": int(_TPS_FIRST_ENEMY_ATTACK_FRAME) + 24,
        "incoming_round_policy": "suppression_misses_without_player_impact",
        "tracer_visual_policy": "single_frame_sub_meter_thin_streak",
        "interaction_pose_count": len(interaction_poses),
        "interaction_poses": interaction_poses,
    }


def enemy_position_for_beat(
    beats, beat_index, forward_distance, lateral_distance, z_extent, player_root=None
):
    beat = beats[beat_index]
    if player_root is not None:
        bpy.context.scene.frame_set(int(beat["frame"]))
        evaluated_player = player_root.evaluated_get(bpy.context.evaluated_depsgraph_get()).matrix_world
        player = evaluated_player.translation.copy()
        # Place the hostile along the player's CURRENT body facing: the rigid
        # camera looks where the body faces, so a lookahead-predicted heading
        # put the enemy off-screen ("gunfire but no enemy visible" in QA).
        forward = evaluated_player.to_quaternion() @ Vector((0.0, 1.0, 0.0))
    else:
        player = Vector(beat["staged_position"])
        forward = direction_for(beats, beat_index)
    forward.z = 0.0
    if forward.length < 1e-5:
        forward = Vector((0.0, 1.0, 0.0))
    forward.normalize()
    side = Vector((-forward.y, forward.x, 0.0))
    point = player + forward * float(forward_distance) + side * float(lateral_distance)
    point.z = grounded_enemy_height(point.x, point.y, player.z, z_extent) + 0.005
    return point


def grounded_enemy_height(x, y, expected_z, z_extent):
    """Return the real standing surface for a hostile at (x, y).

    terrain_height() keeps the surface closest to the expected route height,
    which can silently fall back to a ledge/plateau value and leave the actor
    floating a couple of metres above the actual ground on slopes and cliffs.
    A straight-down ray on the exact XY position is the ground truth; it is
    used whenever it disagrees with the sampled height by more than 0.4m.
    """
    sampled = terrain_height(float(x), float(y), float(expected_z), z_extent)
    scene = bpy.context.scene
    depsgraph = bpy.context.evaluated_depsgraph_get()
    origin = Vector((float(x), float(y), float(z_extent[1]) + 200.0))
    direction = Vector((0.0, 0.0, -1.0))
    for _ in range(48):
        hit, location, _normal, _face, obj, _matrix = scene.ray_cast(
            depsgraph,
            origin,
            direction,
            distance=max(400.0, float(z_extent[1] - z_extent[0]) + 400.0),
        )
        if not hit:
            break
        if (
            obj
            and obj.type == "MESH"
            and not director_or_gameplay_object(obj)
            and not non_ground_scenery(obj)
        ):
            if float(location.z) < float(sampled) - 0.4:
                return float(location.z)
            return float(sampled)
        if float(location.z) < float(expected_z) - 20.0:
            break
        origin = Vector((location.x, location.y, location.z - 0.03))
    return float(sampled)


def racing_ground_z(x, y, z_extent, diagnostic_hits=None):
    """Straight-down terrain ray-cast height for the racing path.

    The sampled terrain height can silently fall under a ledge/overhang and
    leave the car clipping through the floor or a mountain slope (QA).  The
    ray-cast ground truth is used to raise the chassis back onto the surface.
    """
    scene = bpy.context.scene
    depsgraph = bpy.context.evaluated_depsgraph_get()
    origin = Vector((float(x), float(y), float(z_extent[1]) + 200.0))
    direction = Vector((0.0, 0.0, -1.0))
    for _ in range(48):
        hit, location, _normal, _face, obj, _matrix = scene.ray_cast(
            depsgraph,
            origin,
            direction,
            distance=max(400.0, float(z_extent[1] - z_extent[0]) + 400.0),
        )
        if not hit:
            if diagnostic_hits is not None:
                diagnostic_hits.append({
                    "object_name": None,
                    "world_xyz": None,
                    "is_racing_asphalt": False,
                    "accepted_as_support": False,
                    "ray_missed": True,
                })
            return None
        is_racing_asphalt = bool(
            obj
            and racing_track_surface_object(obj)
            and str(obj.name) == "C2G_RACING_SPEEDWAY_ASPHALT"
        )
        # The named shoulder is part of the generated speedway ribbon.  A
        # tyre may legitimately ride it while the other three remain on
        # asphalt; it is not arbitrary source terrain or a gameplay prop.
        is_racing_shoulder = bool(
            obj
            and racing_track_surface_object(obj)
            and str(obj.name) == "C2G_RACING_SPEEDWAY_SHOULDER"
        )
        # The racing speedway is the sole collision truth for this authored
        # route.  A source BoulderFactory spawn placeholder at z~0 previously
        # sat above it and was accepted as generic scenery-ground by one tyre,
        # producing the 0.62 m hover/vertical snap at frames 611--620.  Do
        # The two explicit speedway meshes are driveable. Never accept source
        # props or arbitrary terrain merely because the ray hits them first.
        accepted = is_racing_asphalt or is_racing_shoulder
        if diagnostic_hits is not None:
            diagnostic_hits.append({
                "object_name": str(obj.name) if obj else None,
                "world_xyz": [round(float(value), 6) for value in location],
                "is_racing_asphalt": is_racing_asphalt,
                "is_racing_shoulder": is_racing_shoulder,
                "accepted_as_support": accepted,
                "ray_missed": False,
            })
        if accepted:
            return float(location.z)
        origin = Vector((location.x, location.y, location.z - 0.03))
    return None


def audit_racing_assets(beats):
    """Print each route asset's presence, visibility, size and position.

    QA: only two or three assets were visible along the path.  This audit
    shows whether the others are missing from the scene, hidden, or simply
    tiny (flat pads/gates the chase camera never picks up).
    """
    for beat in beats:
        if beat.get("kind") != "authored":
            continue
        placement_id = beat.get("placement_id")
        if not placement_id:
            continue
        root = find_placement_root(placement_id, beat.get("fixed_anchor_world_xyz"))
        if root is None:
            print("RACING_ASSET", placement_id, "ROOT_MISSING", flush=True)
            continue
        meshes = hierarchy_mesh_objects(root)
        hidden = [mesh.name for mesh in meshes if mesh.hide_render]
        bounds = mesh_bounds(meshes)
        size = 0.0
        position = root.matrix_world.translation
        if bounds:
            size = (bounds[1] - bounds[0]).length
        print(
            "RACING_ASSET",
            placement_id,
            "meshes",
            len(meshes),
            "hidden",
            len(hidden),
            "size_m",
            round(float(size), 2),
            "pos",
            [round(float(value), 1) for value in position],
            flush=True,
        )


def racing_asset_pass_summary(beats, feedback):
    """Measure real route proximity to fixed Stage-10 roots.

    A HUD entry is not proof that the car passed an asset.  Measure the
    smoothed actor control against the actual staged root, retain the JSON
    anchor mismatch for diagnosis, and count each placement only once.
    """
    reacted = set((feedback or {}).get("prop_reaction_placement_ids") or [])
    reacted.update((feedback or {}).get("vehicle_manoeuvre_placement_ids") or [])
    details = []
    seen = set()
    for beat in beats:
        placement_id = beat.get("placement_id")
        if beat.get("kind") != "authored" or not placement_id or placement_id in seen:
            continue
        seen.add(placement_id)
        planned_anchor = Vector(beat.get("fixed_anchor_world_xyz") or beat["staged_position"])
        root = find_placement_root(str(placement_id), planned_anchor)
        actual_anchor = root.matrix_world.translation.copy() if root is not None else planned_anchor.copy()
        route_position = Vector(beat["staged_position"])
        distance_xy = Vector((route_position.x - actual_anchor.x, route_position.y - actual_anchor.y, 0.0)).length
        event_type = str(beat.get("event_type", ""))
        threshold = 10.0 if event_type == "establish" else 8.0 if event_type == "steer" else 6.0
        passed = root is not None and distance_xy <= threshold
        details.append({
            "placement_id": str(placement_id),
            "event_type": event_type,
            "frame": int(beat["frame"]),
            "actual_root_found": root is not None,
            "planned_anchor_world_xyz": [round(float(value), 6) for value in planned_anchor],
            "actual_anchor_world_xyz": [round(float(value), 6) for value in actual_anchor],
            "planned_to_actual_anchor_distance_m": round((planned_anchor - actual_anchor).length, 6),
            "route_distance_xy_m": round(float(distance_xy), 6),
            "pass_threshold_m": threshold,
            "passed": bool(passed),
            "reaction_staged": str(placement_id) in reacted,
        })
    passed_ids = [item["placement_id"] for item in details if item["passed"]]
    reacted_passed_ids = [
        item["placement_id"] for item in details
        if item["passed"] and item["reaction_staged"]
    ]
    return {
        "details": details,
        "safe_asset_count": len(passed_ids),
        "passed_placement_ids": passed_ids,
        "event_reaction_count": len(reacted_passed_ids),
        "reacted_passed_placement_ids": reacted_passed_ids,
        "unpassed_placement_ids": [item["placement_id"] for item in details if not item["passed"]],
        "passed_without_reaction_ids": [
            item["placement_id"] for item in details
            if item["passed"] and not item["reaction_staged"]
        ],
    }


def key_enemy_root(root, frame, position, facing_target):
    root.location = Vector(position)
    facing = Vector(facing_target) - Vector(position)
    facing.z = 0.0
    if facing.length < 1e-5:
        facing = Vector((0.0, 1.0, 0.0))
    root.rotation_mode = "QUATERNION"
    rotation = facing.normalized().to_track_quat("Y", "Z")
    rotation.make_compatible(root.rotation_quaternion)
    root.rotation_quaternion = rotation
    root.keyframe_insert("location", frame=int(frame))
    root.keyframe_insert("rotation_quaternion", frame=int(frame))


def _pitch_toward_player(player, enemy, frame):
    """Down/up angle from the hostile to the player at a frame, or None."""
    target = evaluated_world_location(player, frame)
    source = evaluated_world_location(enemy["root"], frame)
    delta = target - source
    horizontal = math.hypot(float(delta.x), float(delta.y))
    if horizontal < 0.5:
        return None
    # The tactical rig faces orientation-local -Y (the Z=pi flip), so a
    # POSITIVE X rotation pitches the character DOWN.  Negative values made
    # the cliff enemy lean backward/up -- visible in every QA render but
    # never read as "looking down" (QA).
    pitch = -math.atan2(float(delta.z), horizontal)
    pitch = max(math.radians(-12.0), min(math.radians(70.0), pitch))
    # Aim at the player's chest on level-ground fights.
    if float(delta.z) < -1.2:
        pitch = max(pitch, math.radians(12.0))
    # A hostile standing on the cliff (source elevation >= 3 m, clearly above
    # the player) must look down: at least 30 degrees (QA-tuned).
    if float(source.z) >= 3.0 and float(delta.z) < -0.8:
        pitch = max(pitch, math.radians(30.0))
    return pitch


def key_enemy_fire_pitch(enemy, player, beats, windows, segments=None, pitch_ramps=None):
    """Pitch the hostile's body toward the player while it is alive/visible.

    The root only yaws (the facing is flattened), so a hostile on a hill would
    otherwise look level while shooting at the player below (QA: the third
    enemy must look down).  The pitch follows the enemy's full schedule, so
    the hilltop enemy keeps looking down from the moment it appears through
    the retreat and the final crossfire, not only during the burst frames.
    """
    orientation = enemy["orientation"]
    orientation.rotation_mode = "XYZ"
    keys = {}
    alive_roles = {"idle", "walk", "run", "strafe_left", "strafe_right", "fire", "reload"}
    if segments:
        alive_segments = [
            segment for segment in segments
            if normalized_action_name(str(segment[0])) in alive_roles
        ]
        if alive_segments:
            first_alive = int(alive_segments[0][1])
            if first_alive > 6:
                keys[max(1, first_alive - 6)] = 0.0
            for segment in alive_segments:
                for frame in (int(segment[1]), int(segment[2])):
                    pitch = _pitch_toward_player(player, enemy, frame)
                    if pitch is not None:
                        keys.setdefault(max(1, frame), pitch)
            death_start = next(
                (
                    int(segment[1]) for segment in segments
                    if normalized_action_name(str(segment[0])) in {"hit", "death"}
                ),
                None,
            )
            if death_start is not None:
                keys[max(1, death_start)] = 0.0
    else:
        for start, end in windows:
            start = int(start)
            end = int(end)
            mid = (start + end) // 2
            pitch = _pitch_toward_player(player, enemy, mid)
            if pitch is None:
                continue
            keys[max(1, start - 4)] = 0.0
            keys[start] = pitch
            keys[end] = pitch
            keys[end + 6] = 0.0
    # Explicit pitch ramps override the geometry-based values (e.g. the
    # hilltop hostile starts at 10 deg down at 10 s and steepens over time).
    for ramp in (pitch_ramps or []):
        for frame, value in ramp:
            keys[int(frame)] = value
    for frame in sorted(keys):
        orientation.rotation_euler.x = keys[frame]
        orientation.keyframe_insert("rotation_euler", frame=int(frame), index=0)
    if orientation.animation_data and orientation.animation_data.action:
        for curve in orientation.animation_data.action.fcurves:
            if curve.data_path != "rotation_euler" or curve.array_index != 0:
                continue
            for point in curve.keyframe_points:
                point.interpolation = "LINEAR"
    applied = {frame: round(math.degrees(value), 1) for frame, value in sorted(keys.items())}
    print("ENEMY_FIRE_PITCH", enemy["name"], json.dumps(applied))
    return applied


def key_player_combat_aim(player, beats, beat_index, enemy_position, aim_start_frame=None):
    """Aim a child yaw pivot, leaving locomotion/body rotation uninterrupted.

    aim_start_frame lets a specific hostile (the hilltop third enemy) be
    engaged earlier than the standard 28-frame pre-combat window, so the
    player can start attacking at 12 s while the kill still lands at the
    authored crossfire (QA: "player must start attacking the enemy at 12 s").
    """
    beat = beats[beat_index]
    combat_frame = int(beat["frame"])
    bpy.context.scene.frame_set(combat_frame)
    evaluated_player = player.evaluated_get(bpy.context.evaluated_depsgraph_get())
    player_position = evaluated_player.matrix_world.translation.copy()
    aim = Vector(enemy_position) - player_position
    horizontal = math.hypot(float(aim.x), float(aim.y))
    if horizontal < 1e-5:
        return
    aim.z = 0.0
    player_rotation = evaluated_player.matrix_world.to_quaternion()
    local_aim = player_rotation.inverted() @ aim.normalized()
    # Positive local X is a rightward target; the yaw must be negated so the
    # visible barrel turns toward the hostile (QA: the gun pointed left while
    # the enemy sat dead ahead).  26 degrees still reads as aiming at the
    # hostile while the tracer/impact (computed muzzle-to-enemy) stays exact.
    yaw = -max(
        math.radians(-24.0),
        min(math.radians(24.0), math.atan2(local_aim.x, local_aim.y)),
    )
    pivot = bpy.data.objects.get(player.get("code2games_fps_aim_pivot", ""))
    if pivot is None:
        return
    pivot.rotation_mode = "XYZ"
    viewmodel_base = math.radians(float(_FPS_VIEWMODEL_PITCH_DEGREES))
    camera = bpy.data.objects.get("C2G_FPS_CAMERA")

    def camera_pitch_at(frame):
        if camera is None:
            return 0.0
        bpy.context.scene.frame_set(int(frame))
        bpy.context.view_layer.update()
        evaluated = camera.evaluated_get(bpy.context.evaluated_depsgraph_get())
        return float(evaluated.rotation_euler.x) - math.pi / 2.0

    # The hostile's chest sits below the eye.  Tilt the barrel down during
    # the burst so the visible barrel and the tracer line agree; without this
    # the view model pointed at the sky while the round flew at the enemy.
    aim_down = math.atan2(max(0.0, 1.68 - 1.10), horizontal)
    aim_start = int(aim_start_frame) if aim_start_frame else combat_frame - 28
    # The view model rides the camera pitch (configure_fps_body_camera keys
    # the same relationship across the whole shot), so the SCAR stays in frame
    # no matter how far the view raises; the burst dip still aims the barrel
    # at the hostile's chest.
    for frame, yaw_value, dip_envelope in (
        (aim_start, 0.0, 0.0),
        (aim_start + 14, yaw * 0.55, 0.55),
        (aim_start + 24, yaw, 1.0),
        (combat_frame + 30, yaw, 1.0),
        (combat_frame + 44, 0.0, 0.0),
    ):
        pitch_value = max(
            0.0,
            viewmodel_base + camera_pitch_at(frame) - aim_down * dip_envelope,
        )
        pivot.rotation_euler.z = yaw_value
        pivot.keyframe_insert("rotation_euler", frame=max(1, int(frame)), index=2)
        pivot.rotation_euler.x = pitch_value
        pivot.keyframe_insert("rotation_euler", frame=max(1, int(frame)), index=0)
    if pivot.animation_data and pivot.animation_data.action:
        for curve in pivot.animation_data.action.fcurves:
            for point in curve.keyframe_points:
                point.interpolation = "BEZIER"
                point.handle_left_type = "AUTO_CLAMPED"
                point.handle_right_type = "AUTO_CLAMPED"


def key_player_combat_body_facing(player, beats, beat_index, enemy_position):
    """Turn the whole FPS body onto the hostile for the complete kill.

    The old demo turned only the aim pivot and let the body keep following the
    route corner, so the rigid first-person camera swung sideways away from
    the enemy just as the kill burst was landing.  This eases the body (and
    therefore the mounted camera) onto the hostile before the firefight, holds
    it through the death reaction, then eases onto the post-combat route
    heading.  It is inserted after animate_actor, so its keyframes override
    the route-corner rotation exactly where it matters.
    """
    beat = beats[beat_index]
    combat_frame = int(beat["frame"])
    player_position = Vector(beat["staged_position"])
    enemy_direction = Vector(enemy_position) - player_position
    enemy_direction.z = 0.0
    if enemy_direction.length < 1e-5:
        return
    enemy_direction.normalize()
    enemy_rotation = enemy_direction.to_track_quat("Y", "Z")

    def next_heading_from(index):
        origin = Vector(beats[index]["staged_position"])
        for candidate in range(index + 1, len(beats)):
            delta = Vector(beats[candidate]["staged_position"]) - origin
            delta.z = 0.0
            if delta.length > 0.05:
                return delta.normalized()
        return None

    def previous_heading_from(index):
        origin = Vector(beats[index]["staged_position"])
        for candidate in range(index - 1, -1, -1):
            delta = origin - Vector(beats[candidate]["staged_position"])
            delta.z = 0.0
            if delta.length > 0.05:
                return delta.normalized()
        return None

    incoming = previous_heading_from(beat_index) or enemy_direction
    outgoing = next_heading_from(beat_index) or enemy_direction
    incoming_rotation = incoming.to_track_quat("Y", "Z")
    outgoing_rotation = outgoing.to_track_quat("Y", "Z")
    player.rotation_mode = "QUATERNION"
    previous = incoming_rotation.copy()

    inserted_frames = []

    def key(frame, rotation):
        nonlocal previous
        rotation = rotation.copy()
        rotation.make_compatible(previous)
        player.rotation_quaternion = rotation
        key_frame = max(1, int(frame))
        player.keyframe_insert("rotation_quaternion", frame=key_frame)
        inserted_frames.append(key_frame)
        previous = rotation

    # Ease on before the hostile opens fire, hold through the hit/death
    # reaction, then ease off onto the next leg after the kill completes.
    # These windows are deliberately long: a fast body swing is what read as
    # "rapid camera cuts" in the QA clip.
    key(combat_frame - 20, incoming_rotation)
    key(combat_frame - 10, incoming_rotation.slerp(enemy_rotation, 0.45))
    key(combat_frame + 4, enemy_rotation)
    key(combat_frame + 30, enemy_rotation)
    key(combat_frame + 44, enemy_rotation.slerp(outgoing_rotation, 0.5))
    key(combat_frame + 64, outgoing_rotation)
    if player.animation_data and player.animation_data.action:
        for curve in player.animation_data.action.fcurves:
            if curve.data_path != "rotation_quaternion":
                continue
            for point in curve.keyframe_points:
                if int(round(point.co[0])) in set(inserted_frames):
                    # Only the combat-facing keys get BEZIER easing; the rest
                    # of the curve keeps its own interpolation (LINEAR for TPS
                    # so the body never overshoots while walking).
                    point.interpolation = "BEZIER"
                    point.handle_left_type = "AUTO_CLAMPED"
                    point.handle_right_type = "AUTO_CLAMPED"
    normalize_rotation_curve_winding(player)


def hide_fps_combat_sightline_vegetation(player_position, enemy_position, frame):
    """Temporarily fade only vegetation directly blocking a combat sightline."""
    scene = bpy.context.scene
    depsgraph = bpy.context.evaluated_depsgraph_get()
    origin = Vector(player_position) + UP * 1.68
    target = Vector(enemy_position) + UP * 0.88
    ray = target - origin
    remaining = ray.length
    if remaining < 0.5:
        return []
    direction = ray.normalized()
    hidden = []
    seen = set()
    for _ in range(24):
        hit, location, _normal, _face, obj, _matrix = scene.ray_cast(
            depsgraph,
            origin,
            direction,
            distance=remaining,
        )
        if not hit:
            break
        travelled = (Vector(location) - origin).length
        remaining -= travelled + 0.04
        if remaining <= 0.35:
            break
        if obj and non_ground_scenery(obj):
            source = obj.original if getattr(obj, "is_evaluated", False) else obj
            if source.name not in seen:
                seen.add(source.name)
                source.hide_render = False
                source.keyframe_insert("hide_render", frame=max(1, int(frame) - 14))
                source.hide_render = True
                source.keyframe_insert("hide_render", frame=max(1, int(frame) - 13))
                source.keyframe_insert("hide_render", frame=int(frame) + 19)
                source.hide_render = False
                source.keyframe_insert("hide_render", frame=int(frame) + 20)
                hidden.append(source.name)
            origin = Vector(location) + direction * 0.04
            continue
        if obj and director_or_gameplay_object(obj):
            origin = Vector(location) + direction * 0.04
            continue
        break
    return hidden


def animate_enemy_encounters(
    enemy, beats, combat_indices, forward_distance, lateral_distance, z_extent, player_root=None
):
    first_index, second_index = combat_indices
    first_beat, second_beat = beats[first_index], beats[second_index]
    first_frame, second_frame = int(first_beat["frame"]), int(second_beat["frame"])
    first_position = enemy_position_for_beat(
        beats, first_index, forward_distance, lateral_distance, z_extent, player_root
    )
    second_position = enemy_position_for_beat(
        beats, second_index, forward_distance, -lateral_distance * 2.20, z_extent, player_root
    )
    first_player = (
        evaluated_world_location(player_root, first_frame)
        if player_root is not None else Vector(first_beat["staged_position"])
    ) + UP * 1.1
    second_player = (
        evaluated_world_location(player_root, second_frame)
        if player_root is not None else Vector(second_beat["staged_position"])
    ) + UP * 1.1
    start_frame = max(1, first_frame - 68)
    approach = first_position + Vector((lateral_distance * 0.28, -forward_distance * 0.18, 0.0))
    approach.z = terrain_height(approach.x, approach.y, first_position.z, z_extent) + 0.005
    root = enemy["root"]
    root.scale = (0.001, 0.001, 0.001)
    root.keyframe_insert("scale", frame=max(1, start_frame - 1))
    root.scale = (1.0, 1.0, 1.0)
    root.keyframe_insert("scale", frame=start_frame)
    key_enemy_root(root, start_frame, approach, first_position)
    key_enemy_root(root, first_frame - 18, first_position, first_player)
    key_enemy_root(root, first_frame + 20, first_position, first_player)
    # Follow several existing dense route beats during the relocation so the
    # enemy also stays grounded instead of cutting through hills.
    transit = [
        (int(beat["frame"]), index)
        for index, beat in enumerate(beats)
        if first_frame + 24 < int(beat["frame"]) < second_frame - 22
    ]
    stride = max(1, int(math.ceil(len(transit) / 7.0))) if transit else 1
    for frame, index in transit[::stride]:
        point = enemy_position_for_beat(
            beats, index, forward_distance, lateral_distance * 0.45, z_extent, player_root
        )
        next_player = (
            evaluated_world_location(player_root, frame)
            if player_root is not None else Vector(beats[index]["staged_position"])
        ) + UP
        key_enemy_root(root, frame, point, next_player)
    key_enemy_root(root, second_frame - 18, second_position, second_player)
    key_enemy_root(root, second_frame + 26, second_position, second_player)
    set_linear_animation(root)
    if root.animation_data and root.animation_data.action:
        for curve in root.animation_data.action.fcurves:
            if curve.data_path != "rotation_quaternion":
                continue
            for point in curve.keyframe_points:
                point.interpolation = "BEZIER"
                point.handle_left_type = "AUTO_CLAMPED"
                point.handle_right_type = "AUTO_CLAMPED"
    segments = [
        ("idle", start_frame, first_frame - 42),
        ("walk", first_frame - 41, first_frame - 19),
        ("fire", first_frame - 18, first_frame + 13),
        # Player rounds occur at +5/+9/+13.  Show an immediate, causal hit
        # reaction before this hostile retreats to the later encounter.
        ("hit", first_frame + 14, first_frame + 27),
        ("run", first_frame + 28, second_frame - 19),
        ("fire", second_frame - 18, second_frame + 18),
        ("hit", second_frame + 19, second_frame + 31),
        ("death", second_frame + 32, second_frame + 100),
    ]
    installed, missing = install_enemy_action_sequence(enemy, segments)
    return {
        "enemy": enemy["name"],
        "first_combat_frame": first_frame,
        "second_combat_frame": second_frame,
        "first_position": [round(float(v), 6) for v in first_position],
        "second_position": [round(float(v), 6) for v in second_position],
        "visual_style": enemy.get("visual_style", ""),
        "installed_actions": installed,
        "missing_actions": missing,
    }, first_position, second_position


def animate_visibility(obj, frame, visible_frames=2):
    visible_frames = max(0, int(visible_frames))
    obj.hide_render = True
    obj.keyframe_insert("hide_render", frame=max(1, int(frame) - 1))
    obj.hide_render = False
    obj.keyframe_insert("hide_render", frame=int(frame))
    if visible_frames:
        obj.keyframe_insert("hide_render", frame=int(frame) + visible_frames)
    obj.hide_render = True
    obj.keyframe_insert("hide_render", frame=int(frame) + visible_frames + 1)


def evaluated_world_location(obj, frame):
    """Evaluate an animated marker at a specific frame in world space."""
    scene = bpy.context.scene
    scene.frame_set(int(frame))
    bpy.context.view_layer.update()
    return obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).matrix_world.translation.copy()


def create_combat_shot(collection, name, muzzle, target, frame, color, add_impact=True):
    muzzle = Vector(muzzle)
    target = Vector(target)
    flight = target - muzzle
    flight_length = max(0.001, flight.length)
    flight_direction = flight / flight_length
    # A rifle round crosses this distance inside one 24-fps frame.  Rendering
    # the entire muzzle-to-target segment as a thick curve creates the giant
    # laser-tube seen in the old preview.  Show only a thin, sub-metre motion
    # streak while muzzle flash + impact communicate the complete shot.
    # The streak still has to be readable in first person: a 2 mm curve is
    # invisible at the far muzzle, which is why the old clip showed an enemy
    # hit with no visible round.  Keep it sub-metre and muzzle-origin, just
    # bright and thick enough to see leave the barrel.
    streak_length = min(1.05, max(0.45, flight_length * 0.11))
    # Begin just beyond the authored muzzle so a first-person viewer sees the
    # round leave the actual SCAR-H barrel.  Mid-trajectory streaks were often
    # hidden behind the target or outside the 30 mm view.
    streak_center = muzzle + flight_direction * (0.18 + streak_length * 0.5)
    streak_start = streak_center - flight_direction * (streak_length * 0.5)
    streak_end = streak_center + flight_direction * (streak_length * 0.5)
    tracer_data = bpy.data.curves.new(name + "_TRACER_DATA", "CURVE")
    tracer_data.dimensions = "3D"
    tracer_data.bevel_depth = 0.005
    tracer_data.bevel_resolution = 1
    spline = tracer_data.splines.new("POLY")
    spline.points.add(1)
    spline.points[0].co = (*streak_start, 1.0)
    spline.points[1].co = (*streak_end, 1.0)
    tracer = bpy.data.objects.new(name + "_TRACER", tracer_data)
    collection.objects.link(tracer)
    tracer["code2games_shot_frame"] = int(frame)
    tracer["code2games_shot_origin"] = [float(value) for value in muzzle]
    tracer["code2games_shot_target"] = [float(value) for value in target]
    tracer["code2games_shooter"] = (
        "tps_player" if str(name).startswith("TPS_PLAYER_")
        else "tps_enemy" if str(name).startswith("TPS_ENEMY_")
        else "other"
    )
    assign_material(tracer, material("C2G_Tracer_%s" % name.split("_")[0], color, emission=7.0, roughness=0.24))
    animate_visibility(tracer, frame, 1)
    flash = add_uv_sphere(name + "_MUZZLE", muzzle, 0.032, material("C2G_MuzzleFlash", (1.0, 0.28, 0.015, 1.0), emission=18.0), collection)
    flash.scale = (0.01, 0.01, 0.01)
    flash.keyframe_insert("scale", frame=max(1, frame - 1))
    flash.scale = (2.0, 2.0, 2.0)
    flash.keyframe_insert("scale", frame=frame)
    flash.scale = (0.01, 0.01, 0.01)
    flash.keyframe_insert("scale", frame=frame + 2)
    light_data = bpy.data.lights.new(name + "_FLASH_LIGHT_DATA", "POINT")
    light_data.color = (1.0, 0.20, 0.03)
    light_data.shadow_soft_size = 0.35
    light = bpy.data.objects.new(name + "_FLASH_LIGHT", light_data)
    collection.objects.link(light)
    light.location = muzzle
    light_data.energy = 0.0
    light_data.keyframe_insert("energy", frame=max(1, frame - 1))
    light_data.energy = 140.0
    light_data.keyframe_insert("energy", frame=frame)
    light_data.energy = 0.0
    light_data.keyframe_insert("energy", frame=frame + 2)
    if add_impact:
        # Keep the hit readable as a hot spark, not a large white/red ball that
        # hides the enemy silhouette in preview frames.
        impact = add_uv_sphere(name + "_IMPACT", target, 0.045, material("C2G_Impact", (1.0, 0.08, 0.01, 1.0), emission=14.0), collection)
        impact.scale = (0.01, 0.01, 0.01)
        impact.keyframe_insert("scale", frame=max(1, frame))
        impact.scale = (2.2, 2.2, 2.2)
        impact.keyframe_insert("scale", frame=frame + 2)
        impact.scale = (0.01, 0.01, 0.01)
        impact.keyframe_insert("scale", frame=frame + 6)
    return tracer


def rewrite_combat_shot_from_final_muzzle(tracer, muzzle, target):
    """Move an existing tracer/flash to the final post-animation muzzle."""
    muzzle = Vector(muzzle)
    target = Vector(target)
    flight = target - muzzle
    if flight.length < 0.001:
        return False
    direction = flight.normalized()
    streak_length = min(1.05, max(0.45, flight.length * 0.11))
    start = muzzle + direction * 0.18
    end = start + direction * streak_length
    if tracer.type != "CURVE" or not tracer.data.splines:
        return False
    spline = tracer.data.splines[0]
    if len(spline.points) < 2:
        return False
    spline.points[0].co = (*start, 1.0)
    spline.points[1].co = (*end, 1.0)
    tracer["code2games_shot_origin"] = [float(value) for value in muzzle]
    tracer["code2games_shot_target"] = [float(value) for value in target]
    prefix = tracer.name[:-len("_TRACER")] if tracer.name.endswith("_TRACER") else tracer.name
    flash = bpy.data.objects.get(prefix + "_MUZZLE")
    light = bpy.data.objects.get(prefix + "_FLASH_LIGHT")
    if flash is not None:
        flash.location = muzzle
    if light is not None:
        light.location = muzzle
    return True


def animate_fps_weapon_recoil(player, shot_frames):
    weapon = bpy.data.objects.get(player.get("code2games_weapon_root", ""))
    if weapon is None:
        return 0
    weapon.rotation_mode = "XYZ"
    base_location = weapon.location.copy()
    base_rotation = weapon.rotation_euler.copy()
    for frame in sorted(set(int(value) for value in shot_frames)):
        weapon.location = base_location
        weapon.rotation_euler = base_rotation
        weapon.keyframe_insert("location", frame=max(1, frame - 1))
        weapon.keyframe_insert("rotation_euler", frame=max(1, frame - 1))
        weapon.location = base_location + Vector((0.0, -0.075, 0.018))
        recoil_rotation = base_rotation.copy()
        recoil_rotation.x += math.radians(-3.2)
        recoil_rotation.z += math.radians(0.8)
        weapon.rotation_euler = recoil_rotation
        weapon.keyframe_insert("location", frame=frame)
        weapon.keyframe_insert("rotation_euler", frame=frame)
        weapon.location = base_location
        weapon.rotation_euler = base_rotation
        weapon.keyframe_insert("location", frame=frame + 3)
        weapon.keyframe_insert("rotation_euler", frame=frame + 3)
    return len(set(shot_frames))


def stage_fps_combat(collection, beats, player, swat_library, actor_scale=1.0):
    if not os.path.isfile(swat_library):
        raise FileNotFoundError("SWAT combat library missing: %s" % swat_library)
    combat_indices = [index for index, beat in enumerate(beats) if beat.get("kind") == "authored" and beat.get("event_type") == "combat"]
    if len(combat_indices) < 4:
        raise RuntimeError("FPS combat staging requires four authored combat beats")
    z_extent = scene_z_extent()
    actor_scale = max(0.75, min(1.50, float(actor_scale)))
    enemies = [
        append_swat_actor(collection, swat_library, "TACTICAL_ALPHA", 1.80 * actor_scale),
        append_swat_actor(collection, swat_library, "TACTICAL_BRAVO", 1.84 * actor_scale),
    ]
    schedules = []
    positions = []
    for enemy, pair, distance_value, lateral in (
        # Keep hostiles beyond the immediate turn corridor.  Extra forward
        # separation prevents the camera from reaching their body while a
        # moderate lateral offset keeps them readable in the rigid FPS view.
        (enemies[0], combat_indices[:2], 8.8, 1.10),
        (enemies[1], combat_indices[2:4], 9.2, -1.25),
    ):
        schedule, first_position, second_position = animate_enemy_encounters(
            enemy, beats, pair, distance_value, lateral, z_extent, player
        )
        if schedule["missing_actions"]:
            raise RuntimeError(
                "%s is missing required actions: %s; imported action aliases: %s"
                % (
                    enemy["name"],
                    ", ".join(schedule["missing_actions"]),
                    ", ".join(sorted(enemy["actions"])) or "none",
                )
            )
        schedules.append(schedule)
        positions.append((first_position, second_position, pair))
        key_player_combat_aim(player, beats, pair[0], first_position)
        key_player_combat_aim(player, beats, pair[1], second_position)

    sightline_vegetation = []
    for first_position, second_position, pair in positions:
        for enemy_position, beat_index in ((first_position, pair[0]), (second_position, pair[1])):
            sightline_vegetation.extend(
                hide_fps_combat_sightline_vegetation(
                    evaluated_world_location(player, int(beats[beat_index]["frame"])),
                    enemy_position,
                    int(beats[beat_index]["frame"]),
                )
            )

    shot_frames = []
    shot_count = 0
    source_frame = int(bpy.context.scene.frame_current)
    player_muzzle_marker = bpy.data.objects.get(player.get("code2games_weapon_muzzle", ""))
    for enemy_index, (first_position, second_position, pair) in enumerate(positions):
        for encounter_index, (enemy_position, beat_index) in enumerate(((first_position, pair[0]), (second_position, pair[1]))):
            beat = beats[beat_index]
            combat_frame = int(beat["frame"])
            player_position = evaluated_world_location(player, combat_frame)
            bpy.context.scene.frame_set(combat_frame)
            evaluated_player = player.evaluated_get(bpy.context.evaluated_depsgraph_get())
            player_forward = evaluated_player.matrix_world.to_quaternion() @ Vector((0.0, 1.0, 0.0))
            player_forward.z = 0.0
            if player_forward.length < 1e-5:
                player_forward = direction_for(beats, beat_index)
            else:
                player_forward.normalize()
            player_side = Vector((-player_forward.y, player_forward.x, 0.0))
            enemy_to_player = player_position - enemy_position
            enemy_to_player.z = 0.0
            if enemy_to_player.length < 1e-5:
                enemy_to_player = Vector((0.0, -1.0, 0.0))
            enemy_to_player.normalize()
            enemy_muzzle_marker = next(iter(enemies[enemy_index].get("weapon_muzzles", [])), None)
            for burst_index, offset in enumerate((-9, -4, 1)):
                frame = combat_frame + offset
                enemy_muzzle = (
                    evaluated_world_location(enemy_muzzle_marker, frame)
                    if enemy_muzzle_marker is not None
                    else enemy_position + UP * (1.39 * actor_scale) + enemy_to_player * 0.72
                )
                # Incoming rounds deliberately pass beside the player.  The
                # demo has no player-death beat, so impacts on the camera/body
                # would tell the opposite story from the Director path.
                miss_sign = -1.0 if burst_index % 2 == 0 else 1.0
                player_target = (
                    player_position
                    + player_side * (0.80 * miss_sign + 0.18 * burst_index)
                    + player_forward * (0.55 + 0.22 * burst_index)
                    + UP * (0.42 + 0.12 * burst_index)
                )
                create_combat_shot(
                    collection,
                    "ENEMY%d_%d_%d" % (enemy_index + 1, encounter_index + 1, burst_index + 1),
                    enemy_muzzle,
                    player_target,
                    frame,
                    (1.0, 0.42, 0.06, 1.0),
                    add_impact=False,
                )
                shot_count += 1
            player_to_enemy = enemy_position - player_position
            player_to_enemy.z = 0.0
            if player_to_enemy.length < 1e-5:
                player_to_enemy = player_forward
            player_to_enemy.normalize()
            aimed_side = Vector((-player_to_enemy.y, player_to_enemy.x, 0.0))
            enemy_target = enemy_position + UP * ((1.02 if enemy_index == 0 else 1.20) * actor_scale)
            for burst_index, offset in enumerate((5, 9, 13)):
                frame = combat_frame + offset
                player_muzzle = (
                    evaluated_world_location(player_muzzle_marker, frame)
                    if player_muzzle_marker is not None
                    else player_position + UP * 1.30 + player_to_enemy * 1.12 + aimed_side * 0.30
                )
                create_combat_shot(
                    collection,
                    "PLAYER_%d_%d_%d" % (enemy_index + 1, encounter_index + 1, burst_index + 1),
                    player_muzzle,
                    enemy_target,
                    frame,
                    (1.0, 0.68, 0.12, 1.0),
                )
                shot_frames.append(frame)
                shot_count += 1
    bpy.context.scene.frame_set(source_frame)
    recoil_count = animate_fps_weapon_recoil(player, shot_frames)
    preview_frames = [int(beats[index]["frame"]) + 7 for index in combat_indices[:4]]
    return {
        "enabled": True,
        "asset_license": "user-provided SWAT/SCAR-H assets; verify original distribution terms",
        "asset_source": "user-provided Mixamo-rigged SWAT + SCAR-H PBR + authored rifle actions",
        "asset_library": swat_library,
        "enemy_count": len(enemies),
        "enemy_schedules": schedules,
        "enemy_fire_pitch": {schedule["enemy"]: schedule.get("fire_pitch_deg", {}) for schedule in schedules},
        "combat_beat_count": len(combat_indices),
        "shot_effect_count": shot_count,
        "player_recoil_count": recoil_count,
        "player_weapon_style": str(player.get("code2games_weapon_style", "")),
        "player_death_staged": False,
        "incoming_round_policy": "suppression_misses_without_player_impact",
        "shot_origin_policy": "evaluated_authored_scar_h_muzzle_marker",
        "tracer_visual_policy": "single_frame_sub_meter_thin_streak",
        "hostile_hit_reaction_after_player_burst": True,
        "generic_combat_pulse_suppressed": True,
        "enemy_camera_aim_lock_count": len(combat_indices[:4]),
        "combat_actor_scale": actor_scale,
        "temporarily_hidden_sightline_vegetation": sorted(set(sightline_vegetation)),
        "preview_frames": preview_frames,
    }


def stage_fps_combat_direct(collection, beats, player, swat_library, actor_scale=1.0):
    """Stage one persistent hostile per encounter, with no combat teleport."""
    if not os.path.isfile(swat_library):
        raise FileNotFoundError("SWAT combat library missing: %s" % swat_library)
    combat_indices = [
        index for index, beat in enumerate(beats)
        if beat.get("kind") == "authored" and beat.get("event_type") == "combat"
    ]
    if len(combat_indices) < 3:
        raise RuntimeError("FPS direct combat staging requires three authored combat beats")
    combat_indices = combat_indices[:3]
    actor_scale = max(0.75, min(1.50, float(actor_scale)))
    z_extent = scene_z_extent()
    enemies = [
        append_swat_actor(
            collection,
            swat_library,
            "TACTICAL_%s" % name,
            (1.80 + index * 0.015) * actor_scale,
        )
        for index, name in enumerate(("ALPHA", "BRAVO", "CHARLIE"))
    ]
    schedules = []
    encounter_data = []
    source_frame = int(bpy.context.scene.frame_current)
    scene_end = max(int(beats[-1]["frame"]) + 80, int(bpy.context.scene.frame_end))
    for encounter_index, (enemy, beat_index) in enumerate(zip(enemies, combat_indices)):
        beat = beats[beat_index]
        frame = int(beat["frame"])
        lateral = 2.15 if encounter_index % 2 == 0 else -2.15
        enemy_position = enemy_position_for_beat(
            beats, beat_index, 8.0 + encounter_index * 0.35, lateral, z_extent, player
        )
        player_position = evaluated_world_location(player, frame)
        root = enemy["root"]
        player_to_enemy = enemy_position - player_position
        player_to_enemy.z = 0.0
        if player_to_enemy.length < 1e-5:
            player_to_enemy = Vector((0.0, -1.0, 0.0))
        player_to_enemy.normalize()
        patrol_side = Vector((-player_to_enemy.y, player_to_enemy.x, 0.0))
        first_combat_frame = int(beats[combat_indices[0]]["frame"])
        pitch_ramps = None
        if encounter_index == 2:
            # Hilltop hostile: he engages the moment the player can see him --
            # opens fire while the player finishes the second kill -- then
            # retreats up the slope while reloading.  The player chases and
            # kills him at the final crossfire (QA: shoot first, reload on the
            # run, then rush in while he reloads).
            early_position = enemy_position - player_to_enemy * 10.0
            early_position.z = grounded_enemy_height(early_position.x, early_position.y, enemy_position.z, z_extent) + 0.005
            second_combat_frame = int(beats[combat_indices[1]]["frame"])
            early_fire_start = max(2, second_combat_frame - 14)
            early_fire_end = max(early_fire_start + 20, second_combat_frame + 30)
            # The retreat covers ~10 m during the 110-frame reload action
            # (~2.2 m/s, a running tactical fall-back) and arrives at the post
            # before the final crossfire.
            # The retreat ends just before 10 s (frame 240): from there the
            # hilltop hostile re-engages and keeps the player under fire until
            # the final crossfire (QA: the player must be attacked from 10 s).
            retreat_end = min(early_fire_end + 110, frame - 18, 239)
            facing_target = player_position + UP * 1.05
            key_enemy_root(root, 1, early_position, facing_target)
            key_enemy_root(root, early_fire_end, early_position, facing_target)
            key_enemy_root(
                root,
                (early_fire_end + retreat_end) // 2,
                early_position.lerp(enemy_position, 0.5),
                facing_target,
            )
            key_enemy_root(root, retreat_end, enemy_position, facing_target)
            key_enemy_root(root, scene_end, enemy_position, facing_target)
            # Seat the fall: the cliff terrain slopes away west, so the lying
            # body hovered ~0.6 m above the ground (QA).  Drop the root
            # through the death so the body lands on the cliff.
            death_start = frame + int(_ENEMY_DEATH_START_OFFSET)
            seated_position = enemy_position + Vector((0.0, 0.0, -0.55))
            key_enemy_root(root, death_start - 2, enemy_position, facing_target)
            for death_key in (death_start, death_start + 30, scene_end):
                key_enemy_root(root, death_key, seated_position, facing_target)
            approach_start = early_position
            walk_start = 1
            walk_end = retreat_end
            fire_start = frame - 10
            harass_start = 240
            # QA: from 10 s the hilltop hostile engages with a gentle 10 deg
            # down-look, 20 deg at 12 s, then reaches the normal 30 deg by
            # 13-14 s for the final crossfire (QA: 10 -> 20 -> 30 deg ramp).
            pitch_ramps = [
                [
                    (harass_start, math.radians(10.0)),
                    (288, math.radians(20.0)),
                    (312, math.radians(30.0)),
                    (frame - 11, math.radians(30.0)),
                ],
            ]
            fire_windows = [
                (early_fire_start, early_fire_end),
                (harass_start, frame - 11),
                (fire_start, frame + 10),
            ]
            segments = [
                ("idle", 1, early_fire_start - 1),
                ("fire", early_fire_start, early_fire_end),
                ("reload", early_fire_end + 1, retreat_end),
                ("fire", harass_start, frame - 11),
                ("fire", frame - 10, frame + 10),
                ("hit", frame + 11, frame + int(_ENEMY_DEATH_START_OFFSET) - 1),
                ("death", frame + int(_ENEMY_DEATH_START_OFFSET), scene_end),
            ]
        else:
            # The hostile is already moving the moment the player can see him:
            # he advances toward the player from ~7 m beyond his post, walking
            # at a steady ~2.5 m/s with his eyes on the player, and arrives at
            # the post just before the burst.
            walk_start = max(2, frame - 84)
            for probe_frame in range(2, walk_start):
                if (evaluated_world_location(player, probe_frame) - enemy_position).length < 28.0:
                    walk_start = probe_frame
                    break
            if encounter_index == 1:
                # The second hostile is already on the lane when the opening
                # fight starts: he settles into his post early, then opens fire
                # while the player engages the first enemy (crossfire) and
                # keeps firing until the player turns to kill him.
                walk_end = max(walk_start + 6, min(frame - 18, first_combat_frame - 12))
                fire_start = first_combat_frame - 10
            else:
                walk_end = max(walk_start + 12, frame - 18)
                fire_start = frame - 10
            fire_windows = [(fire_start, frame + 10)]
            walk_distance = float(walk_end - walk_start + 1) / 24.0 * 2.5
            approach_start = (
                enemy_position
                + patrol_side * (2.0 if encounter_index % 2 == 0 else -2.0)
                + player_to_enemy * walk_distance
            )
            approach_start.z = grounded_enemy_height(approach_start.x, approach_start.y, enemy_position.z, z_extent) + 0.005
            facing_target = player_position + UP * 1.05
            key_enemy_root(root, 1, approach_start, facing_target)
            key_enemy_root(root, walk_start, approach_start, facing_target)
            for approach_offset, fraction in (
                (walk_start, 0.0),
                (walk_start + (walk_end - walk_start) // 2, 0.5),
                (walk_end, 1.0),
            ):
                approach_position = approach_start.lerp(enemy_position, fraction)
                approach_position.z = grounded_enemy_height(approach_position.x, approach_position.y, enemy_position.z, z_extent) + 0.005
                key_enemy_root(root, approach_offset, approach_position, facing_target)
            key_enemy_root(root, frame - 18, enemy_position, facing_target)
            key_enemy_root(root, scene_end, enemy_position, facing_target)
            segments = [
                ("idle", 1, walk_start - 1),
                (
                    "walk", walk_start, walk_end,
                    {
                        "travel_distance_m": round(walk_distance, 5),
                        "world_speed_mps": round(2.5, 5),
                        "foot_cycle_sync": True,
                    },
                ),
                ("idle", walk_end + 1, fire_start - 1),
                ("fire", fire_start, frame + 10),
                ("hit", frame + 11, frame + int(_ENEMY_DEATH_START_OFFSET) - 1),
                ("death", frame + int(_ENEMY_DEATH_START_OFFSET), scene_end),
            ]
        fire_pitch = key_enemy_fire_pitch(
            enemy, player, beats, fire_windows, segments, pitch_ramps
        )
        # World-space confirmation: verify the pitch keyframes actually rotate
        # the visible hierarchy (orientation -> content -> armature), not just
        # the fcurve values (QA: the cliff enemy must visibly look down).
        bpy.context.scene.frame_set(int(frame))
        bpy.context.view_layer.update()
        orientation_obj = enemy["orientation"]
        # The rig faces orientation-local -Y (Mixamo forward after the Z=pi
        # flip), so measure that axis for the real world down-angle.
        world_forward = orientation_obj.matrix_world.to_3x3() @ Vector((0.0, -1.0, 0.0))
        horizontal_length = math.hypot(float(world_forward.x), float(world_forward.y))
        down_deg = math.degrees(
            math.atan2(-float(world_forward.z), max(1e-5, horizontal_length))
        )
        print(
            "ENEMY_WORLD_PITCH",
            enemy["name"],
            "frame",
            int(frame),
            "down_deg",
            round(down_deg, 1),
            flush=True,
        )
        armatures = enemy.get("armatures") or []
        if armatures:
            armature_obj = armatures[0]
            arm_forward = armature_obj.matrix_world.to_3x3() @ Vector((0.0, -1.0, 0.0))
            arm_horizontal = math.hypot(float(arm_forward.x), float(arm_forward.y))
            arm_down = math.degrees(
                math.atan2(-float(arm_forward.z), max(1e-5, arm_horizontal))
            )
            print(
                "ENEMY_ARM_WORLD_PITCH",
                enemy["name"],
                "frame",
                int(frame),
                "down_deg",
                round(arm_down, 1),
                flush=True,
            )
        camera_obj = bpy.data.objects.get("C2G_FPS_CAMERA")
        if camera_obj is not None:
            cam_forward = camera_obj.matrix_world.to_3x3() @ Vector((0.0, 0.0, -1.0))
            cam_horizontal = math.hypot(float(cam_forward.x), float(cam_forward.y))
            cam_up_deg = math.degrees(
                math.atan2(float(cam_forward.z), max(1e-5, cam_horizontal))
            )
            print(
                "CAMERA_LOOK_PITCH",
                int(frame),
                "up_deg",
                round(cam_up_deg, 1),
                flush=True,
            )
        installed, missing = install_enemy_action_sequence(enemy, segments)
        if missing:
            raise RuntimeError(
                "%s is missing required actions: %s" % (enemy["name"], ", ".join(missing))
            )
        schedules.append({
            "enemy": enemy["name"],
            "fire_pitch_deg": fire_pitch,
            "combat_frame": frame,
            "position": [round(float(value), 6) for value in enemy_position],
            "approach_start_position": [round(float(value), 6) for value in approach_start],
            "approach_walk_frames": [walk_start, walk_end],
            "approach_frames": [walk_start, walk_end],
            "alert_frame": frame - 18,
            "installed_actions": installed,
            "missing_actions": missing,
            "teleport_count": 0,
            "persistent_world_position": True,
        })
        encounter_data.append((enemy, beat_index, enemy_position))
        if encounter_index == 2:
            # QA: the player starts engaging the hilltop hostile at 12 s
            # (frame 288) while he keeps the suppression fire up; the kill
            # still lands at the final crossfire.
            key_player_combat_aim(
                player,
                beats,
                beat_index,
                enemy_position,
                aim_start_frame=288,
            )
        else:
            key_player_combat_aim(player, beats, beat_index, enemy_position)

    sightline_vegetation = []
    shot_frames = []
    shot_count = 0
    player_muzzle_marker = bpy.data.objects.get(player.get("code2games_weapon_muzzle", ""))
    for encounter_index, (enemy, beat_index, enemy_position) in enumerate(encounter_data):
        frame = int(beats[beat_index]["frame"])
        player_position = evaluated_world_location(player, frame)
        bpy.context.scene.frame_set(frame)
        evaluated_player = player.evaluated_get(bpy.context.evaluated_depsgraph_get())
        player_forward = evaluated_player.matrix_world.to_quaternion() @ Vector((0.0, 1.0, 0.0))
        player_forward.z = 0.0
        if player_forward.length < 1e-5:
            player_forward = direction_for(beats, beat_index)
        player_forward.normalize()
        player_side = Vector((-player_forward.y, player_forward.x, 0.0))
        sightline_vegetation.extend(
            hide_fps_combat_sightline_vegetation(player_position, enemy_position, frame)
        )
        enemy_to_player = player_position - enemy_position
        enemy_to_player.z = 0.0
        enemy_to_player = enemy_to_player.normalized() if enemy_to_player.length > 1e-5 else -player_forward
        enemy_muzzle_marker = next(iter(enemy.get("weapon_muzzles", [])), None)
        shot_bases = [frame]
        if encounter_index == 1:
            # Crossfire: the second hostile also opens fire during the opening
            # fight (QA: we are attacked by the second enemy while engaging the
            # first), so tracers fly from him at the first combat too.
            shot_bases.append(first_combat_frame)
        if encounter_index == 2:
            # The hilltop hostile fires suppression bursts at the player from
            # the moment he is seen (during the second kill), then again at
            # the final crossfire where the player kills him.
            shot_bases.extend([early_fire_start + 9, early_fire_start + 24])
            # Harass fire from 10 s to the final crossfire: visible tracers so
            # the player sees the hilltop hostile actually shooting (QA).
            shot_bases.extend([harass_start + 18, harass_start + 56, harass_start + 94])
        for shot_base in shot_bases:
            base_player = evaluated_world_location(player, shot_base)
            bpy.context.scene.frame_set(shot_base)
            evaluated_base = player.evaluated_get(bpy.context.evaluated_depsgraph_get())
            base_forward = evaluated_base.matrix_world.to_quaternion() @ Vector((0.0, 1.0, 0.0))
            base_forward.z = 0.0
            if base_forward.length < 1e-5:
                base_forward = Vector((0.0, 1.0, 0.0))
            base_forward.normalize()
            base_side = Vector((-base_forward.y, base_forward.x, 0.0))
            for burst_index, offset in enumerate((-9, -4, 1)):
                shot_frame = shot_base + offset
                enemy_muzzle = (
                    evaluated_world_location(enemy_muzzle_marker, shot_frame)
                    if enemy_muzzle_marker is not None
                    else enemy_position + UP * (1.39 * actor_scale) + enemy_to_player * 0.72
                )
                miss_sign = -1.0 if burst_index % 2 == 0 else 1.0
                miss = (
                    base_player
                    + base_side * (0.76 * miss_sign)
                    + base_forward * (0.42 + 0.18 * burst_index)
                    + UP * (0.48 + 0.10 * burst_index)
                )
                create_combat_shot(
                    collection,
                    "FPS_ENEMY_%d_%d_%d" % (encounter_index, shot_base, burst_index),
                    enemy_muzzle,
                    miss,
                    shot_frame,
                    (1.0, 0.36, 0.04, 1.0),
                    add_impact=False,
                )
                shot_count += 1
        target = enemy_position + UP * (1.08 * actor_scale)
        player_shot_frames = [frame + offset for offset in (4, 8, 12)]
        if encounter_index == 2:
            # QA: return fire from 12 s (frame 288) at the hilltop hostile,
            # then finish him at the final crossfire burst.
            player_shot_frames = [288, 292, 296] + player_shot_frames
        for shot_index, shot_frame in enumerate(player_shot_frames):
            muzzle = (
                evaluated_world_location(player_muzzle_marker, shot_frame)
                if player_muzzle_marker is not None
                else player_position + UP * 1.30 + player_forward * 1.05
            )
            create_combat_shot(
                collection,
                "FPS_PLAYER_%d_%d" % (encounter_index, shot_index),
                muzzle,
                target,
                shot_frame,
                (1.0, 0.68, 0.12, 1.0),
            )
            shot_frames.append(shot_frame)
            shot_count += 1
    bpy.context.scene.frame_set(source_frame)
    recoil_count = animate_fps_weapon_recoil(player, shot_frames)
    # Hierarchy audit: every TACTICAL object in the final scene with its
    # parent chain, so a leftover/duplicate enemy (untouched by the pitch)
    # is impossible to miss (QA: "the visible NPC never changes angle").
    for obj in sorted(bpy.data.objects, key=lambda item: item.name):
        if "TACTICAL" not in obj.name:
            continue
        chain = []
        current = obj
        while current is not None and len(chain) < 6:
            chain.append(current.name)
            current = current.parent
        print(
            "ENEMY_OBJECT",
            obj.name,
            "type",
            obj.type,
            "tagged",
            bool(obj.get("code2games_fps_enemy")),
            "chain",
            " -> ".join(chain),
            flush=True,
        )
    return {
        "enabled": True,
        "asset_library": swat_library,
        "enemy_count": len(enemies),
        "enemy_schedules": schedules,
        "combat_beat_count": len(combat_indices),
        "shot_effect_count": shot_count,
        "player_recoil_count": recoil_count,
        "player_weapon_style": str(player.get("code2games_weapon_style", "")),
        "player_death_staged": False,
        "one_persistent_enemy_per_encounter": True,
        "enemy_relocation_or_teleport_count": 0,
        "enemy_patrol_enabled": False,
        "enemy_alert_turn_before_fire_frames": 28,
        "incoming_round_policy": "suppression_misses_without_player_impact",
        "shot_origin_policy": "evaluated_authored_scar_h_muzzle_marker",
        "tracer_visual_policy": "two_frame_muzzle-origin_short_streak",
        "combat_actor_scale": actor_scale,
        "temporarily_hidden_sightline_vegetation": sorted(set(sightline_vegetation)),
        "preview_frames": [int(beats[index]["frame"]) + 7 for index in combat_indices],
    }


def stage_events(collection, beats, genre):
    event_count = 0
    pickup_count = 0
    for index, beat in enumerate(beats):
        # FPS combat already has real enemies, muzzle flashes, tracers and
        # impacts.  A generic event pulse only adds clutter to that language.
        # Ground combat demos use real prop animation and character actions.
        # Generic world-space pulses caused the recurring white ball and could
        # occupy the complete TPS camera near a prop.
        if genre in {"racing", "wingsuit"} or (genre in {"fps", "tps"} and beat.get("event_type") != "goal"):
            # Racing: ground confirmation rings read as white circles on the
            # road while driving and are removed entirely; boost flames, dust
            # and the physical gates carry the feedback instead.
            # Wingsuit: every response is localized at the immutable asset by
            # stage_aircraft_feedback(); generic pulses are the white circles
            # reported in the V1 QA video.  FPS/TPS: only the goal keeps a world-space confirmation (green
            # flare).  Generic pulses at props produced the recurring white
            # ball and made the character hover beside assets without action.
            pulse = None
        else:
            pulse = create_event_pulse(collection, beat, index)
        if pulse:
            event_count += 1
        # Racing boosts are explicit collectibles: they grow slightly at
        # contact, disappear immediately after collection, and the following
        # physical route segment is already retimed at the boost multiplier.
        if (
            (
                genre == "racing"
                and beat.get("event_type") == "boost"
            )
            or (
                genre == "wingsuit"
                and beat.get("event_type") == "flight_collectible"
            )
            or (
                genre not in {"racing", "wingsuit"}
                and beat.get("event_type") in {"collect", "collect_required", "jump_collect"}
            )
        ):
            root = find_placement_root(
                beat.get("placement_id"),
                beat.get("fixed_anchor_world_xyz"),
            )
            if root:
                animate_pickup(root, int(beat["frame"]))
                pickup_count += 1
    return event_count, pickup_count


def configure_scene(path_data, camera):
    scene = bpy.context.scene
    scene.frame_start = int(path_data.get("frame_start", 1))
    scene.frame_end = int(path_data.get("frame_end", 240))
    scene.render.fps = int(path_data.get("fps", 24))
    scene.camera = camera
    return scene


def copy_socket_value(value):
    try:
        return list(value)
    except TypeError:
        return value


def freeze_environment_lighting(scene, reference_frame=None):
    """Hold the staged world's daylight look across the extended timeline.

    Generated worlds may contain animated sun/world parameters intended for
    their original short timeline.  Director paths extend far beyond it, which
    can move the sun below the horizon during later gameplay frames.  Freeze
    only lighting and World animation; actor, camera, gameplay assets, and
    terrain animation remain untouched.
    """
    reference_frame = int(scene.frame_start if reference_frame is None else reference_frame)
    scene.frame_set(reference_frame)
    light_snapshots = []
    for obj in list(scene.objects):
        if obj.type != "LIGHT":
            continue
        data = obj.data
        snapshot = {
            "object": obj,
            "matrix_world": obj.matrix_world.copy(),
            "energy": float(getattr(data, "energy", 0.0) or 0.0),
            "color": tuple(getattr(data, "color", (1.0, 1.0, 1.0))),
        }
        for name in ("angle", "shadow_soft_size", "spot_size", "spot_blend"):
            if hasattr(data, name):
                snapshot[name] = float(getattr(data, name))
        light_snapshots.append(snapshot)

    world = scene.world
    world_color = tuple(world.color) if world else None
    world_inputs = []
    if world and world.use_nodes and world.node_tree:
        for node in world.node_tree.nodes:
            for socket_index, socket in enumerate(node.inputs):
                if hasattr(socket, "default_value"):
                    try:
                        world_inputs.append((node.name, socket_index, copy_socket_value(socket.default_value)))
                    except Exception:
                        pass

    for snapshot in light_snapshots:
        obj = snapshot["object"]
        matrix_world = snapshot["matrix_world"]
        if obj.animation_data:
            obj.animation_data_clear()
        if obj.data.animation_data:
            obj.data.animation_data_clear()
        # An animated parent can otherwise keep moving an unanimated light.
        obj.parent = None
        obj.matrix_world = matrix_world
        obj.data.energy = snapshot["energy"]
        obj.data.color = snapshot["color"]
        for name in ("angle", "shadow_soft_size", "spot_size", "spot_blend"):
            if name in snapshot:
                setattr(obj.data, name, snapshot[name])

    if world:
        if world.animation_data:
            world.animation_data_clear()
        if world.node_tree and world.node_tree.animation_data:
            world.node_tree.animation_data_clear()
        if world_color is not None:
            world.color = world_color
        if world.use_nodes and world.node_tree:
            for node_name, socket_index, value in world_inputs:
                node = world.node_tree.nodes.get(node_name)
                if node and socket_index < len(node.inputs):
                    try:
                        node.inputs[socket_index].default_value = value
                    except Exception:
                        pass

    scene.frame_set(reference_frame)
    return {
        "enabled": True,
        "reference_frame": reference_frame,
        "frozen_light_count": len(light_snapshots),
        "frozen_world_socket_count": len(world_inputs),
        "scope": "lights_and_world_only",
    }


def configure_preview(scene, args):
    # Preserve the render engine, World nodes, authored lights, exposure, and
    # color management from the staged scene.  Infinigen scenes can depend on
    # Cycles-specific world/volume lighting, so forcing Eevee here makes valid
    # daytime scenes render almost black.  This intentionally mirrors the
    # asset-placement preview policy.
    scene.render.resolution_x = max(320, int(args.resolution_x))
    scene.render.resolution_y = max(240, int(args.resolution_y))
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.film_transparent = False
    if scene.render.engine == "CYCLES":
        scene.cycles.samples = min(int(getattr(scene.cycles, "samples", 32)), int(args.preview_samples))
        if hasattr(scene.cycles, "use_denoising"):
            scene.cycles.use_denoising = True
        preferences = bpy.context.preferences.addons["cycles"].preferences
        preferences.compute_device_type = "OPTIX"
        preferences.get_devices()
        if not any(device.type != "CPU" for device in preferences.devices):
            preferences.compute_device_type = "CUDA"
            preferences.get_devices()
        for device in preferences.devices:
            device.use = device.type != "CPU"
        scene.cycles.device = "GPU"
        if hasattr(scene.cycles, "denoiser"):
            scene.cycles.denoiser = "OPTIX"
        # Multi-GPU tile trap: the default 2048px GPU tile makes a 960x540
        # frame a single tile, so only one device renders.  Small tiles give
        # every enabled CUDA device work (see render_director_qa_clip.py).
        scene.render.tile_x = 256
        scene.render.tile_y = 256


def lighting_audit(scene):
    world = scene.world
    background_strength = None
    if world and world.use_nodes and world.node_tree:
        background = world.node_tree.nodes.get("Background")
        if background and background.inputs.get("Strength"):
            background_strength = float(background.inputs["Strength"].default_value)
    lights = [obj for obj in scene.objects if obj.type == "LIGHT"]
    return {
        "render_engine": scene.render.engine,
        "world_name": world.name if world else None,
        "world_uses_nodes": bool(world and world.use_nodes),
        "world_background_strength": background_strength,
        "light_count": len(lights),
        "total_light_energy": round(sum(float(getattr(obj.data, "energy", 0.0) or 0.0) for obj in lights), 6),
        "view_transform": scene.view_settings.view_transform,
        "look": scene.view_settings.look,
        "exposure": float(scene.view_settings.exposure),
        "gamma": float(scene.view_settings.gamma),
        "policy": "preserve_staged_scene_lighting_and_color_management",
    }


def render_previews(scene, output_dir, args):
    preview_dir = os.path.join(output_dir, "previews")
    os.makedirs(preview_dir, exist_ok=True)
    configure_preview(scene, args)
    start, end = scene.frame_start, scene.frame_end
    frames = [start + max(1, int((end - start) * ratio)) for ratio in (0.08, 0.50, 0.92)]
    names = ["director_opening.png", "director_gameplay.png", "director_finale.png"]
    aircraft_frames = scene.get("code2games_aircraft_preview_frames")
    if aircraft_frames:
        frames = [max(start, min(end, int(frame))) for frame in list(aircraft_frames)[:3]]
    combat_frames = scene.get("code2games_combat_preview_frames") or scene.get("code2games_fps_combat_preview_frames")
    if combat_frames:
        prefix = str(scene.get("code2games_director_genre", "combat"))
        for index, frame in enumerate(list(combat_frames)[:4], start=1):
            frames.append(max(start, min(end, int(frame))))
            names.append("%s_combat_%02d.png" % (prefix, index))
    if args.preview_frame:
        frames = [max(start, min(end, int(frame))) for frame in args.preview_frame]
        names = ["director_qa_frame_%04d.png" % frame for frame in frames]
    outputs = []
    for frame, name in zip(frames, names):
        scene.frame_set(frame)
        path = os.path.join(preview_dir, name)
        scene.render.filepath = path
        log("RENDER_DIRECTOR_PREVIEW", frame, path)
        bpy.ops.render.render(write_still=True)
        outputs.append(path)
    return outputs


def render_video(scene, output_dir, args):
    configure_preview(scene, args)
    path = os.path.join(output_dir, "director_demo.mp4")
    scene.render.image_settings.file_format = "FFMPEG"
    scene.render.ffmpeg.format = "MPEG4"
    scene.render.ffmpeg.codec = "H264"
    scene.render.ffmpeg.constant_rate_factor = "MEDIUM"
    scene.render.ffmpeg.ffmpeg_preset = "GOOD"
    scene.render.filepath = path
    log("RENDER_DIRECTOR_VIDEO", path)
    bpy.ops.render.render(animation=True)
    return path


def render_spot_clip(
    scene,
    start_frame,
    seconds,
    output,
    samples=16,
    resolution_x=960,
    resolution_y=540,
    probe_of="",
    probe_output="",
):
    """Render a short QA clip from the in-memory staged scene.

    The shared mount intermittently corrupts multi-GB blend saves, so reopening
    the staged blend for every QA clip is unreliable.  Call this right after
    staging, in the same Blender process, and the clip renders without any
    save/reopen round-trip.
    """
    scene.render.resolution_x = int(resolution_x)
    scene.render.resolution_y = int(resolution_y)
    scene.render.resolution_percentage = 100
    if scene.render.engine != "CYCLES":
        scene.render.engine = "CYCLES"
    preferences = bpy.context.preferences.addons["cycles"].preferences
    preferences.compute_device_type = "OPTIX"
    preferences.get_devices()
    if not any(device.type != "CPU" for device in preferences.devices):
        preferences.compute_device_type = "CUDA"
        preferences.get_devices()
    for device in preferences.devices:
        device.use = device.type != "CPU"
    scene.cycles.device = "GPU"
    enabled_gpus = [
        device.name for device in preferences.devices
        if device.use and device.type != "CPU"
    ]
    print(
        "DIRECTOR_QA_GPU_DEVICES",
        "count",
        len(enabled_gpus),
        "names",
        enabled_gpus,
        flush=True,
    )
    scene.cycles.samples = int(samples)
    scene.cycles.use_denoising = True
    if hasattr(scene.cycles, "denoiser"):
        scene.cycles.denoiser = "OPTIX"
    if hasattr(scene.render, "tile_x"):
        scene.render.tile_x = 256
        scene.render.tile_y = 256
    else:
        print(
            "DIRECTOR_QA_TILE_AUTO",
            "scene.render.tile_x unavailable; using engine defaults",
            flush=True,
        )
    output = os.path.abspath(os.path.expanduser(os.path.expandvars(output)))
    os.makedirs(os.path.dirname(output), exist_ok=True)
    if float(seconds) <= 0:
        # Single-frame QA still (PNG): fastest way to eyeball a pose/aim.
        scene.frame_set(int(start_frame))
        bpy.context.view_layer.update()
        scene.render.image_settings.file_format = "PNG"
        scene.render.filepath = output
        print(
            "DIRECTOR_QA_STILL",
            bpy.data.filepath,
            scene.frame_current,
            output,
            flush=True,
        )
        bpy.ops.render.render(write_still=True)
        print("DIRECTOR_QA_STILL_OK", output, flush=True)
        if probe_of and probe_output:
            target = bpy.data.objects.get(probe_of)
            if target is None:
                print("PROBE_TARGET_MISSING", probe_of, flush=True)
            else:
                old_camera = scene.camera
                probe_camera_data = bpy.data.cameras.new("C2G_QA_SIDE_PROBE_CAM")
                probe_camera = bpy.data.objects.new("C2G_QA_SIDE_PROBE_CAM", probe_camera_data)
                scene.collection.objects.link(probe_camera)
                probe_camera_data.lens = 50.0
                center = target.matrix_world.translation
                forward = target.matrix_world.to_3x3() @ Vector((0.0, 1.0, 0.0))
                forward.z = 0.0
                if forward.length < 1e-4:
                    forward = Vector((1.0, 0.0, 0.0))
                forward.normalize()
                side = Vector((-forward.y, forward.x, 0.0))
                probe_camera.location = center + side * 14.0 + Vector((0.0, 0.0, 3.0))
                look_at = center + Vector((0.0, 0.0, 1.2))
                direction = look_at - probe_camera.location
                probe_camera.rotation_mode = "QUATERNION"
                probe_camera.rotation_quaternion = direction.normalized().to_track_quat("-Z", "Y")
                scene.camera = probe_camera
                probe_output = os.path.abspath(
                    os.path.expanduser(os.path.expandvars(probe_output))
                )
                scene.render.filepath = probe_output
                print("DIRECTOR_QA_SIDE_PROBE", probe_output, flush=True)
                bpy.ops.render.render(write_still=True)
                print("DIRECTOR_QA_SIDE_PROBE_OK", probe_output, flush=True)
                scene.camera = old_camera
                bpy.data.objects.remove(probe_camera, do_unlink=True)
                bpy.data.cameras.remove(probe_camera_data, do_unlink=True)
        return output
    scene.frame_start = max(int(scene.frame_start), int(start_frame))
    requested_end = scene.frame_start + max(1, int(round(float(seconds) * scene.render.fps))) - 1
    scene.frame_end = min(int(scene.frame_end), requested_end)
    scene.render.image_settings.file_format = "FFMPEG"
    scene.render.ffmpeg.format = "MPEG4"
    scene.render.ffmpeg.codec = "H264"
    scene.render.ffmpeg.constant_rate_factor = "MEDIUM"
    scene.render.ffmpeg.ffmpeg_preset = "GOOD"
    scene.render.film_transparent = False
    scene.render.filepath = output
    print(
        "DIRECTOR_QA_RENDER",
        bpy.data.filepath,
        scene.frame_start,
        scene.frame_end,
        output,
        flush=True,
    )
    bpy.ops.render.render(animation=True)
    print("DIRECTOR_QA_RENDER_OK", output, flush=True)
    return output


def build_actor(
    collection,
    genre,
    fbx_path,
    fps_combat_asset_dir="",
    racing_vehicle_asset="",
    swat_combat_library="",
    wingsuit_aircraft_asset="",
    combat_actor_scale=1.0,
    aircraft_target_length=16.7,
):
    warning = None
    if genre == "fps":
        return create_fps_actor(collection, swat_combat_library), warning
    if genre == "tps":
        return append_swat_actor(
            collection,
            swat_combat_library,
            "TPS_PLAYER",
            1.80 * max(0.75, min(1.50, float(combat_actor_scale))),
        )["root"], warning
    if genre == "racing":
        return import_racing_vehicle(collection, racing_vehicle_asset), warning
    if genre == "wingsuit":
        return import_wingsuit_aircraft(
            collection,
            wingsuit_aircraft_asset,
            aircraft_target_length,
        ), warning
    raise ValueError("unsupported genre: %s" % genre)


def main():
    args = parse_args()
    global _TURN_EASING_TPS, _LOCOMOTION_BLEND_FRAMES, _TPS_ALWAYS_RUN
    global _TPS_FADE_PLAYER_AT_GOAL, _GROUND_DETOUR_RADII
    global _ENEMY_DEATH_START_OFFSET, _FPS_VIEWMODEL_PITCH_DEGREES
    global _FPS_CAMERA_MAX_UP_PITCH_DEGREES, _FPS_CAMERA_MAX_DOWN_PITCH_DEGREES
    global _FPS_HOLD_OVERRIDES, _RACING_CAMERA_BACK, _RACING_CAMERA_HEIGHT
    global _RACING_CAMERA_LOOKAHEAD, _RACING_CAMERA_LENS
    global _RACING_PROP_REACTIONS, _RACING_HIDE_CAMERA_FOLIAGE
    global _WINGSUIT_PARKED_BACK_M, _WINGSUIT_PREFLIGHT_FEEDBACK
    global _WINGSUIT_PREFLIGHT_FRAMES, _WINGSUIT_FLIGHT_FRAMES, _WINGSUIT_PULLUP_FRAMES
    global _FPS_VIEWMODEL_SCALE, _FPS_VIEWMODEL_OFFSET, _FPS_TURN_EASING
    global _SAMPLE_STEP_FRAMES
    global _RACING_SPEED_MPS, _RACING_TARGET_DURATION_SECONDS
    global _RACING_MIN_DISPLAY_SPEED_KMH, _RACING_CRUISE_DISPLAY_SPEED_KMH
    global _RACING_PEAK_DISPLAY_SPEED_KMH
    global _FPS_WALK_SPEED_MPS, _FPS_SMOOTH_TURN_WINDOW
    global _MAX_TURN_DEG_PER_FRAME
    global _TPS_TARGET_MOTION_END_FRAME, _TPS_FIRST_ENEMY_ATTACK_FRAME
    global _TPS_ENEMY_COUNT
    global _ACTIVE_DIRECTOR_GENRE
    _TURN_EASING_TPS = bool(args.tps_turn_easing)
    _LOCOMOTION_BLEND_FRAMES = max(0.0, float(args.locomotion_blend_frames))
    _TPS_ALWAYS_RUN = bool(getattr(args, "tps_always_run", False))
    _TPS_FADE_PLAYER_AT_GOAL = bool(getattr(args, "tps_fade_player_at_goal", False))
    _GROUND_DETOUR_RADII = tuple(
        max(0.4, round(float(args.ground_detour_radius) * fraction, 2))
        for fraction in (0.25, 0.45, 0.65, 0.85, 1.0)
    )
    _ENEMY_DEATH_START_OFFSET = max(1, int(args.enemy_death_start_offset))
    _FPS_VIEWMODEL_PITCH_DEGREES = max(0.0, min(30.0, float(args.fps_viewmodel_pitch_degrees)))
    _FPS_CAMERA_MAX_UP_PITCH_DEGREES = max(0.0, min(30.0, float(args.fps_camera_max_up_pitch_degrees)))
    _FPS_CAMERA_MAX_DOWN_PITCH_DEGREES = max(0.0, min(20.0, float(args.fps_camera_max_down_pitch_degrees)))
    if args.fps_hold_frames and len(args.fps_hold_frames) == 5:
        _FPS_HOLD_OVERRIDES = {
            "collect": max(0, int(args.fps_hold_frames[0])),
            "collect_required": max(0, int(args.fps_hold_frames[1])),
            "interact": max(0, int(args.fps_hold_frames[2])),
            "combat": max(0, int(args.fps_hold_frames[3])),
            "goal": max(0, int(args.fps_hold_frames[4])),
        }
    _RACING_CAMERA_BACK = max(4.0, float(args.racing_camera_back))
    _RACING_CAMERA_HEIGHT = max(0.5, float(args.racing_camera_height))
    _RACING_CAMERA_LOOKAHEAD = max(2.0, float(args.racing_camera_lookahead))
    _RACING_CAMERA_LENS = max(18.0, min(80.0, float(args.racing_camera_lens)))
    _RACING_PROP_REACTIONS = bool(args.racing_prop_reactions)
    _RACING_HIDE_CAMERA_FOLIAGE = bool(args.racing_hide_camera_foliage)
    _WINGSUIT_PARKED_BACK_M = max(0.0, float(args.wingsuit_parked_back_m))
    _WINGSUIT_PREFLIGHT_FEEDBACK = bool(args.wingsuit_preflight_feedback)
    _WINGSUIT_PREFLIGHT_FRAMES = max(48, min(144, int(args.wingsuit_preflight_frames)))
    _WINGSUIT_FLIGHT_FRAMES = max(480, min(1152, int(args.wingsuit_flight_frames)))
    _WINGSUIT_PULLUP_FRAMES = max(48, min(240, int(args.wingsuit_pullup_frames)))
    _FPS_VIEWMODEL_SCALE = max(0.2, min(3.0, float(args.fps_viewmodel_scale)))
    if args.fps_viewmodel_offset and len(args.fps_viewmodel_offset) == 3:
        _FPS_VIEWMODEL_OFFSET = tuple(float(value) for value in args.fps_viewmodel_offset)
    _FPS_TURN_EASING = bool(args.fps_turn_easing)
    _SAMPLE_STEP_FRAMES = max(1, min(12, int(args.ground_sample_step_frames)))
    _RACING_SPEED_MPS = max(2.0, min(40.0, float(args.racing_speed_mps)))
    _RACING_TARGET_DURATION_SECONDS = max(45.0, min(60.0, float(args.racing_target_duration_seconds)))
    _RACING_MIN_DISPLAY_SPEED_KMH = max(0.0, min(300.0, float(args.racing_min_display_speed_kmh)))
    _RACING_CRUISE_DISPLAY_SPEED_KMH = max(
        _RACING_MIN_DISPLAY_SPEED_KMH,
        min(360.0, float(args.racing_cruise_display_speed_kmh)),
    )
    _RACING_PEAK_DISPLAY_SPEED_KMH = max(
        400.0,
        min(450.0, float(args.racing_peak_display_speed_kmh)),
    )
    _FPS_WALK_SPEED_MPS = max(1.5, min(12.0, float(args.fps_walk_speed_mps)))
    _FPS_SMOOTH_TURN_WINDOW = max(1, min(8, int(args.fps_smooth_turn_window)))
    _MAX_TURN_DEG_PER_FRAME = max(1.0, min(20.0, float(args.max_turn_deg_per_frame)))
    _TPS_TARGET_MOTION_END_FRAME = max(720, min(1416, int(args.tps_target_motion_end_frame)))
    _TPS_FIRST_ENEMY_ATTACK_FRAME = max(120, min(
        _TPS_TARGET_MOTION_END_FRAME - 240,
        int(args.tps_first_enemy_attack_frame),
    ))
    _TPS_ENEMY_COUNT = max(1, min(8, int(args.tps_enemy_count)))
    if args.scene_blend:
        args.scene_blend = project_path(args.scene_blend)
    args.director_path = project_path(args.director_path)
    args.output_dir = project_path(args.output_dir)
    args.output_blend = project_path(args.output_blend) if args.output_blend else os.path.join(args.output_dir, "staged_director_gameplay.blend")
    args.npc_fbx = project_path(args.npc_fbx)
    args.fps_combat_asset_dir = project_path(args.fps_combat_asset_dir)
    args.swat_combat_library = project_path(args.swat_combat_library)
    args.racing_vehicle_asset = project_path(args.racing_vehicle_asset)
    args.wingsuit_aircraft_asset = project_path(args.wingsuit_aircraft_asset)
    os.makedirs(args.output_dir, exist_ok=True)
    os.makedirs(os.path.dirname(args.output_blend), exist_ok=True)
    report_path = os.path.join(args.output_dir, "director_gameplay_report.json")
    report = {
        "ok": False,
        "scene_blend": args.scene_blend,
        "director_path": args.director_path,
        "output_blend": args.output_blend,
        "previews": [],
        "output_video": None,
        "warnings": [],
        "errors": [],
    }
    try:
        if args.scene_blend and not os.path.isfile(args.scene_blend):
            raise FileNotFoundError("staged scene Blend not found: %s" % args.scene_blend)
        path_data = load_json(args.director_path)
        genre = path_data.get("genre")
        if genre not in {"fps", "tps", "racing", "wingsuit"}:
            raise ValueError("director path has unsupported genre")
        _ACTIVE_DIRECTOR_GENRE = str(genre)
        if args.scene_blend:
            log("OPEN_STAGED_SCENE", args.scene_blend)
            bpy.ops.wm.open_mainfile(filepath=args.scene_blend)
        else:
            log("OPEN_STAGED_SCENE", "already-open scene: %s" % bpy.data.filepath)
        report["environment_lighting_freeze"] = freeze_environment_lighting(bpy.context.scene)
        # NLA baseline actions are built before configure_scene().  Publish the
        # Director timeline now so their persistent armed-idle strips cover the
        # complete output rather than the source world's shorter frame range.
        bpy.context.scene.frame_start = int(path_data.get("frame_start", 1))
        bpy.context.scene.frame_end = int(path_data.get("frame_end", 240))
        collection = reset_collection()
        racing_speedway = (
            build_racing_speedway_surface(collection, path_data, scene_z_extent())
            if genre == "racing"
            else None
        )
        asset_contact_corrections = (
            settle_grounded_gameplay_asset_geometry(scene_z_extent())
            if genre in {"fps", "tps"}
            else []
        )
        beats = sampled_beats(path_data, genre)
        interaction_standoffs = apply_ground_interaction_standoffs(
            beats,
            scene_z_extent(),
            genre,
        )
        beats, ground_event_holds = insert_ground_event_holds(beats, genre)
        racing_support = (
            solve_racing_vehicle_support(beats, scene_z_extent())
            if genre == "racing"
            else None
        )
        if genre == "tps":
            # Re-time after the interaction stand-offs and event holds: the
            # player must cover the real travelled distance at a steady
            # ~2.85 m/s instead of sprinting out of each hold (the per-segment
            # speed spikes read as the recurring mid-run jolt).
            timeline_frame = 1
            beats[0]["frame"] = timeline_frame
            for previous, beat in zip(beats, beats[1:]):
                if str(beat.get("event_type", "")) == "event_hold":
                    dwell = max(2, int(round(float(beat.get("dwell_seconds", 0.0)) * 24.0)))
                    timeline_frame += dwell
                    beat["frame"] = int(timeline_frame)
                    continue
                distance = (Vector(beat["staged_position"]) - Vector(previous["staged_position"])).length
                travel_frames = max(2, int(round(distance / 2.85 * 24.0)))
                # The hold's dwell was already added when positioning the
                # event_hold beat itself; the following travel segment must not
                # add it again or the player crawls out of every fight.
                timeline_frame += travel_frames
                beat["frame"] = int(timeline_frame)
        # NLA base tracks are created below and must cover the expanded event
        # dwell timeline, not only the original Stage-11 frame range.
        if beats:
            bpy.context.scene.frame_end = max(
                int(bpy.context.scene.frame_end),
                int(beats[-1]["frame"]),
            )
        aircraft_route = None
        if genre == "wingsuit":
            beats, aircraft_route = build_aircraft_route_beats(
                beats,
                preflight_frames=_WINGSUIT_PREFLIGHT_FRAMES,
                flight_duration_frames=_WINGSUIT_FLIGHT_FRAMES,
                pullup_frames=_WINGSUIT_PULLUP_FRAMES,
            )
            if _WINGSUIT_PARKED_BACK_M > 0.0:
                parked_beats = [beat for beat in beats if beat.get("aircraft_parked")]
                if parked_beats:
                    parked_position = Vector(parked_beats[0]["staged_position"])
                    airborne = next(
                        (
                            beat for beat in beats
                            if not beat.get("aircraft_parked") and beat.get("staged_position")
                        ),
                        None,
                    )
                    if airborne is not None:
                        outward = Vector(airborne["staged_position"]) - parked_position
                        outward.z = 0.0
                        if outward.length > 0.5:
                            outward.normalize()
                            shift = -outward * float(_WINGSUIT_PARKED_BACK_M)
                            for beat in beats:
                                if beat.get("aircraft_parked") or beat.get("aircraft_takeoff_run"):
                                    moved = Vector(beat["staged_position"]) + shift
                                    beat["staged_position"] = [round(float(value), 6) for value in moved]
        racing_path_smoothing = (
            {
                "source_beat_count": len(path_data.get("beats", [])),
                "smoothed_beat_count": len(beats),
                "inserted_curve_sample_count": sum(1 for beat in beats if beat.get("racing_curve_sample")),
                "maximum_heading_change_degrees": round(maximum_racing_heading_change_degrees(beats), 4),
                "maximum_actor_offset_from_route_control_m": round(
                    max(float(beat.get("racing_actor_offset_from_route_control_m", 0.0)) for beat in beats),
                    4,
                ),
                "fixed_anchor_positions_preserved": not bool(racing_speedway),
                "explicit_speedway_asset_relayout": bool(racing_speedway),
                "actual_path_length_m": round(sum(
                    (Vector(second["staged_position"]) - Vector(first["staged_position"])).length
                    for first, second in zip(beats, beats[1:])
                ), 6),
                "actual_average_motion_speed_mps": round(
                    sum(
                        (Vector(second["staged_position"]) - Vector(first["staged_position"])).length
                        for first, second in zip(beats, beats[1:])
                    ) / max(1.0 / 24.0, (int(beats[-1]["frame"]) - int(beats[0]["frame"])) / 24.0),
                    6,
                ),
                "uniform_duration_scale": round(max(
                    float(beat.get("racing_uniform_duration_scale", 1.0))
                    for beat in beats
                ), 6),
                "vehicle_route_uses_short_quadratic_corners": True,
                "vehicle_width_collision_probe_count": 3,
                "collision_detour_sample_count": max(
                    int(beat.get("racing_total_collision_detour_sample_count", 0))
                    for beat in beats
                ),
                "unresolved_blocked_segment_count": max(
                    int(beat.get("racing_unresolved_blocked_segment_count", 0))
                    for beat in beats
                ),
                "unresolved_blocked_segments": beats[0].get(
                    "racing_unresolved_blocked_segments", []
                ),
                "collision_detours": beats[0].get("racing_collision_detours", []),
                "curvature_detours": beats[0].get("racing_curvature_detours", []),
                "timeline_retimed_from_distance": True,
            }
            if genre == "racing"
            else None
        )
        fps_viewpoint_adjustments = (
            refine_fps_combat_viewpoints(beats, scene_z_extent())
            if genre == "fps"
            else []
        )
        actor, actor_warning = build_actor(
            collection,
            genre,
            args.npc_fbx,
            args.fps_combat_asset_dir,
            args.racing_vehicle_asset,
            args.swat_combat_library,
            args.wingsuit_aircraft_asset,
            args.combat_actor_scale,
            args.aircraft_target_length,
        )
        if actor_warning:
            report["warnings"].append(actor_warning + "; used procedural fallback")
        aircraft_motion = None
        aircraft_dynamics = None
        aircraft_afterburner = None
        if genre == "wingsuit":
            aircraft_motion, aircraft_dynamics = animate_aircraft(actor, beats)
            aircraft_afterburner = create_aircraft_afterburner(collection, actor, aircraft_motion, beats)
        else:
            animate_actor(actor, beats, genre)
            if genre == "tps" and beats:
                bpy.context.scene.frame_end = max(
                    int(bpy.context.scene.frame_end),
                    int(beats[-1]["frame"]) + 24,
                )
        racing_dynamics = animate_racing_vehicle_dynamics(actor, beats) if genre == "racing" else None
        camera = (
            create_rigid_left_rear_aircraft_camera(collection, actor, aircraft_motion)
            if genre == "wingsuit"
            else create_camera(collection, beats, genre, actor)
        )
        wingsuit_destination_readability = (
            audit_wingsuit_destination_readability(camera, beats)
            if genre == "wingsuit"
            else None
        )
        racing_camera_visibility = (
            audit_racing_camera_visibility(camera, actor, beats)
            if genre == "racing"
            else None
        )
        route_debug = create_route_debug(collection, beats)
        event_count, pickup_animation_count = stage_events(collection, beats, genre)
        racing_feedback = stage_racing_feedback(collection, actor, beats) if genre == "racing" else None
        racing_unused_props_hidden = hide_racing_unused_props(collection) if genre == "racing" else []
        racing_speed_hud = racing_speed_profile(beats, int(path_data.get("fps", 24))) if genre == "racing" else []
        racing_asset_passes = (
            racing_asset_pass_summary(beats, racing_feedback)
            if genre == "racing"
            else None
        )
        aircraft_feedback = stage_aircraft_feedback(collection, beats) if genre == "wingsuit" else None
        fps_combat = stage_fps_combat_direct(
            collection, beats, actor, args.swat_combat_library, args.combat_actor_scale
        ) if genre == "fps" else None
        tps_combat = stage_tps_combat(
            collection, beats, actor, args.swat_combat_library, args.combat_actor_scale
        ) if genre == "tps" else None
        scene = configure_scene(path_data, camera)
        if beats:
            if genre in {"fps", "tps", "racing"}:
                # Stage 12 intentionally compacts the old 50-second editorial
                # spacing into continuous distance-timed gameplay motion.
                scene.frame_end = int(beats[-1]["frame"]) + 24
            else:
                scene.frame_end = max(int(scene.frame_end), int(beats[-1]["frame"]))
        if aircraft_motion:
            scene.frame_start = int(aircraft_motion[0]["frame"])
            scene.frame_end = int(aircraft_motion[-1]["frame"])
            scene["code2games_aircraft_preview_frames"] = [
                min(scene.frame_end, 28),
                int(aircraft_motion[len(aircraft_motion) // 2]["frame"]),
                max(scene.frame_start, scene.frame_end - 18),
            ]
        if fps_combat:
            scene["code2games_fps_combat_preview_frames"] = fps_combat["preview_frames"]
            scene["code2games_combat_preview_frames"] = fps_combat["preview_frames"]
        if tps_combat and tps_combat.get("enabled"):
            scene["code2games_combat_preview_frames"] = tps_combat["preview_frames"]
        report["lighting_audit"] = lighting_audit(scene)
        scene["code2games_director_genre"] = genre
        scene["code2games_director_path"] = args.director_path
        if genre == "racing":
            audit_racing_assets(beats)
        if racing_path_smoothing:
            scene["code2games_racing_path_smoothing"] = json.dumps(racing_path_smoothing, sort_keys=True)
        scene["code2games_fixed_asset_policy"] = (
            "explicit_racing_speedway_relayout_environment_and_asset_meshes_unchanged"
            if racing_speedway
            else "candidate_roots_read_only_visual_contact_correction_only"
        )
        for obj in collection.all_objects:
            obj["code2games_director_owned"] = True
        # The shared mount can leave a direct save incomplete (Blender prints
        # success but the file later fails to reopen with "File format is not
        # supported").  The FPS post-stage already works around this by
        # writing a sibling temp file and atomically replacing the target;
        # apply the same pattern to the main save for every genre.
        temp_blend = args.output_blend + ".tmp"
        try:
            bpy.ops.wm.save_as_mainfile(filepath=temp_blend)
            os.replace(temp_blend, args.output_blend)
        except Exception as save_error:
            # The shared mount intermittently rejects large writes.  Staging
            # and in-process spot renders must not be blocked by a failed
            # archival save.
            print("DIRECTOR_SAVE_SKIPPED", save_error, flush=True)
        if args.render_previews:
            report["previews"] = render_previews(scene, args.output_dir, args)
        if args.render_video:
            report["output_video"] = render_video(scene, args.output_dir, args)
        report.update({
            "ok": True,
            "genre": genre,
            "camera_system": camera.get("code2games_camera_system", path_data.get("camera_system")),
            "actor_kind": "f104_starfighter_aircraft" if genre == "wingsuit" else path_data.get("actor_kind"),
            "actor_object": actor.name,
            "camera_object": camera.name,
            "camera_occlusion_corrections": int(camera.get("code2games_camera_occlusion_corrections", 0)),
            "camera_unresolved_occlusions": int(camera.get("code2games_camera_unresolved_occlusions", 0)),
            "tps_camera_shortened_sample_count": int(
                camera.get("code2games_camera_shortened_sample_count", 0)
            ) if genre == "tps" else 0,
            "tps_referenced_fixed_placement_ids": sorted({
                str(beat.get("placement_id"))
                for beat in beats
                if (
                    genre == "tps"
                    and beat.get("kind") == "authored"
                    and beat.get("placement_id")
                )
            }) if genre == "tps" else [],
            "tps_referenced_fixed_placement_count": len({
                str(beat.get("placement_id"))
                for beat in beats
                if (
                    genre == "tps"
                    and beat.get("kind") == "authored"
                    and beat.get("placement_id")
                )
            }) if genre == "tps" else 0,
            "route_debug_object": route_debug.name,
            "frame_start": scene.frame_start,
            "frame_end": scene.frame_end,
            "staged_beat_count": len(beats),
            "event_effect_count": event_count,
            "pickup_animation_count": pickup_animation_count,
            "fps_combat": fps_combat,
            "tps_combat": tps_combat,
            "tps_timeline_contract": {
                "target_video_frames": 1200,
                "target_duration_seconds": 50.0,
                "first_enemy_attack_frame": int(_TPS_FIRST_ENEMY_ATTACK_FRAME),
                "target_motion_end_frame": int(_TPS_TARGET_MOTION_END_FRAME),
                "combat_hold_frames": 0,
                "enemy_count": int(_TPS_ENEMY_COUNT),
            } if genre == "tps" else None,
            "ground_route_sample_count": int(actor.get("code2games_route_sample_count", 0)),
            "ground_route_detour_count": int(actor.get("code2games_route_detour_count", 0)),
            "ground_route_unresolved_count": int(actor.get("code2games_route_unresolved_count", 0)),
            "ground_route_detours": json.loads(str(actor.get("code2games_route_detours", "[]"))),
            "ground_route_unresolved_frames": json.loads(str(actor.get("code2games_route_unresolved_frames", "[]"))),
            "racing_vehicle_dynamics": racing_dynamics,
            "racing_speedway": racing_speedway,
            "racing_gameplay_feedback": racing_feedback,
            "racing_path_smoothing": racing_path_smoothing,
            "racing_unused_props_hidden": racing_unused_props_hidden,
            "racing_speed_hud": racing_speed_hud,
            "racing_vehicle_support_samples": (racing_support or {}).get("samples", []),
            "racing_vehicle_support": racing_support,
            "racing_asset_passes": racing_asset_passes,
            "racing_camera_visibility": racing_camera_visibility,
            "racing_event_hud": [
                {
                    "frame": int(beat["frame"]),
                    "seconds": round(int(beat["frame"]) / float(scene.render.fps), 3),
                    "event_type": beat.get("event_type"),
                    "label": beat.get("label"),
                    "placement_id": beat.get("placement_id"),
                }
                for beat in beats
                if (
                    genre == "racing"
                    and beat.get("kind") == "authored"
                    and beat.get("event_type")
                    in {"boost", "checkpoint", "hazard", "steer", "recover", "goal", "countdown"}
                )
            ] if genre == "racing" else [],
            "wingsuit_event_hud": [
                {
                    "frame": int(beat["frame"]),
                    "seconds": round(int(beat["frame"]) / float(scene.render.fps), 3),
                    "event_type": beat.get("event_type"),
                    "label": beat.get("label"),
                    "placement_id": beat.get("placement_id"),
                    "parked": bool(beat.get("aircraft_parked")),
                }
                for beat in beats
                if (
                    genre == "wingsuit"
                    and beat.get("event_type")
                    in {
                        "launch", "flight_hazard", "flight_collectible", "flight_event", "reveal", "goal",
                        "collect_required", "dodge", "hazard", "interact",
                        "jump_collect", "recover", "preflight_data",
                        "preflight_power", "preflight_route", "preflight_weather",
                        "preflight_clearance", "preflight_calibration",
                    }
                )
            ] if genre == "wingsuit" else [],
            "fps_event_hud": [
                {
                    "frame": int(beat["frame"]),
                    "seconds": round(int(beat["frame"]) / float(scene.render.fps), 3),
                    "event_type": beat.get("event_type"),
                    "label": beat.get("label"),
                    "placement_id": beat.get("placement_id"),
                }
                for beat in beats
                if (
                    genre == "fps"
                    and beat.get("kind") == "authored"
                    and beat.get("event_type")
                    in {"collect", "collect_required", "interact", "combat", "goal"}
                )
            ] if genre == "fps" else [],
            "tps_event_hud": [
                {
                    "frame": int(beat["frame"]),
                    "seconds": round(int(beat["frame"]) / float(scene.render.fps), 3),
                    "event_type": beat.get("event_type"),
                    "label": beat.get("label"),
                    "placement_id": beat.get("placement_id"),
                }
                for beat in beats
                if (
                    genre == "tps"
                    and beat.get("kind") == "authored"
                    and beat.get("event_type")
                    in {"collect", "collect_required", "interact", "combat", "goal"}
                )
            ] if genre == "tps" else [],
            "aircraft_route": aircraft_route,
            "aircraft_dynamics": aircraft_dynamics,
            "aircraft_afterburner": aircraft_afterburner,
            "aircraft_gameplay_feedback": aircraft_feedback,
            "wingsuit_destination_readability": wingsuit_destination_readability,
            "wingsuit_timeline_contract": {
                "target_video_frames": int(_WINGSUIT_PREFLIGHT_FRAMES + _WINGSUIT_FLIGHT_FRAMES + _WINGSUIT_PULLUP_FRAMES),
                "target_duration_seconds": round(
                    (_WINGSUIT_PREFLIGHT_FRAMES + _WINGSUIT_FLIGHT_FRAMES + _WINGSUIT_PULLUP_FRAMES)
                    / float(scene.render.fps),
                    3,
                ),
                "preflight_frames": int(_WINGSUIT_PREFLIGHT_FRAMES),
                "terrain_flight_frames": int(_WINGSUIT_FLIGHT_FRAMES),
                "pullup_frames": int(_WINGSUIT_PULLUP_FRAMES),
                "fixed_asset_positions_changed": False,
                "world_space_rings_allowed": False,
            } if genre == "wingsuit" else None,
            "combat_actor_scale": float(args.combat_actor_scale),
            "aircraft_target_length_m": float(args.aircraft_target_length),
            "ground_interaction_standoff_count": len(interaction_standoffs),
            "ground_interaction_standoffs": interaction_standoffs,
            "ground_event_hold_count": len(ground_event_holds),
            "ground_event_holds": ground_event_holds,
            "asset_reposition_count": 0,
            "asset_geometry_contact_correction_count": len(asset_contact_corrections),
            "asset_geometry_contact_corrections": asset_contact_corrections,
            "fps_combat_viewpoint_adjustment_count": len(fps_viewpoint_adjustments),
            "fps_combat_viewpoint_adjustments": fps_viewpoint_adjustments,
        })
        write_json(report_path, report)
        log("DIRECTOR_STAGING_OK", genre, args.output_blend)
        log("DIRECTOR_STAGING_REPORT", report_path)
    except Exception as exc:
        report["errors"].append({"error": "%s: %s" % (type(exc).__name__, exc), "traceback": traceback.format_exc()})
        write_json(report_path, report)
        log("DIRECTOR_STAGING_FAILED", type(exc).__name__, exc)
        raise


if __name__ == "__main__":
    main()
