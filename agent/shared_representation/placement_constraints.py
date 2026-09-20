"""Deterministic geometry and gameplay-element contracts for placement."""
import math


# Keep this taxonomy compact and semantic. Genre-specific details such as the
# weapon class, enemy archetype, vehicle model, or checkpoint order belong in
# the human/LLM-authored purpose fields and later director plans.
ELEMENT_TYPE_SPECS = {
    # Existing world-overlay primitives.
    "collectible": "visible item collected for score, progression, or objectives",
    "obstacle": "visible object that blocks, redirects, or tests movement",
    "event_trigger": "logical trigger that starts a scripted gameplay event",
    "goal_area": "visible destination marker plus completion trigger",
    "hazard_zone": "visible dangerous surface or volume plus hazard trigger",
    "safe_zone": "logical or visibly marked area where danger is reduced",
    "landmark": "visible orientation or spectacle asset",
    # Actors and vehicles. These are logical spawn anchors; the gameplay
    # director owns character/vehicle loading, rigging, AI, and control.
    "player_spawn": "initial player or respawn anchor",
    "enemy_spawn": "hostile actor spawn or reinforcement anchor",
    "npc_spawn": "friendly, neutral, civilian, companion, or quest NPC anchor",
    "vehicle_spawn": "player, opponent, traffic, or ambient vehicle anchor",
    # Items, objectives, interaction, and combat affordances.
    "item_pickup": "weapon, ammunition, health, power-up, key, or inventory pickup",
    "objective_object": "visible mission-critical object to protect, carry, activate, or destroy",
    "interaction_point": "logical or visibly represented use, dialogue, switch, door, or terminal point",
    "cover_point": "tactical position for player or AI cover behavior",
    "destructible": "visible breakable object with a later damage response",
    "puzzle_element": "visible switch, mechanism, movable piece, or puzzle target",
    # Progression, navigation, traversal, and cameras.
    "checkpoint": "logical or visibly marked progression/race checkpoint",
    "capture_zone": "logical or visibly marked defend, contest, or capture area",
    "traversal_point": "climb, vault, jump, grapple, launch, landing, or transition anchor",
    "navigation_anchor": "unordered AI or gameplay navigation anchor for later path planning",
    "camera_anchor": "first-person, third-person, vehicle, cinematic, or spectator camera anchor",
    "moving_platform": "visible platform or carrier whose motion is added by the director/runtime",
}

ALLOWED_ELEMENT_TYPES = frozenset(ELEMENT_TYPE_SPECS)
CLIFF_NORMAL_MAX_ABS_Z = 0.35
DEFAULT_MIN_VISIBLE_ASSET_CENTER_DISTANCE_M = 6.0
DEFAULT_MIN_VISIBLE_ASSET_FOOTPRINT_GAP_M = 0.75
MINIMUM_GROUNDED_SUPPORT_RADIUS_M = 0.35
ALLOWED_SURFACE_ALIGNMENT_MODES = frozenset({
    "auto",
    "upright",
    "terrain_normal",
    "full_surface_normal",
})

# These types must become visible scene assets before gameplay directing.
VISIBLE_ASSET_REQUIRED_ELEMENT_TYPES = frozenset({
    "collectible",
    "obstacle",
    "goal_area",
    "hazard_zone",
    "landmark",
    "item_pickup",
    "objective_object",
    "destructible",
    "puzzle_element",
    "moving_platform",
})

# These are intentionally anchors, not static Hunyuan-generated actor props.
LOGICAL_ONLY_ELEMENT_TYPES = frozenset({
    "player_spawn",
    "enemy_spawn",
    "npc_spawn",
    "vehicle_spawn",
    "navigation_anchor",
    "camera_anchor",
})

# These may remain logical or receive a visible marker/prop when useful.
OPTIONAL_ASSET_ELEMENT_TYPES = frozenset({
    "event_trigger",
    "safe_zone",
    "interaction_point",
    "cover_point",
    "checkpoint",
    "capture_zone",
    "traversal_point",
})

NON_GLB_ALLOWED_ELEMENT_TYPES = LOGICAL_ONLY_ELEMENT_TYPES | OPTIONAL_ASSET_ELEMENT_TYPES


def element_type_catalog_text():
    return "; ".join(
        "%s: %s" % (name, ELEMENT_TYPE_SPECS[name])
        for name in sorted(ELEMENT_TYPE_SPECS)
    )


def is_finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def finite_number_in_range(value, minimum, maximum):
    return is_finite_number(value) and minimum <= float(value) <= maximum


def get_hard_constraint_config():
    return {
        "vertical_placement_rules": {
            "height_is_not_fixed_by_element_type": True,
            "height_offset_is_relative_to_candidate_anchor": True,
            "llm_must_choose_height_from_specific_gameplay_intent": True,
            "asset_extent_and_scene_mesh_collision_are_validated_after_asset_realization": True,
        },
        "surface_alignment_rules": {
            "default_mode": "full_surface_normal",
            "all_physical_assets_use_full_sampled_surface_normal": True,
            "cliff_normal_max_abs_z": CLIFF_NORMAL_MAX_ABS_Z,
            "above_ground_height_is_applied_along_sampled_surface_normal": True,
            "maximum_normal_alignment_error_degrees": 0.05,
            "llm_may_request_upright_or_explicit_surface_alignment": False,
        },
        "asset_spacing_rules": {
            "minimum_visible_asset_center_distance_m": DEFAULT_MIN_VISIBLE_ASSET_CENTER_DISTANCE_M,
            "minimum_visible_asset_footprint_gap_m": DEFAULT_MIN_VISIBLE_ASSET_FOOTPRINT_GAP_M,
            "distance_mode": "auto",
            "ground_assets_use_xy_distance": True,
            "cliff_or_wall_assets_use_xyz_distance": True,
            "staging_must_reject_violations_without_automatic_reselection": True,
        },
        "physical_platform_rules": {
            "minimum_grounded_support_radius_m": MINIMUM_GROUNDED_SUPPORT_RADIUS_M,
            "candidate_support_is_measured_across_the_full_footprint": True,
            "realized_asset_radius_must_not_exceed_candidate_support_radius": True,
            "flat_platforms_and_clearings_are_preferred_over_slopes": True,
            "staging_must_reject_all_support_violations_without_lifting_or_reselection": True,
        },
        "scene_coverage_rules": {
            "spread_across_scene_sectors": True,
            "spread_across_elevation_bands": True,
            "use_multiple_terrain_regions_when_gameplay_allows": True,
            "vegetated_clearings_are_playable_space": True,
        },
    }


def candidate_surface_geometry(candidate):
    """Return coordinate-free orientation facts derived from a sampled normal."""
    normal = candidate.get("normal_xyz") if isinstance(candidate, dict) else None
    if not isinstance(normal, (list, tuple)) or len(normal) != 3 or not all(is_finite_number(item) for item in normal):
        return {"surface_orientation": "unknown", "surface_tilt_degrees": None}
    length = math.sqrt(sum(float(item) * float(item) for item in normal))
    if length <= 1e-8:
        return {"surface_orientation": "unknown", "surface_tilt_degrees": None}
    abs_z = abs(float(normal[2]) / length)
    tilt = math.degrees(math.acos(max(0.0, min(1.0, abs_z))))
    if abs_z <= CLIFF_NORMAL_MAX_ABS_Z:
        orientation = "cliff_or_wall"
    elif tilt >= 35.0:
        orientation = "steep_slope"
    elif tilt >= 10.0:
        orientation = "gentle_slope"
    else:
        orientation = "level_ground"
    return {"surface_orientation": orientation, "surface_tilt_degrees": round(tilt, 3)}


def resolved_surface_alignment_mode(selection, candidate):
    requested = selection.get("surface_alignment_mode", "auto") if isinstance(selection, dict) else "auto"
    if requested not in ALLOWED_SURFACE_ALIGNMENT_MODES:
        raise ValueError("invalid surface_alignment_mode: %s" % requested)
    # Every physical asset follows the complete sampled normal.  `upright` and
    # capped terrain alignment caused visible penetration on slopes and are
    # retained only as accepted legacy input values.
    return "full_surface_normal"


def valid_nearest_vegetation_context(value):
    return value is None or (isinstance(value, dict) and isinstance(value.get("object_name"), str) and bool(value["object_name"].strip()) and finite_number_in_range(value.get("distance_xy"), 0.0, 1.0e6))


def vegetation_distance(candidate, vegetation_type):
    context = candidate.get("nearest_" + vegetation_type)
    value = context.get("distance_xy") if isinstance(context, dict) else None
    return float(value) if finite_number_in_range(value, 0.0, 1.0e6) else None


def has_no_vegetation_overlap(candidate):
    tree, bush = vegetation_distance(candidate, "tree"), vegetation_distance(candidate, "bush")
    return (tree is None or tree > 0.0) and (bush is None or bush > 0.0)


def physically_supported_modes(candidate):
    kind = candidate.get("candidate_kind")
    if kind == "ground_surface":
        profile = candidate.get("support_profile")
        if isinstance(profile, dict) and profile.get("evaluated") is True:
            radius = profile.get("max_supported_footprint_radius_m")
            if not is_finite_number(radius) or float(radius) < MINIMUM_GROUNDED_SUPPORT_RADIUS_M:
                return ["above_ground"]
            if candidate.get("surface_type") == "ground" and not has_no_vegetation_overlap(candidate):
                return ["above_ground"]
        return ["on_ground", "area_center", "above_ground"]
    if kind in {"occluder_anchor", "visual_anchor"}:
        return ["existing_anchor"]
    if kind == "blocked_surface":
        return ["area_center"]
    return []


def placement_mode_is_physically_compatible(candidate, placement_mode, height_offset):
    if placement_mode not in physically_supported_modes(candidate):
        return False
    if placement_mode == "above_ground":
        return is_finite_number(height_offset) and float(height_offset) > 0.0
    return is_finite_number(height_offset) and float(height_offset) == 0.0


def _violation(selection, constraint_type, actual_value=None, required_min=None, required_max=None):
    result = {"placement_id": selection.get("placement_id"), "candidate_id": selection.get("selected_candidate_id") or selection.get("candidate_id"), "constraint_type": constraint_type}
    if actual_value is not None: result["actual_value"] = actual_value
    if required_min is not None: result["required_min"] = required_min
    if required_max is not None: result["required_max"] = required_max
    return result


def requires_ground_vegetation_clearance(selection, candidate):
    return selection.get("placement_mode") != "existing_anchor" and candidate.get("candidate_kind") == "ground_surface" and candidate.get("surface_type") == "ground"


def validate_realized_footprint_support(resolved):
    """Compare realized grounded-asset radii with measured candidate support."""
    violations = []
    legacy_unmeasured = []
    checked = 0
    for item in resolved:
        if item.get("logical_only") or item.get("placement_mode") in {"above_ground", "existing_anchor"}:
            continue
        if not item.get("support_profile_available"):
            legacy_unmeasured.append(item["placement_id"])
            continue
        checked += 1
        required = float(item["footprint_radius"])
        available = float(item["max_supported_footprint_radius_m"])
        if required > available + 1e-6:
            violations.append({
                "placement_id": item["placement_id"],
                "asset_id": item.get("asset_id"),
                "candidate_id": item.get("resolved_candidate_id"),
                "required_footprint_radius_m": round(required, 6),
                "max_supported_footprint_radius_m": round(available, 6),
                "support_class": item.get("support_class"),
            })
    return {
        "ok": not violations,
        "checked_grounded_asset_count": checked,
        "legacy_unmeasured_placement_ids": legacy_unmeasured,
        "violation_count": len(violations),
        "violations": violations,
    }


def validate_selected_element_hard_constraints(selection, candidate, hard_constraint_config):
    violations = []
    if not placement_mode_is_physically_compatible(candidate, selection.get("placement_mode"), selection.get("height_offset")):
        violations.append(_violation(selection, "placement_mode_physical_compatibility"))
    return violations


def run_self_tests():
    config = get_hard_constraint_config(); ground = {"candidate_kind":"ground_surface","surface_type":"ground","nearest_tree":{"object_name":"t","distance_xy":2},"nearest_bush":{"object_name":"b","distance_xy":2}}
    assert physically_supported_modes(ground) == ["on_ground", "area_center", "above_ground"]
    unsupported = dict(ground); unsupported["support_profile"] = {"evaluated": True, "max_supported_footprint_radius_m": 0.0}
    supported = dict(ground); supported["support_profile"] = {"evaluated": True, "max_supported_footprint_radius_m": 1.25}
    obstructed = dict(supported); obstructed["nearest_tree"] = {"object_name":"t","distance_xy":0}
    assert physically_supported_modes(unsupported) == ["above_ground"]
    assert physically_supported_modes(supported) == ["on_ground", "area_center", "above_ground"]
    assert physically_supported_modes(obstructed) == ["above_ground"]
    support_validation = validate_realized_footprint_support([
        {"placement_id":"fits","asset_id":"a","resolved_candidate_id":"c1","placement_mode":"on_ground","logical_only":False,"support_profile_available":True,"footprint_radius":1.0,"max_supported_footprint_radius_m":1.25,"support_class":"medium"},
        {"placement_id":"fails","asset_id":"b","resolved_candidate_id":"c2","placement_mode":"on_ground","logical_only":False,"support_profile_available":True,"footprint_radius":2.0,"max_supported_footprint_radius_m":0.75,"support_class":"small"},
        {"placement_id":"legacy","asset_id":"c","resolved_candidate_id":"c3","placement_mode":"on_ground","logical_only":False,"support_profile_available":False,"footprint_radius":1.0,"max_supported_footprint_radius_m":None,"support_class":"legacy_unmeasured"},
    ])
    assert not support_validation["ok"]
    assert support_validation["violation_count"] == 1
    assert support_validation["violations"][0]["placement_id"] == "fails"
    assert support_validation["legacy_unmeasured_placement_ids"] == ["legacy"]
    assert not validate_selected_element_hard_constraints({"placement_id":"p","candidate_id":"c","element_type":"collectible","placement_mode":"on_ground","height_offset":0}, ground, config)
    assert resolved_surface_alignment_mode({"placement_mode":"existing_anchor"}, {"normal_xyz":[1,0,0]}) == "full_surface_normal"
    assert resolved_surface_alignment_mode({"placement_mode":"on_ground"}, {"normal_xyz":[0,0,1]}) == "full_surface_normal"
    assert resolved_surface_alignment_mode({"placement_mode":"above_ground"}, {"normal_xyz":[0.6,0,0.8]}) == "full_surface_normal"
    print("PLACEMENT_CONSTRAINTS_SELF_TEST_OK")


if __name__ == "__main__":
    import sys
    if "--self_test" in sys.argv: run_self_tests()
