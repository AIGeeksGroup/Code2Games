"""Single-pass gameplay placement: one LLM call, then immediate coordinate restore."""
import argparse
import json
import math
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
COMMON_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", "shared_representation"))
for _path in (SCRIPT_DIR, COMMON_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from placement_constraints import (
    ALLOWED_ELEMENT_TYPES,
    ALLOWED_SURFACE_ALIGNMENT_MODES,
    DEFAULT_MIN_VISIBLE_ASSET_CENTER_DISTANCE_M,
    DEFAULT_MIN_VISIBLE_ASSET_FOOTPRINT_GAP_M,
    candidate_surface_geometry,
    element_type_catalog_text,
    physically_supported_modes,
    resolved_surface_alignment_mode,
)
from placement_io import clear_downstream_outputs, load_json, packet_path, sanitize_for_llm, strip_json_markdown, write_json, write_text
from run_placement_selector import call_llm, llm_config


def parse_args():
    parser = argparse.ArgumentParser(description="Single-pass Code2Games placement")
    parser.add_argument("--user_prompt", default="design a gameplay demo that fits the current scene")
    return parser.parse_args()


def candidate_for_llm(candidate):
    result = {key: candidate.get(key) for key in (
        "candidate_id", "source_ray_id", "hit_index", "is_first_hit", "screen_uv",
        "grid_cell_3x3", "candidate_kind", "surface_type", "walkability",
        "distance_from_camera", "distance_bucket", "nearest_tree",
        "nearest_bush", "placement_tags",
    )}
    result["allowed_placement_modes"] = physically_supported_modes(candidate)
    result.update(candidate_surface_geometry(candidate))
    return result


def positive_finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) and float(value) > 0.0


def build_prompt(user_prompt, game_rule, candidates, scene, visual):
    payload = {
        "user_prompt": user_prompt,
        "game_rule_plan": sanitize_for_llm(game_rule),
        "scene_understanding": sanitize_for_llm(scene),
        "game_rule_visual_notes": sanitize_for_llm(visual),
        "placement_candidates": [candidate_for_llm(item) for item in candidates],
    }
    return "\n".join([
        "# Code2Games single-pass placement",
        "Choose one existing candidate_id for every required gameplay element in game_rule_plan.required_gameplay_elements_for_placement.",
        "Use only candidate ids and allowed_placement_modes from the input. Never invent coordinates, paths, code, or candidate ids.",
        "The existing scene is the visual base, not the gameplay asset set. Trees, bushes, rocks, and terrain remain background environment. Every selected candidate is the actual location for a newly added gameplay element or logical zone.",
        "nearest_tree and nearest_bush are composition context: prefer suitable ground near environment objects so the final shot has depth and visual integration, but leave enough clearance for a new standalone object. Near does not mean on, inside, or reused.",
        "Prefer clearings inside or between the existing scenic vegetation, where trees, bushes, rocks, or other scene features remain nearby around the gameplay space. Do not place most gameplay elements on empty ground outside the scenic cluster merely because it has maximum clearance. The intended result is gameplay embedded in the scenery, not a separate prop field beside it.",
        "Balance integration and fit: choose a genuinely empty patch within the scenic area whose local clearance can contain the later asset. A point touching vegetation is not a clearing, but a moderately open interior gap is usually compositionally better than a very distant exterior point. Use the complete scene understanding and distribute elements through multiple natural interior clearings rather than forming one isolated open-field group.",
        "Visible assets must not form accidental prop piles. Spread them across different rays, screen grid cells, and depth buckets wherever the game rules permit. The deterministic staging step enforces both a minimum center distance and a positive footprint-to-footprint gap and rejects the plan instead of silently moving assets.",
        "Interpret nearest_tree.distance_xy and nearest_bush.distance_xy as horizontal clearance in scene meters from the candidate to the vegetation object's XY bounding box: 0 means the point overlaps that projected bounding box, below 1 meter is crowded, 1 to below 3 meters has limited clearance, 3 to below 5 meters is open, and 5 meters or more is very open. Never call a 0.16-meter clearance open.",
        "The DEFAULT_CAMERA context is only an initial scene reference, not the final gameplay camera. Do not optimize placement only for that view; the later director may create a first-person, third-person, vehicle, cinematic, or spectator camera.",
        "Candidate facts override narrative convenience. Every placement_reason must agree with that candidate's candidate_kind, surface_type, walkability, vegetation distances, screen grid cell, and hit_object. Never describe a vegetation-crowded candidate as open or clear.",
        "Supported element types: " + element_type_catalog_text() + ".",
        "Collectibles should usually use on_ground. Only a small minority may use above_ground when they intentionally guide a reachable jump or form a clear gameplay cue. Do not make every collectible airborne and do not mechanically reuse one height for all airborne collectibles.",
        "Player, enemy, NPC, and vehicle spawns are logical anchors. Put ground actors on walkable, non-overlapping ground with clearance suitable for their role; the later director supplies rigs, AI, vehicles, and animation.",
        "Cover points need tactically useful nearby occlusion and reachable ground. Camera anchors and traversal points may use above_ground only when the requested height has a clear purpose.",
        "Checkpoints, capture zones, event triggers, objectives, and navigation anchors must form a coherent layout, but do not invent an ordered path or reuse one candidate for multiple elements.",
        "Item pickups must be reachable and readable. Destructibles, moving platforms, puzzle elements, and objective objects need enough physical clearance for their later visible asset.",
        "A safe_zone must use a ground_surface candidate with good or medium walkability, with both nearest_tree.distance_xy and nearest_bush.distance_xy at least 3 meters. Do not use vegetation-crowded points for safe zones.",
        "Obstacles are newly added game obstacles, not existing vegetation. Select walkable ground near useful visual context, never an existing object surface. Their later asset should clearly communicate jump, dodge, or route-around gameplay.",
        "Landmarks and goal markers are also newly added visible assets. Select ground beside useful scene context rather than using an existing tree, bush, or rock as the landmark or goal.",
        "Choose the height for the specific object, not from a fixed element-type rule. Use placement_mode=above_ground and a positive numeric height_offset only when the gameplay object should be airborne and reachable. For every other mode use height_offset=0.",
        "Each placement may optionally specify surface_alignment_mode=auto|upright|terrain_normal|full_surface_normal plus max_tilt_degrees, surface_clearance_m, vegetation_clearance_radius_m, and yaw_degrees. Prefer auto: Python deterministically uses terrain normals on ground/slopes and full sampled normals on cliff_or_wall candidates. Use upright only for objects whose gameplay meaning requires vertical orientation.",
        "Asset dimensions, fog or water volume height, interaction size, collision size, and exact mesh intersection are decided later after the real asset exists.",
        "Return strict JSON only.",
        "Schema: {selection_status:{usable:true,confidence:0.0,reason:'non-empty'},placements:[{element_type,source_requirement_index,selected_candidate_id,placement_mode,height_offset,gameplay_purpose,placement_reason,surface_alignment_mode?,max_tilt_degrees?,surface_clearance_m?,vegetation_clearance_radius_m?,yaw_degrees?}],selection_notes:['layout summary']}",
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    ])


def resolve_placements(selection, requirements, candidates):
    placements = selection.get("placements")
    if not isinstance(placements, list):
        raise ValueError("LLM output placements must be a list")
    requirement_by_index = {index: item for index, item in enumerate(requirements)}
    candidate_by_id = {item["candidate_id"]: item for item in candidates}
    expected_total = sum(item["required_count"] for item in requirements)
    if len(placements) != expected_total:
        raise ValueError("LLM output placement count must be %d" % expected_total)

    used_candidates = set()
    counts = {}
    elements = []
    for index, item in enumerate(placements):
        if not isinstance(item, dict):
            raise ValueError("placement %d must be an object" % index)
        source_index = item.get("source_requirement_index")
        requirement = requirement_by_index.get(source_index)
        if requirement is None or item.get("element_type") != requirement.get("element_type"):
            raise ValueError("placement %d does not match its game-rule requirement" % index)
        candidate_id = item.get("selected_candidate_id")
        candidate = candidate_by_id.get(candidate_id)
        if candidate is None or candidate_id in used_candidates:
            raise ValueError("placement %d has an invalid or duplicate candidate_id" % index)
        used_candidates.add(candidate_id)
        counts[source_index] = counts.get(source_index, 0) + 1

        mode = item.get("placement_mode")
        if mode not in physically_supported_modes(candidate):
            raise ValueError("placement %d uses an unsupported placement_mode" % index)
        height_offset = item.get("height_offset", 0)
        if mode == "above_ground":
            if not positive_finite(height_offset):
                raise ValueError("placement %d above_ground height_offset must be a positive number" % index)
            height_offset = float(height_offset)
        else:
            height_offset = 0.0

        alignment_mode = item.get("surface_alignment_mode", "auto")
        if alignment_mode not in ALLOWED_SURFACE_ALIGNMENT_MODES:
            raise ValueError("placement %d uses an invalid surface_alignment_mode" % index)
        if mode == "above_ground" and alignment_mode not in {"auto", "upright"}:
            raise ValueError("placement %d above_ground alignment must be auto or upright" % index)
        for key, minimum, maximum in (
            ("max_tilt_degrees", 0.0, 180.0),
            ("surface_clearance_m", 0.0, 10.0),
            ("vegetation_clearance_radius_m", 0.0, 100.0),
            ("yaw_degrees", -180.0, 180.0),
        ):
            value = item.get(key)
            if key in item and not (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
                and minimum <= float(value) <= maximum
            ):
                raise ValueError("placement %d has invalid %s" % (index, key))

        anchor = list(candidate["world_xyz"])
        world = [anchor[0], anchor[1], anchor[2] + height_offset] if mode == "above_ground" else list(anchor)
        elements.append({
            "placement_id": "placement_%03d" % index,
            "element_type": item["element_type"],
            "source_requirement_index": source_index,
            "candidate_id": candidate_id,
            "anchor_world_xyz": anchor,
            "world_xyz": world,
            "normal_xyz": candidate.get("normal_xyz"),
            "candidate_kind": candidate.get("candidate_kind"),
            "surface_type": candidate.get("surface_type"),
            "hit_object": candidate.get("hit_object"),
            "visual_context": {
                "nearest_tree": candidate.get("nearest_tree"),
                "nearest_bush": candidate.get("nearest_bush"),
            },
            "location_semantics": "actual_new_gameplay_element_location_near_scene_context",
            "placement_mode": mode,
            "height_offset": height_offset,
            "gameplay_purpose": str(item.get("gameplay_purpose") or ""),
            "placement_reason": str(item.get("placement_reason") or ""),
            "scene_mesh_collision_status": "deferred_until_asset_geometry_is_known",
        })
        tuning = {"surface_alignment_mode": resolved_surface_alignment_mode(item, candidate)}
        for key in ("max_tilt_degrees", "surface_clearance_m", "vegetation_clearance_radius_m", "yaw_degrees"):
            if key in item:
                tuning[key] = float(item[key])
        elements[-1]["_resolved_tuning"] = tuning

    for source_index, requirement in requirement_by_index.items():
        if counts.get(source_index, 0) != requirement.get("required_count"):
            raise ValueError("requirement %d placement count is wrong" % source_index)
    tuning_overrides = {item["placement_id"]: item.pop("_resolved_tuning") for item in elements}
    return elements, tuning_overrides


def main(user_prompt="design a gameplay demo that fits the current scene"):
    clear_downstream_outputs("prepare")
    try:
        game_rule = load_json(packet_path("game_rule_plan.json"))
        candidate_data = load_json(packet_path("placement_candidates.json"))
        scene = load_json(packet_path("scene_understanding_default_camera.json"))
        visual = load_json(packet_path("game_rule_visual_notes.json"))
        requirements = game_rule.get("game_rule_plan", {}).get("required_gameplay_elements_for_placement", [])
        candidates = candidate_data.get("candidates", [])
        llm_candidates = [item for item in candidates if physically_supported_modes(item)]
        if not requirements or not candidates:
            raise ValueError("game rules or placement candidates are empty")
        invalid_types = []
        for item in requirements:
            element_type = item.get("element_type") if isinstance(item, dict) else "<non-object requirement>"
            if element_type not in ALLOWED_ELEMENT_TYPES:
                invalid_types.append(element_type)
        invalid_types = sorted(set(invalid_types), key=str)
        if invalid_types:
            raise ValueError("game rules contain unsupported element types: %s" % invalid_types)
        if not llm_candidates:
            raise ValueError("no physically supported candidates are available for new gameplay elements")

        print("PLACEMENT_START candidates", len(llm_candidates))
        prompt = build_prompt(user_prompt, game_rule, llm_candidates, scene, visual)
        write_text(packet_path("gameplay_placement_prompt.md"), prompt)
        raw = call_llm(prompt, llm_config())
        write_text(packet_path("gameplay_placement_raw_response.md"), raw)
        selection = json.loads(strip_json_markdown(raw))
        status = selection.get("selection_status")
        if not isinstance(status, dict) or status.get("usable") is not True:
            raise ValueError(str((status or {}).get("reason") or "LLM placement is not usable"))

        elements, tuning_overrides = resolve_placements(selection, requirements, candidates)
        plan = {
            "placement_status": status,
            "placement_plan": {
                "coordinate_source": "placement_candidates.json",
                "placement_scope": "candidate_region_from_current_scene_extraction",
                "elements": elements,
                "tuning_overrides": tuning_overrides,
                "staging_constraints": {
                    "minimum_visible_asset_center_distance_m": DEFAULT_MIN_VISIBLE_ASSET_CENTER_DISTANCE_M,
                    "minimum_visible_asset_footprint_gap_m": DEFAULT_MIN_VISIBLE_ASSET_FOOTPRINT_GAP_M,
                    "distance_mode": "auto",
                },
                "selection_notes": selection.get("selection_notes", []),
            },
        }
        write_json(packet_path("gameplay_placement_plan.json"), plan)
        print("PLACEMENT_OK elements", len(elements))
        return {"ok": True, "output_files": [packet_path("gameplay_placement_plan.json")]}
    except Exception as exc:
        print("PLACEMENT_FAILED", str(exc))
        return {"ok": False, "error": str(exc)}


if __name__ == "__main__":
    args = parse_args()
    result = main(args.user_prompt)
    sys.exit(0 if result.get("ok") else 1)
