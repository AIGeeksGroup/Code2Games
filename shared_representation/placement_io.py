"""Shared, atomic I/O helpers for the placement pipeline."""
import glob
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone

from staging_paths import get_packet_dir, get_staging_root


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
STAGING_ROOT = get_staging_root(PROJECT_ROOT)
PACKET_DIR = get_packet_dir(PROJECT_ROOT)

SANITIZE_KEYS = {
    "world_xyz", "ground_world_xyz", "anchor_world_xyz", "normal_xyz", "matrix_world",
    "world_position", "camera_position", "coordinates", "coordinate", "position", "location",
    "ray_origin", "ray_direction",
}


def packet_path(name):
    return os.path.join(PACKET_DIR, name)


def load_json(path):
    if not os.path.exists(path):
        raise FileNotFoundError("required JSON not found: " + path)
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
    os.replace(temporary, path)


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(temporary, path)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_json_canonical(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def new_run_id():
    return "placement_%s_%s" % (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S"), uuid.uuid4().hex[:8])


def remove_file_if_exists(path):
    if os.path.isfile(path):
        os.remove(path)


def remove_matching_files(pattern):
    for path in glob.glob(pattern):
        remove_file_if_exists(path)


def clear_downstream_outputs(stage="prepare"):
    names = ["placement_preflight.json", "placement_preflight_report.json", "placement_llm_packet.json", "gameplay_placement_prompt.md", "gameplay_placement_raw_response.md", "gameplay_placement_selection.json", "placement_selection_report.json", "placement_final_validation.json", "gameplay_placement_plan.json", "gameplay_placement_report.json"]
    if stage == "selector": names = ["gameplay_placement_raw_response.md", "gameplay_placement_selection.json", "placement_selection_report.json"]
    if stage == "finalize": names = ["gameplay_placement_plan.json", "gameplay_placement_report.json", "placement_final_validation.json"]
    for name in names: remove_file_if_exists(packet_path(name))
    if stage in {"prepare", "selector"}: remove_matching_files(packet_path("gameplay_placement_raw_response_attempt_*.md"))


def redact_for_repair(text):
    value = str(text)
    value = re.sub(r"```[\s\S]*?```", "[redacted code block]", value)
    value = re.sub(r"\[\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?\s*\]", "[redacted coordinate]", value)
    return re.sub(r"(?i)world_xyz|position|location|path|waypoint|trajectory|bpy", "[redacted]", value)


def write_failure_json(path, run_id, errors, **extra):
    payload = {"ok": False, "run_id": run_id, "validation_errors": list(errors)}
    payload.update(extra)
    write_json(path, payload)
    return payload


def sanitize_for_llm(value):
    if isinstance(value, dict):
        return {str(key): sanitize_for_llm(item) for key, item in value.items() if str(key).lower() not in SANITIZE_KEYS}
    if isinstance(value, list):
        return [sanitize_for_llm(item) for item in value]
    return value


def contains_coordinate_key(value):
    if isinstance(value, dict):
        return any(str(key).lower() in SANITIZE_KEYS or contains_coordinate_key(item) for key, item in value.items())
    if isinstance(value, list):
        return any(contains_coordinate_key(item) for item in value)
    return False


def strip_json_markdown(text):
    content = (text or "").strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content, flags=re.IGNORECASE)
        content = re.sub(r"\s*```$", "", content)
    if not content.startswith("{"):
        start, end = content.find("{"), content.rfind("}")
        if start >= 0 and end > start:
            content = content[start:end + 1]
    return content.strip()
