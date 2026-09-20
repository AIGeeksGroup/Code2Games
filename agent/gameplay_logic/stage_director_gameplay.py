import argparse
import json
import math
import os
import sys
import traceback

import bpy
import mathutils

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
COMMON_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "shared_representation"))
if COMMON_DIR not in sys.path:
    sys.path.insert(0, COMMON_DIR)
from staging_paths import get_demo_name, get_staging_root


PROJECT_ROOT = os.path.abspath(os.environ.get("CODE2WORLDS_ROOT") or os.path.join(SCRIPT_DIR, "..", ".."))
STAGING_ROOT = get_staging_root(PROJECT_ROOT)
SCENE_BLEND = os.path.join(STAGING_ROOT, "staged_scene.blend")
DIRECTOR_PATH_JSON = os.path.join(STAGING_ROOT, "director_path", "director_path.json")
OUTPUT_DIR = os.path.join(STAGING_ROOT, "director_gameplay")
FRAME_DIR = os.path.join(OUTPUT_DIR, "frames")
OUTPUT_BLEND = os.path.join(OUTPUT_DIR, "staged_director_gameplay.blend")
OUTPUT_REPORT = os.path.join(OUTPUT_DIR, "director_gameplay_report.json")
OUTPUT_MP4 = os.path.join(OUTPUT_DIR, "director_gameplay.mp4")
DEFAULT_NPC_FBX = os.path.join(PROJECT_ROOT, "assets", "mixamo", "mixamo_run.fbx")
COLLECTION_NAME = "Code2Games_Director_Gameplay"
UP = mathutils.Vector((0.0, 0.0, 1.0))
FPS = 24
FRAME_START = 1
FRAME_END = 192


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser(description="Stage an NPC and follow camera on the fixed gameplay route")
    parser.add_argument("--scene_blend", default=SCENE_BLEND)
    parser.add_argument("--director_path", default=DIRECTOR_PATH_JSON)
    parser.add_argument("--output_dir", default=OUTPUT_DIR)
    parser.add_argument("--npc_fbx", default=DEFAULT_NPC_FBX)
    parser.add_argument("--npc_height_m", type=float, default=1.7)
    parser.add_argument("--npc_yaw_offset_degrees", type=float, default=90.0)
    parser.add_argument("--actor_radius_m", type=float, default=0.42)
    parser.add_argument("--ground_clearance_m", type=float, default=0.01)
    parser.add_argument("--maximum_ground_step_m", type=float, default=0.85)
    parser.add_argument("--maximum_slope_degrees", type=float, default=45.0)
    parser.add_argument("--maximum_collision_detour_m", type=float, default=4.2)
    parser.add_argument("--camera_distance_m", type=float, default=7.5)
    parser.add_argument("--camera_height_m", type=float, default=3.8)
    parser.add_argument("--camera_lens_mm", type=float, default=36.0)
    parser.add_argument("--camera_smoothing", type=float, default=0.20)
    parser.add_argument("--camera_collision_radius_m", type=float, default=1.0)
    parser.add_argument("--camera_min_ground_clearance_m", type=float, default=1.2)
    parser.add_argument(
        "--maximum_actor_turn_degrees_per_frame", type=float, default=6.0,
        help="cap evaluated actor heading change on the final timeline",
    )
    parser.add_argument(
        "--freeze_environment_lighting", action="store_true",
        help="freeze animated lights/world at frame_start while preserving geometry",
    )
    parser.add_argument("--add_events", action="store_true", help="add optional VFX; disabled for the clean route preview")
    parser.add_argument("--render_previews", action="store_true")
    parser.add_argument("--render_video", action="store_true")
    parser.add_argument("--video_samples", type=int, default=16)
    return parser.parse_args(argv)


def project_path(value):
    value = os.path.expandvars(os.path.expanduser(str(value)))
    return os.path.normpath(value if os.path.isabs(value) else os.path.join(PROJECT_ROOT, value))


def log(message, *values):
    print(" ".join([str(message), *[str(value) for value in values]]))


def ensure_dirs():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(FRAME_DIR, exist_ok=True)


def clear_old_preview_frames():
    if not os.path.isdir(FRAME_DIR):
        return
    for name in os.listdir(FRAME_DIR):
        if name.startswith("director_gameplay_frame_") and name.lower().endswith(".png"):
            os.remove(os.path.join(FRAME_DIR, name))


def write_json(path, data):
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    temporary = os.path.abspath(path) + ".tmp"
    with open(temporary, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, os.path.abspath(path))


def save_blend_atomically(path):
    """Save to a sibling temporary Blend, then replace the destination."""
    path = os.path.abspath(path)
    temporary = path + ".tmp"
    if os.path.exists(temporary):
        os.remove(temporary)
    bpy.ops.wm.save_as_mainfile(filepath=temporary)
    os.replace(temporary, path)
    return path


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def copy_socket_value(value):
    try:
        return list(value)
    except TypeError:
        return value


def freeze_environment_lighting(scene, reference_frame=None):
    """Freeze only animated lights and World sockets at one reference frame."""
    reference_frame = int(scene.frame_start if reference_frame is None else reference_frame)
    scene.frame_set(reference_frame)
    lights = []
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
        lights.append(snapshot)
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
    for snapshot in lights:
        obj = snapshot["object"]
        matrix_world = snapshot["matrix_world"]
        if obj.animation_data:
            obj.animation_data_clear()
        if obj.data.animation_data:
            obj.data.animation_data_clear()
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
        "frozen_light_count": len(lights),
        "frozen_world_socket_count": len(world_inputs),
        "scope": "lights_and_world_only",
    }


def vec_to_list(vec, ndigits=6):
    return [round(float(vec.x), ndigits), round(float(vec.y), ndigits), round(float(vec.z), ndigits)]


def maybe_open_source_blend():
    current = os.path.normcase(os.path.abspath(bpy.data.filepath)) if bpy.data.filepath else ""
    target = os.path.normcase(os.path.abspath(SCENE_BLEND))
    if current == target:
        return "already_loaded"
    if not os.path.isfile(SCENE_BLEND):
        raise FileNotFoundError(f"staged scene not found: {SCENE_BLEND}")
    bpy.ops.wm.open_mainfile(filepath=SCENE_BLEND)
    return "opened"


def get_or_reset_collection(name=COLLECTION_NAME):
    collection = bpy.data.collections.get(name)
    if collection:
        for obj in list(collection.objects):
            bpy.data.objects.remove(obj, do_unlink=True)
        return collection
    collection = bpy.data.collections.new(name)
    bpy.context.scene.collection.children.link(collection)
    return collection


def link_to_collection(obj, collection):
    if obj.name not in collection.objects.keys():
        collection.objects.link(obj)
    for col in list(obj.users_collection):
        if col != collection:
            col.objects.unlink(obj)


def set_socket_value(node, names, value):
    for name in names:
        if name in node.inputs:
            node.inputs[name].default_value = value
            return True
    return False


def make_material(name, color, emission_strength=0.0, roughness=0.7, alpha=1.0):
    material = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    color = (color[0], color[1], color[2], alpha)
    material.diffuse_color = color
    material.use_nodes = True
    bsdf = material.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        set_socket_value(bsdf, ["Base Color"], color)
        set_socket_value(bsdf, ["Roughness"], roughness)
        if emission_strength > 0.0:
            set_socket_value(bsdf, ["Emission Color", "Emission"], color)
            set_socket_value(bsdf, ["Emission Strength"], emission_strength)
        if alpha < 1.0:
            set_socket_value(bsdf, ["Alpha"], alpha)
            material.blend_method = "BLEND"
    return material


def shade_smooth(obj):
    if obj and obj.type == "MESH":
        for polygon in obj.data.polygons:
            polygon.use_smooth = True


def add_bevel(obj, amount=0.04, segments=2):
    if obj.type != "MESH":
        return
    bevel = obj.modifiers.new("Director_Bevel", "BEVEL")
    bevel.width = amount
    bevel.segments = segments
    obj.modifiers.new("Director_WeightedNormals", "WEIGHTED_NORMAL")


def horizontal_dir(vec):
    result = mathutils.Vector((vec.x, vec.y, 0.0))
    if result.length < 1e-5:
        return mathutils.Vector((0.0, 1.0, 0.0))
    return result.normalized()


def direction_to_yaw(vec):
    direction = horizontal_dir(vec)
    return math.atan2(direction.y, direction.x)


def unwrap_angle(previous, current):
    while current - previous > math.pi:
        current -= math.tau
    while current - previous < -math.pi:
        current += math.tau
    return current


def look_at(obj, target):
    direction = mathutils.Vector(target) - obj.location
    if direction.length > 1e-5:
        obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


def set_keyframe_interpolation(obj, interpolation="BEZIER"):
    if not obj.animation_data or not obj.animation_data.action:
        return
    for fcurve in obj.animation_data.action.fcurves:
        for keyframe in fcurve.keyframe_points:
            keyframe.interpolation = interpolation


def frame_for_beat(index, beat_count):
    if beat_count <= 1:
        return FRAME_START
    return int(round(FRAME_START + (FRAME_END - FRAME_START) * index / (beat_count - 1)))


def parse_beats(path_data):
    beats = path_data.get("beats")
    if not isinstance(beats, list) or len(beats) < 2:
        raise ValueError("director_path.json must contain at least two beats")
    parsed = []
    for beat in beats:
        parsed.append({
            **beat,
            "character_position_vec": mathutils.Vector(beat["character_position"]),
            "camera_position_vec": mathutils.Vector(beat["camera_position"]),
            "look_at_vec": mathutils.Vector(beat["look_at"]),
        })
    return parsed


def director_owned_object(obj):
    if obj is None:
        return False
    return any(collection.name == COLLECTION_NAME for collection in obj.users_collection)


def gameplay_placement_object(obj):
    current = obj
    for _index in range(32):
        if current is None:
            return False
        if current.get("code2games_placement_id"):
            return True
        current = current.parent
    return False


def object_text(obj):
    names = []
    current = obj
    for _index in range(12):
        if current is None:
            break
        names.append(current.name.lower())
        current = current.parent
    names.extend(
        slot.material.name.lower()
        for slot in getattr(obj, "material_slots", [])
        if slot.material
    )
    return " ".join(names)


def is_terrain_object(obj):
    if obj is None or obj.type != "MESH":
        return False
    text = object_text(obj)
    return any(token in text for token in (
        "terrain", "ground", "landscape", "landmass", "mountain", "cliff_ground",
        "road", "track_surface", "snowfield", "seafloor", "ocean_floor",
    ))


def non_ground_scenery(obj):
    text = object_text(obj)
    return any(token in text for token in (
        "leaf", "leaves", "canopy", "grass", "fern", "flower", "bush", "shrub",
        "branch", "trunk", "tree", "vine", "roof", "wall", "fence", "building",
        "vehicle", "crate", "barrier", "boulder", "rock_prop",
    ))


def solid_route_scenery(obj):
    if obj is None or director_owned_object(obj):
        return False
    if gameplay_placement_object(obj):
        return True
    text = object_text(obj)
    if any(token in text for token in (
        "trunk", "wood", "bark", "stump", "log", "pine", "oak", "birch",
        "boulder", "rock", "wall", "fence", "building", "pillar", "column",
        "wreck", "vehicle", "crate", "barrier",
    )):
        return True
    if "tree" in text and not any(token in text for token in ("leaf", "leaves", "canopy", "grass", "fern")):
        return True
    return False


def scene_z_extent():
    values = []
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH" or director_owned_object(obj):
            continue
        try:
            values.extend((obj.matrix_world @ mathutils.Vector(corner)).z for corner in obj.bound_box)
        except Exception:
            pass
    return (min(values), max(values)) if values else (-100.0, 100.0)


def terrain_surface(x, y, fallback_z, z_extent):
    """Choose the walkable surface closest to the route's expected height."""
    depsgraph = bpy.context.evaluated_depsgraph_get()
    origin = mathutils.Vector((float(x), float(y), float(z_extent[1]) + 200.0))
    direction = mathutils.Vector((0.0, 0.0, -1.0))
    preferred = []
    fallback_hits = []
    ray_distance = max(400.0, float(z_extent[1] - z_extent[0]) + 400.0)
    for _index in range(64):
        hit, location, normal, _face, obj, _matrix = bpy.context.scene.ray_cast(
            depsgraph, origin, direction, distance=ray_distance)
        if not hit:
            break
        if (
            obj and obj.type == "MESH" and not director_owned_object(obj)
            and not gameplay_placement_object(obj) and float(normal.z) >= 0.45
            and not non_ground_scenery(obj)
        ):
            item = (abs(float(location.z) - float(fallback_z)), float(location.z), obj)
            (preferred if is_terrain_object(obj) else fallback_hits).append(item)
            if item[0] <= 1.5 and is_terrain_object(obj):
                return item[1], obj
        if float(location.z) < float(fallback_z) - 12.0:
            break
        origin = mathutils.Vector((location.x, location.y, location.z - 0.03))
    choices = preferred or fallback_hits
    if not choices:
        return float(fallback_z), None
    distance_from_expected, height, obj = min(choices, key=lambda item: (item[0], -item[1]))
    if distance_from_expected > 12.0:
        return float(fallback_z), None
    return height, obj


def terrain_height_at_xy(x, y, fallback_z, z_extent=None):
    height, obj = terrain_surface(x, y, fallback_z, z_extent or scene_z_extent())
    return height, obj.name if obj else None


def hierarchy_mesh_objects(root):
    """Return mesh descendants of a generated gameplay placement root."""
    if root is None:
        return []
    result = [root] if root.type == "MESH" else []
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
    """Sample distributed lower support vertices, avoiding a single bbox corner."""
    bounds = mesh_world_bounds(meshes)
    if not meshes or bounds[2].z <= 1e-5:
        return []
    minimum, maximum, size = bounds
    width = max(1e-5, float(maximum.x - minimum.x))
    depth = max(1e-5, float(maximum.y - minimum.y))
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
    support_band = max(0.16, min(0.48, float(size.z) * 0.16))
    return [point for point in cells.values() if point.z <= minimum.z + support_band]


def placement_roots():
    """Find each generated placement Empty exactly once.

    Placement roots are the transform authority. A mesh child may carry the
    same metadata, so descendants are skipped; only visual children are ever
    lowered by the grounding pass.
    """
    roots = []
    for obj in list(bpy.context.scene.objects):
        if obj.type != "EMPTY":
            continue
        placement_id = obj.get("code2games_placement_id")
        if not placement_id:
            continue
        parent = obj.parent
        if parent is not None and parent.get("code2games_placement_id") == placement_id:
            continue
        roots.append(obj)
    return roots


def settle_grounded_gameplay_assets(ground_clearance=0.012):
    """Lower only generated asset children until distributed supports meet terrain."""
    corrections = []
    z_extent = scene_z_extent()
    for root in placement_roots():
        meshes = hierarchy_mesh_objects(root)
        support_points = asset_support_points(meshes)
        if len(support_points) < 2:
            continue
        support_gaps = [
            float(point.z) - terrain_height_at_xy(point.x, point.y, root.matrix_world.translation.z, z_extent)[0]
            for point in support_points
        ]
        governing_gap = percentile(support_gaps, 0.90)
        if governing_gap <= float(ground_clearance) + 0.028:
            continue
        correction = min(3.0, governing_gap - float(ground_clearance))
        if correction <= 0.0:
            continue
        for child in list(root.children):
            matrix = child.matrix_world.copy()
            matrix.translation.z -= correction
            child.matrix_world = matrix
        bpy.context.view_layer.update()
        corrections.append({
            "placement_id": str(root.get("code2games_placement_id")),
            "support_point_count": len(support_points),
            "support_gap_p90_before_m": round(float(governing_gap), 6),
            "geometry_lowered_m": round(float(correction), 6),
            "placement_root_transform_changed": False,
        })
    return corrections


def apply_interaction_standoffs(beats, personal_space_m=0.72):
    """Keep generic actors outside solid gameplay assets at semantic events."""
    interaction_types = {"collect", "collect_required", "interact", "combat", "goal", "reveal"}
    adjustments = []
    for index, beat in enumerate(beats):
        if beat.get("kind") != "authored" or str(beat.get("event_type", "")) not in interaction_types:
            continue
        anchor_value = beat.get("fixed_anchor_world_xyz")
        placement_id = beat.get("placement_id")
        if not anchor_value or not placement_id:
            continue
        anchor = mathutils.Vector(anchor_value)
        previous = beats[max(0, index - 1)]["character_position_vec"]
        approach = anchor - previous
        approach.z = 0.0
        if approach.length < 1e-5 and index + 1 < len(beats):
            approach = anchor - beats[index + 1]["character_position_vec"]
            approach.z = 0.0
        approach = horizontal_dir(approach)
        root = find_placement_root(placement_id)
        meshes = hierarchy_mesh_objects(root)
        bounds = mesh_world_bounds(meshes) if meshes else None
        footprint_radius = 0.55
        if bounds:
            dimensions = bounds[2]
            footprint_radius = max(0.25, min(4.5, 0.5 * max(float(dimensions.x), float(dimensions.y))))
        stand_distance = footprint_radius + max(0.25, float(personal_space_m))
        position = anchor - approach * stand_distance
        terrain_z, _terrain_name = terrain_height_at_xy(position.x, position.y, position.z)
        position.z = terrain_z + 0.01
        original = beat["character_position_vec"].copy()
        delta = position - original
        beat["character_position_vec"] = position
        beat["character_position"] = vec_to_list(position)
        beat["position"] = vec_to_list(position)
        beat["camera_position_vec"] += delta
        beat["camera_position"] = vec_to_list(beat["camera_position_vec"])
        beat["look_at_vec"] += delta
        beat["look_at"] = vec_to_list(beat["look_at_vec"])
        beat["actor_interaction_standoff_m"] = round(float(stand_distance), 6)
        adjustments.append({
            "beat_id": beat.get("beat_id"),
            "placement_id": placement_id,
            "event_type": beat.get("event_type"),
            "original_actor_position": vec_to_list(original),
            "refined_actor_position": vec_to_list(position),
            "standoff_m": round(float(stand_distance), 6),
            "asset_anchor_unchanged": vec_to_list(anchor),
        })
    return adjustments


def sample_beats_to_terrain(beats, ground_clearance=0.01, actor_radius=0.42):
    z_extent = scene_z_extent()
    sampled_count = 0
    unsupported = []
    maximum_support_range = 0.0
    for beat in beats:
        if not beat.get("terrain_sample", True):
            continue
        position = beat["character_position_vec"]
        terrain_z, terrain_object = terrain_height_at_xy(position.x, position.y, position.z, z_extent)
        support_heights = [
            terrain_height_at_xy(position.x + dx, position.y + dy, terrain_z, z_extent)[0]
            for dx, dy in ((0.0, 0.0), (actor_radius, 0.0), (-actor_radius, 0.0),
                           (0.0, actor_radius), (0.0, -actor_radius))
        ]
        support_range = max(support_heights) - min(support_heights)
        maximum_support_range = max(maximum_support_range, support_range)
        delta_z = terrain_z + float(ground_clearance) - position.z
        position.z = terrain_z + float(ground_clearance)
        beat["camera_position_vec"].z += delta_z
        beat["look_at_vec"].z += delta_z
        beat["terrain_object"] = terrain_object
        beat["ground_support_range_m"] = round(float(support_range), 6)
        if terrain_object:
            sampled_count += 1
        else:
            unsupported.append(beat.get("beat_id"))
    return {
        "sampled_count": sampled_count,
        "unsupported_beat_ids": unsupported,
        "maximum_support_range_m": round(float(maximum_support_range), 6),
        "ground_clearance_m": float(ground_clearance),
    }


def route_segment_blocked(start, end, actor_radius=0.42):
    """Sweep a small body-width ray bundle through the staged scene."""
    start = mathutils.Vector(start)
    end = mathutils.Vector(end)
    horizontal = end - start
    horizontal.z = 0.0
    if horizontal.length < 0.08:
        return False, None
    forward = horizontal.normalized()
    side = mathutils.Vector((-forward.y, forward.x, 0.0))
    depsgraph = bpy.context.evaluated_depsgraph_get()

    def probe(side_offset, height):
        origin = start + side * side_offset + UP * height
        target = end + side * side_offset + UP * height
        ray = target - origin
        remaining = max(0.0, ray.length - 0.12)
        if remaining <= 0.0:
            return False, None
        direction = ray.normalized()
        for _index in range(32):
            hit, location, _normal, _face, obj, _matrix = bpy.context.scene.ray_cast(
                depsgraph, origin, direction, distance=remaining)
            if not hit:
                return False, None
            travelled = (mathutils.Vector(location) - origin).length
            remaining -= travelled + 0.04
            if remaining <= 0.12:
                return False, None
            if director_owned_object(obj):
                origin = mathutils.Vector(location) + direction * 0.04
                continue
            if is_terrain_object(obj) or solid_route_scenery(obj):
                return True, obj.name if obj else None
            origin = mathutils.Vector(location) + direction * 0.04
        return True, "ray_iteration_limit"

    offsets = (0.0, -float(actor_radius), float(actor_radius))
    for height in (0.45, 1.05):
        for offset in offsets:
            blocked, object_name = probe(offset, height)
            if blocked:
                return True, object_name
    return False, None


def ground_support(position, z_extent, actor_radius=0.42):
    position = mathutils.Vector(position)
    heights = [
        terrain_height_at_xy(position.x + dx, position.y + dy, position.z, z_extent)[0]
        for dx, dy in ((0.0, 0.0), (actor_radius, 0.0), (-actor_radius, 0.0),
                       (0.0, actor_radius), (0.0, -actor_radius))
    ]
    return max(heights) - min(heights), heights[0]


def interpolate_runtime_detour_beat(source, target, position, fraction, detour_index):
    """Create a non-event route sample without moving either authored anchor."""
    beat = dict(target)
    source_frame = beat_frame(source, 0, 2)
    target_frame = beat_frame(target, 1, 2)
    position = mathutils.Vector(position)
    camera = source["camera_position_vec"].lerp(target["camera_position_vec"], fraction)
    look_at = source["look_at_vec"].lerp(target["look_at_vec"], fraction)
    target_id = str(target.get("beat_id") or "beat")
    beat.update({
        "beat_id": "%s_runtime_detour_%02d" % (target_id, detour_index),
        "kind": "runtime_transit",
        "placement_id": None,
        "element_id": None,
        "event_type": "move",
        "label": "runtime collision detour",
        "frame": int(round(source_frame + (target_frame - source_frame) * fraction)),
        "position": vec_to_list(position),
        "character_position": vec_to_list(position),
        "character_position_vec": position.copy(),
        "camera_position": vec_to_list(camera),
        "camera_position_vec": camera,
        "look_at": vec_to_list(look_at),
        "look_at_vec": look_at,
        "terrain_sample": True,
        "dwell_seconds": 0.0,
        "route_position_is_final_actor_target": True,
        "runtime_collision_detour": True,
    })
    return beat


def insert_arc_collision_detours(beats, actor_radius=0.42, maximum_ground_step=0.85,
                                 maximum_slope_degrees=45.0, maximum_detour=4.2,
                                 ground_clearance=0.01):
    """Insert a smooth local bypass when an obstacle lies between two beats.

    Moving only the destination beat cannot avoid an obstacle near the middle
    of a segment: the incoming diagonal may still cross the same mesh.  This
    solver leaves authored/event anchors untouched and inserts a sine-shaped
    lateral arc between them.  Every proposed arc is accepted only after the
    same swept-body, terrain-support and slope checks used by the point solver.
    """
    if len(beats) < 2:
        return {"inserted_sample_count": 0, "detours": [], "unresolved": []}

    z_extent = scene_z_extent()
    output = [beats[0]]
    detours = []
    unresolved = []
    preferred_side = 0
    lateral_step = max(0.55, float(actor_radius))
    first_radius = max(0.75, float(actor_radius) * 1.8)
    radii = []
    radius = first_radius
    while radius <= float(maximum_detour) + 1e-6:
        radii.append(radius)
        radius += lateral_step
    if not radii or radii[-1] < float(maximum_detour):
        radii.append(float(maximum_detour))
    maximum_slope = math.tan(math.radians(float(maximum_slope_degrees)))

    for target in beats[1:]:
        source = output[-1]
        start = source["character_position_vec"].copy()
        end = target["character_position_vec"].copy()
        is_ground_route = (
            target.get("movement_mode") != "flight"
            and target.get("terrain_sample", True)
        )
        blocked, blocker = route_segment_blocked(start, end, actor_radius)
        if not is_ground_route or not blocked:
            output.append(target)
            if not blocked:
                preferred_side = 0
            continue

        horizontal = end - start
        horizontal.z = 0.0
        if horizontal.length < 0.10:
            output.append(target)
            unresolved.append({
                "beat_id": target.get("beat_id"),
                "blocker": blocker,
                "reason": "blocked zero-length segment",
            })
            continue
        forward = horizontal.normalized()
        side = mathutils.Vector((-forward.y, forward.x, 0.0))
        # Roughly one body-width between samples gives collision rays enough
        # resolution while keeping the inserted curve compact.
        interior_count = max(3, int(math.ceil(horizontal.length / max(0.70, actor_radius * 1.75))))
        signs = (preferred_side, -preferred_side) if preferred_side else (1, -1)
        accepted = None
        for detour_radius in radii:
            for sign in signs:
                points = []
                valid = True
                previous = start
                maximum_support = 0.0
                maximum_seen_slope = 0.0
                for point_index in range(1, interior_count + 1):
                    fraction = point_index / float(interior_count + 1)
                    point = start.lerp(end, fraction)
                    point += side * (math.sin(math.pi * fraction) * detour_radius * sign)
                    support_range, ground = ground_support(point, z_extent, actor_radius)
                    point.z = ground + float(ground_clearance)
                    step = point - previous
                    horizontal_step = mathutils.Vector((step.x, step.y, 0.0)).length
                    slope = abs(step.z) / max(0.05, horizontal_step)
                    segment_blocked, _segment_blocker = route_segment_blocked(
                        previous, point, actor_radius)
                    maximum_support = max(maximum_support, float(support_range))
                    maximum_seen_slope = max(maximum_seen_slope, float(slope))
                    if (
                        segment_blocked
                        or support_range > float(maximum_ground_step)
                        or slope > maximum_slope
                    ):
                        valid = False
                        break
                    points.append(point.copy())
                    previous = point
                if valid:
                    final_step = end - previous
                    final_horizontal = mathutils.Vector((final_step.x, final_step.y, 0.0)).length
                    final_slope = abs(final_step.z) / max(0.05, final_horizontal)
                    final_blocked, _final_blocker = route_segment_blocked(
                        previous, end, actor_radius)
                    maximum_seen_slope = max(maximum_seen_slope, float(final_slope))
                    valid = not final_blocked and final_slope <= maximum_slope
                if valid:
                    accepted = (
                        points, int(sign), float(detour_radius),
                        float(maximum_support), float(maximum_seen_slope),
                    )
                    break
            if accepted is not None:
                break

        if accepted is None:
            output.append(target)
            unresolved.append({
                "beat_id": target.get("beat_id"),
                "blocker": blocker,
                "reason": "no collision-free arc within detour budget",
                "maximum_detour_m": float(maximum_detour),
            })
            preferred_side = 0
            continue

        points, preferred_side, detour_radius, maximum_support, maximum_seen_slope = accepted
        for point_index, point in enumerate(points, start=1):
            fraction = point_index / float(len(points) + 1)
            output.append(interpolate_runtime_detour_beat(
                source, target, point, fraction, point_index))
        output.append(target)
        detours.append({
            "target_beat_id": target.get("beat_id"),
            "blocker": blocker,
            "detour_side": int(preferred_side),
            "detour_radius_m": round(float(detour_radius), 6),
            "inserted_sample_count": len(points),
            "maximum_support_range_m": round(float(maximum_support), 6),
            "maximum_slope_degrees": round(
                math.degrees(math.atan(maximum_seen_slope)), 4),
        })

    beats[:] = output
    return {
        "inserted_sample_count": sum(item["inserted_sample_count"] for item in detours),
        "detours": detours,
        "unresolved": unresolved,
    }


def resolve_ground_route_collisions(beats, actor_radius=0.42, maximum_ground_step=0.85,
                                    maximum_slope_degrees=45.0, maximum_detour=4.2,
                                    ground_clearance=0.01):
    """Move only actor route samples around obstacles; fixed assets stay put."""
    z_extent = scene_z_extent()
    corrections = []
    unresolved = []
    preferred_side = 0
    clear_samples = 0
    radii = []
    radius = max(0.65, float(actor_radius) * 1.6)
    while radius <= float(maximum_detour) + 1e-6:
        radii.append(radius)
        radius += max(0.55, float(actor_radius))
    if not radii or radii[-1] < float(maximum_detour):
        radii.append(float(maximum_detour))

    for index in range(1, len(beats)):
        beat = beats[index]
        if beat.get("movement_mode") == "flight" or not beat.get("terrain_sample", True):
            preferred_side = 0
            clear_samples = 0
            continue
        previous = beats[index - 1]["character_position_vec"].copy()
        candidate = beat["character_position_vec"].copy()
        support_range, ground = ground_support(candidate, z_extent, actor_radius)
        candidate.z = ground + float(ground_clearance)
        horizontal = candidate - previous
        horizontal.z = 0.0
        slope = abs(candidate.z - previous.z) / max(0.05, horizontal.length)
        blocked, blocker = route_segment_blocked(previous, candidate, actor_radius)
        valid = (
            support_range <= float(maximum_ground_step)
            and slope <= math.tan(math.radians(float(maximum_slope_degrees)))
            and not blocked
        )
        if valid:
            delta_z = candidate.z - beat["character_position_vec"].z
            beat["character_position_vec"] = candidate
            beat["camera_position_vec"].z += delta_z
            beat["look_at_vec"].z += delta_z
            clear_samples += 1
            if clear_samples >= 3:
                preferred_side = 0
            continue

        clear_samples = 0
        next_hint = (
            beats[index + 1]["character_position_vec"].copy()
            if index + 1 < len(beats) else candidate + horizontal
        )
        travel = next_hint - previous
        travel.z = 0.0
        if travel.length < 1e-5:
            travel = horizontal if horizontal.length > 1e-5 else mathutils.Vector((0.0, 1.0, 0.0))
        travel.normalize()
        side = mathutils.Vector((-travel.y, travel.x, 0.0))
        signs = (preferred_side, -preferred_side) if preferred_side else (1, -1)
        best = None
        for detour_radius in radii:
            for sign in signs:
                option = candidate + side * detour_radius * sign
                option_support, option_ground = ground_support(option, z_extent, actor_radius)
                option.z = option_ground + float(ground_clearance)
                option_horizontal = option - previous
                option_horizontal.z = 0.0
                option_slope = abs(option.z - previous.z) / max(0.05, option_horizontal.length)
                blocked_in, blocker_in = route_segment_blocked(previous, option, actor_radius)
                # The exit check prevents a one-frame sidestep that points the
                # following segment straight back through the same obstacle.
                exit_probe = option + travel * min(1.5, max(0.6, (next_hint - option).length))
                exit_ground = terrain_height_at_xy(exit_probe.x, exit_probe.y, option.z, z_extent)[0]
                exit_probe.z = exit_ground + float(ground_clearance)
                blocked_out, blocker_out = route_segment_blocked(option, exit_probe, actor_radius)
                score = (
                    int(blocked_in) + int(blocked_out),
                    max(float(option_support), 0.0),
                    float(option_slope),
                    float(detour_radius),
                )
                option_valid = (
                    not blocked_in and not blocked_out
                    and option_support <= float(maximum_ground_step)
                    and option_slope <= math.tan(math.radians(float(maximum_slope_degrees)))
                )
                if best is None or score < best[0]:
                    best = (score, option.copy(), sign, blocker_in or blocker_out, option_valid)
                if option_valid:
                    best = (score, option.copy(), sign, None, True)
                    break
            if best is not None and best[4]:
                break

        if best is None or not best[4]:
            unresolved.append({
                "beat_id": beat.get("beat_id"), "frame": int(beat.get("frame", 0)),
                "blocker": blocker or (best[3] if best else None),
                "support_range_m": round(float(support_range), 6),
                "slope_degrees": round(math.degrees(math.atan(slope)), 4),
            })
            beat["character_position_vec"] = candidate
            continue

        original = beat["character_position_vec"].copy()
        corrected = best[1]
        preferred_side = int(best[2])
        delta = corrected - original
        beat["character_position_vec"] = corrected
        beat["camera_position_vec"] += delta
        beat["look_at_vec"] += delta
        beat["position"] = vec_to_list(corrected)
        beat["character_position"] = vec_to_list(corrected)
        beat["runtime_collision_detour"] = True
        corrections.append({
            "beat_id": beat.get("beat_id"), "frame": int(beat.get("frame", 0)),
            "blocker": blocker, "detour_side": preferred_side,
            "original_position": vec_to_list(original),
            "corrected_position": vec_to_list(corrected),
            "detour_distance_m": round(float(delta.length), 6),
        })
    return {"corrections": corrections, "unresolved": unresolved}


def staged_forward(beats, index):
    origin = beats[index]["character_position_vec"]
    before = index - 1
    while before >= 0 and (origin - beats[before]["character_position_vec"]).length <= 1e-5:
        before -= 1
    after = index + 1
    while after < len(beats) and (beats[after]["character_position_vec"] - origin).length <= 1e-5:
        after += 1
    if before >= 0 and after < len(beats):
        direction = beats[after]["character_position_vec"] - beats[before]["character_position_vec"]
    elif after < len(beats):
        direction = beats[after]["character_position_vec"] - origin
    elif before >= 0:
        direction = origin - beats[before]["character_position_vec"]
    else:
        direction = mathutils.Vector((0.0, 1.0, 0.0))
    return horizontal_dir(direction)


def retime_runtime_route(beats, path_data, fps):
    """Revalidate speed and angular velocity after terrain/collision solving."""
    speed_by_mode = path_data.get("speed_by_mode_mps") or {"ground": 7.0, "drive": 14.0, "flight": 22.0}
    maximum_turn_rate = float(path_data.get("maximum_turn_degrees_per_second", 120.0))
    headings = []
    for index, beat in enumerate(beats):
        forward = staged_forward(beats, index)
        heading = math.degrees(math.atan2(forward.y, forward.x))
        if headings:
            while heading - headings[-1] > 180.0:
                heading -= 360.0
            while heading - headings[-1] < -180.0:
                heading += 360.0
        headings.append(heading)
        beat["heading_yaw_degrees"] = round(float(heading), 6)

    original_end = int(path_data.get("frame_end", beats[-1].get("frame", 1)))
    added_frames = 0
    maximum_speed = 0.0
    maximum_turn = 0.0
    previous_frame = int(beats[0].get("frame", 1))
    beats[0]["frame"] = previous_frame
    for index in range(1, len(beats)):
        beat = beats[index]
        previous = beats[index - 1]
        distance = (beat["character_position_vec"] - previous["character_position_vec"]).length
        speed = float(speed_by_mode.get(beat.get("movement_mode", "ground"), speed_by_mode.get("ground", 7.0)))
        speed *= max(0.1, float(beat.get("speed_multiplier", 1.0)))
        travel_frames = max(1, int(math.ceil(distance / max(0.1, speed) * fps)))
        turn_degrees = abs(headings[index] - headings[index - 1])
        turn_frames = max(1, int(math.ceil(turn_degrees / max(1e-5, maximum_turn_rate) * fps)))
        dwell_frames = max(0, int(round(float(previous.get("dwell_seconds", 0.0)) * fps)))
        desired = int(beat.get("frame", previous_frame + 1)) + added_frames
        # The actor is keyed in place for the previous beat's dwell.  Reserve
        # those frames before budgeting travel/turn time; otherwise the hold
        # key shortens the following movement and creates an evaluated speed
        # spike even though the sparse beat report still looks valid.
        minimum = previous_frame + dwell_frames + max(travel_frames, turn_frames)
        if desired < minimum:
            added_frames += minimum - desired
            desired = minimum
        beat["frame"] = desired
        elapsed = max(1, desired - previous_frame - dwell_frames) / float(fps)
        maximum_speed = max(maximum_speed, distance / elapsed)
        maximum_turn = max(maximum_turn, turn_degrees / elapsed)
        previous_frame = desired
    frame_end = max(original_end + added_frames, int(beats[-1]["frame"]) + fps * 2)
    return {
        "frame_end": frame_end,
        "added_frames": added_frames,
        "maximum_observed_speed_mps": round(float(maximum_speed), 6),
        "maximum_observed_turn_degrees_per_second": round(float(maximum_turn), 6),
        "speed_budget_respected": True,
        "turn_rate_budget_respected": True,
    }


def beat_frame(beat, index, beat_count):
    value = beat.get("frame")
    return int(value) if isinstance(value, (int, float)) else frame_for_beat(index, beat_count)


def mesh_world_bounds(meshes):
    points = []
    for obj in meshes:
        for corner in obj.bound_box:
            points.append(obj.matrix_world @ mathutils.Vector(corner))
    if not points:
        zero = mathutils.Vector((0.0, 0.0, 0.0))
        return zero, zero, zero
    minimum = mathutils.Vector((min(p.x for p in points), min(p.y for p in points), min(p.z for p in points)))
    maximum = mathutils.Vector((max(p.x for p in points), max(p.y for p in points), max(p.z for p in points)))
    return minimum, maximum, maximum - minimum


def imported_top_level_objects(imported):
    imported_set = set(imported)
    return [obj for obj in imported if obj.parent not in imported_set]


def make_action_cyclic(obj):
    if not obj.animation_data or not obj.animation_data.action:
        return
    for fcurve in obj.animation_data.action.fcurves:
        cycles = fcurve.modifiers.new(type="CYCLES")
        cycles.mode_before = "REPEAT"
        cycles.mode_after = "REPEAT"


def normalize_rotation_curve_winding(obj):
    """Force quaternion keys onto the shortest interpolation path.

    Imported actions and later event keys can legally use opposite signs for
    the same quaternion. Blender treats those signs as equivalent poses but
    interpolates between them along the long arc, producing an apparent spin.
    This audit is data-driven and applies to any actor/camera object; it has
    no dependency on a genre, weapon, or placement ID.
    """
    animation = getattr(obj, "animation_data", None)
    action = animation.action if animation else None
    if action is None:
        return
    curves = [curve for curve in action.fcurves if curve.data_path == "rotation_quaternion"]
    if not curves:
        return
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
        quaternion = mathutils.Quaternion(components)
        if reference is not None:
            quaternion.make_compatible(reference)
            for channel in range(4):
                by_channel[channel][frame].co[1] = quaternion[channel]
        reference = quaternion


def import_mixamo_character(collection, fbx_path, target_height, start_position):
    if not os.path.isfile(fbx_path):
        raise FileNotFoundError(f"Mixamo NPC FBX not found: {fbx_path}")

    before = set(bpy.data.objects)
    bpy.ops.import_scene.fbx(filepath=fbx_path)
    imported = [obj for obj in bpy.data.objects if obj not in before]
    armatures = [obj for obj in imported if obj.type == "ARMATURE"]
    meshes = [obj for obj in imported if obj.type == "MESH"]
    if not armatures:
        raise RuntimeError(f"Mixamo FBX contains no armature: {fbx_path}")
    if not meshes:
        raise RuntimeError(f"Mixamo FBX contains no skinned mesh: {fbx_path}")

    root = bpy.data.objects.new("Director_Player_Mixamo_Root", None)
    root.empty_display_type = "PLAIN_AXES"
    root.empty_display_size = 0.6
    collection.objects.link(root)
    for obj in imported:
        link_to_collection(obj, collection)

    armature = armatures[0]
    armature.name = "Director_Player_Mixamo_Armature"
    for index, mesh in enumerate(meshes, start=1):
        mesh.name = f"Director_Player_Mixamo_Mesh_{index:02d}"

    bpy.context.view_layer.update()
    _minimum, _maximum, size = mesh_world_bounds(meshes)
    if size.z <= 1e-5:
        raise RuntimeError("Mixamo character has a degenerate height")
    scale_factor = float(target_height) / float(size.z)
    top_level = imported_top_level_objects(imported)
    for obj in top_level:
        obj.scale = obj.scale * scale_factor
    bpy.context.view_layer.update()

    minimum, maximum, _size = mesh_world_bounds(meshes)
    foot_center = mathutils.Vector(((minimum.x + maximum.x) * 0.5, (minimum.y + maximum.y) * 0.5, minimum.z))
    for obj in top_level:
        # Bounds are in world space, so remove the foot offset in world space
        # too. Subtracting it from a rotated/scaled object's local location is
        # the old source of scene-dependent hovering and sinking.
        world_matrix = obj.matrix_world.copy()
        world_matrix.translation -= foot_center
        obj.matrix_world = world_matrix
    bpy.context.view_layer.update()

    for obj in top_level:
        world_matrix = obj.matrix_world.copy()
        obj.parent = root
        obj.matrix_world = world_matrix
    root.location = start_position
    for obj in armatures:
        make_action_cyclic(obj)

    root["source_fbx"] = fbx_path
    root["target_height_m"] = float(target_height)
    root["mixamo_armature"] = armature.name
    root["mixamo_mesh_count"] = len(meshes)
    return root


def configure_distance_synchronized_animation(root, beats, fps, actor_height):
    """Retarget one imported locomotion action to travelled world distance."""
    armature_name = root.get("mixamo_armature")
    armature = bpy.data.objects.get(armature_name) if armature_name else None
    animation_data = armature.animation_data if armature else None
    action = animation_data.action if animation_data else None
    if action is None:
        return {"configured": False, "reason": "imported armature has no active action"}
    source_start, source_end = (float(action.frame_range[0]), float(action.frame_range[1]))
    source_length = max(1.0, source_end - source_start)
    stride_distance = max(0.75, float(actor_height) * 0.92)
    segments = []
    for index in range(len(beats) - 1):
        beat = beats[index]
        following = beats[index + 1]
        start = int(beat_frame(beat, index, len(beats)))
        end = int(beat_frame(following, index + 1, len(beats)))
        if end <= start:
            continue
        dwell = min(max(0, int(round(float(beat.get("dwell_seconds", 0.0)) * fps))), max(0, end - start - 1))
        if dwell >= 2:
            segments.append(("hold", start, start + dwell, 0.0))
            start += dwell
        if end > start:
            distance = (following["character_position_vec"] - beat["character_position_vec"]).length
            segments.append(("move", start, end, float(distance)))

    merged = []
    for role, start, end, distance in segments:
        if merged and merged[-1][0] == role and merged[-1][2] == start:
            previous = merged[-1]
            merged[-1] = (role, previous[1], end, previous[3] + distance)
        else:
            merged.append((role, start, end, distance))

    action = action.copy()
    action.name = "Director_DistanceSynchronized_Locomotion"
    armature.animation_data_clear()
    armature.animation_data_create()
    track = armature.animation_data.nla_tracks.new()
    track.name = "Director_PhysicalLocomotion"
    installed = []
    for segment_index, (role, start, end, distance) in enumerate(merged):
        duration = max(1.0, float(end - start))
        strip = track.strips.new("Director_%s_%03d" % (role, segment_index), int(start), action)
        strip.action_frame_start = source_start
        if role == "hold":
            strip.action_frame_end = min(source_end, source_start + 0.05)
            strip.repeat = 1.0
            strip.scale = max(1.0, duration / max(0.05, strip.action_frame_end - strip.action_frame_start))
        else:
            strip.action_frame_end = source_end
            cycles = max(0.05, float(distance) / stride_distance)
            strip.repeat = cycles
            strip.scale = duration / max(0.05, source_length * cycles)
        strip.frame_end = int(end)
        strip.extrapolation = "NOTHING"
        strip.blend_type = "REPLACE"
        strip.blend_in = 0.0
        strip.blend_out = 0.0
        installed.append({
            "role": role, "frame_start": int(start), "frame_end": int(end),
            "travel_distance_m": round(float(distance), 6),
            "animation_repeat": round(float(strip.repeat), 6),
        })
    return {
        "configured": True,
        "armature": armature.name,
        "action": action.name,
        "stride_distance_m": round(float(stride_distance), 6),
        "distance_synchronized": True,
        "stationary_dwell_uses_held_pose": True,
        "segments": installed,
    }


def create_character(collection):
    mat_body = make_material("Director_Character_Body", (0.18, 0.42, 0.95), emission_strength=0.05, roughness=0.55)
    mat_head = make_material("Director_Character_Head", (0.95, 0.82, 0.62), roughness=0.6)
    mat_accent = make_material("Director_Character_Accent", (0.08, 0.12, 0.18), roughness=0.45)
    root = bpy.data.objects.new("Director_Player_Rig", None)
    root.empty_display_type = "PLAIN_AXES"
    root.empty_display_size = 0.6
    collection.objects.link(root)

    bpy.ops.mesh.primitive_cylinder_add(vertices=24, radius=0.28, depth=1.05, location=(0.0, 0.0, 0.75))
    body = bpy.context.object
    body.name = "Director_Player_Body"
    body.data.materials.append(mat_body)
    shade_smooth(body)
    link_to_collection(body, collection)
    body.parent = root

    bpy.ops.mesh.primitive_uv_sphere_add(segments=24, ring_count=12, radius=0.24, location=(0.0, 0.0, 1.42))
    head = bpy.context.object
    head.name = "Director_Player_Head"
    head.scale = (0.92, 0.92, 1.05)
    head.data.materials.append(mat_head)
    shade_smooth(head)
    link_to_collection(head, collection)
    head.parent = root

    for name, x, z in [("LeftFoot", -0.18, 0.18), ("RightFoot", 0.18, 0.18)]:
        bpy.ops.mesh.primitive_uv_sphere_add(segments=16, ring_count=8, radius=0.12, location=(x, -0.04, z))
        foot = bpy.context.object
        foot.name = f"Director_Player_{name}"
        foot.scale = (1.35, 0.75, 0.45)
        foot.data.materials.append(mat_accent)
        shade_smooth(foot)
        link_to_collection(foot, collection)
        foot.parent = root
    return root


def animate_character(root, beats, yaw_offset_degrees=-90.0):
    previous_yaw = None
    hold_key_count = 0
    for index, beat in enumerate(beats):
        pos = beat["character_position_vec"].copy()
        event_type = beat.get("event_type", "")
        if event_type in {"jump", "jump_collect"}:
            pos.z += float(beat.get("jump_height_m", 1.0))
        elif event_type == "dodge" and not beat.get("route_position_is_final_actor_target"):
            forward = forward_for_index(beats, index)
            pos += forward.cross(UP).normalized() * 0.75
        if isinstance(beat.get("heading_yaw_degrees"), (int, float)):
            yaw = math.radians(float(beat["heading_yaw_degrees"]) + float(yaw_offset_degrees))
        else:
            forward = forward_for_index(beats, index)
            yaw = direction_to_yaw(forward) + math.radians(float(yaw_offset_degrees))
        if previous_yaw is not None:
            yaw = unwrap_angle(previous_yaw, yaw)
        previous_yaw = yaw
        frame = beat_frame(beat, index, len(beats))
        root.location = pos
        root.rotation_euler = (0.0, 0.0, yaw)
        root.keyframe_insert(data_path="location", frame=frame)
        root.keyframe_insert(data_path="rotation_euler", frame=frame)
        dwell_frames = max(0, int(round(float(beat.get("dwell_seconds", 0.0)) * FPS)))
        if dwell_frames > 1:
            root.keyframe_insert(data_path="location", frame=frame + dwell_frames)
            root.keyframe_insert(data_path="rotation_euler", frame=frame + dwell_frames)
            hold_key_count += 1
    # Linear position keys keep the preview route from overshooting below the
    # terrain between the densely sampled route beats.
    set_keyframe_interpolation(root, "LINEAR")
    return hold_key_count


def audit_and_clamp_actor_rotation(root, frame_start, frame_end, maximum_degrees_per_frame):
    """Audit the evaluated final timeline and cap impossible heading changes."""
    scene = bpy.context.scene
    maximum_step = math.radians(max(0.1, float(maximum_degrees_per_frame)))
    original_frame = int(scene.frame_current)
    samples = []
    previous = None
    corrected_count = 0
    maximum_observed = 0.0
    for frame in range(int(frame_start), int(frame_end) + 1):
        scene.frame_set(frame)
        bpy.context.view_layer.update()
        rotation = root.evaluated_get(bpy.context.evaluated_depsgraph_get()).rotation_quaternion.copy()
        rotation.normalize()
        if previous is not None:
            rotation.make_compatible(previous)
            delta = previous.rotation_difference(rotation).angle
            maximum_observed = max(maximum_observed, math.degrees(delta))
            if delta > maximum_step + 1e-7:
                rotation = previous.slerp(rotation, maximum_step / delta)
                rotation.normalize()
                rotation.make_compatible(previous)
                corrected_count += 1
        samples.append((frame, rotation.copy()))
        previous = rotation.copy()
    if corrected_count:
        if root.animation_data and root.animation_data.action:
            action = root.animation_data.action
            for curve in list(action.fcurves):
                if curve.data_path in {"rotation_euler", "rotation_quaternion"}:
                    action.fcurves.remove(curve)
        root.rotation_mode = "QUATERNION"
        for frame, rotation in samples:
            root.rotation_quaternion = rotation
            root.keyframe_insert(data_path="rotation_quaternion", frame=frame)
        set_keyframe_interpolation(root, "LINEAR")
    scene.frame_set(original_frame)
    root["code2games_rotation_audit_max_step_degrees"] = round(float(maximum_degrees_per_frame), 6)
    root["code2games_rotation_audit_observed_max_degrees"] = round(float(maximum_observed), 6)
    root["code2games_rotation_audit_corrected_frame_count"] = int(corrected_count)
    return {
        "audited_frame_count": int(max(0, int(frame_end) - int(frame_start) + 1)),
        "maximum_allowed_degrees_per_frame": round(float(maximum_degrees_per_frame), 6),
        "maximum_observed_degrees_per_frame": round(float(maximum_observed), 6),
        "corrected_frame_count": int(corrected_count),
        "correction_applied": bool(corrected_count),
    }


def forward_for_index(beats, index):
    if index < len(beats) - 1:
        return horizontal_dir(beats[index + 1]["character_position_vec"] - beats[index]["character_position_vec"])
    return horizontal_dir(beats[index]["character_position_vec"] - beats[index - 1]["character_position_vec"])


def smoothed_route_forward(points, index, look_behind=2, look_ahead=3):
    start = points[max(0, index - look_behind)]
    end = points[min(len(points) - 1, index + look_ahead)]
    return horizontal_dir(end - start)


def set_camera_interpolation(camera):
    if not camera.animation_data or not camera.animation_data.action:
        return
    for fcurve in camera.animation_data.action.fcurves:
        for keyframe in fcurve.keyframe_points:
            if fcurve.data_path == "location":
                # Dense physical keys already describe the curve. A free
                # Bezier handle can overshoot through a wall between them.
                keyframe.interpolation = "LINEAR"
            else:
                keyframe.interpolation = "BEZIER"
                keyframe.handle_left_type = "AUTO_CLAMPED"
                keyframe.handle_right_type = "AUTO_CLAMPED"


def camera_view_blocked(camera_position, target):
    origin = mathutils.Vector(camera_position)
    target = mathutils.Vector(target)
    ray = target - origin
    if ray.length < 0.25:
        return False, None
    direction = ray.normalized()
    origin += direction * 0.08
    remaining = ray.length - 0.16
    depsgraph = bpy.context.evaluated_depsgraph_get()
    for _index in range(32):
        hit, location, _normal, _face, obj, _matrix = bpy.context.scene.ray_cast(
            depsgraph, origin, direction, distance=max(0.0, remaining))
        if not hit:
            return False, None
        travelled = (mathutils.Vector(location) - origin).length
        remaining -= travelled + 0.04
        if remaining <= 0.30:
            return False, None
        if director_owned_object(obj):
            origin = mathutils.Vector(location) + direction * 0.04
            continue
        if obj and non_ground_scenery(obj) and not solid_route_scenery(obj):
            origin = mathutils.Vector(location) + direction * 0.04
            continue
        return True, obj.name if obj else None
    return True, "ray_iteration_limit"


def camera_position_clear(position, radius=1.0):
    origin = mathutils.Vector(position)
    depsgraph = bpy.context.evaluated_depsgraph_get()
    directions = [
        mathutils.Vector((math.cos(angle), math.sin(angle), 0.0))
        for angle in (0.0, math.pi * 0.25, math.pi * 0.5, math.pi * 0.75,
                      math.pi, math.pi * 1.25, math.pi * 1.5, math.pi * 1.75)
    ] + [UP, -UP]
    for direction in directions:
        hit, _location, _normal, _face, obj, _matrix = bpy.context.scene.ray_cast(
            depsgraph, origin, direction, distance=float(radius))
        if hit and obj and not director_owned_object(obj):
            return False, obj.name
    return True, None


def select_camera_position(point, forward, target, distance_m, height_m, preferred_index,
                           collision_radius, minimum_ground_clearance, z_extent):
    side = mathutils.Vector((-forward.y, forward.x, 0.0))
    distance_m = max(0.0, float(distance_m))
    height_m = max(0.2, float(height_m))
    candidates = [
        (distance_m, 0.0, height_m),
        (distance_m * 0.82, distance_m * 0.24, height_m + 0.35),
        (distance_m * 0.82, -distance_m * 0.24, height_m + 0.35),
        (distance_m * 0.62, 0.0, height_m + 1.0),
        (distance_m * 0.42, distance_m * 0.16, height_m + 1.35),
        (distance_m * 0.42, -distance_m * 0.16, height_m + 1.35),
    ]
    order = list(range(len(candidates)))
    if preferred_index is not None and preferred_index in order:
        order = [preferred_index] + [index for index in order if index != preferred_index]
    fallback = None
    blockers = []
    for candidate_index in order:
        back, lateral, height = candidates[candidate_index]
        candidate = point - forward * back + side * lateral + UP * height
        ground, _name = terrain_height_at_xy(candidate.x, candidate.y, point.z, z_extent)
        candidate.z = max(candidate.z, ground + float(minimum_ground_clearance))
        fallback = candidate
        clear, embedded_in = camera_position_clear(candidate, collision_radius)
        blocked, occluder = camera_view_blocked(candidate, target)
        if clear and not blocked:
            return candidate, candidate_index, False, blockers
        blockers.append(embedded_in or occluder)
    return fallback, order[-1], True, blockers


def create_camera(collection, beats, distance_m, height_m, lens_mm, smoothing,
                  collision_radius=1.0, minimum_ground_clearance=1.2):
    bpy.ops.object.camera_add()
    camera = bpy.context.object
    camera.name = "Director_Gameplay_Camera"
    camera.data.lens = float(lens_mm)
    camera.data.clip_end = 100000.0
    link_to_collection(camera, collection)
    points = [beat["character_position_vec"] for beat in beats]
    smoothing = max(0.05, min(float(smoothing), 1.0))
    camera_position = None
    camera_target = None
    previous_rotation = None
    preferred_candidate = None
    unresolved = []
    candidate_switches = 0
    z_extent = scene_z_extent()
    for index, beat in enumerate(beats):
        frame = beat_frame(beat, index, len(beats))
        point = points[index]
        forward = smoothed_route_forward(points, index)
        desired_position = None
        desired_target = None
        candidate_index = None
        candidate_unresolved = True
        blockers = []
        target_variant = 0
        # Looking several metres ahead gives readable movement, but an actor
        # standing immediately before a wall or boulder can make that target
        # physically invisible from every chase position.  Retract the aim
        # point toward the actor before declaring the shot unresolved.
        for target_variant, lookahead in enumerate((2.4, 1.2, 0.0)):
            target = point + forward * lookahead + UP * 1.15
            position, selected_index, unresolved_target, target_blockers = select_camera_position(
                point, forward, target, distance_m, height_m, preferred_candidate,
                collision_radius, minimum_ground_clearance, z_extent)
            desired_position = position
            desired_target = target
            candidate_index = selected_index
            blockers.extend(target_blockers)
            candidate_unresolved = unresolved_target
            if not unresolved_target:
                break
        if preferred_candidate is not None and candidate_index != preferred_candidate:
            candidate_switches += 1
        preferred_candidate = candidate_index
        if candidate_unresolved:
            unresolved.append({
                "beat_id": beat.get("beat_id"), "frame": int(frame),
                "candidate_index": int(candidate_index),
                "target_variant": int(target_variant),
                "blockers": [name for name in blockers if name],
            })
        if camera_position is None:
            camera_position = desired_position
            camera_target = desired_target
        else:
            smoothed_position = camera_position.lerp(desired_position, smoothing)
            smoothed_target = camera_target.lerp(desired_target, min(1.0, smoothing * 1.25))
            clear, _embedded = camera_position_clear(smoothed_position, collision_radius)
            blocked, _occluder = camera_view_blocked(smoothed_position, smoothed_target)
            camera_position = smoothed_position if clear and not blocked else desired_position
            camera_target = smoothed_target if clear and not blocked else desired_target
        camera.location = camera_position
        direction = camera_target - camera.location
        rotation = direction.to_track_quat("-Z", "Y").to_euler("XYZ")
        if previous_rotation is not None:
            rotation.make_compatible(previous_rotation)
        camera.rotation_euler = rotation
        previous_rotation = rotation.copy()
        camera.keyframe_insert(data_path="location", frame=frame)
        camera.keyframe_insert(data_path="rotation_euler", frame=frame)
        beat["runtime_camera_position"] = vec_to_list(camera_position)
        beat["runtime_camera_candidate_index"] = int(candidate_index)
        beat["runtime_camera_target_variant"] = int(target_variant)
    bpy.context.scene.camera = camera
    set_camera_interpolation(camera)
    return camera, {
        "candidate_switch_count": candidate_switches,
        "unresolved_count": len(unresolved),
        "unresolved": unresolved,
        "target_fallback_count": sum(
            1 for beat in beats if int(beat.get("runtime_camera_target_variant", 0)) > 0),
        "collision_radius_m": float(collision_radius),
        "minimum_ground_clearance_m": float(minimum_ground_clearance),
    }


def repair_camera_occlusion_frames(camera, actor, distance_m, height_m,
                                   collision_radius=1.0,
                                   minimum_ground_clearance=1.2):
    """Bake a final per-frame visibility pass against evaluated actor motion.

    Beat-level endpoints can both be clear while the interpolated camera boom
    crosses a trunk or prop between them.  The final output is a rendered
    timeline, so validate that timeline directly and key only collision-free
    alternatives.  This pass is scene-generic and never hides or moves source
    geometry.
    """
    scene = bpy.context.scene
    original_frame = int(scene.frame_current)
    frames = list(range(int(scene.frame_start), int(scene.frame_end) + 1))
    points = []
    original_camera_positions = []
    for frame in frames:
        scene.frame_set(frame)
        bpy.context.view_layer.update()
        points.append(actor.matrix_world.translation.copy())
        original_camera_positions.append(camera.matrix_world.translation.copy())

    z_extent = scene_z_extent()
    preferred_candidate = None
    corrected_frames = []
    unresolved_frames = []
    candidate_switches = 0
    previous_rotation = None
    previous_position = None
    maximum_camera_step = 0.0
    for index, frame in enumerate(frames):
        scene.frame_set(frame)
        bpy.context.view_layer.update()
        point = points[index]
        forward = smoothed_route_forward(points, index, look_behind=8, look_ahead=12)
        actor_target = point + UP * 1.15
        desired_position, selected_index, unresolved, blockers = select_camera_position(
            point, forward, actor_target, distance_m, height_m,
            preferred_candidate, collision_radius,
            minimum_ground_clearance, z_extent)
        look_target = actor_target + forward * 2.4
        lookahead_blocked, _lookahead_occluder = camera_view_blocked(
            desired_position, look_target)
        target_variant = 0 if not lookahead_blocked else 2
        if lookahead_blocked:
            look_target = actor_target

        current_position = desired_position
        # Following a continuously moving target is smoother when the camera
        # advances by the smallest collision-free fraction toward its desired
        # spring-arm position.  Trying increasing fractions also avoids the
        # old one-frame side-switch teleport when an obstacle first appears.
        if previous_position is not None:
            for alpha in (0.15, 0.25, 0.40, 0.60, 0.80, 1.0):
                trial = previous_position.lerp(desired_position, alpha)
                ground, _terrain_name = terrain_height_at_xy(
                    trial.x, trial.y, point.z, z_extent)
                trial.z = max(trial.z, ground + float(minimum_ground_clearance))
                clear, _embedded_in = camera_position_clear(trial, collision_radius)
                blocked, _occluder = camera_view_blocked(trial, actor_target)
                if clear and not blocked:
                    current_position = trial
                    break

        original_position = original_camera_positions[index]
        if (current_position - original_position).length > 0.05:
            corrected_frames.append({
                "frame": int(frame),
                "candidate_index": int(selected_index),
                "target_variant": int(target_variant),
                "position_adjustment_m": round(
                    float((current_position - original_position).length), 6),
            })
        if unresolved:
            unresolved_frames.append({
                "frame": int(frame),
                "candidate_index": int(selected_index),
                "blockers": [name for name in blockers if name],
            })
        if (
            preferred_candidate is not None
            and selected_index is not None
            and selected_index != preferred_candidate
        ):
            candidate_switches += 1
        if selected_index is not None:
            preferred_candidate = selected_index
        if previous_position is not None:
            maximum_camera_step = max(
                maximum_camera_step, (current_position - previous_position).length)
        previous_position = current_position.copy()
        camera.location = current_position
        rotation = (look_target - current_position).to_track_quat("-Z", "Y").to_euler("XYZ")
        if previous_rotation is not None:
            rotation.make_compatible(previous_rotation)
        camera.rotation_euler = rotation
        previous_rotation = rotation.copy()
        camera.keyframe_insert(data_path="location", frame=frame)
        camera.keyframe_insert(data_path="rotation_euler", frame=frame)

    set_camera_interpolation(camera)
    scene.frame_set(original_frame)
    return {
        "audited_frame_count": len(frames),
        "corrected_frame_count": len(corrected_frames),
        "candidate_switch_count": int(candidate_switches),
        "maximum_camera_step_m": round(float(maximum_camera_step), 6),
        "maximum_camera_speed_mps": round(
            float(maximum_camera_step) * float(scene.render.fps), 6),
        "unresolved_count": len(unresolved_frames),
        "unresolved": unresolved_frames,
        "corrections": corrected_frames,
    }


def add_light_orb(collection, location, frame, index):
    mat = make_material("Director_Event_Orb_Glow", (0.34, 0.88, 1.0), emission_strength=2.5, roughness=0.25)
    bpy.ops.mesh.primitive_uv_sphere_add(segments=20, ring_count=10, radius=0.18, location=location)
    orb = bpy.context.object
    orb.name = f"Director_Event_CollectOrb_{index:03d}"
    orb.data.materials.append(mat)
    shade_smooth(orb)
    link_to_collection(orb, collection)
    for f, scale, hide in [(frame - 12, 1.0, False), (frame, 1.45, False), (frame + 8, 0.02, True)]:
        orb.scale = (scale, scale, scale)
        orb.hide_render = hide
        orb.hide_viewport = hide
        orb.keyframe_insert(data_path="scale", frame=max(FRAME_START, f))
        orb.keyframe_insert(data_path="hide_render", frame=max(FRAME_START, f))
        orb.keyframe_insert(data_path="hide_viewport", frame=max(FRAME_START, f))
    return orb


def add_collect_ring(collection, location, frame, index):
    material = make_material(
        "Director_Event_CollectRing_Amber",
        (1.0, 0.48, 0.08),
        emission_strength=2.2,
        roughness=0.3,
        alpha=0.72,
    )
    bpy.ops.mesh.primitive_torus_add(
        major_radius=0.5,
        minor_radius=0.035,
        major_segments=48,
        minor_segments=8,
        location=location + UP * 0.08,
    )
    ring = bpy.context.object
    ring.name = f"Director_Event_CollectRing_{index:03d}"
    ring.data.materials.append(material)
    link_to_collection(ring, collection)
    for key_frame, scale, hidden in [
        (max(FRAME_START, frame - 3), 0.35, False),
        (frame + 4, 1.3, False),
        (frame + 11, 1.9, True),
    ]:
        ring.scale = (scale, scale, scale)
        ring.hide_render = hidden
        ring.keyframe_insert(data_path="scale", frame=min(FRAME_END, key_frame))
        ring.keyframe_insert(data_path="hide_render", frame=min(FRAME_END, key_frame))
    return ring


def add_collect_flash_light(collection, location, frame, index):
    bpy.ops.object.light_add(type="POINT", location=location + UP * 0.7)
    light = bpy.context.object
    light.name = f"Director_Event_CollectFlash_{index:03d}"
    light.data.color = (1.0, 0.42, 0.08)
    light.data.shadow_soft_size = 1.4
    link_to_collection(light, collection)
    for key_frame, energy in [
        (max(FRAME_START, frame - 4), 0.0),
        (frame, 420.0),
        (min(FRAME_END, frame + 9), 0.0),
    ]:
        light.data.energy = energy
        light.data.keyframe_insert(data_path="energy", frame=key_frame)
    return light


def add_impact_ring(collection, location, frame, index):
    mat = make_material("Director_Event_Impact_Glow", (1.0, 0.45, 0.18), emission_strength=0.9, roughness=0.6, alpha=0.45)
    bpy.ops.mesh.primitive_torus_add(major_radius=0.55, minor_radius=0.025, major_segments=48, minor_segments=8, location=location + UP * 0.04)
    ring = bpy.context.object
    ring.name = f"Director_Event_ImpactRing_{index:03d}"
    ring.data.materials.append(mat)
    link_to_collection(ring, collection)
    for f, scale, hide in [(frame - 6, 0.15, False), (frame + 4, 1.15, False), (frame + 14, 1.6, True)]:
        ring.scale = (scale, scale, scale)
        ring.hide_render = hide
        ring.hide_viewport = hide
        ring.keyframe_insert(data_path="scale", frame=max(FRAME_START, f))
        ring.keyframe_insert(data_path="hide_render", frame=max(FRAME_START, f))
        ring.keyframe_insert(data_path="hide_viewport", frame=max(FRAME_START, f))
    return ring


def add_rock(collection, location, frame, index):
    mat = make_material("Director_Event_Hazard_Stone", (0.45, 0.43, 0.38), roughness=0.9)
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=1, radius=0.32, location=location + UP * 4.0)
    rock = bpy.context.object
    rock.name = f"Director_Event_FallingRock_{index:03d}"
    rock.scale = (1.0, 0.75, 0.62)
    rock.data.materials.append(mat)
    shade_smooth(rock)
    link_to_collection(rock, collection)
    for f, pos in [(frame - 16, location + UP * 4.0), (frame, location + UP * 0.34), (frame + 20, location + UP * 0.25)]:
        rock.location = pos
        rock.rotation_euler = (f * 0.08, f * 0.05, f * 0.04)
        rock.keyframe_insert(data_path="location", frame=max(FRAME_START, f))
        rock.keyframe_insert(data_path="rotation_euler", frame=max(FRAME_START, f))
    return rock


def add_goal_glow(collection, location, frame):
    mat = make_material("Director_Event_Goal_Glow", (0.55, 1.0, 0.82), emission_strength=2.2, roughness=0.35, alpha=0.55)
    bpy.ops.mesh.primitive_uv_sphere_add(segments=32, ring_count=16, radius=0.9, location=location + UP * 1.2)
    glow = bpy.context.object
    glow.name = "Director_Event_Goal_RelicGlow"
    glow.scale = (1.0, 1.0, 1.35)
    glow.data.materials.append(mat)
    shade_smooth(glow)
    link_to_collection(glow, collection)
    for f, scale in [(frame - 32, 0.55), (frame, 1.0), (FRAME_END, 1.18)]:
        glow.scale = (scale, scale, scale * 1.35)
        glow.keyframe_insert(data_path="scale", frame=max(FRAME_START, f))
    bpy.ops.object.light_add(type="POINT", location=location + UP * 1.7)
    light = bpy.context.object
    light.name = "Director_Event_Goal_PointLight"
    light.data.energy = 450.0
    light.data.color = (0.55, 1.0, 0.82)
    link_to_collection(light, collection)
    return glow


def add_reveal_light(collection, location, frame, index):
    bpy.ops.object.light_add(type="AREA", location=location + UP * 5.0)
    light = bpy.context.object
    light.name = f"Director_Event_Reveal_AreaLight_{index:03d}"
    light.data.energy = 60.0
    light.data.size = 8.0
    light.data.color = (0.75, 0.88, 1.0)
    link_to_collection(light, collection)
    light.data.keyframe_insert(data_path="energy", frame=max(FRAME_START, frame - 12))
    light.data.energy = 240.0
    light.data.keyframe_insert(data_path="energy", frame=frame)
    light.data.energy = 80.0
    light.data.keyframe_insert(data_path="energy", frame=min(FRAME_END, frame + 24))
    return light


def add_turn_marker(collection, location, forward, frame, index):
    mat = make_material("Director_Event_Turn_Glow", (0.9, 0.9, 1.0), emission_strength=0.75, roughness=0.45, alpha=0.35)
    side = forward.cross(UP).normalized()
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=location + side * 0.75 + UP * 0.18)
    marker = bpy.context.object
    marker.name = f"Director_Event_TurnFlash_{index:03d}"
    marker.dimensions = (0.12, 0.75, 0.05)
    marker.rotation_euler.z = direction_to_yaw(forward)
    marker.data.materials.append(mat)
    link_to_collection(marker, collection)
    add_bevel(marker, 0.02, 1)
    for f, scale, hide in [(frame - 8, 0.2, False), (frame, 1.0, False), (frame + 12, 0.2, True)]:
        marker.scale = (scale, scale, scale)
        marker.hide_render = hide
        marker.hide_viewport = hide
        marker.keyframe_insert(data_path="scale", frame=max(FRAME_START, f))
        marker.keyframe_insert(data_path="hide_render", frame=max(FRAME_START, f))
        marker.keyframe_insert(data_path="hide_viewport", frame=max(FRAME_START, f))
    return marker


def find_placement_root(placement_id):
    if not placement_id:
        return None
    prefix = f"C2G_{placement_id}_"
    for obj in bpy.data.objects:
        if obj.name.startswith(prefix) and obj.name.endswith("_ROOT"):
            return obj
    return None


def animate_collectible_pickup(root, frame):
    if root is None:
        return
    original = root.scale.copy()
    for key_frame, multiplier in [
        (max(FRAME_START, frame - 5), 1.0),
        (frame, 1.18),
        (min(FRAME_END, frame + 7), 0.015),
    ]:
        root.scale = original * multiplier
        root.keyframe_insert(data_path="scale", frame=key_frame)
    set_keyframe_interpolation(root, "BEZIER")


def create_events(collection, beats):
    created = []
    collected_placements = set()
    for index, beat in enumerate(beats):
        event_type = beat.get("event_type", "")
        frame = beat_frame(beat, index, len(beats))
        pos = beat["character_position_vec"]
        forward = forward_for_index(beats, index)
        side = forward.cross(UP).normalized()
        placement_id = beat.get("placement_id")
        is_jump_collect = event_type == "jump" and "collect" in str(beat.get("label", "")).lower()
        is_collect = event_type == "collect" or is_jump_collect
        if is_collect and placement_id not in collected_placements:
            collected_placements.add(placement_id)
            asset_root = find_placement_root(placement_id)
            effect_position = asset_root.matrix_world.translation.copy() if asset_root else pos.copy()
            animate_collectible_pickup(asset_root, frame)
            created.append(add_collect_ring(collection, effect_position, frame, len(created)))
            created.append(add_collect_flash_light(collection, effect_position, frame, len(created)))
        elif event_type == "hazard":
            created.append(add_impact_ring(collection, pos + forward * 1.1, frame, len(created)))
            created.append(add_rock(collection, pos + forward * 1.1 + side * 0.35, frame, len(created)))
        elif event_type == "goal":
            created.append(add_goal_glow(collection, pos + forward * 2.2, frame))
        elif event_type == "reveal":
            created.append(add_reveal_light(collection, pos, frame, index))
        elif event_type == "turn":
            created.append(add_turn_marker(collection, pos, forward, frame, index))
        elif event_type == "jump":
            created.append(add_impact_ring(collection, pos - forward * 0.3, frame - 10, len(created)))
        elif event_type == "start_run":
            created.append(add_impact_ring(collection, pos, frame + 4, len(created)))
    return created


def configure_render_for_stills(path, camera):
    scene = bpy.context.scene
    scene.camera = camera
    scene.render.filepath = path
    scene.render.resolution_x = 1280
    scene.render.resolution_y = 720
    scene.render.film_transparent = False
    scene.render.image_settings.file_format = "PNG"
    # Keep the staged scene's original engine, lights, world and color
    # management.  Infinigen scenes can become almost black when forced from
    # their authored Cycles setup into Eevee.
    if scene.render.engine == "CYCLES":
        scene.cycles.samples = min(int(getattr(scene.cycles, "samples", 32)), 24)
        if hasattr(scene.cycles, "use_denoising"):
            scene.cycles.use_denoising = True


def render_frame_previews(camera):
    paths = []
    span = FRAME_END - FRAME_START
    preview_frames = sorted({
        FRAME_START,
        FRAME_START + round(span * 0.25),
        FRAME_START + round(span * 0.50),
        FRAME_START + round(span * 0.75),
        FRAME_END,
    })
    for frame in preview_frames:
        path = os.path.join(FRAME_DIR, f"director_gameplay_frame_{frame:04d}.png")
        bpy.context.scene.frame_set(frame)
        configure_render_for_stills(path, camera)
        log("RENDER_GAMEPLAY_FRAME", path)
        bpy.ops.render.render(write_still=True)
        paths.append(path)
    return paths


def configure_render_for_video(video_samples):
    scene = bpy.context.scene
    scene.frame_start = FRAME_START
    scene.frame_end = FRAME_END
    scene.render.fps = FPS
    scene.render.resolution_x = 1280
    scene.render.resolution_y = 720
    scene.render.filepath = OUTPUT_MP4
    scene.render.film_transparent = False
    # Preserve the render engine and lighting authored in staged_scene.blend.
    if scene.render.engine == "CYCLES":
        scene.cycles.samples = max(1, int(video_samples))
        if hasattr(scene.cycles, "use_denoising"):
            scene.cycles.use_denoising = True
    try:
        scene.render.image_settings.file_format = "FFMPEG"
        scene.render.ffmpeg.format = "MPEG4"
        scene.render.ffmpeg.codec = "H264"
        scene.render.ffmpeg.constant_rate_factor = "MEDIUM"
    except Exception:
        pass


def render_video(video_samples):
    configure_render_for_video(video_samples)
    log("RENDER_GAMEPLAY_VIDEO", OUTPUT_MP4)
    bpy.ops.render.render(animation=True)


def main():
    global SCENE_BLEND, DIRECTOR_PATH_JSON, OUTPUT_DIR, FRAME_DIR
    global OUTPUT_BLEND, OUTPUT_REPORT, OUTPUT_MP4, FPS, FRAME_START, FRAME_END

    args = parse_args()
    SCENE_BLEND = project_path(args.scene_blend)
    DIRECTOR_PATH_JSON = project_path(args.director_path)
    OUTPUT_DIR = project_path(args.output_dir)
    FRAME_DIR = os.path.join(OUTPUT_DIR, "frames")
    OUTPUT_BLEND = os.path.join(OUTPUT_DIR, "staged_director_gameplay.blend")
    OUTPUT_REPORT = os.path.join(OUTPUT_DIR, "director_gameplay_report.json")
    OUTPUT_MP4 = os.path.join(OUTPUT_DIR, "director_gameplay.mp4")
    npc_fbx = project_path(args.npc_fbx)
    if min(
        args.npc_height_m, args.actor_radius_m, args.maximum_ground_step_m,
        args.maximum_slope_degrees, args.maximum_collision_detour_m,
        args.camera_collision_radius_m, args.camera_min_ground_clearance_m,
    ) <= 0.0:
        raise ValueError("physical actor, terrain, detour, and camera limits must be positive")
    if args.ground_clearance_m < 0.0:
        raise ValueError("ground_clearance_m may not be negative")
    ensure_dirs()
    report = {
        "ok": False,
        "demo_name": get_demo_name() or None,
        "scene_blend": SCENE_BLEND,
        "director_path_json": DIRECTOR_PATH_JSON,
        "output_dir": OUTPUT_DIR,
    }
    try:
        open_status = maybe_open_source_blend()
        if not os.path.exists(DIRECTOR_PATH_JSON):
            raise FileNotFoundError(f"director path not found: {DIRECTOR_PATH_JSON}")
        director_path = load_json(DIRECTOR_PATH_JSON)
        FPS = int(director_path.get("fps", FPS))
        FRAME_START = int(director_path.get("frame_start", FRAME_START))
        FRAME_END = int(director_path.get("frame_end", FRAME_END))
        beats = parse_beats(director_path)
        asset_contact_corrections = settle_grounded_gameplay_assets(args.ground_clearance_m)
        interaction_standoff_report = apply_interaction_standoffs(beats)
        terrain_report = sample_beats_to_terrain(
            beats, args.ground_clearance_m, args.actor_radius_m)
        arc_detour_report = insert_arc_collision_detours(
            beats,
            actor_radius=args.actor_radius_m,
            maximum_ground_step=args.maximum_ground_step_m,
            maximum_slope_degrees=args.maximum_slope_degrees,
            maximum_detour=args.maximum_collision_detour_m,
            ground_clearance=args.ground_clearance_m,
        )
        collision_report = resolve_ground_route_collisions(
            beats,
            actor_radius=args.actor_radius_m,
            maximum_ground_step=args.maximum_ground_step_m,
            maximum_slope_degrees=args.maximum_slope_degrees,
            maximum_detour=args.maximum_collision_detour_m,
            ground_clearance=args.ground_clearance_m,
        )
        collision_report["arc_detours"] = arc_detour_report
        runtime_motion_report = retime_runtime_route(beats, director_path, FPS)
        FRAME_END = int(runtime_motion_report["frame_end"])
        bpy.context.scene.frame_start = FRAME_START
        bpy.context.scene.frame_end = FRAME_END
        bpy.context.scene.render.fps = FPS
        lighting_report = (
            freeze_environment_lighting(bpy.context.scene, FRAME_START)
            if args.freeze_environment_lighting else
            {"enabled": False, "scope": "lights_and_world_only"}
        )
        collection = get_or_reset_collection()
        character = import_mixamo_character(collection, npc_fbx, args.npc_height_m, beats[0]["character_position_vec"])
        # FBX importers may overwrite the scene FPS with the source clip rate
        # (commonly 30).  All route timing above is expressed in director FPS,
        # so restore it before animation and saving or the evaluated actor will
        # move 25% faster than the validated 24-fps budget.
        bpy.context.scene.render.fps = FPS
        hold_key_count = animate_character(character, beats, args.npc_yaw_offset_degrees)
        animation_report = configure_distance_synchronized_animation(
            character, beats, FPS, args.npc_height_m)
        rotation_report = audit_and_clamp_actor_rotation(
            character,
            FRAME_START,
            FRAME_END,
            args.maximum_actor_turn_degrees_per_frame,
        )
        normalize_rotation_curve_winding(character)
        camera, camera_collision_report = create_camera(
            collection,
            beats,
            args.camera_distance_m,
            args.camera_height_m,
            args.camera_lens_mm,
            args.camera_smoothing,
            args.camera_collision_radius_m,
            args.camera_min_ground_clearance_m,
        )
        camera_collision_report["per_frame_audit"] = repair_camera_occlusion_frames(
            camera,
            character,
            args.camera_distance_m,
            args.camera_height_m,
            args.camera_collision_radius_m,
            args.camera_min_ground_clearance_m,
        )
        normalize_rotation_curve_winding(camera)
        event_objects = create_events(collection, beats) if args.add_events else []
        bpy.context.scene.frame_set(FRAME_START)
        save_blend_atomically(OUTPUT_BLEND)
        if args.render_previews:
            clear_old_preview_frames()
        frame_paths = render_frame_previews(camera) if args.render_previews else []
        if args.render_video:
            if os.path.isfile(OUTPUT_MP4):
                os.remove(OUTPUT_MP4)
            render_video(args.video_samples)
        report.update({
            "ok": True,
            "open_status": open_status,
            "render_engine": bpy.context.scene.render.engine,
            "beat_count": len(beats),
            "terrain_sampled_count": terrain_report["sampled_count"],
            "terrain_contact": terrain_report,
            "asset_geometry_contact_corrections": asset_contact_corrections,
            "asset_geometry_contact_correction_count": len(asset_contact_corrections),
            "interaction_standoffs": interaction_standoff_report,
            "interaction_standoff_count": len(interaction_standoff_report),
            "route_collision_solver": collision_report,
            "runtime_motion_validation": runtime_motion_report,
            "event_count": len(event_objects),
            "character_object": character.name,
            "character_fbx": npc_fbx,
            "character_height_m": float(args.npc_height_m),
            "character_ground_clearance_m": float(args.ground_clearance_m),
            "character_hold_key_count": int(hold_key_count),
            "character_animation": animation_report,
            "character_rotation_audit": rotation_report,
            "environment_lighting": lighting_report,
            "camera_object": camera.name,
            "camera_distance_m": float(args.camera_distance_m),
            "camera_height_m": float(args.camera_height_m),
            "camera_lens_mm": float(args.camera_lens_mm),
            "camera_smoothing": float(args.camera_smoothing),
            "camera_collision_solver": camera_collision_report,
            "output_blend": OUTPUT_BLEND,
            "output_mp4": OUTPUT_MP4 if args.render_video else None,
            "frame_previews": frame_paths,
            "render_video": bool(args.render_video),
            "video_samples": int(args.video_samples) if args.render_video else None,
        })
        log("DIRECTOR_GAMEPLAY_BLEND", OUTPUT_BLEND)
        log("DIRECTOR_GAMEPLAY_REPORT", OUTPUT_REPORT)
        log("DIRECTOR_RENDER_ENGINE", bpy.context.scene.render.engine)
        if args.render_video:
            log("DIRECTOR_GAMEPLAY_MP4", OUTPUT_MP4)
        log("DIRECTOR_GAMEPLAY_BEAT_COUNT", len(beats))
    except Exception as exc:
        report.update({
            "ok": False,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        })
        log("DIRECTOR_GAMEPLAY_ERROR", exc)
        log(traceback.format_exc())
        error = exc
    else:
        error = None
    finally:
        write_json(OUTPUT_REPORT, report)
        log("DIRECTOR_GAMEPLAY_REPORT", OUTPUT_REPORT)
    if error is not None:
        raise error


if __name__ == "__main__":
    main()
