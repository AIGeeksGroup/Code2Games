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

from placement_constraints import ALLOWED_ELEMENT_TYPES
from staging_paths import get_packet_dir


PROJECT_ROOT = os.getcwd()
PACKET_DIR = get_packet_dir(PROJECT_ROOT)
GRID_IMAGE = os.path.join(PACKET_DIR, "default_camera_view_grid.png")
METADATA_JSON = os.path.join(PACKET_DIR, "default_camera_metadata.json")
SCENE_UNDERSTANDING_JSON = os.path.join(PACKET_DIR, "scene_understanding_default_camera.json")

VISUAL_NOTES_JSON = os.path.join(PACKET_DIR, "game_rule_visual_notes.json")
VISUAL_PROMPT = os.path.join(PACKET_DIR, "game_rule_visual_prompt.md")
VISUAL_RAW = os.path.join(PACKET_DIR, "game_rule_visual_raw_response.md")

OUTPUT_JSON = os.path.join(PACKET_DIR, "game_rule_plan.json")
OUTPUT_PROMPT = os.path.join(PACKET_DIR, "game_rule_prompt.md")
OUTPUT_RAW = os.path.join(PACKET_DIR, "game_rule_raw_response.md")
OUTPUT_REPORT = os.path.join(PACKET_DIR, "game_rule_report.json")

DEFAULT_USER_PROMPT = "temple-run-like jungle adventure"
DEFAULT_DURATION_SECONDS = 30
FIXED_VLM_MODEL = "qwen3-vl-plus"
FIXED_RULE_MODEL = "qwen3.6-plus"
DEFAULT_DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
REQUIRED_MOVE_ACTIONS = {"move_forward", "move_backward", "move_left", "move_right"}
ALLOWED_PLACEMENT_ELEMENT_TYPES = ALLOWED_ELEMENT_TYPES


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


def normalize_chat_completions_url(base_url):
    base = (base_url or "").rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


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


def get_vlm_visual_config():
    return {
        "base_url": os.environ.get("DASHSCOPE_BASE_URL") or DEFAULT_DASHSCOPE_BASE_URL,
        "api_key": os.environ.get("DASHSCOPE_API_KEY"),
        "model": FIXED_VLM_MODEL,
    }


def get_rule_llm_config():
    return {
        "base_url": (
            os.environ.get("CODE2GAMES_RULE_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL")
            or os.environ.get("DASHSCOPE_BASE_URL")
            or DEFAULT_DASHSCOPE_BASE_URL
        ),
        "api_key": (
            os.environ.get("CODE2GAMES_RULE_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or os.environ.get("DASHSCOPE_API_KEY")
        ),
        "model": os.environ.get("CODE2GAMES_RULE_MODEL") or FIXED_RULE_MODEL,
    }


def compact_visual_reader_inputs(metadata, scene_understanding, user_prompt):
    return {
        "user_prompt": user_prompt,
        "default_camera_metadata": metadata,
        "scene_understanding_default_camera": scene_understanding,
    }


def build_visual_reader_prompt(metadata, scene_understanding, user_prompt):
    compact = json.dumps(
        compact_visual_reader_inputs(metadata, scene_understanding, user_prompt),
        indent=2,
        ensure_ascii=False,
    )
    return "\n".join(
        [
            "# Role",
            "You are rule_visual_reader for Code2Games.",
            "You are a visual model. Your only job is to extract rule-relevant visual notes from the DEFAULT_CAMERA image.",
            "You are not the game rule designer, not placement_agent, not a path planner, and not a script writer.",
            "",
            "# Inputs",
            "1. default_camera_view_grid.png",
            "2. default_camera_metadata.json",
            "3. scene_understanding_default_camera.json",
            "4. user_prompt as broad context only",
            "",
            "# Task",
            "Summarize only visual information that may be useful for later game rule design.",
            "Do not design game goals, win conditions, fail conditions, scoring, task points, routes, or placement.",
            "Do not output coordinates, point locations, fixed paths, waypoints, or Blender code.",
            "Do not interpret visual gaps as routes, corridors, lanes, or intended movement directions.",
            "If there is an open gap between objects, describe it neutrally as visible open space, lower vegetation density, reduced occlusion, or clearer ground visibility.",
            "movement_space_observation may only describe ground visibility, occlusion level, vegetation density, openness, and visible terrain continuity.",
            "movement_space_observation must not describe where a player should go, a default direction, route, path, lane, or progression plan.",
            "notes_for_rule_designer must not recommend turning a gap into a route, aligning camera behavior, or reinforcing one-way movement.",
            "",
            "# Output Rules",
            "Return only strict JSON. Do not output markdown.",
            "",
            "# Required JSON Schema",
            "```json",
            "{",
            '  "visual_rule_notes_status": {',
            '    "usable": true,',
            '    "confidence": 0.85,',
            '    "reason": ""',
            "  },",
            '  "visual_rule_notes": {',
            '    "rule_relevant_scene_summary": "",',
            '    "scene_features_useful_for_rules": [],',
            '    "movement_space_observation": "",',
            '    "visual_landmarks_for_rules": [],',
            '    "rule_design_risks_from_visuals": [],',
            '    "notes_for_rule_designer": []',
            "  }",
            "}",
            "```",
            "",
            "# Text Inputs",
            "```json",
            compact,
            "```",
        ]
    )


def build_rule_designer_prompt(metadata, scene_understanding, visual_notes, user_prompt, duration_seconds):
    compact = json.dumps(
        {
            "user_prompt": user_prompt,
            "duration_seconds": duration_seconds,
            "default_camera_metadata": metadata,
            "scene_understanding_default_camera": scene_understanding,
            "game_rule_visual_notes": visual_notes,
        },
        indent=2,
        ensure_ascii=False,
    )
    return "\n".join(
        [
            "# Role",
            "You are game_rule_agent for Code2Games.",
            "You are not scene_understanding, not placement_agent, not a path planner, and not a script writer.",
            "You are a text/coding LLM call and you do not receive images. Use only the text inputs below.",
            "",
            "# Task",
            "Design a playable game rule plan that fits the current DEFAULT_CAMERA scene.",
            "You design how the game works. You do not choose exact placement points and you do not write Blender code.",
            "The user prompt is a goal direction, not something to copy blindly. Combine it with scene understanding and visual notes.",
            "The phrase temple-run-like is a style reference only. It does not mean the game must use endless-runner mechanics.",
            "Do not design forced automatic forward motion unless the user explicitly requests that exact mechanic.",
            "Do not make the core loop depend on forced one-way movement.",
            "The game should be a task-based free-navigation challenge.",
            "",
            "# Design Scope",
            "Include game concept, core loop, player/NPC role, objective, win condition, fail condition, time limit, scoring rules, allowed actions, scene element usage, randomness/free-navigation rules, and gameplay element categories needed by a later placement_agent.",
            "Do not design the game as only 'NPC runs forward'. The player or NPC should support free movement and random choices.",
            "allowed_actions must include move_forward, move_backward, move_left, move_right. Add jump, dodge, collect, interact, dive, or swim_or_wade when appropriate.",
            "Recommended rule direction: free-navigation exploration, required item collection, interaction with a goal or event category, obstacle management, and completion within time_limit_seconds.",
            "The existing scene is the immutable visual world base. Existing trees, bushes, rocks, water, and terrain may inspire the rules and provide navigation context, but they are not automatically gameplay obstacles, collectibles, goals, landmarks, or newly placed assets.",
            "Gameplay elements listed for later placement describe new game content to add on top of that base. For example, observing an existing bush may justify vegetation-compatible gameplay, but must not define that bush itself as a placed obstacle or collision trigger.",
            "",
            "# Time And Failure",
            "duration_seconds is a reference budget. The rule time_limit_seconds may be larger, for example 45 or 60 when duration_seconds is 30.",
            "The default fail condition should be time-based: if the win_condition is not achieved within time_limit_seconds, the player fails.",
            "win_condition must require completing a task objective. It must not only be surviving until the timer ends.",
            "Keep fail_condition simple. Light extra fail conditions are allowed, such as a collision penalty count exceeding a limit or health reaching zero.",
            "Do not make the first collision an immediate failure unless the user explicitly asks for harsh one-hit failure rules.",
            "",
            "# Forbidden Output",
            "Do not output concrete coordinates.",
            "Do not output exact spawn points, item points, event points, route points, fixed paths, waypoints, trajectories, or keyframes.",
            "Do not output Blender code, Python code, bpy calls, or asset-generation prompts.",
            "If later placement needs locations, placement_agent will choose them from this game_rule_plan.",
            "",
            "# Placement Categories Only",
            "required_gameplay_elements_for_placement must contain categories and purposes only.",
            "Allowed element_type values: collectible, obstacle, event_trigger, goal_area, hazard_zone, safe_zone, landmark.",
            "required_gameplay_elements_for_placement should describe only the element category, gameplay purpose, coarse scene-based placement intent, and required_count.",
            "placement_hint_from_scene may request a new element near useful environment context for composition, but must never request reusing, converting, or attaching gameplay behavior to a specific existing scene object.",
            "For obstacle requirements, describe the intended player response such as jump, dodge, or route-around. Leave the concrete new obstacle asset type to the later asset planner and do not default all obstacles to existing vegetation.",
            "Do not output coordinates, exact locations, paths, or world coordinate fields. The later placement_agent will decide exact world positions.",
            "",
            "# Output Rules",
            "Return only strict JSON. Do not output markdown.",
            "",
            "# Required JSON Schema",
            "```json",
            "{",
            '  "game_rule_status": {',
            '    "usable": true,',
            '    "confidence": 0.85,',
            '    "reason": ""',
            "  },",
            '  "game_rule_plan": {',
            '    "game_concept": {',
            '      "title": "",',
            '      "description": "",',
            '      "why_it_fits_this_scene": ""',
            "    },",
            '    "core_loop": "",',
            '    "player_or_npc_role": "",',
            '    "objective": "",',
            '    "win_condition": "",',
            '    "fail_condition": "",',
            '    "time_limit_seconds": 60,',
            '    "scoring_rules": [],',
            '    "allowed_actions": [],',
            '    "scene_element_usage": [],',
            '    "randomness_policy": {',
            '      "free_navigation": true,',
            '      "fixed_path_required": false,',
            '      "random_choice_allowed": true,',
            '      "description": ""',
            "    },",
            '    "required_gameplay_elements_for_placement": [',
            "      {",
            '        "element_type": "",',
            '        "purpose": "",',
            '        "placement_hint_from_scene": "",',
            '        "required_count": 1',
            "      }",
            "    ]",
            "  }",
            "}",
            "```",
            "",
            "# Text Inputs",
            "```json",
            compact,
            "```",
        ]
    )


def call_vlm_visual_reader(prompt, image_path, vlm_config):
    image_b64 = image_to_base64(image_path)
    payload = {
        "model": vlm_config["model"],
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a strict JSON rule_visual_reader. "
                    "Return visual notes only. Do not design rules, routes, points, coordinates, or code."
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
        normalize_chat_completions_url(vlm_config["base_url"]),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {vlm_config['api_key']}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"VLM visual reader HTTP {exc.code}: {detail}") from exc
    return extract_response_text(body, "VLM visual reader")


def call_llm_rule_designer(prompt, llm_config):
    payload = {
        "model": llm_config["model"],
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a strict JSON game_rule_agent. "
                    "Design playable game rules from text inputs only. "
                    "Do not output coordinates, paths, waypoints, keyframes, Blender code, or asset prompts."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.25,
        "max_tokens": 3072,
    }
    request = urllib.request.Request(
        normalize_chat_completions_url(llm_config["base_url"]),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {llm_config['api_key']}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Rule designer HTTP {exc.code}: {detail}") from exc
    return extract_response_text(body, "Rule designer")


def extract_response_text(body, label):
    choices = body.get("choices") or []
    if not choices:
        raise RuntimeError(f"{label} response did not contain choices: {body}")
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


def contains_forbidden_key(value):
    forbidden = {
        "world_xyz",
        "spawn_point",
        "spawn_points",
        "item_point",
        "item_points",
        "event_point",
        "event_points",
        "route",
        "trajectory",
        "keyframe",
        "keyframes",
        "asset_generation_prompt",
        "hunyuan3d_prompt",
        "blender_code",
        "python_code",
    }
    if isinstance(value, dict):
        for key, item in value.items():
            lower = str(key).lower()
            if lower in forbidden or lower.endswith("_path") or lower == "path" or lower == "waypoints":
                return True
            if contains_forbidden_key(item):
                return True
    elif isinstance(value, list):
        return any(contains_forbidden_key(item) for item in value)
    return False


def contains_forbidden_code_or_coordinates(value):
    text = json.dumps(value, ensure_ascii=False).lower()
    forbidden_snippets = [
        "world_xyz",
        "bpy.",
        "bpy.ops",
        "import bpy",
        "keyframe_insert",
        "```python",
        "```",
    ]
    return any(snippet in text for snippet in forbidden_snippets)


def phrase(*parts):
    return " ".join(parts)


def forbidden_runner_phrases():
    return [
        phrase("automatically", "runs", "forward"),
        phrase("continuously", "runs", "forward"),
        phrase("fixed", "forward", "momentum"),
        phrase("speed", "gradually", "increases"),
        phrase("lane", "assignment"),
        phrase("endless", "runner"),
        phrase("oncoming", "obstacles"),
        phrase("fall", "off", "terrain"),
        phrase("instant", "failure"),
        phrase("survive", "the", "full", "time"),
    ]


def contains_forbidden_runner_text(value):
    text = json.dumps(value, ensure_ascii=False).lower()
    return bool(find_unnegated_forbidden_runner_phrases(text))


def is_negated_context(text, start_index):
    context = text[max(0, start_index - 80) : start_index]
    negation_markers = [
        "do not",
        "don't",
        "not ",
        "no ",
        "avoid",
        "without",
        "must not",
        "should not",
        "is not",
        "isn't",
        "never",
        "forbid",
        "forbidden",
        "non-",
        "not an",
        "not a",
    ]
    return any(marker in context for marker in negation_markers)


def find_unnegated_forbidden_runner_phrases(text):
    hits = []
    for item in forbidden_runner_phrases():
        start = 0
        while True:
            index = text.find(item, start)
            if index < 0:
                break
            if not is_negated_context(text, index):
                hits.append(item)
            start = index + len(item)
    return sorted(set(hits))


def contains_visual_path_bias(value):
    text = json.dumps(value, ensure_ascii=False).lower()
    biased_phrases = [
        phrase("central", "corridor"),
        phrase("forward", "progression"),
        phrase("default", "path"),
        phrase("running", "lane"),
        phrase("route", "option"),
        phrase("intended", "forward", "direction"),
        phrase("camera-follow", "behavior"),
        phrase("follow", "forward", "lane"),
    ]
    return any(item in text for item in biased_phrases)


def win_condition_is_only_survival(plan):
    win_condition = str(plan.get("win_condition", "")).lower()
    if "surviv" not in win_condition:
        return False
    task_terms = ["collect", "interact", "activate", "trigger", "reach", "complete", "goal"]
    return not any(term in win_condition for term in task_terms)


def fail_condition_mentions_time_limit(plan):
    fail_condition = str(plan.get("fail_condition", "")).lower()
    return "time_limit_seconds" in fail_condition or ("time" in fail_condition and "win_condition" in fail_condition)


def require_nonempty_string(data, key, field_name, errors):
    if not isinstance(data.get(key), str) or not data.get(key).strip():
        errors.append(f"{field_name} must be a non-empty string")


def require_list(data, key, field_name, errors):
    if not isinstance(data.get(key), list):
        errors.append(f"{field_name} must be a list")


def validate_status(status, field_name, errors):
    if not isinstance(status, dict):
        errors.append(f"{field_name} must exist and be an object")
        return
    if not isinstance(status.get("usable"), bool):
        errors.append(f"{field_name}.usable must be boolean")
    confidence = status.get("confidence")
    if not isinstance(confidence, (int, float)) or not (0.0 <= float(confidence) <= 1.0):
        errors.append(f"{field_name}.confidence must be a number from 0.0 to 1.0")
    require_nonempty_string(status, "reason", f"{field_name}.reason", errors)


def validate_visual_rule_notes(data):
    errors = []
    warnings = []
    if not isinstance(data, dict):
        return ["top-level visual notes output must be a JSON object"], warnings
    if contains_forbidden_key(data) or contains_forbidden_code_or_coordinates(data):
        errors.append("visual notes must not contain world_xyz, paths, waypoints, keyframes, Blender code, or asset prompts")
    if contains_visual_path_bias(data):
        errors.append("visual notes must not convert open visual gaps into movement routes, corridors, lanes, or one-way travel plans")

    validate_status(data.get("visual_rule_notes_status"), "visual_rule_notes_status", errors)
    notes = data.get("visual_rule_notes")
    if not isinstance(notes, dict):
        errors.append("visual_rule_notes must exist and be an object")
        return errors, warnings

    require_nonempty_string(notes, "rule_relevant_scene_summary", "visual_rule_notes.rule_relevant_scene_summary", errors)
    require_list(notes, "scene_features_useful_for_rules", "visual_rule_notes.scene_features_useful_for_rules", errors)
    require_nonempty_string(notes, "movement_space_observation", "visual_rule_notes.movement_space_observation", errors)
    require_list(notes, "visual_landmarks_for_rules", "visual_rule_notes.visual_landmarks_for_rules", errors)
    require_list(notes, "rule_design_risks_from_visuals", "visual_rule_notes.rule_design_risks_from_visuals", errors)
    require_list(notes, "notes_for_rule_designer", "visual_rule_notes.notes_for_rule_designer", errors)
    return errors, warnings


def validate_game_rule_plan(data):
    errors = []
    warnings = []
    if not isinstance(data, dict):
        return ["top-level output must be a JSON object"], warnings
    if contains_forbidden_key(data) or contains_forbidden_code_or_coordinates(data):
        errors.append("output must not contain world_xyz, paths, waypoints, keyframes, Blender code, or asset prompts")
    runner_hits = find_unnegated_forbidden_runner_phrases(json.dumps(data, ensure_ascii=False).lower())
    if runner_hits:
        errors.append(
            "output must not positively use automatic forward runner, endless runner, lane, oncoming obstacle, "
            "instant failure, or survive-full-time mechanics: " + ", ".join(runner_hits)
        )

    validate_status(data.get("game_rule_status"), "game_rule_status", errors)
    plan = data.get("game_rule_plan")
    if not isinstance(plan, dict):
        errors.append("game_rule_plan must exist and be an object")
        return errors, warnings

    concept = plan.get("game_concept")
    if not isinstance(concept, dict):
        errors.append("game_rule_plan.game_concept must exist and be an object")
    else:
        require_nonempty_string(concept, "title", "game_concept.title", errors)
        require_nonempty_string(concept, "description", "game_concept.description", errors)
        require_nonempty_string(concept, "why_it_fits_this_scene", "game_concept.why_it_fits_this_scene", errors)

    for key in ["core_loop", "objective", "win_condition", "fail_condition"]:
        require_nonempty_string(plan, key, f"game_rule_plan.{key}", errors)
    if win_condition_is_only_survival(plan):
        errors.append("win_condition must be task-completion based, not only surviving for time_limit_seconds")
    if not fail_condition_mentions_time_limit(plan):
        warnings.append("fail_condition should state that failing to achieve win_condition within time_limit_seconds causes failure")

    time_limit = plan.get("time_limit_seconds")
    if not isinstance(time_limit, int) or time_limit <= 0:
        errors.append("game_rule_plan.time_limit_seconds must be a positive integer")

    require_list(plan, "scoring_rules", "game_rule_plan.scoring_rules", errors)
    require_list(plan, "allowed_actions", "game_rule_plan.allowed_actions", errors)
    actions = plan.get("allowed_actions")
    if isinstance(actions, list):
        action_set = {str(action) for action in actions}
        missing = sorted(REQUIRED_MOVE_ACTIONS - action_set)
        if missing:
            errors.append("allowed_actions must include move_forward, move_backward, move_left, move_right")

    require_list(plan, "scene_element_usage", "game_rule_plan.scene_element_usage", errors)

    randomness = plan.get("randomness_policy")
    if not isinstance(randomness, dict):
        errors.append("game_rule_plan.randomness_policy must be an object")
    else:
        if randomness.get("free_navigation") is not True:
            errors.append("randomness_policy.free_navigation must be true")
        if randomness.get("fixed_path_required") is not False:
            errors.append("randomness_policy.fixed_path_required must be false")
        if randomness.get("random_choice_allowed") is not True:
            errors.append("randomness_policy.random_choice_allowed must be true")
        require_nonempty_string(randomness, "description", "randomness_policy.description", errors)

    required_elements = plan.get("required_gameplay_elements_for_placement")
    if not isinstance(required_elements, list):
        errors.append("game_rule_plan.required_gameplay_elements_for_placement must be a list")
    else:
        if not required_elements:
            warnings.append("required_gameplay_elements_for_placement is empty")
        for index, item in enumerate(required_elements):
            prefix = f"required_gameplay_elements_for_placement[{index}]"
            if not isinstance(item, dict):
                errors.append(f"{prefix} must be an object")
                continue
            element_type = item.get("element_type")
            if element_type not in ALLOWED_PLACEMENT_ELEMENT_TYPES:
                errors.append(f"{prefix}.element_type is invalid")
            require_nonempty_string(item, "purpose", f"{prefix}.purpose", errors)
            require_nonempty_string(item, "placement_hint_from_scene", f"{prefix}.placement_hint_from_scene", errors)
            if not isinstance(item.get("required_count"), int) or item.get("required_count") <= 0:
                errors.append(f"{prefix}.required_count must be a positive integer")

    return errors, warnings


def validate_config(config, required_keys):
    return [key for key in required_keys if not config.get(key)]


def main():
    args = parse_args()
    ensure_dirs()
    report = {
        "ok": False,
        "input_scene_understanding": SCENE_UNDERSTANDING_JSON,
        "input_metadata": METADATA_JSON,
        "input_grid_image": GRID_IMAGE,
        "visual_notes_json": VISUAL_NOTES_JSON,
        "output_json": OUTPUT_JSON,
        "user_prompt": args.user_prompt,
        "duration_seconds": args.duration_seconds,
    }
    try:
        metadata = load_json(METADATA_JSON)
        scene_understanding = load_json(SCENE_UNDERSTANDING_JSON)
        if not os.path.exists(GRID_IMAGE):
            raise FileNotFoundError(f"required image not found: {GRID_IMAGE}")

        vlm_config = get_vlm_visual_config()
        report.update({"vlm_model": vlm_config["model"], "vlm_base_url": vlm_config["base_url"]})
        missing_vlm = validate_config(vlm_config, ["api_key", "base_url", "model"])
        if missing_vlm:
            report.update({"ok": False, "failed_stage": "rule_visual_reader", "missing_config": missing_vlm})
            log("GAME_RULE_VISUAL_READER_SKIPPED_MISSING_CONFIG", ",".join(missing_vlm))
            return

        visual_prompt = build_visual_reader_prompt(metadata, scene_understanding, args.user_prompt)
        write_text(VISUAL_PROMPT, visual_prompt)
        visual_raw = call_vlm_visual_reader(visual_prompt, GRID_IMAGE, vlm_config)
        write_text(VISUAL_RAW, visual_raw)
        visual_notes = json.loads(strip_json_markdown(visual_raw))
        visual_errors, visual_warnings = validate_visual_rule_notes(visual_notes)
        if visual_errors:
            raise ValueError("visual rule notes validation failed: " + "; ".join(visual_errors))
        write_json(VISUAL_NOTES_JSON, visual_notes)

        llm_config = get_rule_llm_config()
        report.update({"rule_model": llm_config.get("model"), "rule_base_url": llm_config.get("base_url")})
        missing_llm = validate_config(llm_config, ["api_key", "base_url", "model"])
        if missing_llm:
            remove_file_if_exists(OUTPUT_JSON)
            report.update({"ok": False, "failed_stage": "rule_designer", "missing_config": missing_llm})
            log("GAME_RULE_DESIGNER_SKIPPED_MISSING_CONFIG", ",".join(missing_llm))
            return

        rule_prompt = build_rule_designer_prompt(
            metadata,
            scene_understanding,
            visual_notes,
            args.user_prompt,
            args.duration_seconds,
        )
        write_text(OUTPUT_PROMPT, rule_prompt)
        rule_raw = call_llm_rule_designer(rule_prompt, llm_config)
        write_text(OUTPUT_RAW, rule_raw)
        data = json.loads(strip_json_markdown(rule_raw))
        rule_errors, rule_warnings = validate_game_rule_plan(data)
        if rule_errors:
            raise ValueError("game rule plan validation failed: " + "; ".join(rule_errors))
        write_json(OUTPUT_JSON, data)

        report.update(
            {
                "ok": True,
                "visual_prompt": VISUAL_PROMPT,
                "visual_raw_response": VISUAL_RAW,
                "prompt": OUTPUT_PROMPT,
                "raw_response": OUTPUT_RAW,
                "game_rule_plan_json": OUTPUT_JSON,
                "validation_warnings": visual_warnings + rule_warnings,
            }
        )
        log("GAME_RULE_VISUAL_NOTES_JSON", VISUAL_NOTES_JSON)
        log("GAME_RULE_PLAN_JSON", OUTPUT_JSON)
    except Exception as exc:
        remove_file_if_exists(OUTPUT_JSON)
        report.update(
            {
                "ok": False,
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }
        )
        log("GAME_RULE_ERROR", exc)
        log(traceback.format_exc())
    finally:
        write_json(OUTPUT_REPORT, report)
        log("GAME_RULE_REPORT", OUTPUT_REPORT)


if __name__ == "__main__":
    main()
