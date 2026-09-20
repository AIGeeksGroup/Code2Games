import argparse
import json
import math
import os
import sys

import bpy
from mathutils import Matrix, Vector

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
COMMON_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", "shared_representation"))
if COMMON_DIR not in sys.path:
    sys.path.insert(0, COMMON_DIR)

from staging_paths import get_packet_dir


PROJECT_ROOT = os.path.abspath(os.getcwd())
PACKET_DIR = get_packet_dir(PROJECT_ROOT)
METADATA_JSON = os.path.join(PACKET_DIR, "default_camera_metadata.json")
GAME_RULE_PLAN_JSON = os.path.join(PACKET_DIR, "game_rule_plan.json")
OUTPUT_JSON = os.path.join(PACKET_DIR, "placement_candidates.json")
OUTPUT_REPORT = os.path.join(PACKET_DIR, "placement_candidates_report.json")

DEFAULT_SAMPLE_COLS = 48
DEFAULT_SAMPLE_ROWS = 27
V_MIN = 0.05
V_MAX = 0.95
DEFAULT_MAX_CANDIDATES = 450
MAX_UNIQUE_HITS_PER_RAY = 4
MAX_RAYCAST_STEPS = 24
RAY_EPSILON = 0.05
MIN_DISTANCE_FROM_CAMERA = 1.0
MAX_DISTANCE_FROM_CAMERA = 180.0
DEDUP_DISTANCE = 1.25
SUPPORT_TEST_RADII_M = (0.35, 0.75, 1.25, 2.0, 3.0)
SUPPORT_RING_SAMPLES = 8
SUPPORT_MAX_CENTER_TILT_DEGREES = 18.0
SUPPORT_MAX_SAMPLE_TILT_DEGREES = 70.0
SUPPORT_RAY_HALF_HEIGHT_M = 40.0
SUPPORT_RAY_EPSILON_M = 0.03
SUPPORT_MAX_RAY_HITS = 12
DEFAULT_COLLECTIBLE_TREE_MIN_DISTANCE = 1.0
DEFAULT_COLLECTIBLE_TREE_MAX_DISTANCE = 5.0
DEFAULT_OBSTACLE_BUSH_MIN_DISTANCE = 0.75
DEFAULT_OBSTACLE_BUSH_MAX_DISTANCE = 3.5

CANDIDATE_KINDS = [
    "ground_surface",
    "occluder_anchor",
    "visual_anchor",
    "blocked_surface",
    "unknown",
]
DISTANCE_BUCKETS = [
    "too_near",
    "placement_preferred",
    "far_background",
    "very_far_background",
]
GRID_CELLS = [
    "lower_left",
    "lower_center",
    "lower_right",
    "middle_left",
    "middle_center",
    "middle_right",
    "upper_left",
    "upper_center",
    "upper_right",
]
KIND_CELL_QUOTAS = {
    "ground_surface": 8,
    "occluder_anchor": 5,
    "visual_anchor": 4,
    "blocked_surface": 2,
    "unknown": 1,
}


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def script_argv():
    if "--" in sys.argv:
        return sys.argv[sys.argv.index("--") + 1 :]
    return sys.argv[1:]


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--blend", default=None)
    parser.add_argument("--sample_cols", type=int, default=DEFAULT_SAMPLE_COLS)
    parser.add_argument("--sample_rows", type=int, default=DEFAULT_SAMPLE_ROWS)
    parser.add_argument("--max_candidates", type=int, default=DEFAULT_MAX_CANDIDATES)
    argv = script_argv()
    if "--" in sys.argv:
        args = parser.parse_args(argv)
        validate_sampling_args(parser, args)
        return args
    args, unknown = parser.parse_known_args(argv)
    mistaken_script_flags = {"--blned", "--scene", "--blend_file"}
    if any(flag in mistaken_script_flags for flag in unknown):
        parser.error("unknown script argument; use --blend <scene.blend>")
    validate_sampling_args(parser, args)
    return args


def validate_sampling_args(parser, args):
    if args.sample_cols < 12 or args.sample_cols > 160:
        parser.error("--sample_cols must be within 12..160")
    if args.sample_rows < 8 or args.sample_rows > 90:
        parser.error("--sample_rows must be within 8..90")
    if args.max_candidates < 30 or args.max_candidates > 1200:
        parser.error("--max_candidates must be within 30..1200")


def normalized_path(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def resolve_blend_path(raw_path):
    if raw_path is None:
        return None
    path = os.path.abspath(os.path.expanduser(raw_path))
    if not path.lower().endswith(".blend"):
        raise ValueError(f"--blend must point to a .blend file: {raw_path}")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"blend file does not exist: {path}")
    return normalized_path(path)


def ensure_target_blend_loaded(blend_path):
    current = bpy.data.filepath
    if blend_path is not None:
        if not current or normalized_path(current) != blend_path:
            bpy.ops.wm.open_mainfile(filepath=blend_path)
        loaded = bpy.data.filepath
        if not loaded or normalized_path(loaded) != blend_path:
            raise RuntimeError(f"requested blend was not loaded: {blend_path}")
        return normalized_path(loaded)
    if not current:
        raise RuntimeError(
            "No blend file is loaded. Pass --blend <scene.blend> or run Blender with -b <scene.blend>."
        )
    return normalized_path(current)


def load_json_if_exists(path):
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def env_distance(name, default):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(value) or value < 0.0:
        raise ValueError(f"{name} must be a finite number >= 0")
    return value


def get_distance_context_config():
    config = {
        "collectible_tree_min_distance": env_distance(
            "CODE2GAMES_COLLECTIBLE_TREE_MIN_DISTANCE", DEFAULT_COLLECTIBLE_TREE_MIN_DISTANCE
        ),
        "collectible_tree_max_distance": env_distance(
            "CODE2GAMES_COLLECTIBLE_TREE_MAX_DISTANCE", DEFAULT_COLLECTIBLE_TREE_MAX_DISTANCE
        ),
        "obstacle_bush_min_distance": env_distance(
            "CODE2GAMES_OBSTACLE_BUSH_MIN_DISTANCE", DEFAULT_OBSTACLE_BUSH_MIN_DISTANCE
        ),
        "obstacle_bush_max_distance": env_distance(
            "CODE2GAMES_OBSTACLE_BUSH_MAX_DISTANCE", DEFAULT_OBSTACLE_BUSH_MAX_DISTANCE
        ),
    }
    if config["collectible_tree_max_distance"] <= config["collectible_tree_min_distance"]:
        raise ValueError("collectible tree max distance must be greater than min distance")
    if config["obstacle_bush_max_distance"] <= config["obstacle_bush_min_distance"]:
        raise ValueError("obstacle bush max distance must be greater than min distance")
    return config


def vec_to_list(vec):
    return [round(float(vec.x), 6), round(float(vec.y), 6), round(float(vec.z), 6)]


def camera_forward(camera):
    return (camera.matrix_world.to_quaternion() @ Vector((0.0, 0.0, -1.0))).normalized()


def ray_for_screen_point(camera, scene, u, v):
    frame = list(camera.data.view_frame(scene=scene))
    bottom_pair = sorted(frame, key=lambda point: point.y)[:2]
    top_pair = sorted(frame, key=lambda point: point.y)[-2:]
    bottom_left, bottom_right = sorted(bottom_pair, key=lambda point: point.x)
    top_left, top_right = sorted(top_pair, key=lambda point: point.x)
    bottom = bottom_left.lerp(bottom_right, u)
    top = top_left.lerp(top_right, u)
    local_point = bottom.lerp(top, v)
    if camera.data.type == "ORTHO":
        return camera.matrix_world @ local_point, camera_forward(camera)
    origin = camera.matrix_world.translation
    world_point = camera.matrix_world @ local_point
    return origin, (world_point - origin).normalized()


def material_names(obj):
    if not obj or not getattr(obj, "material_slots", None):
        return []
    result = []
    for slot in obj.material_slots:
        if slot.material:
            result.append(slot.material.name.lower())
    return result


def object_text(obj):
    return " ".join([obj.name.lower() if obj else "", *material_names(obj)])


def classify_surface(obj, normal):
    text = object_text(obj)
    if any(token in text for token in ["water", "river", "ocean", "lake", "sea"]):
        return "water"
    if any(
        token in text
        for token in [
            "bush",
            "tree",
            "plant",
            "grass",
            "vegetation",
            "leaf",
            "leaves",
            "branch",
            "fern",
            "shrub",
            "trunk",
            "canopy",
        ]
    ):
        return "vegetation"
    if any(token in text for token in ["rock", "cliff", "stone", "boulder", "slope"]):
        return "rock"
    if any(token in text for token in ["terrain", "ground", "soil", "opaque terrain", "dirt", "sand", "floor", "mud"]):
        return "ground"
    if normal and normal.z > 0.68:
        return "ground"
    return "unknown"


def walkability_for(surface_type, normal):
    normal_z = normal.z if normal else 0.0
    if surface_type == "ground":
        if normal_z >= 0.78:
            return "good"
        if normal_z >= 0.55:
            return "medium"
        return "bad"
    if surface_type == "rock":
        return "medium" if normal_z >= 0.72 else "bad"
    if surface_type in {"vegetation", "water"}:
        return "bad"
    return "unknown"


def candidate_kind_for(surface_type, normal):
    normal_z = normal.z if normal else 0.0
    if surface_type == "ground":
        return "ground_surface"
    if surface_type == "vegetation":
        return "occluder_anchor"
    if surface_type == "rock":
        # A flat rock or summit shelf is a legitimate physical platform.  The
        # old classification exposed every rock only as a visual anchor, which
        # forced mountain assets onto slopes or required authored coordinates.
        return "ground_surface" if normal_z >= math.cos(math.radians(25.0)) else "visual_anchor" if normal_z >= 0.45 else "occluder_anchor"
    if surface_type == "water":
        return "blocked_surface"
    return "unknown"


def placement_tags_for_kind(candidate_kind):
    if candidate_kind == "ground_surface":
        return [
            "walkable",
            "open_surface",
            "candidate_for_goal_area",
            "candidate_for_safe_zone",
            "candidate_for_collectible_ground_anchor",
        ]
    if candidate_kind == "occluder_anchor":
        return [
            "candidate_for_obstacle",
            "candidate_for_landmark",
            "candidate_for_collectible_visual_anchor",
            "occluder",
        ]
    if candidate_kind == "visual_anchor":
        return ["candidate_for_landmark", "candidate_for_obstacle", "visual_anchor"]
    if candidate_kind == "blocked_surface":
        return ["avoid", "candidate_for_hazard_zone"]
    return []


def screen_region(u, v):
    col = "left" if u < 1.0 / 3.0 else "center" if u < 2.0 / 3.0 else "right"
    row = "lower" if v < 1.0 / 3.0 else "middle" if v < 2.0 / 3.0 else "upper"
    return f"{row}_{col}"


def distance_bucket(distance):
    if distance < 5.0:
        return "too_near"
    if distance <= 80.0:
        return "placement_preferred"
    if distance <= 120.0:
        return "far_background"
    return "very_far_background"


def object_key(obj):
    if not obj:
        return None
    original = getattr(obj, "original", None)
    return original.as_pointer() if original else obj.as_pointer()


def semantic_type_for_object(obj):
    for key in ("semantic_type", "semantic", "asset_type", "category"):
        value = obj.get(key) if hasattr(obj, "get") else None
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return ""


def classify_vegetation_object(obj):
    if not obj or obj.type != "MESH":
        return None
    name = obj.name.lower()
    semantic_type = semantic_type_for_object(obj)
    ignored = ("terrain", "grass", "fern", "flower", "particle", "camera", "light")
    if any(token in name or token in semantic_type for token in ignored):
        return None
    if semantic_type == "tree" or "treefactory" in name or "tree" in name:
        return "tree"
    if semantic_type in {"bush", "shrub"} or "bushfactory" in name or "bush" in name or "shrub" in name:
        return "bush"
    return None


def world_xy_bounds(obj, depsgraph):
    evaluated = obj.evaluated_get(depsgraph)
    corners = getattr(evaluated, "bound_box", None)
    if not corners:
        return None
    world_corners = [evaluated.matrix_world @ Vector(corner) for corner in corners]
    return {
        "min_x": min(point.x for point in world_corners),
        "max_x": max(point.x for point in world_corners),
        "min_y": min(point.y for point in world_corners),
        "max_y": max(point.y for point in world_corners),
    }


def point_to_xy_bounds_distance(point_xyz, bounds):
    x, y = float(point_xyz[0]), float(point_xyz[1])
    dx = max(bounds["min_x"] - x, 0.0, x - bounds["max_x"])
    dy = max(bounds["min_y"] - y, 0.0, y - bounds["max_y"])
    return math.sqrt(dx * dx + dy * dy)


def build_vegetation_bounds(depsgraph):
    result = {"tree": [], "bush": []}
    seen = set()
    for obj in bpy.context.scene.objects:
        vegetation_type = classify_vegetation_object(obj)
        if not vegetation_type:
            continue
        key = object_key(obj)
        if key in seen:
            continue
        seen.add(key)
        bounds = world_xy_bounds(obj, depsgraph)
        if bounds is not None:
            result[vegetation_type].append({"object_name": obj.name, "bounds": bounds})
    return result


def nearest_context_for(point_xyz, contexts):
    nearest = None
    for context in contexts:
        distance_xy = point_to_xy_bounds_distance(point_xyz, context["bounds"])
        if nearest is None or distance_xy < nearest["distance_xy"]:
            nearest = {"object_name": context["object_name"], "distance_xy": round(float(distance_xy), 6)}
    return nearest


def annotate_ground_vegetation_context(candidates, depsgraph, distance_config):
    vegetation_bounds = build_vegetation_bounds(depsgraph)
    for candidate in candidates:
        candidate["nearest_tree"] = None
        candidate["nearest_bush"] = None
        candidate["near_tree_for_collectible"] = False
        candidate["near_bush_for_obstacle"] = False
        if candidate.get("candidate_kind") != "ground_surface" or candidate.get("surface_type") != "ground":
            continue
        point = candidate["world_xyz"]
        tree = nearest_context_for(point, vegetation_bounds["tree"])
        bush = nearest_context_for(point, vegetation_bounds["bush"])
        candidate["nearest_tree"] = tree
        candidate["nearest_bush"] = bush
        walkable_ground = candidate.get("walkability") in {"good", "medium"}
        candidate["near_tree_for_collectible"] = bool(
            walkable_ground
            and tree is not None
            and distance_config["collectible_tree_min_distance"] <= tree["distance_xy"] <= distance_config["collectible_tree_max_distance"]
        )
        candidate["near_bush_for_obstacle"] = bool(
            walkable_ground
            and bush is not None
            and distance_config["obstacle_bush_min_distance"] <= bush["distance_xy"] <= distance_config["obstacle_bush_max_distance"]
        )
    return vegetation_bounds


def normal_tilt_degrees(normal):
    if normal is None or normal.length <= 1e-8:
        return 180.0
    normalized = normal.normalized()
    return math.degrees(math.acos(max(-1.0, min(1.0, float(normalized.z)))))


def vertical_support_hit(scene, depsgraph, x, y, center_z):
    """Find the first solid ground/rock hit below a footprint probe.

    Vegetation can sit above otherwise valid terrain, so downward probing
    advances through non-supporting surfaces rather than treating a canopy as
    a platform.  Water and arbitrary unknown meshes are never accepted as
    physical support.
    """
    direction = Vector((0.0, 0.0, -1.0))
    origin = Vector((float(x), float(y), float(center_z) + SUPPORT_RAY_HALF_HEIGHT_M))
    remaining = SUPPORT_RAY_HALF_HEIGHT_M * 2.0
    for _index in range(SUPPORT_MAX_RAY_HITS):
        hit, location, normal, _face_index, obj, _matrix = scene.ray_cast(
            depsgraph,
            origin,
            direction,
            distance=remaining,
        )
        if not hit:
            return None
        surface_type = classify_surface(obj, normal)
        if surface_type in {"ground", "rock"}:
            return {
                "height": float(location.z),
                "normal": normal.normalized() if normal.length > 1e-8 else Vector((0.0, 0.0, 1.0)),
                "surface_type": surface_type,
                "hit_object": obj.name if obj else "",
            }
        travelled = max(SUPPORT_RAY_EPSILON_M, float((origin - location).length) + SUPPORT_RAY_EPSILON_M)
        remaining -= travelled
        if remaining <= 0.0:
            return None
        origin = location + direction * SUPPORT_RAY_EPSILON_M
    return None


def footprint_probe_offsets(radius):
    offsets = [(0.0, 0.0)]
    for ring_radius in (float(radius) * 0.5, float(radius)):
        for sample in range(SUPPORT_RING_SAMPLES):
            angle = math.tau * float(sample) / float(SUPPORT_RING_SAMPLES)
            offsets.append((math.cos(angle) * ring_radius, math.sin(angle) * ring_radius))
    return offsets


def support_height_tolerance(radius):
    # Rigid props need a genuinely shelf-like patch.  The tolerance grows
    # mildly with footprint size but never permits a whole hillside.
    return min(0.35, 0.08 + 0.09 * float(radius))


def measure_footprint_support(candidate, radius, scene, depsgraph):
    center = Vector(candidate["world_xyz"])
    hits = []
    for dx, dy in footprint_probe_offsets(radius):
        support = vertical_support_hit(scene, depsgraph, center.x + dx, center.y + dy, center.z)
        if support is None:
            return {
                "supported": False,
                "sample_count": len(hits),
                "required_sample_count": 1 + SUPPORT_RING_SAMPLES * 2,
                "failure_reason": "missing_solid_support",
            }
        # Avoid snapping a probe to a different overhang or terrace far above
        # or below the authored candidate anchor.
        if abs(float(support["height"]) - float(center.z)) > max(1.0, float(radius) * 0.75):
            return {
                "supported": False,
                "sample_count": len(hits) + 1,
                "required_sample_count": 1 + SUPPORT_RING_SAMPLES * 2,
                "failure_reason": "disconnected_support_layer",
            }
        hits.append(support)
    heights = [item["height"] for item in hits]
    height_range = max(heights) - min(heights)
    center_tilt = normal_tilt_degrees(hits[0]["normal"])
    max_sample_tilt = max(normal_tilt_degrees(item["normal"]) for item in hits)
    tolerance = support_height_tolerance(radius)
    supported = (
        center_tilt <= SUPPORT_MAX_CENTER_TILT_DEGREES
        and max_sample_tilt <= SUPPORT_MAX_SAMPLE_TILT_DEGREES
        and height_range <= tolerance
    )
    reasons = []
    if center_tilt > SUPPORT_MAX_CENTER_TILT_DEGREES:
        reasons.append("center_too_steep")
    if max_sample_tilt > SUPPORT_MAX_SAMPLE_TILT_DEGREES:
        reasons.append("footprint_contains_steep_faces")
    if height_range > tolerance:
        reasons.append("height_variation_exceeds_tolerance")
    return {
        "supported": bool(supported),
        "sample_count": len(hits),
        "required_sample_count": len(hits),
        "height_range_m": round(float(height_range), 6),
        "height_tolerance_m": round(float(tolerance), 6),
        "center_tilt_degrees": round(float(center_tilt), 6),
        "max_sample_tilt_degrees": round(float(max_sample_tilt), 6),
        "failure_reason": "+".join(reasons) if reasons else None,
    }


def support_class_for_radius(radius):
    if radius >= 3.0:
        return "extra_large"
    if radius >= 2.0:
        return "large"
    if radius >= 1.25:
        return "medium"
    if radius >= 0.75:
        return "small"
    if radius >= 0.35:
        return "tiny"
    return "unsupported"


def annotate_footprint_support(candidates, depsgraph):
    scene = bpy.context.scene
    for candidate in candidates:
        profile = {
            "evaluated": False,
            "tested_footprint_radii_m": list(SUPPORT_TEST_RADII_M),
            "supported_footprint_radii_m": [],
            "max_supported_footprint_radius_m": 0.0,
            "support_class": "not_applicable",
            "measurements": {},
        }
        if candidate.get("candidate_kind") == "ground_surface" and candidate.get("surface_type") in {"ground", "rock"}:
            profile["evaluated"] = True
            for radius in SUPPORT_TEST_RADII_M:
                measurement = measure_footprint_support(candidate, radius, scene, depsgraph)
                profile["measurements"]["%.2f" % radius] = measurement
                if not measurement["supported"]:
                    # Support must be monotonic: if a small footprint cannot be
                    # carried, a larger one must not be advertised as valid.
                    break
                profile["supported_footprint_radii_m"].append(float(radius))
            maximum = max(profile["supported_footprint_radii_m"], default=0.0)
            profile["max_supported_footprint_radius_m"] = maximum
            profile["support_class"] = support_class_for_radius(maximum)
            if maximum > 0.0:
                candidate["placement_tags"].append("physically_supported_platform")
                candidate["placement_tags"].append("supports_%s_footprint" % profile["support_class"])
        candidate["support_profile"] = profile


def quantized_scene_axis(value, minimum, maximum, labels):
    if maximum - minimum <= 1e-6:
        return labels[len(labels) // 2]
    ratio = max(0.0, min(0.999999, (float(value) - minimum) / (maximum - minimum)))
    return labels[min(len(labels) - 1, int(ratio * len(labels)))]


def annotate_scene_regions(candidates):
    if not candidates:
        return
    xs = [float(item["world_xyz"][0]) for item in candidates]
    ys = [float(item["world_xyz"][1]) for item in candidates]
    zs = [float(item["world_xyz"][2]) for item in candidates]
    for candidate in candidates:
        x, y, z = candidate["world_xyz"]
        candidate["scene_sector"] = "%s_%s" % (
            quantized_scene_axis(x, min(xs), max(xs), ("west", "central", "east")),
            quantized_scene_axis(y, min(ys), max(ys), ("south", "middle", "north")),
        )
        candidate["elevation_band"] = quantized_scene_axis(
            z,
            min(zs),
            max(zs),
            ("low", "mid", "high"),
        )
        if candidate.get("surface_type") == "rock":
            terrain_region = "mountain_platform" if candidate.get("candidate_kind") == "ground_surface" else "mountain_slope"
        elif candidate.get("surface_type") == "ground":
            distances = [
                context.get("distance_xy")
                for context in (candidate.get("nearest_tree"), candidate.get("nearest_bush"))
                if isinstance(context, dict) and isinstance(context.get("distance_xy"), (int, float))
            ]
            if distances and min(distances) <= 0.0:
                terrain_region = "vegetation_obstructed_ground"
            elif distances and min(distances) <= 8.0:
                terrain_region = "vegetated_clearing"
            else:
                terrain_region = "open_ground"
        elif candidate.get("surface_type") == "vegetation":
            terrain_region = "vegetation_surface"
        else:
            terrain_region = candidate.get("surface_type") or "unknown"
        candidate["terrain_region"] = terrain_region


def candidate_priority(candidate):
    kind = candidate["candidate_kind"]
    walkability = candidate["walkability"]
    bucket = candidate["distance_bucket"]
    u, v = candidate["screen_uv"]
    distance = candidate["distance_from_camera"]
    normal = candidate.get("normal_xyz") or [0.0, 0.0, 0.0]
    normal_z = max(-1.0, min(1.0, float(normal[2])))
    center_tilt = math.degrees(math.acos(normal_z))
    score = 0.0
    score += {
        "ground_surface": 120.0,
        "occluder_anchor": 70.0,
        "visual_anchor": 62.0,
        "blocked_surface": 25.0,
        "unknown": 10.0,
    }.get(kind, 0.0)
    score += {"good": 28.0, "medium": 14.0, "bad": 0.0, "unknown": 2.0}.get(walkability, 0.0)
    score += {
        "too_near": -22.0,
        "placement_preferred": 30.0,
        "far_background": 6.0,
        "very_far_background": -10.0,
    }.get(bucket, 0.0)
    score += max(0.0, 1.0 - abs(v - 0.62)) * 8.0
    score += max(0.0, 1.0 - abs(u - 0.50) * 2.0) * 3.0
    score -= abs(distance - 35.0) * 0.035
    # Prefer platform-like centers before the more expensive footprint audit.
    # Dense ray sampling then gives the selector several alternatives in each
    # screen/depth region instead of one attractive but unusable slope point.
    score += max(0.0, 22.0 - center_tilt) * 1.25
    if kind == "ground_surface" and center_tilt > SUPPORT_MAX_CENTER_TILT_DEGREES:
        score -= (center_tilt - SUPPORT_MAX_CENTER_TILT_DEGREES) * 2.5
    vegetation_distances = [
        context.get("distance_xy")
        for context in (candidate.get("nearest_tree"), candidate.get("nearest_bush"))
        if isinstance(context, dict) and isinstance(context.get("distance_xy"), (int, float))
    ]
    if vegetation_distances:
        nearest_vegetation = min(float(value) for value in vegetation_distances)
        if nearest_vegetation <= 0.0:
            score -= 90.0
        elif nearest_vegetation <= 8.0:
            score += 12.0
    return score


def too_close_to_existing(candidate, selected):
    location = Vector(candidate["world_xyz"])
    for item in selected:
        if item["candidate_kind"] != candidate["candidate_kind"]:
            continue
        other = Vector(item["world_xyz"])
        if (location - other).length < DEDUP_DISTANCE:
            return True
    return False


def build_raw_hit(raw_index, ray_id, hit_index, u, v, location, normal, obj, distance):
    surface = classify_surface(obj, normal)
    walkability = walkability_for(surface, normal)
    kind = candidate_kind_for(surface, normal)
    region = screen_region(u, v)
    return {
        "raw_candidate_id": f"raw_cand_{raw_index:06d}",
        "candidate_id": "",
        "source_ray_id": ray_id,
        "hit_index": hit_index,
        "is_first_hit": hit_index == 0,
        "screen_uv": [round(float(u), 6), round(float(v), 6)],
        "screen_region": region,
        "grid_cell_3x3": region,
        "world_xyz": vec_to_list(location),
        "normal_xyz": vec_to_list(normal),
        "distance_from_camera": round(float(distance), 6),
        "distance_bucket": distance_bucket(distance),
        "hit_object": obj.name if obj else "",
        "surface_type": surface,
        "walkability": walkability,
        "candidate_kind": kind,
        "placement_tags": placement_tags_for_kind(kind),
        "visibility": "directly_visible" if hit_index == 0 else "occluded_along_ray",
    }


def trace_ray(camera, scene, depsgraph, ray_index, u, v):
    ray_id = f"ray_{ray_index:06d}"
    ray_origin, direction = ray_for_screen_point(camera, scene, u, v)
    camera_origin = camera.matrix_world.translation
    max_distance = min(float(camera.data.clip_end), MAX_DISTANCE_FROM_CAMERA)
    current_origin = ray_origin
    hit_chain = []
    seen_surfaces = set()
    repeated_surface_hits_skipped = 0

    for _step_index in range(MAX_RAYCAST_STEPS):
        if len(hit_chain) >= MAX_UNIQUE_HITS_PER_RAY:
            break
        remaining = max_distance - (current_origin - camera_origin).length
        if remaining <= 0.0:
            break
        hit, location, normal, _face_index, obj, _matrix = scene.ray_cast(
            depsgraph, current_origin, direction, distance=remaining
        )
        if not hit:
            break
        distance = (location - camera_origin).length
        if obj and obj.type not in {"CAMERA", "LIGHT"} and MIN_DISTANCE_FROM_CAMERA <= distance <= MAX_DISTANCE_FROM_CAMERA:
            key = object_key(obj)
            if key in seen_surfaces:
                repeated_surface_hits_skipped += 1
            else:
                seen_surfaces.add(key)
                hit_chain.append(
                    build_raw_hit(
                        len(hit_chain),
                        ray_id,
                        len(hit_chain),
                        u,
                        v,
                        location,
                        normal.normalized() if normal.length > 0 else Vector((0.0, 0.0, 1.0)),
                        obj,
                        distance,
                    )
                )
        current_origin = location + direction * RAY_EPSILON

    return {
        "ray_id": ray_id,
        "screen_uv": [round(float(u), 6), round(float(v), 6)],
        "grid_cell_3x3": screen_region(u, v),
        "hit_count": len(hit_chain),
        "hit_chain": hit_chain,
        "repeated_surface_hits_skipped": repeated_surface_hits_skipped,
    }


def sample_rays(camera, sample_cols, sample_rows):
    scene = bpy.context.scene
    depsgraph = bpy.context.evaluated_depsgraph_get()
    rays = []
    raw_candidates = []
    ray_index = 0
    raw_index = 0
    for row in range(sample_rows):
        v = V_MIN + (V_MAX - V_MIN) * ((row + 0.5) / sample_rows)
        for col in range(sample_cols):
            u = (col + 0.5) / sample_cols
            ray = trace_ray(camera, scene, depsgraph, ray_index, u, v)
            for hit in ray["hit_chain"]:
                hit["raw_candidate_id"] = f"raw_cand_{raw_index:06d}"
                raw_candidates.append(hit)
                raw_index += 1
            rays.append(ray)
            ray_index += 1
    return rays, raw_candidates


def add_candidate(candidate, selected, max_candidates):
    if len(selected) >= max_candidates:
        return False
    if too_close_to_existing(candidate, selected):
        return False
    selected.append(candidate)
    return True


def select_candidates(raw_candidates, max_candidates):
    buckets = {}
    for candidate in raw_candidates:
        key = (candidate["grid_cell_3x3"], candidate["candidate_kind"])
        buckets.setdefault(key, []).append(candidate)
    for items in buckets.values():
        items.sort(key=candidate_priority, reverse=True)

    selected = []
    selected_raw_ids = set()

    for cell in GRID_CELLS:
        for kind in CANDIDATE_KINDS:
            quota = KIND_CELL_QUOTAS[kind]
            kept = 0
            for candidate in buckets.get((cell, kind), []):
                if kept >= quota:
                    break
                if add_candidate(candidate, selected, max_candidates):
                    selected_raw_ids.add(candidate["raw_candidate_id"])
                    kept += 1

    remaining = [
        candidate
        for candidate in raw_candidates
        if candidate["raw_candidate_id"] not in selected_raw_ids
    ]
    remaining.sort(key=candidate_priority, reverse=True)
    for candidate in remaining:
        if len(selected) >= max_candidates:
            break
        add_candidate(candidate, selected, max_candidates)

    for index, candidate in enumerate(selected):
        candidate["candidate_id"] = f"cand_{index:06d}"
    return selected


def strip_raw_fields(candidate):
    support_profile = candidate.get("support_profile") or {}
    return {
        "candidate_id": candidate["candidate_id"],
        "source_ray_id": candidate["source_ray_id"],
        "hit_index": candidate["hit_index"],
        "is_first_hit": candidate["is_first_hit"],
        "screen_uv": candidate["screen_uv"],
        "screen_region": candidate["screen_region"],
        "grid_cell_3x3": candidate["grid_cell_3x3"],
        "world_xyz": candidate["world_xyz"],
        "normal_xyz": candidate["normal_xyz"],
        "distance_from_camera": candidate["distance_from_camera"],
        "distance_bucket": candidate["distance_bucket"],
        "hit_object": candidate["hit_object"],
        "surface_type": candidate["surface_type"],
        "walkability": candidate["walkability"],
        "candidate_kind": candidate["candidate_kind"],
        "placement_tags": candidate["placement_tags"],
        "visibility": candidate["visibility"],
        "nearest_tree": candidate.get("nearest_tree"),
        "nearest_bush": candidate.get("nearest_bush"),
        "near_tree_for_collectible": bool(candidate.get("near_tree_for_collectible")),
        "near_bush_for_obstacle": bool(candidate.get("near_bush_for_obstacle")),
        "support_profile": support_profile,
        "support_class": support_profile.get("support_class", "not_applicable"),
        "max_supported_footprint_radius_m": float(support_profile.get("max_supported_footprint_radius_m", 0.0)),
        "terrain_region": candidate.get("terrain_region"),
        "scene_sector": candidate.get("scene_sector"),
        "elevation_band": candidate.get("elevation_band"),
    }


def compact_rays(rays, selected_candidates):
    selected_by_raw_id = {item["raw_candidate_id"]: item["candidate_id"] for item in selected_candidates}
    compact = []
    for ray in rays:
        hit_chain = []
        for hit in ray["hit_chain"]:
            hit_chain.append(
                {
                    "hit_index": hit["hit_index"],
                    "is_first_hit": hit["is_first_hit"],
                    "candidate_id": selected_by_raw_id.get(hit["raw_candidate_id"], ""),
                    "world_xyz": hit["world_xyz"],
                    "normal_xyz": hit["normal_xyz"],
                    "distance_from_camera": hit["distance_from_camera"],
                    "hit_object": hit["hit_object"],
                    "surface_type": hit["surface_type"],
                    "walkability": hit["walkability"],
                    "candidate_kind": hit["candidate_kind"],
                    "placement_tags": hit["placement_tags"],
                    "visibility": hit["visibility"],
                }
            )
        compact.append(
            {
                "ray_id": ray["ray_id"],
                "screen_uv": ray["screen_uv"],
                "grid_cell_3x3": ray["grid_cell_3x3"],
                "hit_count": ray["hit_count"],
                "hit_chain": hit_chain,
            }
        )
    return compact


def count_by(items, key, values):
    counts = {value: 0 for value in values}
    for item in items:
        value = item.get(key)
        counts[value] = counts.get(value, 0) + 1
    return counts


def candidate_count_by_grid_cell(candidates):
    counts = {cell: 0 for cell in GRID_CELLS}
    for candidate in candidates:
        cell = candidate["grid_cell_3x3"]
        counts[cell] = counts.get(cell, 0) + 1
    return counts


def candidate_kind_by_grid_cell(candidates):
    counts = {cell: {kind: 0 for kind in CANDIDATE_KINDS} for cell in GRID_CELLS}
    for candidate in candidates:
        cell = candidate["grid_cell_3x3"]
        kind = candidate["candidate_kind"]
        counts.setdefault(cell, {item: 0 for item in CANDIDATE_KINDS})
        counts[cell][kind] = counts[cell].get(kind, 0) + 1
    return counts


def count_rays_by_grid_cell(rays):
    counts = {cell: 0 for cell in GRID_CELLS}
    for ray in rays:
        cell = ray["grid_cell_3x3"]
        counts[cell] = counts.get(cell, 0) + 1
    return counts


def count_hit_rays_by_grid_cell(rays):
    counts = {cell: 0 for cell in GRID_CELLS}
    for ray in rays:
        if ray["hit_count"] <= 0:
            continue
        cell = ray["grid_cell_3x3"]
        counts[cell] = counts.get(cell, 0) + 1
    return counts


def count_raw_hits_by_grid_cell(rays):
    counts = {cell: 0 for cell in GRID_CELLS}
    for ray in rays:
        cell = ray["grid_cell_3x3"]
        counts[cell] = counts.get(cell, 0) + ray["hit_count"]
    return counts


def raw_candidate_kind_by_grid_cell(raw_candidates):
    counts = {cell: {kind: 0 for kind in CANDIDATE_KINDS} for cell in GRID_CELLS}
    for candidate in raw_candidates:
        cell = candidate["grid_cell_3x3"]
        kind = candidate["candidate_kind"]
        counts.setdefault(cell, {item: 0 for item in CANDIDATE_KINDS})
        counts[cell][kind] = counts[cell].get(kind, 0) + 1
    return counts


def build_warnings(candidates, rays, raw_candidates):
    warnings = []
    if len(candidates) < 30:
        warnings.append("candidate_count_below_30")
    kind_counts = count_by(candidates, "candidate_kind", CANDIDATE_KINDS)
    if kind_counts.get("ground_surface", 0) < 30:
        warnings.append("ground_surface_count_below_30")
    if kind_counts.get("occluder_anchor", 0) < 5:
        warnings.append("occluder_anchor_count_below_5")
    supported_ground = [
        item for item in candidates
        if (item.get("support_profile") or {}).get("max_supported_footprint_radius_m", 0.0) >= 0.75
    ]
    if len(supported_ground) < 12:
        warnings.append("physically_supported_platform_count_below_12")
    terrain_regions = {item.get("terrain_region") for item in supported_ground}
    if len(terrain_regions - {None, "unknown"}) < 2:
        warnings.append("supported_platforms_lack_terrain_region_diversity")

    left_count = sum(1 for item in candidates if item["grid_cell_3x3"].endswith("_left"))
    center_count = sum(1 for item in candidates if item["grid_cell_3x3"].endswith("_center"))
    right_count = sum(1 for item in candidates if item["grid_cell_3x3"].endswith("_right"))
    if left_count == 0:
        warnings.append("left_regions_have_no_candidates")
    if center_count == 0:
        warnings.append("center_regions_have_no_candidates")
    if candidates and right_count / float(len(candidates)) > 0.8:
        warnings.append("right_side_over_80_percent")

    ray_count_by_cell = count_rays_by_grid_cell(rays)
    for cell, count in ray_count_by_cell.items():
        if count == 0:
            warnings.append(f"sampling_missing_grid_cell:{cell}")
    if not any(ray["grid_cell_3x3"].endswith("_left") for ray in rays):
        warnings.append("sampling_missing_left_rays")
    if not any(ray["grid_cell_3x3"].endswith("_center") for ray in rays):
        warnings.append("sampling_missing_center_rays")
    if not any(ray["grid_cell_3x3"].endswith("_right") for ray in rays):
        warnings.append("sampling_missing_right_rays")
    if not any(ray["grid_cell_3x3"].startswith("lower_") for ray in rays):
        warnings.append("sampling_missing_lower_rays")
    if not any(ray["grid_cell_3x3"].startswith("middle_") for ray in rays):
        warnings.append("sampling_missing_middle_rays")
    if not any(ray["grid_cell_3x3"].startswith("upper_") for ray in rays):
        warnings.append("sampling_missing_upper_rays")

    raw_left = sum(1 for item in raw_candidates if item["grid_cell_3x3"].endswith("_left"))
    raw_center = sum(1 for item in raw_candidates if item["grid_cell_3x3"].endswith("_center"))
    if raw_left > 0 and left_count == 0:
        warnings.append("left_raw_candidates_exist_but_none_selected")
    if raw_center > 0 and center_count == 0:
        warnings.append("center_raw_candidates_exist_but_none_selected")
    return warnings


def current_blend_path():
    return bpy.data.filepath


def resolve_camera(scene, metadata):
    camera = scene.camera
    camera_name = ""
    if isinstance(metadata, dict):
        camera_name = metadata.get("camera", {}).get("name", "")
    if camera is None and camera_name:
        camera = bpy.data.objects.get(camera_name)
    if camera and camera.type == "CAMERA":
        camera_metadata = metadata.get("camera", {}) if isinstance(metadata, dict) else {}
        matrix = camera_metadata.get("matrix_world")
        if matrix is not None:
            if not (
                isinstance(matrix, list)
                and len(matrix) == 4
                and all(isinstance(row, list) and len(row) == 4 for row in matrix)
            ):
                raise ValueError("default camera metadata matrix_world must be a 4x4 array")
            camera.matrix_world = Matrix(matrix)
        lens = camera_metadata.get("lens")
        if isinstance(lens, (int, float)) and float(lens) > 0:
            camera.data.lens = float(lens)
        bpy.context.evaluated_depsgraph_get().update()
        return camera
    raise RuntimeError("No scene camera found after loading blend file.")


def validate_scene_for_extraction(scene, loaded_blend):
    if not loaded_blend:
        raise RuntimeError("Refusing to extract placement candidates without a loaded blend file.")
    object_names = {obj.name for obj in scene.objects}
    mesh_count = sum(1 for obj in scene.objects if obj.type == "MESH")
    default_names = {"Cube", "Camera", "Light"}
    if object_names and object_names <= default_names:
        raise RuntimeError("Refusing to extract placement candidates from Blender's default scene.")
    if mesh_count == 0:
        raise RuntimeError("Loaded blend contains no mesh objects for placement candidate extraction.")
    return mesh_count


def main():
    args = parse_args()
    requested_blend = resolve_blend_path(args.blend) if args.blend else None
    os.makedirs(PACKET_DIR, exist_ok=True)
    report = {
        "ok": False,
        "requested_blend": requested_blend,
        "loaded_blend": None,
        "source_blend": None,
        "source_blend_exists": False,
        "source_blend_matches_request": False,
        "camera_name": "",
        "sample_grid": f"{args.sample_cols}x{args.sample_rows}",
        "total_rays": args.sample_cols * args.sample_rows,
        "ray_hits": 0,
        "multi_hit_total": 0,
        "multi_hit_ray_count": 0,
        "total_unique_hit_count": 0,
        "repeated_surface_hits_skipped": 0,
        "candidate_count": 0,
        "candidate_kind_counts": {kind: 0 for kind in CANDIDATE_KINDS},
        "candidate_count_by_grid_cell": {cell: 0 for cell in GRID_CELLS},
        "candidate_kind_by_grid_cell": {
            cell: {kind: 0 for kind in CANDIDATE_KINDS} for cell in GRID_CELLS
        },
        "ray_count_by_grid_cell": {cell: 0 for cell in GRID_CELLS},
        "ray_hit_count_by_grid_cell": {cell: 0 for cell in GRID_CELLS},
        "raw_hit_count_by_grid_cell": {cell: 0 for cell in GRID_CELLS},
        "raw_candidate_kind_counts": {kind: 0 for kind in CANDIDATE_KINDS},
        "raw_candidate_kind_by_grid_cell": {
            cell: {kind: 0 for kind in CANDIDATE_KINDS} for cell in GRID_CELLS
        },
        "selected_candidate_count_by_grid_cell": {cell: 0 for cell in GRID_CELLS},
        "selected_candidate_kind_by_grid_cell": {
            cell: {kind: 0 for kind in CANDIDATE_KINDS} for cell in GRID_CELLS
        },
        "directly_visible_count": 0,
        "occluded_along_ray_count": 0,
        "distance_bucket_counts": {bucket: 0 for bucket in DISTANCE_BUCKETS},
        "warnings": [],
    }
    try:
        loaded_blend = ensure_target_blend_loaded(requested_blend)
        scene = bpy.context.scene
        if not scene:
            raise RuntimeError("No active scene after loading blend file.")
        metadata = load_json_if_exists(METADATA_JSON)
        game_rule = load_json_if_exists(GAME_RULE_PLAN_JSON)
        distance_context_config = get_distance_context_config()
        mesh_count = validate_scene_for_extraction(scene, loaded_blend)
        camera = resolve_camera(scene, metadata)
        source_matches_request = requested_blend is None or normalized_path(bpy.data.filepath) == requested_blend
        if not source_matches_request:
            raise RuntimeError("loaded blend does not match requested --blend path")
        print("PLACEMENT_REQUESTED_BLEND", requested_blend or "<already-loaded>")
        print("PLACEMENT_LOADED_BLEND", loaded_blend)
        print("PLACEMENT_SOURCE_BLEND_MATCH", str(source_matches_request).lower())
        print("PLACEMENT_CAMERA", camera.name)
        print("PLACEMENT_SCENE_OBJECT_COUNT", len(scene.objects))
        print("PLACEMENT_SCENE_MESH_COUNT", mesh_count)
        rays, raw_candidates = sample_rays(camera, args.sample_cols, args.sample_rows)
        depsgraph = bpy.context.evaluated_depsgraph_get()
        vegetation_bounds = annotate_ground_vegetation_context(raw_candidates, depsgraph, distance_context_config)
        selected = select_candidates(raw_candidates, args.max_candidates)
        annotate_footprint_support(selected, depsgraph)
        annotate_scene_regions(selected)
        candidates = [strip_raw_fields(candidate) for candidate in selected]
        compacted_rays = compact_rays(rays, selected)

        ray_hits = sum(1 for ray in rays if ray["hit_count"] > 0)
        total_unique_hit_count = sum(ray["hit_count"] for ray in rays)
        multi_hit_ray_count = sum(1 for ray in rays if ray["hit_count"] > 1)
        repeated_surface_hits_skipped = sum(ray["repeated_surface_hits_skipped"] for ray in rays)
        if total_unique_hit_count == 0:
            raise RuntimeError("Ray sampling produced no scene hits; refusing to write an empty candidate packet.")

        data = {
            "candidate_schema_version": 3,
            "features": {
                "ground_vegetation_distance_context": True,
                "footprint_support_profile": True,
                "terrain_region_context": True,
                "scene_sector_context": True,
            },
            "source": {
                "blend": loaded_blend,
                "requested_blend": requested_blend,
                "loaded_blend": loaded_blend,
                "camera": camera.name,
                "visibility": "visible_default_camera",
                "metadata": METADATA_JSON if metadata is not None else None,
                "game_rule_plan": GAME_RULE_PLAN_JSON if game_rule is not None else None,
            },
            "sampling": {
                "sample_grid": f"{args.sample_cols}x{args.sample_rows}",
                "total_rays": args.sample_cols * args.sample_rows,
                "ray_hits": ray_hits,
                "multi_hit_total": total_unique_hit_count,
                "total_unique_hit_count": total_unique_hit_count,
                "multi_hit_ray_count": multi_hit_ray_count,
                "repeated_surface_hits_skipped": repeated_surface_hits_skipped,
                "max_unique_hits_per_ray": MAX_UNIQUE_HITS_PER_RAY,
                "max_raycast_steps": MAX_RAYCAST_STEPS,
                "ray_epsilon": RAY_EPSILON,
                "v_range": [V_MIN, V_MAX],
                "max_candidates": args.max_candidates,
                "dedup_distance": DEDUP_DISTANCE,
                "support_test_radii_m": list(SUPPORT_TEST_RADII_M),
                "support_max_center_tilt_degrees": SUPPORT_MAX_CENTER_TILT_DEGREES,
                "support_max_sample_tilt_degrees": SUPPORT_MAX_SAMPLE_TILT_DEGREES,
            },
            "distance_context_config": distance_context_config,
            "vegetation_context_object_counts": {
                "tree": len(vegetation_bounds["tree"]),
                "bush": len(vegetation_bounds["bush"]),
            },
            "rays": compacted_rays,
            "candidates": candidates,
        }
        write_json(OUTPUT_JSON, data)

        report.update(
            {
                "ok": True,
                "requested_blend": requested_blend,
                "loaded_blend": loaded_blend,
                "source_blend": loaded_blend,
                "source_blend_exists": os.path.isfile(loaded_blend),
                "source_blend_matches_request": source_matches_request,
                "camera_name": camera.name,
                "sample_grid": f"{args.sample_cols}x{args.sample_rows}",
                "total_rays": args.sample_cols * args.sample_rows,
                "ray_hits": ray_hits,
                "multi_hit_total": total_unique_hit_count,
                "multi_hit_ray_count": multi_hit_ray_count,
                "total_unique_hit_count": total_unique_hit_count,
                "repeated_surface_hits_skipped": repeated_surface_hits_skipped,
                "candidate_count": len(candidates),
                "candidate_kind_counts": count_by(candidates, "candidate_kind", CANDIDATE_KINDS),
                "candidate_count_by_grid_cell": candidate_count_by_grid_cell(candidates),
                "candidate_kind_by_grid_cell": candidate_kind_by_grid_cell(candidates),
                "ray_count_by_grid_cell": count_rays_by_grid_cell(rays),
                "ray_hit_count_by_grid_cell": count_hit_rays_by_grid_cell(rays),
                "raw_hit_count_by_grid_cell": count_raw_hits_by_grid_cell(rays),
                "raw_candidate_kind_counts": count_by(raw_candidates, "candidate_kind", CANDIDATE_KINDS),
                "raw_candidate_kind_by_grid_cell": raw_candidate_kind_by_grid_cell(raw_candidates),
                "selected_candidate_count_by_grid_cell": candidate_count_by_grid_cell(candidates),
                "selected_candidate_kind_by_grid_cell": candidate_kind_by_grid_cell(candidates),
                "directly_visible_count": sum(1 for item in candidates if item["visibility"] == "directly_visible"),
                "occluded_along_ray_count": sum(1 for item in candidates if item["visibility"] == "occluded_along_ray"),
                "distance_bucket_counts": count_by(candidates, "distance_bucket", DISTANCE_BUCKETS),
                "support_class_counts": count_by(candidates, "support_class", []),
                "terrain_region_counts": count_by(candidates, "terrain_region", []),
                "scene_sector_counts": count_by(candidates, "scene_sector", []),
                "supported_platform_count_radius_0_75m": sum(
                    1 for item in candidates
                    if (item.get("support_profile") or {}).get("max_supported_footprint_radius_m", 0.0) >= 0.75
                ),
                "supported_platform_count_radius_2_0m": sum(
                    1 for item in candidates
                    if (item.get("support_profile") or {}).get("max_supported_footprint_radius_m", 0.0) >= 2.0
                ),
                "distance_context_config": distance_context_config,
                "vegetation_context_object_counts": {
                    "tree": len(vegetation_bounds["tree"]),
                    "bush": len(vegetation_bounds["bush"]),
                },
                "ground_candidate_count_with_nearest_tree": sum(
                    1 for item in candidates if item["candidate_kind"] == "ground_surface" and item["nearest_tree"] is not None
                ),
                "ground_candidate_count_with_nearest_bush": sum(
                    1 for item in candidates if item["candidate_kind"] == "ground_surface" and item["nearest_bush"] is not None
                ),
                "collectible_eligible_ground_candidate_count": sum(
                    1 for item in candidates if item["near_tree_for_collectible"]
                ),
                "obstacle_eligible_ground_candidate_count": sum(
                    1 for item in candidates if item["near_bush_for_obstacle"]
                ),
                "warnings": build_warnings(candidates, rays, raw_candidates),
                "output_json": OUTPUT_JSON,
            }
        )
        print("PLACEMENT_CANDIDATES_JSON", OUTPUT_JSON)
        print("PLACEMENT_RAY_COUNT", len(rays))
        print("PLACEMENT_UNIQUE_HIT_COUNT", total_unique_hit_count)
        print("PLACEMENT_MULTI_HIT_RAY_COUNT", multi_hit_ray_count)
        print("PLACEMENT_CANDIDATE_COUNT", len(candidates))
    except Exception as exc:
        report.update({"ok": False, "error": str(exc)})
        print("PLACEMENT_CANDIDATE_EXTRACTION_ERROR", exc)
        raise
    finally:
        write_json(OUTPUT_REPORT, report)
        print("PLACEMENT_CANDIDATES_REPORT", OUTPUT_REPORT)


if __name__ == "__main__":
    main()
