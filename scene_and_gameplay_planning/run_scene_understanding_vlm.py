import argparse
import base64
import json
import os
import re
import sys
import traceback
import urllib.error
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
COMMON_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "shared_representation"))
if COMMON_DIR not in sys.path:
    sys.path.insert(0, COMMON_DIR)

from staging_paths import get_packet_dir


PROJECT_ROOT = os.getcwd()
PACKET_DIR = get_packet_dir(PROJECT_ROOT)
GRID_IMAGE = os.path.join(PACKET_DIR, "default_camera_view_grid.png")
METADATA_JSON = os.path.join(PACKET_DIR, "default_camera_metadata.json")

OUTPUT_JSON = os.path.join(PACKET_DIR, "scene_understanding_default_camera.json")
OUTPUT_PROMPT = os.path.join(PACKET_DIR, "scene_understanding_default_camera_prompt.md")
OUTPUT_RAW = os.path.join(PACKET_DIR, "scene_understanding_default_camera_raw_response.md")
OUTPUT_REPORT = os.path.join(PACKET_DIR, "scene_understanding_default_camera_report.json")

DEFAULT_USER_PROMPT = "cinematic temple-run-like adventure gameplay video"
DEFAULT_DURATION_SECONDS = 30

GRID_AREAS = {
    "upper_left",
    "upper_center",
    "upper_right",
    "middle_left",
    "middle_center",
    "middle_right",
    "lower_left",
    "lower_center",
    "lower_right",
}


def log(message, *values):
    print(" ".join([str(message), *[str(value) for value in values]]))


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--user_prompt", default=DEFAULT_USER_PROMPT)
    parser.add_argument("--duration_seconds", type=float, default=DEFAULT_DURATION_SECONDS)
    return parser.parse_args()


def ensure_dirs():
    os.makedirs(PACKET_DIR, exist_ok=True)


def load_json(path):
    if not os.path.exists(path):
        raise FileNotFoundError(f"required JSON not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def remove_file_if_exists(path):
    if os.path.exists(path):
        os.remove(path)


def image_to_base64(path):
    if not os.path.exists(path):
        raise FileNotFoundError(f"required image not found: {path}")
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def compact_metadata(metadata, duration_seconds):
    result = {
        "source": metadata.get("source"),
        "render": metadata.get("render"),
        "camera": metadata.get("camera"),
        "gameplay_context": dict(metadata.get("gameplay_context") or {}),
        "screen_grid": metadata.get("screen_grid"),
        "local_scene_hints": metadata.get("local_scene_hints"),
    }
    result["gameplay_context"]["duration_seconds"] = duration_seconds
    return result


def build_prompt(metadata, user_prompt, duration_seconds):
    compact = json.dumps(compact_metadata(metadata, duration_seconds), indent=2, ensure_ascii=False)
    return "\n".join(
        [
            "# Role",
            "You are the DEFAULT_CAMERA objective visual scene understanding agent for Code2Games / Code2Worlds.",
            "",
            "# Project Boundary",
            "The current stage only does objective visual scene understanding.",
            "Describe only what is actually visible in the current DEFAULT_CAMERA image, supported by metadata.",
            "Do not design a game, rules, tasks, routes, points, trajectories, placement, scripting, or follow-up actions.",
            "Do not output coordinate point fields or world-space target points.",
            "Do not split the image into fixed near/middle/far narrative fields.",
            "Describe visual elements, surface types, spatial relations, visible continuity, discontinuity, and uncertainty.",
            "",
            "# User Prompt",
            "The user prompt is only a goal/style reference. It is not evidence of what is visible.",
            "Prioritize the current image and metadata over the prompt. If the image shows water, mud, rock, vegetation, or slopes, describe those real visual contents.",
            user_prompt,
            "",
            "# Duration Seconds",
            "This value is passed for downstream context only. Do not use it to design motion or timing.",
            str(duration_seconds),
            "",
            "# Inputs",
            "1. default_camera_view_grid.png",
            "   The DEFAULT_CAMERA render with a light 3x3 screen grid.",
            "",
            "2. default_camera_metadata.json",
            "   Camera parameters, 3x3 grid raycast hints, and coarse local scene hints.",
            "",
            "# Task",
            "Produce a detailed but compact objective visual understanding of the DEFAULT_CAMERA view.",
            "State whether the image is usable as a visual-understanding input.",
            "Identify the overall visual environment type, terrain structure, visible elements, surface distribution, spatial relations, visual openness, visual blockage or complexity, visual discontinuity, guidance features, and uncertainty.",
            "",
            "# Important Visual Reasoning Constraints",
            "Metadata is only auxiliary. If the image clearly shows water, breaks, occlusion, or complex terrain, do not treat a whole region as clear just because a grid-cell center raycast hits ground.",
            "Use screen grid area names when describing regions: upper_left, upper_center, upper_right, middle_left, middle_center, middle_right, lower_left, lower_center, lower_right.",
            "Do not say the character should run forward. Do not infer the user's requested style as visible content.",
            "",
            "# Output Rules",
            "Return only strict JSON. Do not output markdown.",
            "selected_view must be DEFAULT_CAMERA.",
            "confidence must be a number from 0.0 to 1.0.",
            "The top-level JSON must contain only view_status and scene_visual_context.",
            "",
            "# Required JSON Schema",
            "```json",
            "{",
            '  "view_status": {',
            '    "selected_view": "DEFAULT_CAMERA",',
            '    "usable": true,',
            '    "confidence": 0.85,',
            '    "reason": ""',
            "  },",
            '  "scene_visual_context": {',
            '    "overall_summary": "",',
            '    "environment_visual_type": "",',
            '    "terrain_structure": "",',
            '    "visible_elements": [],',
            '    "surface_distribution": {',
            '      "ground_regions": [],',
            '      "water_regions": [],',
            '      "vegetation_regions": [],',
            '      "rock_or_slope_regions": [],',
            '      "uncertain_regions": []',
            "    },",
            '    "spatial_relations": [],',
            '    "traversability_visual_observation": {',
            '      "visually_open_regions": [],',
            '      "visually_blocked_or_complex_regions": [],',
            '      "visually_discontinuous_regions": []',
            "    },",
            '    "visual_guidance_features": [],',
            '    "uncertainty_notes": []',
            "    }",
            "}",
            "```",
            "",
            "# default_camera_metadata.json",
            "```json",
            compact,
            "```",
        ]
    )


def strip_json_markdown(text):
    content = (text or "").strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content, flags=re.IGNORECASE)
        content = re.sub(r"\s*```$", "", content)
    if not content.startswith("{"):
        start = content.find("{")
        end = content.rfind("}")
        if start >= 0 and end > start:
            content = content[start : end + 1]
    return content.strip()


def contains_forbidden_key(value):
    forbidden = {
        "game_goal",
        "victory_condition",
        "failure_condition",
        "score_mechanism",
        "interaction_rules",
        "npc_route",
        "task_points",
        "item_points",
        "spawn_point",
        "event_points",
        "world_xyz",
        "ranked_views",
        "trajectory",
        "timeline",
        "keyframes",
        "waypoints",
        "asset_generation_prompt",
        "hunyuan3d_prompt",
        "game_rules",
        "final_render_plan",
        "movement_plan_hint",
        "start_area",
        "main_direction",
        "duration_seconds",
        "no_fixed_endpoint",
        "expected_motion_style",
        "terrain_action_understanding",
        "staging_zones",
        "obstacle_zones",
        "collectible_zones",
        "event_zones",
        "forward_target_area",
        "camera_follow_hint",
        "next_step",
        "can_enter_gameplay_design",
        "hazards",
        "interaction_opportunities",
        "design_constraints",
        "foreground_description",
        "midground_description",
        "background_description",
    }
    if isinstance(value, dict):
        for key, item in value.items():
            lower = str(key).lower()
            if lower in forbidden or lower.endswith("_path"):
                return True
            if contains_forbidden_key(item):
                return True
    elif isinstance(value, list):
        return any(contains_forbidden_key(item) for item in value)
    return False


def require_nonempty_string(data, key, field_name, errors):
    if not isinstance(data.get(key), str) or not data.get(key).strip():
        errors.append(f"{field_name} must be a non-empty string")


def require_list(data, key, field_name, errors):
    if not isinstance(data.get(key), list):
        errors.append(f"{field_name} must be a list")


def require_dict(data, key, field_name, errors):
    if not isinstance(data.get(key), dict):
        errors.append(f"{field_name} must be an object")


def validate_scene_understanding(data, _duration_seconds):
    errors = []
    warnings = []
    if not isinstance(data, dict):
        return ["top-level output must be a JSON object"], warnings
    if contains_forbidden_key(data):
        errors.append("output contains forbidden game-design, route-planning, coordinate, or old-schema fields")

    allowed_top_level = {"view_status", "scene_visual_context"}
    extra_top_level = set(data.keys()) - allowed_top_level
    if extra_top_level:
        errors.append("top-level JSON must contain only view_status and scene_visual_context")

    view_status = data.get("view_status")
    if not isinstance(view_status, dict):
        errors.append("view_status must be an object")
    else:
        if view_status.get("selected_view") != "DEFAULT_CAMERA":
            errors.append("view_status.selected_view must be DEFAULT_CAMERA")
        if not isinstance(view_status.get("usable"), bool):
            errors.append("view_status.usable must be boolean")
        confidence = view_status.get("confidence")
        if not isinstance(confidence, (int, float)) or not (0.0 <= float(confidence) <= 1.0):
            errors.append("view_status.confidence must be a number from 0.0 to 1.0")
        require_nonempty_string(view_status, "reason", "view_status.reason", errors)

    context = data.get("scene_visual_context")
    if not isinstance(context, dict):
        errors.append("scene_visual_context must be an object")
    else:
        require_nonempty_string(context, "overall_summary", "scene_visual_context.overall_summary", errors)
        require_nonempty_string(context, "environment_visual_type", "scene_visual_context.environment_visual_type", errors)
        require_nonempty_string(context, "terrain_structure", "scene_visual_context.terrain_structure", errors)
        require_list(context, "visible_elements", "scene_visual_context.visible_elements", errors)
        require_dict(context, "surface_distribution", "scene_visual_context.surface_distribution", errors)
        require_list(context, "spatial_relations", "scene_visual_context.spatial_relations", errors)
        require_dict(
            context,
            "traversability_visual_observation",
            "scene_visual_context.traversability_visual_observation",
            errors,
        )
        require_list(context, "visual_guidance_features", "scene_visual_context.visual_guidance_features", errors)
        require_list(context, "uncertainty_notes", "scene_visual_context.uncertainty_notes", errors)

        surface = context.get("surface_distribution")
        if isinstance(surface, dict):
            for field in [
                "ground_regions",
                "water_regions",
                "vegetation_regions",
                "rock_or_slope_regions",
                "uncertain_regions",
            ]:
                require_list(surface, field, f"scene_visual_context.surface_distribution.{field}", errors)

        traversability = context.get("traversability_visual_observation")
        if isinstance(traversability, dict):
            for field in [
                "visually_open_regions",
                "visually_blocked_or_complex_regions",
                "visually_discontinuous_regions",
            ]:
                require_list(
                    traversability,
                    field,
                    f"scene_visual_context.traversability_visual_observation.{field}",
                    errors,
                )

    return errors, warnings


def get_vlm_config():
    return {
        "base_url": os.environ.get("OPENAI_BASE_URL") or os.environ.get("DASHSCOPE_BASE_URL"),
        "api_key": os.environ.get("OPENAI_API_KEY") or os.environ.get("DASHSCOPE_API_KEY"),
        "model": (
            os.environ.get("CODE2GAMES_VLM_MODEL")
            or os.environ.get("OPENAI_VLM_MODEL")
            or os.environ.get("OPENAI_MODEL")
            or os.environ.get("CODE2GAMES_MODEL")
        ),
    }


def normalize_chat_completions_url(base_url):
    base = (base_url or "").rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def call_vlm(prompt, image_path, config):
    image_b64 = image_to_base64(image_path)
    payload = {
        "model": config["model"],
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a strict JSON DEFAULT_CAMERA objective visual scene understanding agent. "
                    "Return only valid JSON. Describe visible scene content only. "
                    "Do not design games, rules, routes, coordinate points, timing, scripts, or assets."
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "text", "text": "Image: default_camera_view_grid.png"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                ],
            },
        ],
        "temperature": 0.2,
        "max_tokens": 2048,
    }
    request = urllib.request.Request(
        normalize_chat_completions_url(config["base_url"]),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config['api_key']}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"VLM HTTP {exc.code}: {detail}") from exc
    choices = body.get("choices") or []
    if not choices:
        raise RuntimeError(f"VLM response did not contain choices: {body}")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        text_parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_parts.append(str(item.get("text", "")))
            elif isinstance(item, str):
                text_parts.append(item)
        return "\n".join(text_parts).strip()
    return str(content or "").strip()


def main():
    args = parse_args()
    ensure_dirs()
    report = {
        "ok": False,
        "packet_dir": PACKET_DIR,
        "grid_image": GRID_IMAGE,
        "metadata_json": METADATA_JSON,
        "output_json": OUTPUT_JSON,
        "user_prompt": args.user_prompt,
        "duration_seconds": args.duration_seconds,
    }
    try:
        metadata = load_json(METADATA_JSON)
        if not os.path.exists(GRID_IMAGE):
            raise FileNotFoundError(f"required image not found: {GRID_IMAGE}")
        prompt = build_prompt(metadata, args.user_prompt, args.duration_seconds)
        write_text(OUTPUT_PROMPT, prompt)

        config = get_vlm_config()
        missing = [key for key in ["api_key", "base_url", "model"] if not config.get(key)]
        if missing:
            remove_file_if_exists(OUTPUT_JSON)
            report.update(
                {
                    "ok": False,
                    "skipped_vlm_due_to_missing_config": missing,
                    "prompt": OUTPUT_PROMPT,
                    "scene_understanding_json_created": False,
                }
            )
            log("DEFAULT_CAMERA_UNDERSTANDING_SKIPPED_MISSING_CONFIG", ",".join(missing))
            return

        raw = call_vlm(prompt, GRID_IMAGE, config)
        write_text(OUTPUT_RAW, raw)
        data = json.loads(strip_json_markdown(raw))
        errors, warnings = validate_scene_understanding(data, args.duration_seconds)
        if errors:
            raise ValueError("DEFAULT_CAMERA scene understanding validation failed: " + "; ".join(errors))
        write_json(OUTPUT_JSON, data)
        report.update(
            {
                "ok": True,
                "model": config["model"],
                "base_url": config["base_url"],
                "prompt": OUTPUT_PROMPT,
                "raw_response": OUTPUT_RAW,
                "scene_understanding_json": OUTPUT_JSON,
                "usable": data.get("view_status", {}).get("usable"),
                "validation_warnings": warnings,
            }
        )
        log("DEFAULT_CAMERA_SCENE_UNDERSTANDING_JSON", OUTPUT_JSON)
    except Exception as exc:
        remove_file_if_exists(OUTPUT_JSON)
        report.update(
            {
                "ok": False,
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }
        )
        log("DEFAULT_CAMERA_UNDERSTANDING_ERROR", exc)
        log(traceback.format_exc())
    finally:
        write_json(OUTPUT_REPORT, report)
        log("DEFAULT_CAMERA_UNDERSTANDING_REPORT", OUTPUT_REPORT)


if __name__ == "__main__":
    main()
