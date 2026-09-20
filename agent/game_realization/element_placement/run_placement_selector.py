"""LLM-only stage: choose packet candidate ids without access to world coordinates."""
import argparse
import json
import math
import os
import re
import sys
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
COMMON_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", "shared_representation"))
for _path in (SCRIPT_DIR, COMMON_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from placement_io import clear_downstream_outputs, contains_coordinate_key, load_json, packet_path, sha256_file, strip_json_markdown, write_json, write_text
from placement_constraints import ALLOWED_SURFACE_ALIGNMENT_MODES

FIXED_PLACEMENT_MODEL = "qwen3.6-plus"
FORBIDDEN_KEYS = {"world_xyz", "ground_world_xyz", "anchor_world_xyz", "normal_xyz", "position", "location", "coordinates", "coordinate", "path", "waypoint", "waypoints", "trajectory"}


def parse_args():
    parser = argparse.ArgumentParser(); parser.add_argument("--self_test", action="store_true"); return parser.parse_args()


def llm_config():
    return {"base_url": os.environ.get("CODE2GAMES_PLACEMENT_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or os.environ.get("DASHSCOPE_BASE_URL") or "https://dashscope.aliyuncs.com/compatible-mode/v1", "api_key": os.environ.get("CODE2GAMES_PLACEMENT_API_KEY") or os.environ.get("OPENAI_API_KEY") or os.environ.get("DASHSCOPE_API_KEY"), "model": os.environ.get("CODE2GAMES_PLACEMENT_MODEL") or FIXED_PLACEMENT_MODEL}


def call_llm(prompt, config):
    if not all(config.values()): raise ValueError("LLM configuration missing api_key, base_url, or model")
    url = config["base_url"].rstrip("/")
    if not url.endswith("/chat/completions"): url += "/chat/completions"
    body = json.dumps({"model": config["model"], "messages": [{"role": "system", "content": "Return only valid JSON. Never output coordinates, code, paths, or waypoints."}, {"role": "user", "content": prompt}], "temperature": 0.15, "max_tokens": 4096}).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={"Authorization": "Bearer " + config["api_key"], "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=900) as response: payload = json.loads(response.read().decode("utf-8"))
    return payload["choices"][0]["message"]["content"]


def forbidden(value):
    if isinstance(value, dict): return any(str(key).lower() in FORBIDDEN_KEYS or forbidden(item) for key, item in value.items())
    if isinstance(value, list): return any(forbidden(item) for item in value)
    if isinstance(value, str): return bool(re.search(r"\[\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?\s*\]", value)) or "bpy." in value.lower()
    return False


def finite(value, minimum, maximum): return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) and minimum <= value <= maximum


def nonnegative_finite(value): return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) and value >= 0


def validate_selection(data, packet, preflight):
    errors = []
    if not isinstance(data, dict): return ["selection must be an object"]
    if forbidden(data) or contains_coordinate_key(data): errors.append("selection must not contain coordinates, paths, waypoints, trajectories, or code")
    status = data.get("selection_status")
    if not isinstance(status, dict) or not isinstance(status.get("usable"), bool) or not finite(status.get("confidence"), 0, 1) or not isinstance(status.get("reason"), str) or not status["reason"].strip(): errors.append("selection_status must contain boolean usable, confidence, and non-empty reason")
    selections = data.get("placement_selections")
    if not isinstance(selections, list): return errors + ["placement_selections must be a list"]
    if isinstance(status, dict) and status.get("usable") is False:
        relaxations = data.get("requested_rule_relaxations")
        if selections: errors.append("placement_selections must be empty when usable=false")
        if not isinstance(relaxations, list) or not relaxations or not all(isinstance(item, dict) for item in relaxations): errors.append("requested_rule_relaxations must be a non-empty object list when usable=false")
        return errors
    requirements = {item["source_requirement_index"]: item for item in packet["requirements"]}
    candidates = {item["candidate_id"]: item for item in packet["candidates"]}
    expected = preflight["required_total_selection_count"]
    if len(selections) != expected: errors.append("placement_selections count must be exactly %d" % expected)
    used_candidates, used_placements, by_requirement = set(), set(), {}
    for index, item in enumerate(selections):
        prefix = "placement_selections[%d]" % index
        if not isinstance(item, dict): errors.append(prefix + " must be object"); continue
        pid, cid, req = item.get("placement_id"), item.get("selected_candidate_id"), item.get("source_requirement_index")
        if not isinstance(pid, str) or not pid.strip() or pid in used_placements: errors.append(prefix + ".placement_id invalid or duplicate")
        used_placements.add(pid)
        if cid not in candidates or cid in used_candidates: errors.append(prefix + ".selected_candidate_id invalid or duplicate")
        used_candidates.add(cid)
        if req not in requirements: errors.append(prefix + ".source_requirement_index invalid"); continue
        by_requirement[req] = by_requirement.get(req, 0) + 1
        element = item.get("element_type")
        if element != requirements[req]["element_type"]: errors.append(prefix + ".element_type does not match requirement")
        modes = candidates.get(cid, {}).get("physically_supported_modes", [])
        if item.get("placement_mode") not in modes: errors.append(prefix + ".placement_mode is not allowed by packet")
        if item.get("placement_mode") == "above_ground":
            if not nonnegative_finite(item.get("height_offset")) or float(item.get("height_offset")) <= 0: errors.append(prefix + ".height_offset must be a positive number for above_ground")
        else:
            item["height_offset"] = 0.0
        alignment_mode = item.get("surface_alignment_mode", "auto")
        if alignment_mode not in ALLOWED_SURFACE_ALIGNMENT_MODES:
            errors.append(prefix + ".surface_alignment_mode is invalid")
        elif item.get("placement_mode") == "above_ground" and alignment_mode not in {"auto", "upright"}:
            errors.append(prefix + ".above_ground placements only support auto or upright alignment")
        item["surface_alignment_mode"] = alignment_mode
        for key, minimum, maximum in (
            ("max_tilt_degrees", 0.0, 180.0),
            ("surface_clearance_m", 0.0, 10.0),
            ("vegetation_clearance_radius_m", 0.0, 100.0),
            ("yaw_degrees", -180.0, 180.0),
        ):
            if key in item and not finite(item.get(key), minimum, maximum):
                errors.append(prefix + "." + key + " is invalid")
        if not all(isinstance(item.get(key), str) and item[key].strip() for key in ("gameplay_purpose", "placement_reason")): errors.append(prefix + ".purpose/reason invalid")
    for index, requirement in requirements.items():
        if by_requirement.get(index, 0) != requirement["required_count"]: errors.append("source_requirement_index %s has wrong selection count" % index)
    notes = data.get("selection_notes")
    if not isinstance(notes, list) or not notes or not all(isinstance(note, str) and note.strip() for note in notes): errors.append("selection_notes must be non-empty string list")
    return errors


def main():
    try:
        clear_downstream_outputs("selector")
        preflight = load_json(packet_path("placement_preflight.json")); packet = load_json(packet_path("placement_llm_packet.json")); prompt_path = packet_path("gameplay_placement_prompt.md"); prompt = open(prompt_path, encoding="utf-8").read()
        if preflight.get("ok") is not True: raise ValueError("placement preflight is not ok")
        run_id = preflight.get("run_id")
        if not run_id or packet.get("run_id") != run_id: raise ValueError("placement run_id mismatch")
        if len(packet.get("candidates", [])) != preflight.get("candidate_count") or len(packet["candidates"]) != preflight.get("prompt_candidate_count"): raise ValueError("preflight candidate count does not match LLM packet")
        candidate_hash = sha256_file(packet_path("placement_candidates.json")); game_rule_hash = sha256_file(packet_path("game_rule_plan.json"))
        expected_fingerprints = preflight.get("input_fingerprints", {})
        if candidate_hash != expected_fingerprints.get("placement_candidates_sha256") or candidate_hash != packet.get("input_fingerprints", {}).get("placement_candidates_sha256"): raise ValueError("placement candidate fingerprint mismatch")
        if game_rule_hash != expected_fingerprints.get("game_rule_plan_sha256") or game_rule_hash != packet.get("input_fingerprints", {}).get("game_rule_plan_sha256"): raise ValueError("game rule fingerprint mismatch")
        for name, key in (("default_camera_metadata.json", "default_camera_metadata_sha256"), ("scene_understanding_default_camera.json", "scene_understanding_sha256"), ("game_rule_visual_notes.json", "game_rule_visual_notes_sha256")):
            if sha256_file(packet_path(name)) != expected_fingerprints.get(key): raise ValueError("input fingerprint mismatch: " + key)
        if sha256_file(packet_path("placement_llm_packet.json")) != preflight.get("placement_llm_packet_sha256") or sha256_file(prompt_path) != preflight.get("gameplay_placement_prompt_sha256"): raise ValueError("packet or prompt fingerprint mismatch")
        config = llm_config(); raw = call_llm(prompt, config); write_text(packet_path("gameplay_placement_raw_response.md"), raw)
        selection = json.loads(strip_json_markdown(raw)); errors = validate_selection(selection, packet, preflight)
        if errors: raise ValueError("placement selection validation failed: " + "; ".join(errors))
        selection["run_id"] = run_id
        selection["input_fingerprints"] = {"placement_candidates_sha256": candidate_hash, "placement_llm_packet_sha256": sha256_file(packet_path("placement_llm_packet.json"))}
        write_json(packet_path("gameplay_placement_selection.json"), selection)
        usable = selection["selection_status"]["usable"]
        report = {"ok": usable, "infeasible": not usable, "run_id": run_id, "llm_call_count": 1, "model": config["model"], "candidate_count": len(packet["candidates"]), "requested_rule_relaxations": selection.get("requested_rule_relaxations", []), "output_selection": packet_path("gameplay_placement_selection.json")}; write_json(packet_path("placement_selection_report.json"), report)
        if not usable: print("PLACEMENT_SELECTOR_INFEASIBLE", selection["selection_status"]["reason"]); return {"ok": False, "infeasible": True, "run_id": run_id, "output_files": [packet_path("gameplay_placement_selection.json")], "error": selection["selection_status"]["reason"]}
        print("PLACEMENT_SELECTOR_OK"); return {"ok": True, "run_id": run_id, "output_files": [packet_path("gameplay_placement_selection.json")]}
    except Exception as exc:
        write_json(packet_path("placement_selection_report.json"), {"ok": False, "run_id": locals().get("run_id"), "llm_call_count": 1 if "raw" in locals() else 0, "raw_response": packet_path("gameplay_placement_raw_response.md") if "raw" in locals() else None, "error": str(exc)}); return {"ok": False, "run_id": locals().get("run_id"), "output_files": [packet_path("placement_selection_report.json")], "error": str(exc)}


def run_self_tests():
    packet = {"requirements": [{"source_requirement_index": 0, "element_type": "collectible", "required_count": 1}], "candidates": [{"candidate_id": "a", "physically_supported_modes": ["on_ground"]}]}; preflight = {"required_total_selection_count": 1}
    good = {"selection_status": {"usable": True, "confidence": .9, "reason":"valid selection"}, "placement_selections": [{"placement_id":"p","element_type":"collectible","source_requirement_index":0,"selected_candidate_id":"a","placement_mode":"on_ground","height_offset":0,"gameplay_purpose":"x","placement_reason":"x"}], "selection_notes":["summary"]}
    assert not validate_selection(good, packet, preflight); bad = dict(good); bad["world_xyz"]=[0,0,0]; assert validate_selection(bad, packet, preflight); print("PLACEMENT_SELECTOR_SELF_TEST_OK")


if __name__ == "__main__":
    args = parse_args(); result = run_self_tests() if args.self_test else main(); sys.exit(0 if result is None or result.get("ok", True) else 1)
