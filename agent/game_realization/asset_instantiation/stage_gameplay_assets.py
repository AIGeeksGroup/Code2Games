"""Place generated Code2Games GLBs into the source Blender scene.

This is the canonical asset staging step for the current pipeline. It joins the
LLM-authored gameplay placement plan with the LLM-authored asset realization
plan and executes those decisions without choosing replacement locations or
inventing layout decisions in Python. Python only validates, imports, measures,
transforms, reports, renders validation previews, and saves the staged scene.
"""
import argparse
import json
import math
import os
import shutil
import sys
import traceback

import bpy
import mathutils
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Matrix, Vector


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
COMMON_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", "shared_representation"))
if COMMON_DIR not in sys.path:
    sys.path.insert(0, COMMON_DIR)
from staging_paths import get_demo_name, get_packet_dir, get_staging_root
from placement_constraints import CLIFF_NORMAL_MAX_ABS_Z, validate_realized_footprint_support

PROJECT_ROOT = os.path.abspath(os.environ.get("CODE2WORLDS_ROOT") or os.path.join(SCRIPT_DIR, "..", "..", ".."))
STAGING_ROOT = get_staging_root(PROJECT_ROOT)
PACKET_DIR = get_packet_dir(PROJECT_ROOT)
DEFAULT_SCENE = os.path.join(PROJECT_ROOT, "infinigen", "outputs", "game_scene", "fine", "scene.blend")
DEFAULT_PLACEMENT_PLAN = os.path.join(PACKET_DIR, "gameplay_placement_plan.json")
DEFAULT_CANDIDATES = os.path.join(PACKET_DIR, "placement_candidates.json")
DEFAULT_ASSET_PLAN = os.path.join(STAGING_ROOT, "asset_realization", "asset_plan.json")
DEFAULT_OUTPUT_DIR = os.path.join(STAGING_ROOT, "asset_placement")
DEFAULT_OUTPUT_BLEND = os.path.join(STAGING_ROOT, "staged_scene.blend")

ASSET_COLLECTION = "Code2Games_Gameplay_Assets"
TEMPLATE_COLLECTION = "Code2Games_Asset_Templates"
PROXY_COLLECTION = "Code2Games_Gameplay_Proxies"
PREVIEW_COLLECTION = "Code2Games_Asset_Preview_Cameras"

PROXY_TYPE_BY_ELEMENT_TYPE = {
    "collectible": "collect_trigger",
    "item_pickup": "pickup_trigger",
    "obstacle": "collision_bounds",
    "destructible": "damageable_bounds",
    "moving_platform": "moving_platform_bounds",
    "puzzle_element": "puzzle_interaction_bounds",
    "objective_object": "objective_interaction_bounds",
    "hazard_zone": "hazard_trigger",
    "goal_area": "goal_trigger",
    "safe_zone": "safe_zone_trigger",
    "capture_zone": "capture_zone_trigger",
    "checkpoint": "checkpoint_trigger",
    "event_trigger": "event_trigger",
    "interaction_point": "interaction_anchor",
    "cover_point": "cover_anchor",
    "traversal_point": "traversal_anchor",
    "navigation_anchor": "navigation_anchor",
    "camera_anchor": "camera_anchor",
    "player_spawn": "player_spawn_anchor",
    "enemy_spawn": "enemy_spawn_anchor",
    "npc_spawn": "npc_spawn_anchor",
    "vehicle_spawn": "vehicle_spawn_anchor",
    "landmark": "landmark_bounds",
}


def log(message, *values):
    print(" ".join([str(message), *[str(value) for value in values]]), flush=True)


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser(description="Stage generated gameplay assets in Blender")
    parser.add_argument(
        "--scene_blend",
        default="",
        help="source Blend override; by default use the Blend recorded in placement_candidates.json",
    )
    parser.add_argument("--placement_plan", default=DEFAULT_PLACEMENT_PLAN)
    parser.add_argument("--asset_plan", default=DEFAULT_ASSET_PLAN)
    parser.add_argument("--candidates", default=DEFAULT_CANDIDATES)
    parser.add_argument("--output_dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--output_blend", default=DEFAULT_OUTPUT_BLEND)
    parser.add_argument(
        "--ground_embed_m",
        type=float,
        default=0.0,
        help="optional manual ground embed in meters; disabled by default",
    )
    parser.add_argument(
        "--ground_clearance_m",
        type=float,
        default=0.02,
        help="small world-Z clearance for solid grounded props",
    )
    parser.add_argument(
        "--hazard_clearance_m",
        type=float,
        default=0.03,
        help="small surface-normal clearance for grounded hazard assets",
    )
    parser.add_argument(
        "--collectible_hover_m",
        type=float,
        default=0.08,
        help="small world-Z hover for grounded collectibles",
    )
    vegetation_group = parser.add_mutually_exclusive_group()
    vegetation_group.add_argument(
        "--clear_blocking_vegetation",
        dest="clear_blocking_vegetation",
        action="store_true",
        help="hide independent vegetation meshes that intersect a staged asset footprint (default)",
    )
    vegetation_group.add_argument(
        "--keep_blocking_vegetation",
        dest="clear_blocking_vegetation",
        action="store_false",
        help="preserve scene vegetation even when it intersects a staged gameplay asset",
    )
    parser.set_defaults(clear_blocking_vegetation=True)
    parser.add_argument(
        "--vegetation_clearance_margin_m",
        type=float,
        default=0.25,
        help="extra XY margin used by --clear_blocking_vegetation",
    )
    parser.add_argument(
        "--collectible_spin_revolutions",
        type=float,
        default=1.0,
        help="number of local-surface-normal rotations made by collectibles across the scene frame range",
    )
    parser.add_argument(
        "--max_ground_tilt_degrees",
        type=float,
        default=25.0,
        help="legacy capped-tilt value; v5 physical assets use the complete sampled surface normal",
    )
    parser.add_argument("--skip_previews", action="store_true")
    parser.add_argument("--preview_limit", type=int, default=18)
    parser.add_argument("--resolution_x", type=int, default=960)
    parser.add_argument("--resolution_y", type=int, default=540)
    return parser.parse_args(argv)


def project_path(value):
    value = os.path.expandvars(os.path.expanduser(str(value)))
    return os.path.normpath(value if os.path.isabs(value) else os.path.join(PROJECT_ROOT, value))


def load_json(path, label):
    path = project_path(path)
    if not os.path.isfile(path):
        raise FileNotFoundError("required %s not found: %s" % (label, path))
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


def finite_or_none(value):
    value = float(value)
    return value if math.isfinite(value) else None


def finite_vec3(value, positive=False):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return False
    for item in value:
        if not isinstance(item, (int, float)) or isinstance(item, bool) or not math.isfinite(float(item)):
            return False
        if positive and float(item) <= 0.0:
            return False
    return True


def vec_list(value, digits=6):
    return [round(float(value[0]), digits), round(float(value[1]), digits), round(float(value[2]), digits)]


def placement_tuning_map(placement_data):
    value = placement_data.get("placement_plan", {}).get("tuning_overrides", {})
    return value if isinstance(value, dict) else {}


def placement_staging_constraints(placement_data):
    value = placement_data.get("placement_plan", {}).get("staging_constraints", {})
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("placement_plan.staging_constraints must be an object")
    minimum = value.get("minimum_visible_asset_center_distance_m", 0.0)
    if (
        not isinstance(minimum, (int, float))
        or isinstance(minimum, bool)
        or not math.isfinite(float(minimum))
        or float(minimum) < 0.0
    ):
        raise ValueError("minimum_visible_asset_center_distance_m must be a finite non-negative number")
    footprint_gap = value.get("minimum_visible_asset_footprint_gap_m", 0.0)
    if (
        not isinstance(footprint_gap, (int, float))
        or isinstance(footprint_gap, bool)
        or not math.isfinite(float(footprint_gap))
        or float(footprint_gap) < 0.0
    ):
        raise ValueError("minimum_visible_asset_footprint_gap_m must be a finite non-negative number")
    distance_mode = value.get("distance_mode", "auto")
    if distance_mode not in {"auto", "xy", "xyz"}:
        raise ValueError("staging distance_mode must be auto, xy, or xyz")
    single_asset_closeups = value.get("single_asset_closeups", False)
    if not isinstance(single_asset_closeups, bool):
        raise ValueError("staging single_asset_closeups must be a boolean")
    closeup_max_other_assets = value.get("closeup_max_other_assets", 0)
    if (
        not isinstance(closeup_max_other_assets, int)
        or isinstance(closeup_max_other_assets, bool)
        or closeup_max_other_assets < 0
    ):
        raise ValueError("staging closeup_max_other_assets must be a non-negative integer")
    closeup_camera_azimuth_samples = value.get("closeup_camera_azimuth_samples", 36)
    if (
        not isinstance(closeup_camera_azimuth_samples, int)
        or isinstance(closeup_camera_azimuth_samples, bool)
        or closeup_camera_azimuth_samples < 8
        or closeup_camera_azimuth_samples > 144
    ):
        raise ValueError("staging closeup_camera_azimuth_samples must be an integer in [8, 144]")
    closeup_lens_mm = value.get("closeup_lens_mm", 70.0)
    if (
        not isinstance(closeup_lens_mm, (int, float))
        or isinstance(closeup_lens_mm, bool)
        or not math.isfinite(float(closeup_lens_mm))
        or not 35.0 <= float(closeup_lens_mm) <= 120.0
    ):
        raise ValueError("staging closeup_lens_mm must be finite and in [35, 120]")
    closeup_min_target_visibility_fraction = value.get(
        "closeup_min_target_visibility_fraction",
        0.35,
    )
    if (
        not isinstance(closeup_min_target_visibility_fraction, (int, float))
        or isinstance(closeup_min_target_visibility_fraction, bool)
        or not math.isfinite(float(closeup_min_target_visibility_fraction))
        or not 0.0 < float(closeup_min_target_visibility_fraction) <= 1.0
    ):
        raise ValueError(
            "staging closeup_min_target_visibility_fraction must be finite and in (0, 1]"
        )
    closeup_target_visibility_samples = value.get("closeup_target_visibility_samples", 32)
    if (
        not isinstance(closeup_target_visibility_samples, int)
        or isinstance(closeup_target_visibility_samples, bool)
        or closeup_target_visibility_samples < 8
        or closeup_target_visibility_samples > 128
    ):
        raise ValueError(
            "staging closeup_target_visibility_samples must be an integer in [8, 128]"
        )
    return {
        "minimum_visible_asset_center_distance_m": float(minimum),
        "minimum_visible_asset_footprint_gap_m": float(footprint_gap),
        "distance_mode": distance_mode,
        "single_asset_closeups": single_asset_closeups,
        "closeup_max_other_assets": closeup_max_other_assets,
        "closeup_camera_azimuth_samples": closeup_camera_azimuth_samples,
        "closeup_lens_mm": float(closeup_lens_mm),
        "closeup_min_target_visibility_fraction": float(closeup_min_target_visibility_fraction),
        "closeup_target_visibility_samples": closeup_target_visibility_samples,
    }


def size_multiplier_xyz(tuning):
    value = tuning.get("size_multiplier", 1.0) if isinstance(tuning, dict) else 1.0
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        factor = float(value)
        return Vector((factor, factor, factor))
    return Vector([float(item) for item in value])


def tuned_size(asset, tuning):
    return Vector([float(value) for value in asset["expected_size_meters"]]) * size_multiplier_xyz(tuning)


def validate_inputs(placement_data, asset_data, candidate_data):
    elements = placement_data.get("placement_plan", {}).get("elements")
    assets = asset_data.get("assets")
    non_glb = asset_data.get("non_glb_placements")
    candidates = candidate_data.get("candidates")
    if not isinstance(elements, list) or not elements:
        raise ValueError("gameplay placement plan has no elements")
    if not isinstance(assets, list) or not isinstance(non_glb, list):
        raise ValueError("asset plan must contain assets and non_glb_placements lists")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("placement candidate list is empty")

    placement_by_id = {}
    for item in elements:
        placement_id = item.get("placement_id") if isinstance(item, dict) else None
        if not isinstance(placement_id, str) or not placement_id or placement_id in placement_by_id:
            raise ValueError("placement ids must be unique non-empty strings")
        if item.get("element_type") not in PROXY_TYPE_BY_ELEMENT_TYPE:
            raise ValueError("placement %s has unsupported element_type %s" % (placement_id, item.get("element_type")))
        if not finite_vec3(item.get("anchor_world_xyz")) or not finite_vec3(item.get("world_xyz")):
            raise ValueError("placement %s has invalid coordinates" % placement_id)
        placement_by_id[placement_id] = item

    asset_by_id = {}
    placement_to_asset = {}
    for asset in assets:
        asset_id = asset.get("asset_id") if isinstance(asset, dict) else None
        if not isinstance(asset_id, str) or not asset_id or asset_id in asset_by_id:
            raise ValueError("asset ids must be unique non-empty strings")
        if not finite_vec3(asset.get("expected_size_meters"), positive=True):
            raise ValueError("asset %s has invalid expected_size_meters" % asset_id)
        placement_ids = asset.get("placement_ids")
        if not isinstance(placement_ids, list):
            raise ValueError("asset %s placement_ids must be a list" % asset_id)
        if not placement_ids and asset.get("staging_policy") != "generated_library_only":
            raise ValueError(
                "asset %s has no placement_ids and is not marked generated_library_only" % asset_id
            )
        if placement_ids:
            expected_path = project_path(asset.get("expected_glb_path", ""))
            if not os.path.isfile(expected_path) or os.path.getsize(expected_path) <= 1024:
                raise FileNotFoundError("asset GLB missing or invalid: %s" % expected_path)
        for placement_id in placement_ids:
            if placement_id not in placement_by_id or placement_id in placement_to_asset:
                raise ValueError("asset placement coverage is invalid for %s" % placement_id)
            if asset.get("asset_role") != placement_by_id[placement_id].get("element_type"):
                raise ValueError("asset %s role does not match placement %s element_type" % (asset_id, placement_id))
            placement_to_asset[placement_id] = asset
        asset_by_id[asset_id] = asset

    logical_ids = set()
    for item in non_glb:
        placement_id = item.get("placement_id") if isinstance(item, dict) else None
        if placement_id not in placement_by_id or placement_id in placement_to_asset or placement_id in logical_ids:
            raise ValueError("non-GLB placement coverage is invalid")
        logical_ids.add(placement_id)
    if asset_data.get("unmapped_placements_are_logical") is True:
        for placement_id in sorted(set(placement_by_id) - set(placement_to_asset) - logical_ids):
            non_glb.append({
                "placement_id": placement_id,
                "implementation": "Runtime-only gameplay proxy; this repeated gameplay point reuses an asset type staged elsewhere.",
                "reason": "The authored staging pass gives every generated asset type one physical hero instance and keeps repeated placements logical.",
            })
            logical_ids.add(placement_id)
    if set(placement_by_id) != set(placement_to_asset) | logical_ids:
        raise ValueError("every placement must be covered exactly once by an asset or logical placement")

    candidate_by_id = {}
    for candidate in candidates:
        candidate_id = candidate.get("candidate_id") if isinstance(candidate, dict) else None
        if isinstance(candidate_id, str) and candidate_id and finite_vec3(candidate.get("world_xyz")):
            candidate_by_id[candidate_id] = candidate

    tuning_by_id = placement_tuning_map(placement_data)
    allowed_alignment = {"auto", "terrain_normal", "upright", "full_surface_normal"}
    for placement_id, tuning in tuning_by_id.items():
        if placement_id not in placement_by_id or not isinstance(tuning, dict):
            raise ValueError("invalid placement tuning override for %s" % placement_id)
        override_id = tuning.get("candidate_id")
        if override_id is not None and override_id not in candidate_by_id:
            raise ValueError("placement tuning candidate is missing for %s: %s" % (placement_id, override_id))
        multiplier = tuning.get("size_multiplier", 1.0)
        if isinstance(multiplier, (int, float)) and not isinstance(multiplier, bool):
            if not math.isfinite(float(multiplier)) or float(multiplier) <= 0.0:
                raise ValueError("invalid size_multiplier for %s" % placement_id)
        elif not finite_vec3(multiplier, positive=True):
            raise ValueError("invalid size_multiplier for %s" % placement_id)
        offset = tuning.get("position_offset_xyz_m", [0.0, 0.0, 0.0])
        if not finite_vec3(offset):
            raise ValueError("invalid position_offset_xyz_m for %s" % placement_id)
        alignment = tuning.get("surface_alignment_mode", "auto")
        if alignment not in allowed_alignment:
            raise ValueError("invalid surface_alignment_mode for %s" % placement_id)
        for key in ("surface_clearance_m", "max_tilt_degrees", "yaw_degrees", "vegetation_clearance_radius_m"):
            value = tuning.get(key)
            if value is not None and (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
            ):
                raise ValueError("invalid %s for %s" % (key, placement_id))
    return elements, assets, non_glb, placement_by_id, placement_to_asset, candidate_by_id, tuning_by_id


def context_clearance(candidate, key):
    value = candidate.get("nearest_" + key) if isinstance(candidate, dict) else None
    distance = value.get("distance_xy") if isinstance(value, dict) else None
    if isinstance(distance, (int, float)) and not isinstance(distance, bool) and math.isfinite(float(distance)):
        return max(0.0, float(distance))
    return float("inf")


def candidate_is_ground(candidate):
    return (
        isinstance(candidate, dict)
        and candidate.get("candidate_kind") == "ground_surface"
        and candidate.get("surface_type") == "ground"
        and candidate.get("walkability") in {"good", "medium", None}
        and finite_vec3(candidate.get("world_xyz"))
    )


def footprint_radius(asset, tuning=None):
    size = tuned_size(asset, tuning or {})
    return max(0.05, 0.5 * math.hypot(size[0], size[1]))


def required_vegetation_clearance(asset, placement, tuning=None):
    # This is a measurement derived from the LLM-planned size, not a rule used
    # to choose or move a placement.
    return footprint_radius(asset, tuning)


def candidate_support_facts(candidate):
    profile = candidate.get("support_profile") if isinstance(candidate, dict) else None
    if not isinstance(profile, dict) or profile.get("evaluated") is not True:
        return {
            "support_profile_available": False,
            "max_supported_footprint_radius_m": None,
            "support_class": "legacy_unmeasured",
        }
    radius = profile.get("max_supported_footprint_radius_m")
    if not isinstance(radius, (int, float)) or isinstance(radius, bool) or not math.isfinite(float(radius)) or float(radius) < 0.0:
        raise ValueError("candidate has invalid footprint support radius: %s" % candidate.get("candidate_id"))
    return {
        "support_profile_available": True,
        "max_supported_footprint_radius_m": float(radius),
        "support_class": str(profile.get("support_class") or "unsupported"),
    }


def resolve_targets(elements, placement_to_asset, candidate_by_id, tuning_by_id):
    resolved = []
    warnings = []

    for placement in elements:
        placement_id = placement["placement_id"]
        asset = placement_to_asset.get(placement_id)
        tuning = tuning_by_id.get(placement_id, {})
        if asset is None:
            resolved.append({
                "placement_id": placement_id,
                "logical_only": True,
                "anchor_world_xyz": list(placement["anchor_world_xyz"]),
                "world_xyz": list(placement["world_xyz"]),
                "shift_xy": 0.0,
                "footprint_radius": 0.0,
            })
            continue

        original_id = placement.get("candidate_id")
        selected_id = tuning.get("candidate_id") or original_id
        original = candidate_by_id.get(selected_id)
        if original is None:
            raise ValueError("selected candidate is missing for placement %s: %s" % (placement_id, selected_id))
        radius = footprint_radius(asset, tuning)
        support_facts = candidate_support_facts(original)
        clearance = required_vegetation_clearance(asset, placement, tuning)
        if selected_id == original_id:
            anchor = [float(value) for value in placement["anchor_world_xyz"]]
            world = [float(value) for value in placement["world_xyz"]]
            normal = placement.get("normal_xyz") or original.get("normal_xyz") or [0.0, 0.0, 1.0]
        else:
            anchor = [float(value) for value in original["world_xyz"]]
            world = list(anchor)
            normal = original.get("normal_xyz") or [0.0, 0.0, 1.0]
        offset = [float(value) for value in tuning.get("position_offset_xyz_m", [0.0, 0.0, 0.0])]
        anchor = [anchor[index] + offset[index] for index in range(3)]
        world = [world[index] + offset[index] for index in range(3)]
        # `world_xyz` is derived data for an above-ground placement.  Candidate
        # overrides used to replace it with the bare surface anchor, silently
        # discarding the authored hover height and burying half of the asset in
        # a slope.  Rebuild it for both original and overridden candidates so
        # the invariant remains true after every deterministic adjustment.
        if placement.get("placement_mode") == "above_ground":
            surface_normal = oriented_surface_normal(normal)
            height_offset = float(placement.get("height_offset") or 0.0)
            world = list(Vector(anchor) + surface_normal * height_offset)
        tree_clearance = context_clearance(original, "tree")
        bush_clearance = context_clearance(original, "bush")
        if min(tree_clearance, bush_clearance) < radius:
            warnings.append(
                "%s: LLM-selected point may be too close to scene vegetation for the planned %.3fm footprint; "
                "Python preserved the decision and did not select another point" % (placement_id, radius)
            )
        resolved.append({
            "placement_id": placement_id,
            "logical_only": False,
            "asset_id": asset["asset_id"],
            "element_type": placement.get("element_type"),
            "placement_mode": placement.get("placement_mode"),
            "height_offset": float(placement.get("height_offset") or 0.0),
            "original_candidate_id": original_id,
            "resolved_candidate_id": selected_id,
            "original_anchor_world_xyz": list(placement["anchor_world_xyz"]),
            "anchor_world_xyz": anchor,
            "world_xyz": world,
            "normal_xyz": normal,
            "surface_alignment_mode": effective_surface_alignment_mode(placement, normal, tuning),
            "shift_xy": 0.0,
            "resolution_reason": "manual_tuning_candidate_override" if selected_id != original_id else "exact_llm_selected_candidate",
            "required_vegetation_clearance": clearance,
            "tree_clearance": finite_or_none(tree_clearance),
            "bush_clearance": finite_or_none(bush_clearance),
            "footprint_radius": radius,
            **support_facts,
        })
    return resolved, warnings


def validate_resolved_surface_support(resolved):
    return validate_realized_footprint_support(resolved)


def validate_resolved_spacing(resolved, constraints):
    """Validate authored asset spacing without moving or reselecting anything.

    Ground assets use plan-view distance because stacking them at different Z
    values on the same hill is still a gameplay cluster.  Cliff/wall assets use
    3D distance so authored vertical traversal layouts remain possible.
    """
    minimum = float(constraints["minimum_visible_asset_center_distance_m"])
    minimum_footprint_gap = float(constraints["minimum_visible_asset_footprint_gap_m"])
    requested_mode = constraints["distance_mode"]
    visible = [item for item in resolved if not item.get("logical_only")]
    closest = None
    closest_footprint_gap = None
    center_violations = []
    footprint_violations = []
    checked = 0
    for left_index, left in enumerate(visible):
        left_xyz = Vector(left["world_xyz"])
        for right in visible[left_index + 1:]:
            right_xyz = Vector(right["world_xyz"])
            mode = requested_mode
            if mode == "auto":
                mode = (
                    "xyz"
                    if "full_surface_normal" in {
                        left.get("surface_alignment_mode"),
                        right.get("surface_alignment_mode"),
                    }
                    else "xy"
                )
            delta = left_xyz - right_xyz
            distance = delta.length if mode == "xyz" else math.hypot(delta.x, delta.y)
            checked += 1
            pair = {
                "placement_a": left["placement_id"],
                "placement_b": right["placement_id"],
                "distance_mode": mode,
                "distance_m": round(float(distance), 6),
                "footprint_gap_m": round(
                    float(distance - left["footprint_radius"] - right["footprint_radius"]),
                    6,
                ),
            }
            if closest is None or distance < closest[0]:
                closest = (distance, pair)
            if closest_footprint_gap is None or pair["footprint_gap_m"] < closest_footprint_gap[0]:
                closest_footprint_gap = (pair["footprint_gap_m"], pair)
            if minimum > 0.0 and distance + 1e-6 < minimum:
                center_violations.append(pair)
            if minimum_footprint_gap > 0.0 and pair["footprint_gap_m"] + 1e-6 < minimum_footprint_gap:
                footprint_violations.append(pair)
    result = {
        **constraints,
        "visible_asset_count": len(visible),
        "checked_pair_count": checked,
        "closest_pair": closest[1] if closest else None,
        "closest_footprint_pair": closest_footprint_gap[1] if closest_footprint_gap else None,
        "center_violation_count": len(center_violations),
        "footprint_gap_violation_count": len(footprint_violations),
        "violations": center_violations + footprint_violations,
        "ok": not center_violations and not footprint_violations,
    }
    return result


def vegetation_kind(obj):
    name = obj.name.lower()
    for kind, tokens in (
        ("tree", ("tree",)),
        ("bush", ("bush", "shrub")),
        ("fern", ("fern",)),
        ("grass", ("grass", "monocot")),
        ("plant", ("flower", "plant")),
    ):
        if any(token in name for token in tokens):
            return kind
    return None


def object_xy_bounds(obj):
    if obj.type != "MESH" or not obj.bound_box:
        return None
    points = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
    return (
        min(point.x for point in points),
        max(point.x for point in points),
        min(point.y for point in points),
        max(point.y for point in points),
        min(point.z for point in points),
        max(point.z for point in points),
    )


def xy_distance_to_bounds(point, bounds):
    dx = max(bounds[0] - point.x, 0.0, point.x - bounds[1])
    dy = max(bounds[2] - point.y, 0.0, point.y - bounds[3])
    return math.hypot(dx, dy)


def xyz_distance_to_bounds(point, bounds):
    dx = max(bounds[0] - point.x, 0.0, point.x - bounds[1])
    dy = max(bounds[2] - point.y, 0.0, point.y - bounds[3])
    dz = max(bounds[4] - point.z, 0.0, point.z - bounds[5])
    return math.sqrt(dx * dx + dy * dy + dz * dz)


def clear_blocking_vegetation(elements, placement_to_asset, resolved_by_id, tuning_by_id, margin):
    """Hide every independent vegetation object intersecting a tuned footprint.

    Large combined scatter meshes are deliberately preserved.  This prevents a
    single gameplay prop from disabling vegetation across the whole scene.
    """
    cleared = []
    seen = set()
    extra = max(0.0, float(margin))
    vegetation = []
    for obj in bpy.data.objects:
        if obj.hide_render or obj.name.startswith("C2G_"):
            continue
        kind = vegetation_kind(obj)
        bounds = object_xy_bounds(obj) if kind else None
        if bounds is None:
            continue
        if (bounds[1] - bounds[0]) > 25.0 or (bounds[3] - bounds[2]) > 25.0:
            continue
        vegetation.append((obj, kind, bounds))
    for placement in elements:
        asset = placement_to_asset.get(placement["placement_id"])
        if asset is None:
            continue
        placement_id = placement["placement_id"]
        resolved = resolved_by_id.get(placement_id)
        if not isinstance(resolved, dict):
            continue
        tuning = tuning_by_id.get(placement_id, {})
        configured = tuning.get("vegetation_clearance_radius_m")
        clearance = max(0.0, float(configured)) if configured is not None else footprint_radius(asset, tuning) + extra
        point = Vector(resolved["world_xyz"])
        for obj, kind, bounds in vegetation:
            if obj.name in seen:
                continue
            distance = (
                xyz_distance_to_bounds(point, bounds)
                if resolved.get("surface_alignment_mode") == "full_surface_normal"
                else xy_distance_to_bounds(point, bounds)
            )
            if distance >= clearance:
                continue
            obj.hide_render = True
            obj.hide_set(True)
            seen.add(obj.name)
            cleared.append({
                "object_name": obj.name,
                "vegetation_kind": kind,
                "placement_id": placement_id,
                "distance_xy": round(float(distance), 6),
                "required_clearance": round(float(clearance), 6),
            })
    return cleared


def remove_collection(name):
    collection = bpy.data.collections.get(name)
    if collection is None:
        return
    for obj in list(collection.all_objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    bpy.data.collections.remove(collection)


def new_collection(name):
    collection = bpy.data.collections.new(name)
    bpy.context.scene.collection.children.link(collection)
    return collection


def relink_object(obj, collection):
    if obj.name not in collection.objects.keys():
        collection.objects.link(obj)
    for old in list(obj.users_collection):
        if old != collection:
            old.objects.unlink(obj)


def bbox_for_objects(objects):
    points = []
    for obj in objects:
        if obj.type != "MESH":
            continue
        for corner in obj.bound_box:
            points.append(obj.matrix_world @ Vector(corner))
    if not points:
        raise ValueError("imported GLB contains no mesh bounding box")
    minimum = Vector((min(point.x for point in points), min(point.y for point in points), min(point.z for point in points)))
    maximum = Vector((max(point.x for point in points), max(point.y for point in points), max(point.z for point in points)))
    return {"min": minimum, "max": maximum, "size": maximum - minimum, "center": (minimum + maximum) * 0.5}


def load_asset_templates(assets, template_collection):
    templates = {}
    for asset in assets:
        asset_id = asset["asset_id"]
        glb_path = project_path(asset["expected_glb_path"])
        log("IMPORT_TEMPLATE", asset_id, glb_path)
        before = set(bpy.data.objects)
        bpy.ops.import_scene.gltf(filepath=glb_path)
        imported = [obj for obj in bpy.data.objects if obj not in before]
        if not imported:
            raise RuntimeError("GLB import created no objects: %s" % glb_path)
        for obj in imported:
            relink_object(obj, template_collection)
            obj.hide_render = True
            obj.hide_set(True)
            obj.name = "TEMPLATE_%s_%s" % (asset_id, obj.name)
        bpy.context.view_layer.update()
        bounds = bbox_for_objects(imported)
        templates[asset_id] = {
            "objects": imported,
            "bounds": bounds,
            "glb_path": glb_path,
        }
    template_collection.hide_render = True
    template_collection.hide_viewport = True
    return templates


def normalized_source_matrix(source, origin):
    return Matrix.Translation(-origin) @ source.matrix_world


def oriented_surface_normal(normal_xyz):
    """Return a stable outward/upward normal for deterministic asset staging."""
    normal = Vector(normal_xyz if normal_xyz is not None else (0.0, 0.0, 1.0))
    if normal.length <= 1e-8:
        return Vector((0.0, 0.0, 1.0))
    normal.normalize()
    # Terrain meshes occasionally expose the opposite winding.  Flip clearly
    # downward ground normals, while retaining horizontal cliff orientation.
    if normal.z < -CLIFF_NORMAL_MAX_ABS_Z:
        normal.negate()
    return normal


def effective_surface_alignment_mode(placement, normal_xyz, tuning=None):
    """Choose alignment from authored intent plus measured candidate geometry.

    `auto` is deliberately geometry-only: normal terrain uses capped tangent
    alignment, while a wall-like candidate uses the complete surface normal.
    This keeps LLM placement semantics separate from deterministic transforms.
    """
    tuning = tuning or {}
    requested = tuning.get("surface_alignment_mode", "auto")
    if requested not in {"auto", "upright", "terrain_normal", "full_surface_normal"}:
        raise ValueError("invalid surface_alignment_mode: %s" % requested)
    if requested != "auto":
        return requested
    normal = oriented_surface_normal(normal_xyz)
    if abs(float(normal.z)) <= CLIFF_NORMAL_MAX_ABS_Z:
        return "full_surface_normal"
    return "terrain_normal"


def surface_tilt_degrees(normal_xyz):
    normal = oriented_surface_normal(normal_xyz)
    return math.degrees(math.acos(max(-1.0, min(1.0, float(normal.z)))))


def residual_slope_support_lift(target_size, normal_xyz, applied_tilt_degrees):
    """Lift a rigid bbox enough that a capped/upright base does not enter a slope."""
    normal = oriented_surface_normal(normal_xyz)
    horizontal = Vector((normal.x, normal.y))
    if horizontal.length <= 1e-8:
        return 0.0
    horizontal.normalize()
    actual_tilt = surface_tilt_degrees(normal)
    residual = max(0.0, actual_tilt - float(applied_tilt_degrees))
    if residual <= 1e-6:
        return 0.0
    half_span = 0.5 * (
        abs(float(horizontal.x)) * float(target_size.x)
        + abs(float(horizontal.y)) * float(target_size.y)
    )
    return half_span * math.sin(math.radians(residual))


def ground_alignment_matrix(normal_xyz, max_tilt_degrees, full_surface=False):
    up = Vector((0.0, 0.0, 1.0))
    normal = oriented_surface_normal(normal_xyz)
    if (not full_surface and normal.z <= 0.0) or float(max_tilt_degrees) <= 0.0:
        return Matrix.Identity(4), 0.0
    angle = math.acos(max(-1.0, min(1.0, up.dot(normal))))
    if not full_surface:
        angle = min(angle, math.radians(float(max_tilt_degrees)))
    axis = up.cross(normal)
    if axis.length <= 1e-8 or angle <= 1e-8:
        return Matrix.Identity(4), 0.0
    axis.normalize()
    return Matrix.Rotation(angle, 4, axis), math.degrees(angle)


def normal_alignment_error_degrees(alignment, normal_xyz):
    """Measure the angle between an asset's local +Z and its sampled surface."""
    local_up = alignment.to_3x3() @ Vector((0.0, 0.0, 1.0))
    if local_up.length <= 1e-8:
        return 180.0
    local_up.normalize()
    normal = oriented_surface_normal(normal_xyz)
    return math.degrees(math.acos(max(-1.0, min(1.0, local_up.dot(normal)))))


def orientation_constraint_error_degrees(alignment, normal_xyz, alignment_mode, max_tilt_degrees):
    """Measure transform error against the requested physical orientation policy."""
    if alignment_mode == "full_surface_normal":
        expected, _tilt = ground_alignment_matrix(normal_xyz, 180.0, full_surface=True)
    elif alignment_mode == "upright":
        expected = Matrix.Identity(4)
    else:
        expected, _tilt = ground_alignment_matrix(normal_xyz, max_tilt_degrees, full_surface=False)
    actual_up = alignment.to_3x3() @ Vector((0.0, 0.0, 1.0))
    expected_up = expected.to_3x3() @ Vector((0.0, 0.0, 1.0))
    if actual_up.length <= 1e-8 or expected_up.length <= 1e-8:
        return 180.0
    actual_up.normalize()
    expected_up.normalize()
    return math.degrees(
        math.acos(max(-1.0, min(1.0, float(actual_up.dot(expected_up)))))
    )


def target_transform(target, scale, rotation=None):
    rotation = rotation or Matrix.Identity(4)
    return (
        Matrix.Translation(Vector(target))
        @ rotation
        @ Matrix.Diagonal(Vector((float(scale[0]), float(scale[1]), float(scale[2]), 1.0)))
    )


def instantiate_asset(
    asset,
    placement,
    resolved,
    template,
    collection,
    ground_embed_m=0.0,
    ground_clearance_m=0.0,
    hazard_clearance_m=0.03,
    collectible_hover_m=0.0,
    max_ground_tilt_degrees=25.0,
    tuning=None,
):
    tuning = tuning or {}
    bounds = template["bounds"]
    source_size = bounds["size"]
    if min(source_size.x, source_size.y, source_size.z) <= 1e-7:
        raise ValueError("asset %s has a degenerate imported bounding box" % asset["asset_id"])
    target_size = tuned_size(asset, tuning)
    scale = Vector((target_size.x / source_size.x, target_size.y / source_size.y, target_size.z / source_size.z))
    above_ground = placement.get("placement_mode") == "above_ground"
    origin = bounds["center"] if above_ground else Vector((bounds["center"].x, bounds["center"].y, bounds["min"].z))
    target = Vector(resolved["world_xyz"])
    normal_xyz = resolved.get("normal_xyz") or [0.0, 0.0, 1.0]
    alignment_mode = effective_surface_alignment_mode(placement, normal_xyz, tuning)
    configured_tilt = float(tuning.get("max_tilt_degrees", max_ground_tilt_degrees))
    alignment, ground_tilt_degrees = ground_alignment_matrix(
        normal_xyz,
        180.0 if alignment_mode == "full_surface_normal" else configured_tilt,
        full_surface=alignment_mode == "full_surface_normal",
    )
    automatic_sink_z = 0.0
    automatic_support_lift_m = 0.0
    applied_clearance_z = 0.0
    ground_adjustment_z = 0.0
    if not above_ground:
        # A single rigid GLB cannot conform to a curved surface. Align its
        # lowest point to the sampled tangent plane and apply only explicit
        # user-requested embed/clearance. Do not bury a percentage of every
        # asset: that made short props and pickups visibly sink into terrain.
        if placement.get("element_type") == "collectible":
            applied_clearance_z = max(0.0, float(collectible_hover_m))
        elif placement.get("element_type") == "hazard_zone":
            applied_clearance_z = max(0.0, float(hazard_clearance_m))
        else:
            applied_clearance_z = max(0.0, float(ground_clearance_m))
        if tuning.get("surface_clearance_m") is not None:
            applied_clearance_z = max(0.0, float(tuning["surface_clearance_m"]))
        automatic_support_lift_m = (
            0.0
            if alignment_mode == "full_surface_normal"
            else residual_slope_support_lift(target_size, normal_xyz, ground_tilt_degrees)
        )
        effective_sink_z = max(0.0, float(ground_embed_m))
        ground_adjustment_z = applied_clearance_z + automatic_support_lift_m - effective_sink_z
        if alignment_mode == "full_surface_normal":
            target += oriented_surface_normal(normal_xyz) * ground_adjustment_z
        else:
            target.z += ground_adjustment_z

    yaw = Matrix.Rotation(math.radians(float(tuning.get("yaw_degrees", 0.0))), 4, "Z")
    alignment = alignment @ yaw
    normal_alignment_error = normal_alignment_error_degrees(alignment, normal_xyz)
    orientation_constraint_error = orientation_constraint_error_degrees(
        alignment,
        normal_xyz,
        alignment_mode,
        configured_tilt,
    )
    if orientation_constraint_error > 0.05:
        raise RuntimeError(
            "asset orientation does not match the requested physical alignment policy "
            "(mode=%s, error_degrees=%.6f)"
            % (alignment_mode, orientation_constraint_error)
        )

    root = bpy.data.objects.new("C2G_%s_%s_ROOT" % (placement["placement_id"], asset["asset_id"]), None)
    collection.objects.link(root)
    root.empty_display_type = "PLAIN_AXES"
    root.empty_display_size = max(0.1, min(target_size) * 0.25)
    root["code2games_placement_id"] = placement["placement_id"]
    root["code2games_asset_id"] = asset["asset_id"]
    root["code2games_element_type"] = placement.get("element_type", "")
    root["code2games_original_candidate_id"] = resolved.get("original_candidate_id", "")
    root["code2games_resolved_candidate_id"] = resolved.get("resolved_candidate_id", "")
    root["code2games_surface_alignment_mode"] = alignment_mode
    root.matrix_world = target_transform(
        target,
        scale,
        alignment,
    )

    content_parent = root
    if placement.get("element_type") == "collectible" and above_ground:
        # Keep the aligned root immutable and spin a child around its local Z.
        # Animating the aligned root's Euler Z directly changes its world-space
        # normal on a slope, violating the surface-normal invariant.
        spin_pivot = bpy.data.objects.new(
            "C2G_%s_%s_SPIN" % (placement["placement_id"], asset["asset_id"]),
            None,
        )
        collection.objects.link(spin_pivot)
        spin_pivot.parent = root
        spin_pivot.matrix_parent_inverse = Matrix.Identity(4)
        spin_pivot.matrix_basis = Matrix.Identity(4)
        root["code2games_spin_pivot"] = spin_pivot.name
        content_parent = spin_pivot

    duplicates = []
    for source in template["objects"]:
        duplicate = source.copy()
        duplicate.data = source.data
        duplicate.animation_data_clear()
        duplicate.name = "C2G_%s_%s" % (placement["placement_id"], source.name.replace("TEMPLATE_", ""))
        collection.objects.link(duplicate)
        duplicate.hide_render = False
        duplicate.hide_set(False)
        duplicate.parent = content_parent
        duplicate.matrix_parent_inverse = Matrix.Identity(4)
        duplicate.matrix_basis = normalized_source_matrix(source, origin)
        duplicates.append(duplicate)
    bpy.context.view_layer.update()
    final_bounds = bbox_for_objects(duplicates)
    return (
        root,
        duplicates,
        final_bounds,
        scale,
        ground_adjustment_z,
        applied_clearance_z,
        automatic_sink_z,
        automatic_support_lift_m,
        ground_tilt_degrees,
        alignment_mode,
        normal_alignment_error,
        orientation_constraint_error,
    )


def animate_collectible_spin(root, placement, revolutions):
    if (
        placement.get("element_type") != "collectible"
        or placement.get("placement_mode") != "above_ground"
        or float(revolutions) <= 0.0
    ):
        return False
    spin_pivot_name = root.get("code2games_spin_pivot")
    spin_pivot = bpy.data.objects.get(spin_pivot_name) if spin_pivot_name else None
    if spin_pivot is None:
        raise RuntimeError("above-ground collectible is missing its normal-preserving spin pivot")
    scene = bpy.context.scene
    start_frame = int(scene.frame_start)
    end_frame = max(start_frame + 1, int(scene.frame_end))
    spin_pivot.rotation_mode = "XYZ"
    spin_pivot.rotation_euler.z = 0.0
    spin_pivot.keyframe_insert(data_path="rotation_euler", index=2, frame=start_frame)
    spin_pivot.rotation_euler.z = math.tau * float(revolutions)
    spin_pivot.keyframe_insert(data_path="rotation_euler", index=2, frame=end_frame)
    action = spin_pivot.animation_data.action if spin_pivot.animation_data else None
    if action:
        for curve in action.fcurves:
            if curve.data_path == "rotation_euler" and curve.array_index == 2:
                for point in curve.keyframe_points:
                    point.interpolation = "LINEAR"
    return True


def create_proxy(placement, asset, bounds, root, proxy_collection):
    element_type = placement.get("element_type")
    if element_type == "landmark":
        return None
    proxy = bpy.data.objects.new("C2G_%s_GAMEPLAY_PROXY" % placement["placement_id"], None)
    proxy_collection.objects.link(proxy)
    # Keep the proxy in world space. Parenting it after calculating a world-space
    # bounding box would apply the asset root transform for a second time.
    proxy.location = bounds["center"]
    proxy.empty_display_type = "SPHERE" if element_type == "collectible" else "CUBE"
    proxy.empty_display_size = 1.0
    proxy.scale = (max(bounds["size"].x * 0.55, 0.1), max(bounds["size"].y * 0.55, 0.1), max(bounds["size"].z * 0.55, 0.1))
    proxy.hide_render = True
    proxy["code2games_proxy_type"] = PROXY_TYPE_BY_ELEMENT_TYPE.get(element_type, "gameplay_bounds")
    proxy["code2games_placement_id"] = placement["placement_id"]
    proxy["code2games_asset_id"] = asset["asset_id"]
    proxy["code2games_root_object"] = root.name
    proxy["code2games_gameplay_purpose"] = str(placement.get("gameplay_purpose") or "")
    return proxy


def create_logical_proxy(placement, resolved, specification, proxy_collection):
    element_type = placement.get("element_type")
    proxy = bpy.data.objects.new("C2G_%s_LOGICAL_PROXY" % placement["placement_id"], None)
    proxy_collection.objects.link(proxy)
    proxy.location = Vector(resolved["world_xyz"])
    proxy.empty_display_type = "CUBE" if element_type in {
        "safe_zone",
        "capture_zone",
        "checkpoint",
        "event_trigger",
    } else "SPHERE"
    proxy.empty_display_size = 1.0
    proxy.hide_render = True
    proxy["code2games_proxy_type"] = PROXY_TYPE_BY_ELEMENT_TYPE.get(element_type, "gameplay_anchor")
    proxy["code2games_placement_id"] = placement["placement_id"]
    proxy["code2games_element_type"] = str(element_type or "")
    proxy["code2games_gameplay_purpose"] = str(placement.get("gameplay_purpose") or "")
    proxy["code2games_implementation"] = str((specification or {}).get("implementation") or "")
    proxy["code2games_reason"] = str((specification or {}).get("reason") or "")
    return proxy


def look_at(camera, target):
    direction = Vector(target) - camera.location
    if direction.length > 1e-6:
        camera.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


def create_camera(name, location, target, collection, ortho_scale=None, lens_mm=48.0):
    camera_data = bpy.data.cameras.new(name + "_DATA")
    camera = bpy.data.objects.new(name, camera_data)
    collection.objects.link(camera)
    camera.location = Vector(location)
    camera_data.clip_start = 0.05
    camera_data.clip_end = 10000.0
    if ortho_scale is not None:
        camera_data.type = "ORTHO"
        camera_data.ortho_scale = float(ortho_scale)
    else:
        camera_data.lens = float(lens_mm)
    look_at(camera, target)
    return camera


def bbox_corners(item):
    center = Vector(item["bbox_center"])
    half = Vector(item["bbox_size"]) * 0.5
    return [
        center + Vector((sx * half.x, sy * half.y, sz * half.z))
        for sx in (-1.0, 1.0)
        for sy in (-1.0, 1.0)
        for sz in (-1.0, 1.0)
    ]


def projected_bbox(scene, camera, item):
    # Newly authored preview cameras need a depsgraph refresh before their
    # matrix_world is reliable. Also classify front/behind explicitly in
    # camera space; relying on a projected z value caused a single distant
    # asset to be reported in every closeup on Blender 4.2.
    camera_inverse = camera.matrix_world.inverted_safe()
    points = []
    for point in bbox_corners(item):
        camera_space = camera_inverse @ point
        if camera_space.z >= -float(camera.data.clip_start):
            continue
        points.append(world_to_camera_view(scene, camera, point))
    if not points:
        return None
    return {
        "min_x": min(point.x for point in points),
        "max_x": max(point.x for point in points),
        "min_y": min(point.y for point in points),
        "max_y": max(point.y for point in points),
        "min_depth": min(point.z for point in points),
    }


def projected_bbox_overlaps_frame(bounds, margin=0.035):
    if bounds is None:
        return False
    return not (
        bounds["max_x"] < margin
        or bounds["min_x"] > 1.0 - margin
        or bounds["max_y"] < margin
        or bounds["min_y"] > 1.0 - margin
    )


def evenly_spaced_items(values, limit):
    if len(values) <= limit:
        return values
    if limit <= 1:
        return [values[0]]
    return [
        values[int(round(float(index) * float(len(values) - 1) / float(limit - 1)))]
        for index in range(limit)
    ]


def target_visibility_sample_points(item, sample_limit):
    """Return deterministic world-space surface samples for closeup occlusion tests."""
    points = []
    mesh_objects = []
    for name in item.get("instance_objects", []):
        obj = bpy.data.objects.get(name)
        if obj is not None and obj.type == "MESH" and obj.data and obj.data.vertices:
            mesh_objects.append(obj)
    if mesh_objects:
        per_object_limit = max(4, int(math.ceil(float(sample_limit) / float(len(mesh_objects)))))
        for obj in mesh_objects:
            vertices = list(obj.data.vertices)
            for vertex in evenly_spaced_items(vertices, per_object_limit):
                points.append(obj.matrix_world @ vertex.co)
    points = evenly_spaced_items(points, sample_limit)
    if points:
        return points
    center = Vector(item["bbox_center"])
    half = Vector(item["bbox_size"]) * 0.5
    return [
        center,
        center + Vector((half.x, 0.0, 0.0)),
        center - Vector((half.x, 0.0, 0.0)),
        center + Vector((0.0, half.y, 0.0)),
        center - Vector((0.0, half.y, 0.0)),
        center + Vector((0.0, 0.0, half.z)),
        center - Vector((0.0, 0.0, half.z)),
    ]


def object_belongs_to_placement(obj, item):
    target_names = set(item.get("instance_objects", []))
    root_name = item.get("root_object")
    current = obj
    while current is not None:
        if current.name in target_names or current.name == root_name:
            return True
        current = current.parent
    return False


def target_visibility_from_camera(scene, camera, item, sample_points):
    """Measure whether scene geometry blocks the authored asset from the camera."""
    depsgraph = bpy.context.evaluated_depsgraph_get()
    camera_origin = camera.matrix_world.translation.copy()
    camera_inverse = camera.matrix_world.inverted_safe()
    visible_count = 0
    tested_count = 0
    blockers = {}
    for point in sample_points:
        camera_space = camera_inverse @ point
        if camera_space.z >= -float(camera.data.clip_start):
            continue
        projected = world_to_camera_view(scene, camera, point)
        if not (0.025 <= projected.x <= 0.975 and 0.025 <= projected.y <= 0.975):
            continue
        ray = point - camera_origin
        distance = ray.length
        if distance <= float(camera.data.clip_start):
            continue
        tested_count += 1
        hit, _location, _normal, _index, hit_object, _matrix = scene.ray_cast(
            depsgraph,
            camera_origin,
            ray.normalized(),
            distance=distance + max(0.05, distance * 1e-4),
        )
        if hit and hit_object is not None and object_belongs_to_placement(hit_object, item):
            visible_count += 1
        elif hit and hit_object is not None:
            blockers[hit_object.name] = blockers.get(hit_object.name, 0) + 1
    fraction = float(visible_count) / float(tested_count) if tested_count else 0.0
    blocker_names = [
        name for name, _count in sorted(blockers.items(), key=lambda pair: (-pair[1], pair[0]))
    ]
    return {
        "visible_rays": visible_count,
        "tested_rays": tested_count,
        "visibility_fraction": fraction,
        "blocking_objects": blocker_names,
    }


def choose_isolated_closeup_camera(scene, item, placed, preview_collection, constraints):
    target = Vector(item["bbox_center"])
    size = Vector(item["bbox_size"])
    largest = max(size.x, size.y, size.z, 0.5)
    distance = largest * 2.35 + 1.5
    lens_mm = float(constraints.get("closeup_lens_mm", 70.0))
    samples = int(constraints.get("closeup_camera_azimuth_samples", 36))
    # Tall landmarks can remain visible from every low-angle azimuth even
    # when their ground footprints are well separated. Search true 3D camera
    # poses instead of repeatedly moving authored assets to solve a preview
    # composition problem.
    elevation_ratios = (0.32, 0.8, 1.6, 2.6)
    minimum_visibility = float(constraints.get("closeup_min_target_visibility_fraction", 0.35))
    visibility_points = target_visibility_sample_points(
        item,
        int(constraints.get("closeup_target_visibility_samples", 32)),
    )
    best = None
    for elevation_index, elevation_ratio in enumerate(elevation_ratios):
        horizontal_distance = distance / math.sqrt(1.0 + elevation_ratio * elevation_ratio)
        vertical_distance = horizontal_distance * elevation_ratio
        for sample in range(samples):
            angle = 2.0 * math.pi * float(sample) / float(samples)
            horizontal = Vector((math.cos(angle), math.sin(angle), 0.0))
            location = target + horizontal * horizontal_distance + Vector((
                0.0,
                0.0,
                vertical_distance + size.z * 0.15,
            ))
            camera = create_camera(
                "C2G_CLOSEUP_%s_E%02d_A%03d" % (item["placement_id"], elevation_index, sample),
                location,
                target,
                preview_collection,
                lens_mm=lens_mm,
            )
            bpy.context.view_layer.update()
            target_bounds = projected_bbox(scene, camera, item)
            target_visibility = target_visibility_from_camera(
                scene,
                camera,
                item,
                visibility_points,
            )
            target_crop = 0.0
            if target_bounds is None:
                target_crop = 100.0
            else:
                target_crop = sum([
                    max(0.0, 0.025 - target_bounds["min_x"]),
                    max(0.0, target_bounds["max_x"] - 0.975),
                    max(0.0, 0.025 - target_bounds["min_y"]),
                    max(0.0, target_bounds["max_y"] - 0.975),
                ])
            intruders = []
            intrusion_area = 0.0
            for other in placed:
                if other["placement_id"] == item["placement_id"]:
                    continue
                bounds = projected_bbox(scene, camera, other)
                if not projected_bbox_overlaps_frame(bounds):
                    continue
                intruders.append(other["placement_id"])
                width = max(0.0, min(1.0, bounds["max_x"]) - max(0.0, bounds["min_x"]))
                height = max(0.0, min(1.0, bounds["max_y"]) - max(0.0, bounds["min_y"]))
                intrusion_area += width * height
            visibility_fraction = target_visibility["visibility_fraction"]
            visibility_deficit = max(0.0, minimum_visibility - visibility_fraction)
            intruder_excess = max(
                0,
                len(intruders) - int(constraints.get("closeup_max_other_assets", 0)),
            )
            score = (
                int(visibility_deficit > 1e-9),
                intruder_excess,
                round(visibility_deficit, 8),
                len(intruders),
                round(intrusion_area, 8),
                -round(visibility_fraction, 8),
                round(target_crop, 8),
                elevation_index,
                sample,
            )
            if best is None or score < best[0]:
                if best is not None:
                    bpy.data.objects.remove(best[1], do_unlink=True)
                best = (
                    score,
                    camera,
                    intruders,
                    sample,
                    elevation_ratio,
                    target_visibility,
                )
            else:
                bpy.data.objects.remove(camera, do_unlink=True)
    return best[1], best[2], best[3], best[4], best[5]


def configure_render(scene, args):
    scene.render.resolution_x = max(320, int(args.resolution_x))
    scene.render.resolution_y = max(240, int(args.resolution_y))
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.film_transparent = False
    if scene.render.engine == "CYCLES":
        scene.cycles.samples = min(int(getattr(scene.cycles, "samples", 32)), 24)
        if hasattr(scene.cycles, "use_denoising"):
            scene.cycles.use_denoising = True


def render_one(scene, camera, path):
    scene.camera = camera
    scene.render.filepath = path
    log("RENDER_PREVIEW", path)
    bpy.ops.render.render(write_still=True)


def render_previews(placed, preview_collection, output_dir, args, constraints):
    if not placed:
        return [], []
    preview_dir = os.path.join(output_dir, "previews")
    closeup_dir = os.path.join(preview_dir, "placements")
    os.makedirs(closeup_dir, exist_ok=True)
    scene = bpy.context.scene
    configure_render(scene, args)
    centers = [Vector(item["bbox_center"]) for item in placed]
    minimum = Vector((min(point.x for point in centers), min(point.y for point in centers), min(point.z for point in centers)))
    maximum = Vector((max(point.x for point in centers), max(point.y for point in centers), max(point.z for point in centers)))
    center = (minimum + maximum) * 0.5
    span = max(maximum.x - minimum.x, maximum.y - minimum.y, 18.0)
    previews = []
    errors = []

    cameras = [
        ("overview_topdown.png", create_camera("C2G_PREVIEW_TOPDOWN", (center.x, center.y, maximum.z + span * 1.4 + 8.0), center, preview_collection, span * 1.25)),
        ("overview_oblique_a.png", create_camera("C2G_PREVIEW_OBLIQUE_A", center + Vector((span * 0.85, -span * 0.85, span * 0.55)), center, preview_collection)),
        ("overview_oblique_b.png", create_camera("C2G_PREVIEW_OBLIQUE_B", center + Vector((-span * 0.8, span * 0.75, span * 0.5)), center, preview_collection)),
    ]
    for filename, camera in cameras:
        path = os.path.join(preview_dir, filename)
        try:
            render_one(scene, camera, path)
            previews.append(path)
        except Exception as exc:
            errors.append("%s: %s" % (filename, exc))

    limit = max(0, min(int(args.preview_limit), len(placed)))
    isolation_failures = []
    target_visibility_failures = []
    for item in placed[:limit]:
        camera, intruders, azimuth_sample, elevation_ratio, target_visibility = choose_isolated_closeup_camera(
            scene,
            item,
            placed,
            preview_collection,
            constraints,
        )
        item["closeup_camera_azimuth_sample"] = int(azimuth_sample)
        item["closeup_camera_elevation_ratio"] = round(float(elevation_ratio), 6)
        item["closeup_other_visible_assets"] = list(intruders)
        item["closeup_target_visible_rays"] = int(target_visibility["visible_rays"])
        item["closeup_target_tested_rays"] = int(target_visibility["tested_rays"])
        item["closeup_target_visibility_fraction"] = round(
            float(target_visibility["visibility_fraction"]),
            6,
        )
        item["closeup_target_blocking_objects"] = list(target_visibility["blocking_objects"])
        if constraints.get("single_asset_closeups") and len(intruders) > int(constraints.get("closeup_max_other_assets", 0)):
            isolation_failures.append(
                "%s=>%s" % (item["placement_id"], ",".join(intruders))
            )
        minimum_visibility = float(constraints.get("closeup_min_target_visibility_fraction", 0.35))
        if target_visibility["visibility_fraction"] + 1e-9 < minimum_visibility:
            target_visibility_failures.append(
                "%s=%.3f<%.3f blockers=%s"
                % (
                    item["placement_id"],
                    target_visibility["visibility_fraction"],
                    minimum_visibility,
                    ",".join(target_visibility["blocking_objects"][:8]) or "none",
                )
            )
        path = os.path.join(closeup_dir, "%s_%s.png" % (item["placement_id"], item["asset_id"]))
        try:
            render_one(scene, camera, path)
            previews.append(path)
            item["closeup_preview"] = path
        except Exception as exc:
            errors.append("%s: %s" % (item["placement_id"], exc))
    if isolation_failures or target_visibility_failures:
        details = []
        if isolation_failures:
            details.append("other-assets: %s" % "; ".join(isolation_failures))
        if target_visibility_failures:
            details.append("target-occlusion: %s" % "; ".join(target_visibility_failures))
        raise RuntimeError(
            "single-asset closeup validation failed; %s"
            % " | ".join(details)
        )
    return previews, errors


def main():
    args = parse_args()
    if not math.isfinite(args.ground_embed_m) or args.ground_embed_m < 0.0:
        raise ValueError("--ground_embed_m must be a finite non-negative number")
    for name in (
        "ground_clearance_m",
        "hazard_clearance_m",
        "collectible_hover_m",
        "collectible_spin_revolutions",
        "max_ground_tilt_degrees",
        "vegetation_clearance_margin_m",
    ):
        value = getattr(args, name)
        if not math.isfinite(value) or value < 0.0:
            raise ValueError("--%s must be a finite non-negative number" % name)
    args.scene_blend = project_path(args.scene_blend) if args.scene_blend else ""
    args.placement_plan = project_path(args.placement_plan)
    args.asset_plan = project_path(args.asset_plan)
    args.candidates = project_path(args.candidates)
    args.output_dir = project_path(args.output_dir)
    args.output_blend = project_path(args.output_blend)
    os.makedirs(args.output_dir, exist_ok=True)
    os.makedirs(os.path.dirname(args.output_blend), exist_ok=True)
    preview_dir = os.path.join(args.output_dir, "previews")
    if os.path.isdir(preview_dir):
        shutil.rmtree(preview_dir)
    report_path = os.path.join(args.output_dir, "asset_placement_report.json")
    resolved_path = os.path.join(args.output_dir, "resolved_gameplay_placement_plan.json")
    report = {
        "ok": False,
        "demo_name": get_demo_name() or None,
        "scene_blend": args.scene_blend,
        "placement_plan": args.placement_plan,
        "asset_plan": args.asset_plan,
        "candidates": args.candidates,
        "output_blend": args.output_blend,
        "ground_contact_policy": "auto_normal_alignment_with_residual_slope_support_and_no_automatic_embed",
        "surface_alignment_policy": {
            "terrain": "sampled_normal_capped_by_max_ground_tilt_degrees",
            "cliff_or_wall": "full_sampled_surface_normal",
            "cliff_normal_max_abs_z": CLIFF_NORMAL_MAX_ABS_Z,
            "above_ground": "full_sampled_surface_normal_with_offset_along_normal",
        },
        "ground_embed_m": float(args.ground_embed_m),
        "ground_clearance_m": float(args.ground_clearance_m),
        "hazard_clearance_m": float(args.hazard_clearance_m),
        "collectible_hover_m": float(args.collectible_hover_m),
        "collectible_spin_revolutions": float(args.collectible_spin_revolutions),
        "max_ground_tilt_degrees": float(args.max_ground_tilt_degrees),
        "clear_blocking_vegetation": bool(args.clear_blocking_vegetation),
        "vegetation_clearance_margin_m": float(args.vegetation_clearance_margin_m),
        "cleared_vegetation": [],
        "staging_constraints": {},
        "spacing_validation": {},
        "surface_support_validation": {},
        "resolved_vegetation_warnings": [],
        "placements": [],
        "previews": [],
        "warnings": [],
        "errors": [],
    }
    try:
        placement_data = load_json(args.placement_plan, "placement plan")
        asset_data = load_json(args.asset_plan, "asset plan")
        candidate_data = load_json(args.candidates, "placement candidates")
        if not args.scene_blend:
            source = candidate_data.get("source") if isinstance(candidate_data.get("source"), dict) else {}
            recorded_blend = source.get("loaded_blend") or source.get("blend") or source.get("requested_blend")
            args.scene_blend = project_path(recorded_blend or DEFAULT_SCENE)
        report["scene_blend"] = args.scene_blend
        elements, assets, non_glb, placement_by_id, placement_to_asset, candidate_by_id, tuning_by_id = validate_inputs(placement_data, asset_data, candidate_data)
        non_glb_by_placement = {item["placement_id"]: item for item in non_glb}
        resolved, warnings = resolve_targets(elements, placement_to_asset, candidate_by_id, tuning_by_id)
        resolved_by_id = {item["placement_id"]: item for item in resolved}
        constraints = placement_staging_constraints(placement_data)
        report["staging_constraints"] = constraints
        report["surface_support_validation"] = validate_resolved_surface_support(resolved)
        if not report["surface_support_validation"]["ok"]:
            examples = "; ".join(
                "%s/%s requires %.3fm but candidate %s supports %.3fm(%s)" % (
                    item["placement_id"],
                    item["asset_id"],
                    item["required_footprint_radius_m"],
                    item["candidate_id"],
                    item["max_supported_footprint_radius_m"],
                    item["support_class"],
                )
                for item in report["surface_support_validation"]["violations"]
            )
            raise ValueError(
                "authored grounded assets exceed measured platform support: " + examples
            )
        log(
            "VISIBLE_ASSET_SURFACE_SUPPORT_OK",
            report["surface_support_validation"]["checked_grounded_asset_count"],
            "legacy_unmeasured",
            len(report["surface_support_validation"]["legacy_unmeasured_placement_ids"]),
        )
        report["spacing_validation"] = validate_resolved_spacing(resolved, constraints)
        if not report["spacing_validation"]["ok"]:
            violations = report["spacing_validation"]["violations"]
            examples = "; ".join(
                "%s/%s=center %.3fm, footprint gap %.3fm(%s)" % (
                    item["placement_a"],
                    item["placement_b"],
                    item["distance_m"],
                    item["footprint_gap_m"],
                    item["distance_mode"],
                )
                for item in violations[:8]
            )
            raise ValueError(
                "authored visible assets violate center/footprint spacing "
                "(minimum center %.3fm, minimum footprint gap %.3fm): %s"
                % (
                    constraints["minimum_visible_asset_center_distance_m"],
                    constraints["minimum_visible_asset_footprint_gap_m"],
                    examples,
                )
            )
        log(
            "VISIBLE_ASSET_SPACING_OK",
            report["spacing_validation"]["visible_asset_count"],
            "closest",
            report["spacing_validation"]["closest_pair"],
        )
        if args.clear_blocking_vegetation:
            report["resolved_vegetation_warnings"].extend(warnings)
        else:
            report["warnings"].extend(warnings)
        write_json(resolved_path, {
            "coordinate_source": args.candidates,
            "resolution_policy": "execute_exact_authored_candidates_with_deterministic_surface_normal_alignment_and_optional_human_tuning_without_python_reselection",
            "staging_constraints": constraints,
            "surface_support_validation": report["surface_support_validation"],
            "spacing_validation": report["spacing_validation"],
            "tuning_overrides": tuning_by_id,
            "placements": resolved,
        })

        if not os.path.isfile(args.scene_blend):
            raise FileNotFoundError("source scene not found: %s" % args.scene_blend)
        log("OPEN_SCENE", args.scene_blend)
        bpy.ops.wm.open_mainfile(filepath=args.scene_blend)
        if args.clear_blocking_vegetation:
            report["cleared_vegetation"] = clear_blocking_vegetation(
                elements,
                placement_to_asset,
                resolved_by_id,
                tuning_by_id,
                args.vegetation_clearance_margin_m,
            )
            log("CLEARED_BLOCKING_VEGETATION", len(report["cleared_vegetation"]))
        for name in (ASSET_COLLECTION, TEMPLATE_COLLECTION, PROXY_COLLECTION, PREVIEW_COLLECTION):
            remove_collection(name)
        asset_collection = new_collection(ASSET_COLLECTION)
        template_collection = new_collection(TEMPLATE_COLLECTION)
        proxy_collection = new_collection(PROXY_COLLECTION)
        preview_collection = new_collection(PREVIEW_COLLECTION)
        proxy_collection.hide_render = True
        staged_assets = [asset for asset in assets if asset.get("placement_ids")]
        templates = load_asset_templates(staged_assets, template_collection)

        placed_for_preview = []
        for placement in elements:
            placement_id = placement["placement_id"]
            tuning = tuning_by_id.get(placement_id, {})
            resolved_item = resolved_by_id[placement_id]
            asset = placement_to_asset.get(placement_id)
            if asset is None:
                logical_proxy = create_logical_proxy(
                    placement,
                    resolved_item,
                    non_glb_by_placement.get(placement_id),
                    proxy_collection,
                )
                report["placements"].append({
                    "placement_id": placement_id,
                    "element_type": placement.get("element_type"),
                    "status": "logical_only",
                    "world_xyz": resolved_item["world_xyz"],
                    "proxy_object": logical_proxy.name,
                    "proxy_type": logical_proxy["code2games_proxy_type"],
                })
                continue
            try:
                (
                    root,
                    objects,
                    bounds,
                    scale,
                    ground_adjustment_z,
                    applied_clearance_z,
                    automatic_sink_z,
                    automatic_support_lift_m,
                    ground_tilt_degrees,
                    surface_alignment_mode,
                    normal_alignment_error_degrees_value,
                    orientation_constraint_error_degrees_value,
                ) = instantiate_asset(
                    asset,
                    placement,
                    resolved_item,
                    templates[asset["asset_id"]],
                    asset_collection,
                    args.ground_embed_m,
                    args.ground_clearance_m,
                    args.hazard_clearance_m,
                    args.collectible_hover_m,
                    args.max_ground_tilt_degrees,
                    tuning,
                )
                spin_animated = animate_collectible_spin(root, placement, args.collectible_spin_revolutions)
                proxy = create_proxy(placement, asset, bounds, root, proxy_collection)
                entry = {
                    "placement_id": placement_id,
                    "element_type": placement.get("element_type"),
                    "asset_id": asset["asset_id"],
                    "glb_path": templates[asset["asset_id"]]["glb_path"],
                    "status": "placed",
                    "original_candidate_id": resolved_item["original_candidate_id"],
                    "resolved_candidate_id": resolved_item["resolved_candidate_id"],
                    "shift_xy": round(float(resolved_item["shift_xy"]), 6),
                    "world_xyz": vec_list(resolved_item["world_xyz"]),
                    "expected_size_meters": vec_list(tuned_size(asset, tuning)),
                    "footprint_radius_m": round(float(resolved_item["footprint_radius"]), 6),
                    "candidate_support_profile_available": bool(resolved_item.get("support_profile_available")),
                    "candidate_max_supported_footprint_radius_m": (
                        round(float(resolved_item["max_supported_footprint_radius_m"]), 6)
                        if resolved_item.get("max_supported_footprint_radius_m") is not None
                        else None
                    ),
                    "candidate_support_class": resolved_item.get("support_class"),
                    "placement_tuning": tuning,
                    "scale_xyz": vec_list(scale),
                    "ground_z_adjustment_m": round(float(ground_adjustment_z), 6),
                    "applied_clearance_z_m": round(float(applied_clearance_z), 6),
                    "automatic_sink_z_m": round(float(automatic_sink_z), 6),
                    "automatic_support_lift_m": round(float(automatic_support_lift_m), 6),
                    "ground_tilt_degrees": round(float(ground_tilt_degrees), 6),
                    "surface_alignment_mode": surface_alignment_mode,
                    "normal_alignment_error_degrees": round(float(normal_alignment_error_degrees_value), 6),
                    "orientation_constraint_error_degrees": round(
                        float(orientation_constraint_error_degrees_value),
                        6,
                    ),
                    "spin_animated": bool(spin_animated),
                    "spin_revolutions": float(args.collectible_spin_revolutions) if spin_animated else 0.0,
                    "bbox_min": vec_list(bounds["min"]),
                    "bbox_max": vec_list(bounds["max"]),
                    "bbox_center": vec_list(bounds["center"]),
                    "bbox_size": vec_list(bounds["size"]),
                    "root_object": root.name,
                    "instance_objects": [obj.name for obj in objects],
                    "proxy_object": proxy.name if proxy else None,
                    "mesh_data_reused": True,
                }
                report["placements"].append(entry)
                placed_for_preview.append(entry)
                log("PLACED", placement_id, asset["asset_id"], "shift", entry["shift_xy"])
            except Exception as exc:
                error = "%s: %s" % (type(exc).__name__, exc)
                report["placements"].append({"placement_id": placement_id, "asset_id": asset["asset_id"], "status": "failed", "error": error})
                report["errors"].append({"placement_id": placement_id, "error": error, "traceback": traceback.format_exc()})

        failed = [item for item in report["placements"] if item["status"] == "failed"]
        if failed:
            raise RuntimeError("%d gameplay asset placements failed" % len(failed))
        if not args.skip_previews:
            previews, preview_errors = render_previews(placed_for_preview, preview_collection, args.output_dir, args, constraints)
            report["previews"] = previews
            report["warnings"].extend("preview failed: " + error for error in preview_errors)
        try:
            bpy.ops.file.pack_all()
        except Exception as exc:
            report["warnings"].append("pack_all failed: %s" % exc)
        log("SAVE_BLEND", args.output_blend)
        bpy.ops.wm.save_as_mainfile(filepath=args.output_blend)
        report["ok"] = True
        report["asset_count"] = len(assets)
        report["placement_count"] = len(elements)
        report["visible_placement_count"] = len(placed_for_preview)
        report["logical_placement_count"] = len(non_glb)
        report["resolved_placement_plan"] = resolved_path
        write_json(report_path, report)
        log("ASSET_PLACEMENT_OK", "assets", len(assets), "placements", len(elements), "output", args.output_blend)
    except Exception as exc:
        report["errors"].append({"stage": "main", "error": "%s: %s" % (type(exc).__name__, exc), "traceback": traceback.format_exc()})
        write_json(report_path, report)
        log("ASSET_PLACEMENT_FAILED", type(exc).__name__, exc)
        raise


if __name__ == "__main__":
    main()
