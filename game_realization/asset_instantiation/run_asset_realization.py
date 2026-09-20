"""One LLM call: turn fixed gameplay placements into style-matched GLB requests."""
import argparse
import json
import math
import os
import sys
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
COMMON_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", "shared_representation"))
PLACEMENT_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "element_placement"))
for _path in (SCRIPT_DIR, COMMON_DIR, PLACEMENT_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from placement_constraints import (
    LOGICAL_ONLY_ELEMENT_TYPES,
    NON_GLB_ALLOWED_ELEMENT_TYPES,
    OPTIONAL_ASSET_ELEMENT_TYPES,
    VISIBLE_ASSET_REQUIRED_ELEMENT_TYPES,
)
from placement_io import load_json, sanitize_for_llm, strip_json_markdown, write_json, write_text
from run_placement_selector import llm_config
from staging_paths import (
    get_generated_glb_prefix,
    get_packet_dir,
    get_staging_root,
)


PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
STAGING_ROOT = get_staging_root(PROJECT_ROOT)
PACKET_DIR = get_packet_dir(PROJECT_ROOT)
OUTPUT_DIR = os.path.join(STAGING_ROOT, "asset_realization")
OUTPUT_PROMPT = os.path.join(OUTPUT_DIR, "asset_realization_prompt.md")
OUTPUT_RAW = os.path.join(OUTPUT_DIR, "asset_realization_raw_response.md")
OUTPUT_PLAN = os.path.join(OUTPUT_DIR, "asset_plan.json")
EXPECTED_GLB_PREFIX = get_generated_glb_prefix()


def parse_args():
    parser = argparse.ArgumentParser(description="Single-pass Code2Games asset realization")
    parser.add_argument("--user_prompt", default="design a gameplay demo that fits the current scene")
    parser.add_argument("--reuse_raw_response", action="store_true", help="validate the existing raw LLM response without calling the LLM again")
    return parser.parse_args()


def call_asset_llm(prompt):
    config = llm_config()
    if not all(config.values()):
        raise ValueError("LLM configuration is incomplete")
    url = config["base_url"].rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    body = json.dumps({
        "model": config["model"],
        "messages": [
            {"role": "system", "content": "Return one strict JSON asset plan. Fixed placements must not be changed."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.65,
        "max_tokens": 4096,
    }).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={"Authorization": "Bearer " + config["api_key"], "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=900) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload["choices"][0]["message"]["content"]


def selected_placement_facts(placement_plan, candidate_data):
    candidates = {item["candidate_id"]: item for item in candidate_data.get("candidates", [])}
    tuning_by_id = placement_plan.get("placement_plan", {}).get("tuning_overrides", {})
    result = []
    for placement in placement_plan.get("placement_plan", {}).get("elements", []):
        candidate = candidates.get(placement.get("candidate_id"), {})
        source_mode = placement.get("placement_mode")
        result.append({
            "placement_id": placement.get("placement_id"),
            "element_type": placement.get("element_type"),
            # An existing-anchor candidate fixes a location; it never authorizes
            # reusing the object hit while that location was sampled.
            "placement_mode": "at_fixed_location" if source_mode == "existing_anchor" else source_mode,
            "height_offset": placement.get("height_offset"),
            "gameplay_purpose": placement.get("gameplay_purpose"),
            "candidate_kind": candidate.get("candidate_kind"),
            "surface_type": candidate.get("surface_type"),
            "walkability": candidate.get("walkability"),
            "normal_xyz": candidate.get("normal_xyz"),
            "hit_object_context": candidate.get("hit_object"),
            "nearest_tree": candidate.get("nearest_tree"),
            "nearest_bush": candidate.get("nearest_bush"),
            "placement_tags": candidate.get("placement_tags"),
            "location_semantics": placement.get("location_semantics"),
            "surface_staging": tuning_by_id.get(placement.get("placement_id"), {}),
        })
    return result


def build_prompt(user_prompt, game_rule, placement_facts, scene, visual):
    payload = {
        "user_prompt": user_prompt,
        "game_rule_plan": sanitize_for_llm(game_rule),
        "fixed_placement_facts": placement_facts,
        "scene_understanding": sanitize_for_llm(scene),
        "game_rule_visual_notes": sanitize_for_llm(visual),
    }
    visible_required = ", ".join(sorted(VISIBLE_ASSET_REQUIRED_ELEMENT_TYPES))
    logical_only = ", ".join(sorted(LOGICAL_ONLY_ELEMENT_TYPES))
    optional_asset = ", ".join(sorted(OPTIONAL_ASSET_ELEMENT_TYPES))
    return "\n".join([
        "# Code2Games direct asset generation plan",
        "All placement locations are final. Do not choose, move, add, remove, or reorder placements.",
        "For each fixed placement, decide whether it needs a visible GLB. Put placements that do not need GLB, such as purely logical zones, in non_glb_placements.",
        "These types require a visible GLB: " + visible_required + ".",
        "These types are logical anchors and MUST be non_glb_placements: " + logical_only + ". The later gameplay director supplies actors, vehicles, AI, rigs, animation, and cameras.",
        "These types may be logical or visibly represented according to their purpose: " + optional_asset + ".",
        "Every obstacle must be a newly generated, visible, physically plausible gameplay object placed at its fixed location. Existing trees, bushes, rocks, terrain, or other scene geometry must never fulfill an obstacle placement.",
        "An obstacle is a game obstacle with a clear player response such as jump, dodge, or route-around. The scene's existing vegetation and terrain are only the visual base. Do not generate ordinary replacement bushes or trees merely because the fixed point is near vegetation.",
        "When several obstacles are requested, create meaningful visual and gameplay variety that remains plausible for each local ground context. Do not assign every obstacle placement to one identical asset; reuse only within a suitable subset.",
        "Do not create freestanding pillars, columns, poles, narrow totems, detached gate pieces, or isolated wall fragments. These depend on a road, gate, wall, ruin complex, or other supporting structure and look arbitrary in a natural clearing. Every selected object must be visually and physically self-contained at its fixed point. Prefer independent natural gameplay forms such as fallen logs, tangled roots, thorny shrubs, spiked plants, bramble masses, grounded relic mounds, or rock formations when locally plausible.",
        "Every goal area must have a newly generated visible marker in addition to its later logical trigger, and every landmark must have a newly generated visible orientation asset.",
        "For placements that need visible objects, creatively choose a physically plausible asset from the actual local road and environment facts. Random variety is welcome only inside physical reality and gameplay intent.",
        "An item_pickup is a visible weapon, ammunition, health, power-up, key, or inventory item; its exact role must follow gameplay_purpose. An objective_object, destructible, puzzle_element, or moving_platform must have a clear standalone silhouette and physical function.",
        "A logical actor/vehicle spawn is not a request to generate a static person, enemy, or vehicle GLB. Record its later implementation in non_glb_placements.",
        "A flat ground point cannot receive floating water, an unsupported hovering structure, or an object inconsistent with gravity. A slope-aware object must sit plausibly on that slope. An airborne asset must match the given above_ground mode and reachable height. Water-like assets require real water or suitable low ground evidence.",
        "hit_object_context, nearest_tree, and nearest_bush only describe the scene base around the fixed ground point. They exist to improve composition and style matching. They are never the asset to reuse, never satisfy the placement, and must not appear as an existing-scene implementation. An upright tree is not a jump obstacle merely because it is nearby.",
        "Every generated asset must match the current scene's shape language, natural materials, color palette, roughness, weathering, vegetation integration, realism, and visual age from scene_understanding and game_rule_visual_notes.",
        "Do not merely repeat a broad biome or style label from scene text. Ground every material and palette decision in the actual visible evidence. Do not carry colors or art direction from a previous demo into the current scene.",
        "Plan dimensions at gameplay scale, not tabletop-decoration scale. Judge every expected_size_meters against the player/vehicle scale and the requested first-person, third-person, vehicle, or cinematic camera: pickups must remain readable, obstacles and cover must communicate their function, hazards must occupy meaningful ground, and objectives/landmarks must read from the intended distance. Use local clearance facts as limits; do not blindly apply one multiplier to all assets.",
        "expected_size_meters always uses Blender axis order [X width, Y depth, Z height] in meters. Never use [width,height,depth]. A flat ground patch must therefore have large X and Y and a small Z. State the intended physical orientation in generation_prompt_en so image-to-3D does not turn a ground patch into a vertical wall.",
        "Every asset must declare flat_base_policy as allow or reject and explain it in flat_base_reason. Use allow only when a broad planar support is an intended physical part of the object, such as a crate/case, parked vehicle, grounded equipment shelter, platform, or deliberately flat floor marker. Use reject for organic rocks, roots, vegetation, irregular relics, pickups, and sculptural objects where a generated slab-like bottom would be an artifact. This policy controls validation; it does not ask the model to add a base.",
        "Respect fixed surface_staging. A full_surface_normal placement is attached to a cliff or wall and needs a plausible contact side; terrain_normal follows ground or slope; upright must visually remain vertical. Do not redesign or move the placement.",
        "Collectibles must have an unmistakable compact pickup silhouette, not resemble a chair, miniature shrine, building, cage, or pile of logs. Landmarks must be broad, asymmetric, self-supporting environmental masses; a renamed upright slab or stone marker still violates the no-pillar requirement.",
        "You decide reuse: multiple placement_ids may share one asset only when the same self-contained object is physically suitable at every assigned placement. Split ground-supported and airborne variants when one model would look unsupported. Do not force either full reuse or one asset per placement.",
        "Default to one unique generated asset per visible placement so the demo has real gameplay variety. Reuse is exceptional and must be justified by an intentionally identical repeated item. When a demo has ten or more visible or optionally visible placements, produce at least ten distinct generated assets.",
        "Prefer unmistakably volumetric 3D objects with substantial height and depth, readable front/side/back surfaces, self-supporting construction, and a strong silhouette from a three-quarter view. Do not represent a hazard, goal, landmark, safe zone, or event cue as a thin ground patch, decal, painted mark, texture sheet, nearly flat tile, or shallow relief. The runtime owns the invisible trigger/zone; the generated GLB should be its three-dimensional visual marker.",
        "generation_prompt_en must describe exactly one isolated standalone object for image-to-3D generation and must explicitly include the current scene style. Do not describe a full scene, terrain, character, camera, text, UI, or animation.",
        "expected_glb_path must be a relative path under " + EXPECTED_GLB_PREFIX + " and end in .glb. This demo-specific prefix prevents assets from different games from overwriting each other.",
        "Do not output world coordinates or Blender code.",
        "Return strict JSON only.",
        "Schema: {assets:[{asset_id,asset_name,asset_role,placement_ids,generation_prompt_en,negative_prompt_en,expected_glb_path,expected_size_meters:[x,y,z],flat_base_policy:'allow|reject',flat_base_reason:'non-empty'}],non_glb_placements:[{placement_id,implementation,reason}],notes:['summary']}",
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    ])


def positive_vec3(value):
    return isinstance(value, list) and len(value) == 3 and all(isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(float(item)) and float(item) > 0 for item in value)


def validate_asset_plan(data, placement_facts):
    if not isinstance(data, dict) or not isinstance(data.get("assets"), list) or not isinstance(data.get("non_glb_placements"), list):
        raise ValueError("asset plan must contain assets and non_glb_placements lists")
    valid_placements = {item["placement_id"] for item in placement_facts}
    element_type_by_placement = {item["placement_id"]: item["element_type"] for item in placement_facts}
    covered = []
    asset_ids = set()
    asset_ids_by_element_type = {}
    for index, asset in enumerate(data["assets"]):
        required = ("asset_id", "asset_name", "asset_role", "generation_prompt_en", "expected_glb_path")
        if not isinstance(asset, dict) or not all(isinstance(asset.get(key), str) and asset[key].strip() for key in required):
            raise ValueError("asset %d is missing required fields" % index)
        if asset["asset_id"] in asset_ids:
            raise ValueError("asset_id must be unique")
        asset_description = " ".join(str(asset.get(key, "")) for key in ("asset_id", "asset_name", "generation_prompt_en")).lower()
        forbidden_freestanding_forms = ("pillar", "column", "totem", "gatepost", "gate post", "wall fragment")
        if any(token in asset_description for token in forbidden_freestanding_forms):
            raise ValueError("asset %d uses a forbidden non-independent pillar/totem/fragment form" % index)
        asset_ids.add(asset["asset_id"])
        placement_ids = asset.get("placement_ids")
        if not isinstance(placement_ids, list) or not placement_ids or not all(item in valid_placements for item in placement_ids):
            raise ValueError("asset %d placement_ids are invalid" % index)
        placement_types = {element_type_by_placement[item] for item in placement_ids}
        if len(placement_types) != 1 or asset["asset_role"] not in placement_types:
            raise ValueError("asset %d role must match all referenced placement element types" % index)
        element_type = next(iter(placement_types))
        if element_type in LOGICAL_ONLY_ELEMENT_TYPES:
            raise ValueError("asset %d assigns a GLB to logical-only type %s" % (index, element_type))
        asset_ids_by_element_type.setdefault(element_type, set()).add(asset["asset_id"])
        if not asset["expected_glb_path"].startswith(EXPECTED_GLB_PREFIX) or not asset["expected_glb_path"].endswith(".glb"):
            raise ValueError("asset %d expected_glb_path is invalid" % index)
        if not positive_vec3(asset.get("expected_size_meters")):
            raise ValueError("asset %d expected_size_meters must be a positive vec3" % index)
        dimensions = [float(value) for value in asset["expected_size_meters"]]
        if element_type in {"hazard_zone", "goal_area", "landmark", "safe_zone", "event_trigger"}:
            if dimensions[2] < 0.15 * max(dimensions[0], dimensions[1]):
                raise ValueError("asset %d is an excessively flat gameplay marker; use a volumetric 3D object" % index)
        if asset.get("flat_base_policy") is None:
            asset["flat_base_policy"] = "reject"
            asset["flat_base_reason"] = "legacy plan default: reject unintended generated base"
        if asset.get("flat_base_policy") not in {"allow", "reject"}:
            raise ValueError("asset %d flat_base_policy must be allow or reject" % index)
        if not isinstance(asset.get("flat_base_reason"), str) or not asset["flat_base_reason"].strip():
            raise ValueError("asset %d flat_base_reason must be non-empty" % index)
        covered.extend(placement_ids)
    for index, item in enumerate(data["non_glb_placements"]):
        if not isinstance(item, dict) or item.get("placement_id") not in valid_placements:
            raise ValueError("non_glb placement %d is invalid" % index)
        element_type = element_type_by_placement[item["placement_id"]]
        if element_type not in NON_GLB_ALLOWED_ELEMENT_TYPES:
            raise ValueError("%s placements require a visible GLB" % element_type)
        covered.append(item["placement_id"])
    if len(covered) != len(valid_placements) or set(covered) != valid_placements:
        raise ValueError("every placement_id must appear exactly once in assets or non_glb_placements")
    visible_or_optional_count = sum(
        1 for item in placement_facts
        if item["element_type"] not in LOGICAL_ONLY_ELEMENT_TYPES
    )
    if visible_or_optional_count >= 10 and len(data["assets"]) < 10:
        raise ValueError("ten or more visible placements require at least ten distinct gaming assets")
    obstacle_count = sum(1 for item in placement_facts if item["element_type"] == "obstacle")
    if obstacle_count >= 4 and len(asset_ids_by_element_type.get("obstacle", set())) < 2:
        raise ValueError("four or more obstacle placements must use at least two distinct gameplay obstacle assets")
    return data


def main(user_prompt="design a gameplay demo that fits the current scene", reuse_raw_response=False):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    cleanup_paths = (OUTPUT_PROMPT, OUTPUT_PLAN) if reuse_raw_response else (OUTPUT_PROMPT, OUTPUT_RAW, OUTPUT_PLAN)
    for path in cleanup_paths:
        if os.path.isfile(path):
            os.remove(path)
    try:
        game_rule = load_json(os.path.join(PACKET_DIR, "game_rule_plan.json"))
        placement_plan = load_json(os.path.join(PACKET_DIR, "gameplay_placement_plan.json"))
        candidate_data = load_json(os.path.join(PACKET_DIR, "placement_candidates.json"))
        scene = load_json(os.path.join(PACKET_DIR, "scene_understanding_default_camera.json"))
        visual = load_json(os.path.join(PACKET_DIR, "game_rule_visual_notes.json"))
        placement_facts = selected_placement_facts(placement_plan, candidate_data)
        if not placement_facts:
            raise ValueError("gameplay placement plan is empty")
        print("ASSET_REALIZATION_START placements", len(placement_facts))
        prompt = build_prompt(user_prompt, game_rule, placement_facts, scene, visual)
        write_text(OUTPUT_PROMPT, prompt)
        if reuse_raw_response:
            if not os.path.isfile(OUTPUT_RAW):
                raise FileNotFoundError("existing raw response not found: " + OUTPUT_RAW)
            with open(OUTPUT_RAW, "r", encoding="utf-8") as handle:
                raw = handle.read()
        else:
            raw = call_asset_llm(prompt)
            write_text(OUTPUT_RAW, raw)
        plan = validate_asset_plan(json.loads(strip_json_markdown(raw)), placement_facts)
        write_json(OUTPUT_PLAN, plan)
        print("ASSET_REALIZATION_OK assets", len(plan["assets"]), "non_glb", len(plan["non_glb_placements"]))
        return {"ok": True, "output_files": [OUTPUT_PLAN]}
    except Exception as exc:
        print("ASSET_REALIZATION_FAILED", str(exc))
        return {"ok": False, "error": str(exc)}


if __name__ == "__main__":
    args = parse_args()
    result = main(args.user_prompt, args.reuse_raw_response)
    sys.exit(0 if result.get("ok") else 1)
