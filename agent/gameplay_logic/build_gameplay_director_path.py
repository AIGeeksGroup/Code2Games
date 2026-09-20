"""Build a reusable Director path from fixed gameplay placements.

This generic builder contains no scene coordinates, placement IDs, or asset-ID
rules. It turns semantic placements into action beats, inserts bounded transit
beats, assigns physical timing, and emits production plus legacy camera fields.
Asset anchors are read-only; only actor/camera route points are synthesized.
"""

import argparse
import json
import math
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
COMMON_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "shared_representation"))
if COMMON_DIR not in sys.path:
    sys.path.insert(0, COMMON_DIR)

from staging_paths import get_packet_dir, get_staging_root


PROJECT_ROOT = os.path.abspath(os.environ.get("CODE2WORLDS_ROOT") or os.path.join(SCRIPT_DIR, "..", ".."))
STAGING_ROOT = get_staging_root(PROJECT_ROOT)
DEFAULT_PLACEMENT_PLAN = os.path.join(get_packet_dir(PROJECT_ROOT), "gameplay_placement_plan.json")
DEFAULT_ASSET_PLAN = os.path.join(STAGING_ROOT, "asset_realization", "asset_plan.json")
DEFAULT_OUTPUT = os.path.join(STAGING_ROOT, "director_path", "director_path.json")


ACTION_BY_EVENT = {
    "spawn": "establish_control", "move": "tactical_traverse",
    "drive": "precision_drive", "collect": "pickup_and_confirm",
    "collect_required": "objective_pickup_and_counter_increment",
    "jump": "jump_and_land", "jump_collect": "jump_pickup_and_land",
    "combat": "aim_burst_reposition", "dodge": "telegraph_then_evade",
    "recover": "brief_recovery_without_stopping_momentum",
    "interact": "hold_interact_then_event_release",
    "reveal": "orient_and_reveal_landmark", "checkpoint": "cross_timing_gate",
    "boost": "collect_and_activate_boost", "hazard": "avoid_or_recover",
    "launch": "launch_and_transition_to_flight", "flight": "sustained_flight",
    "flight_collectible": "intercept_flight_cell_and_increment_power",
    "flight_hazard": "bank_or_dive_near_miss",
    "goal": "complete_objective_and_hold_hero_pose",
}

EFFECTS_BY_EVENT = {
    "collect": ["pickup_flash", "objective_tick"],
    "collect_required": ["objective_energy_burst", "objective_counter_increment"],
    "jump": ["jump_trail", "landing_dust"],
    "jump_collect": ["pickup_flash", "airborne_trail", "landing_dust"],
    "combat": ["muzzle_flash", "directional_tracers", "surface_impacts"],
    "dodge": ["danger_telegraph", "near_miss_impact"],
    "interact": ["interaction_progress", "objective_state_change"],
    "reveal": ["landmark_emission_ramp", "atmospheric_light_sweep"],
    "checkpoint": ["timing_gate_sweep", "split_time_flash"],
    "boost": ["boost_pickup_burst", "exhaust_flame", "speed_streaks"],
    "hazard": ["surface_debris", "camera_vibration"],
    "launch": ["launch_dust", "engine_or_wingsuit_ignition"],
    "flight_hazard": ["near_miss_debris", "wingtip_vortex"],
    "flight_collectible": ["flight_cell_burst", "power_increment"],
    "goal": ["objective_complete_flare"],
}


def script_args():
    """Work from ordinary Python and from Blender's ``--`` argument split."""
    return sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]


def parse_args():
    parser = argparse.ArgumentParser(description="Build a reusable Director path from fixed placements")
    parser.add_argument("--placement_plan", default=DEFAULT_PLACEMENT_PLAN)
    parser.add_argument("--asset_plan", default=DEFAULT_ASSET_PLAN)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--genre", choices=("generic", "fps", "tps", "racing", "wingsuit"), default="generic")
    parser.add_argument("--fps", type=int, default=24)
    parser.add_argument("--run_speed_mps", type=float, default=7.0)
    parser.add_argument("--drive_speed_mps", type=float, default=14.0)
    parser.add_argument("--flight_speed_mps", type=float, default=22.0)
    parser.add_argument("--maximum_step_m", type=float, default=3.0)
    parser.add_argument("--turn_smoothing", type=float, default=0.35)
    parser.add_argument("--maximum_turn_degrees_per_second", type=float, default=120.0)
    parser.add_argument("--target_duration_seconds", type=float, default=0.0)
    parser.add_argument("--camera_distance_m", type=float, default=None)
    parser.add_argument("--camera_height_m", type=float, default=None)
    return parser.parse_args(script_args())


def load_json(path):
    if not os.path.isfile(path):
        raise FileNotFoundError("required JSON not found: %s" % path)
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path, value):
    """Durably publish the Director hand-off before the staging step reads it."""
    absolute = os.path.abspath(path)
    parent = os.path.dirname(absolute)
    if parent:
        os.makedirs(parent, exist_ok=True)
    temporary = absolute + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, absolute)


def vec3(value):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError("expected a three-component world coordinate")
    return [float(value[0]), float(value[1]), float(value[2])]


def add3(a, b):
    return [float(a[i]) + float(b[i]) for i in range(3)]


def sub3(a, b):
    return [float(a[i]) - float(b[i]) for i in range(3)]


def mul3(a, scalar):
    return [float(value) * float(scalar) for value in a]


def distance3(a, b):
    return math.sqrt(sum((float(a[i]) - float(b[i])) ** 2 for i in range(3)))


def normalize3(value, fallback=(1.0, 0.0, 0.0)):
    length = math.sqrt(sum(float(component) ** 2 for component in value))
    return [float(component) / length for component in value] if length > 1e-8 else list(fallback)


def horizontal_forward(anchors, index):
    if index == 0:
        delta = sub3(anchors[1], anchors[0])
    elif index == len(anchors) - 1:
        delta = sub3(anchors[-1], anchors[-2])
    else:
        delta = sub3(anchors[index + 1], anchors[index - 1])
    return normalize3([delta[0], delta[1], 0.0])


def fixed_anchor(item):
    return vec3(item.get("anchor_world_xyz") or item.get("world_xyz"))


def placement_elements(plan):
    elements = plan.get("placement_plan", {}).get("elements")
    if not isinstance(elements, list) or len(elements) < 2:
        raise ValueError("placement plan must contain at least two elements")
    seen = set()
    validated = []
    for item in elements:
        placement_id = item.get("placement_id")
        if not isinstance(placement_id, str) or not placement_id or placement_id in seen:
            raise ValueError("placement IDs must be non-empty unique strings")
        fixed_anchor(item)
        seen.add(placement_id)
        if item.get("route_enabled", True):
            validated.append(dict(item))
    if len(validated) < 2:
        raise ValueError("fewer than two placements are enabled for the Director route")
    validated.sort(key=lambda item: (item.get("route_order", 10**9), item["placement_id"]))
    return validated


def placement_asset_map(asset_data):
    assets = asset_data.get("assets") or asset_data.get("asset_plan", {}).get("assets") or []
    result = {}
    for asset in assets:
        for placement_id in asset.get("placement_ids", []):
            if placement_id in result:
                raise ValueError("multiple assets claim placement %s" % placement_id)
            result[placement_id] = asset
    return result


def asset_size(asset):
    raw = asset.get("expected_size_meters") or [1.0, 1.0, 1.0]
    try:
        size = vec3(raw)
    except (TypeError, ValueError, IndexError):
        size = [1.0, 1.0, 1.0]
    return [max(0.05, abs(value)) for value in size]


def text_hint(placement):
    fields = (placement.get("gameplay_purpose"), placement.get("label"),
              placement.get("description"), placement.get("element_type"))
    return " ".join(str(value).lower() for value in fields if value)


def movement_mode(genre, placement):
    explicit = placement.get("movement_mode")
    if explicit in {"ground", "drive", "flight"}:
        return explicit
    if genre == "racing":
        return "drive"
    if genre == "wingsuit":
        element_type = str(placement.get("element_type") or "").lower()
        return "ground" if element_type in {"player_spawn", "vehicle_spawn", "spawn"} else "flight"
    return "ground"


def infer_event(placement, mode):
    explicit = placement.get("event_type") or placement.get("director_event_type")
    if explicit:
        return str(explicit)
    element_type = str(placement.get("element_type") or "").lower()
    hint = text_hint(placement)
    if "spawn" in element_type:
        return "spawn"
    if element_type in {"goal", "goal_area", "finish", "extraction_zone"} or any(word in hint for word in ("finish", "extract", "goal")):
        return "goal"
    if element_type in {"collectible", "pickup", "resource", "objective_item"}:
        if mode == "flight":
            return "flight_collectible"
        return "collect_required" if any(word in hint for word in ("required", "objective", "cell")) else "collect"
    if element_type in {"enemy", "enemy_spawn", "hostile", "combat_zone"}:
        return "combat"
    if element_type in {"hazard", "hazard_zone"}:
        return "flight_hazard" if mode == "flight" else "hazard"
    if element_type in {"obstacle", "barrier"}:
        return "jump" if any(word in hint for word in ("jump", "vault", "leap", "over")) else "dodge"
    if element_type in {"landmark", "vista", "signal", "relay"}:
        return "reveal" if not any(word in hint for word in ("activate", "secure", "inspect", "interact")) else "interact"
    if element_type in {"interaction", "interaction_point", "event_trigger", "switch", "puzzle_element"}:
        return "interact"
    if element_type in {"checkpoint", "gate", "capture_zone"}:
        return "checkpoint"
    if "boost" in hint:
        return "boost"
    if "launch" in hint:
        return "launch"
    return "flight" if mode == "flight" else "move"


def phase_for(index, count):
    ratio = float(index) / max(1, count - 1)
    if ratio < 1.0 / 3.0:
        return "opening", 0.42 + ratio * 0.42
    if ratio < 2.0 / 3.0:
        return "progression", 0.62 + (ratio - 1.0 / 3.0) * 0.54
    return "climax", min(1.0, 0.82 + (ratio - 2.0 / 3.0) * 0.54)


def default_speed_multiplier(genre, event_type):
    general = {
        "spawn": 0.75, "collect": 0.88, "collect_required": 0.80,
        "jump": 1.04, "jump_collect": 1.02, "combat": 0.78,
        "dodge": 1.12, "recover": 0.72, "interact": 0.56,
        "reveal": 0.74, "checkpoint": 1.08, "boost": 1.35,
        "hazard": 0.84, "launch": 1.15, "flight": 1.18,
        "flight_collectible": 1.10, "flight_hazard": 1.06, "goal": 0.82,
    }
    if genre == "racing" and event_type == "boost":
        return 1.0
    return float(general.get(event_type, 1.0))


def default_dwell_seconds(event_type):
    return float({
        "spawn": 0.70, "collect": 0.28, "collect_required": 0.38,
        "jump": 0.18, "jump_collect": 0.42, "combat": 0.82,
        "dodge": 0.16, "recover": 0.55, "interact": 1.15,
        "reveal": 0.86, "checkpoint": 0.08, "boost": 0.04,
        "hazard": 0.06, "launch": 0.25, "goal": 1.30,
    }.get(event_type, 0.0))


def audio_cue(event_type):
    return {
        "combat": "weapon_burst_and_impacts", "dodge": "near_miss_warning",
        "collect": "pickup_chime", "collect_required": "objective_powerup",
        "jump": "jump_whoosh_and_landing", "jump_collect": "jump_whoosh_and_pickup",
        "interact": "machine_charge_and_release", "reveal": "landmark_music_sting",
        "checkpoint": "checkpoint_confirmation", "boost": "boost_ignition",
        "hazard": "hazard_impact_and_recovery", "launch": "launch_ignition",
        "flight_collectible": "flight_cell_powerup", "flight_hazard": "flight_near_miss",
        "goal": "objective_complete_music_release",
    }.get(event_type, "adaptive_gameplay_ambience")


def camera_defaults(genre, requested_distance=None, requested_height=None):
    defaults = {
        "generic": ("third_person_follow", 38.0, 7.5, 3.8, "center"),
        "fps": ("first_person_follow", 30.0, 0.0, 1.68, "center"),
        "tps": ("right_shoulder_follow", 38.0, 6.8, 3.2, "right"),
        "racing": ("rigid_low_vehicle_chase", 30.0, 10.5, 4.2, "center"),
        "wingsuit": ("rigid_left_rear_flight_chase", 38.0, 28.0, 7.5, "left"),
    }
    shot, lens, distance, height, side = defaults[genre]
    if requested_distance is not None:
        distance = float(requested_distance)
    if requested_height is not None:
        height = float(requested_height)
    return {"shot_type": shot, "lens_mm": lens, "distance_m": distance, "height_m": height, "side": side}


def camera_cue(base_camera, genre, event_type, look_at):
    cue = dict(base_camera)
    overrides = {
        "reveal": ("landmark_reveal", 50.0), "interact": ("interaction_emphasis", 42.0),
        "combat": ("combat_follow", 36.0), "dodge": ("lateral_follow", 32.0),
        "checkpoint": ("gate_crossing_chase", 34.0), "boost": ("speed_emphasis", 26.0),
        "hazard": ("hazard_follow", 28.0), "launch": ("launch_follow", 42.0),
        "flight_hazard": ("flight_near_miss_follow", 30.0), "goal": ("goal_hold", 52.0),
    }
    if event_type in overrides:
        cue["shot_type"], cue["lens_mm"] = overrides[event_type]
    cue["transition"] = "continuous"
    cue["look_at_world_xyz"] = look_at
    cue["camera_shake"] = "medium" if event_type in {"combat", "dodge", "hazard", "flight_hazard"} else "none"
    cue["horizon_policy"] = "bank_with_actor" if genre == "wingsuit" else "stabilized"
    return cue


def actor_kind(genre):
    return {"fps": "first_person_player", "racing": "vehicle", "wingsuit": "flight_actor"}.get(genre, "third_person_character")


def make_beat(placement, position, event_type, mode, phase_id, intensity,
              base_camera, genre, label=None, jump_height=None):
    anchor = fixed_anchor(placement)
    look_events = {"collect", "collect_required", "combat", "interact", "reveal", "checkpoint",
                   "boost", "hazard", "flight_collectible", "flight_hazard", "goal"}
    look_at = anchor if event_type in look_events else None
    position = vec3(position)
    beat = {
        "beat_id": "", "kind": "authored", "placement_id": placement["placement_id"],
        "asset_id": placement.get("asset_id"), "element_type": placement.get("element_type"),
        "event_type": event_type,
        "label": label or placement.get("label") or ("%s %s" % (event_type, placement["placement_id"])),
        "phase_id": phase_id, "intensity": round(float(intensity), 4),
        "movement_mode": mode, "position": [round(value, 6) for value in position],
        "character_position": [round(value, 6) for value in position],
        "route_position_is_final_actor_target": True,
        "fixed_anchor_world_xyz": [round(value, 6) for value in anchor],
        "actor_offset_from_fixed_anchor": [round(position[i] - anchor[i], 6) for i in range(3)],
        "look_at_world_xyz": [round(value, 6) for value in look_at] if look_at else None,
        "terrain_sample": mode in {"ground", "drive"},
        "speed_multiplier": float(placement.get("speed_multiplier", default_speed_multiplier(genre, event_type))),
        "dwell_seconds": float(placement.get("dwell_seconds", default_dwell_seconds(event_type))),
        "action_cue": ACTION_BY_EVENT.get(event_type, "contextual_movement"),
        "camera_cue": camera_cue(base_camera, genre, event_type, look_at),
        "effect_cues": list(EFFECTS_BY_EVENT.get(event_type, [])), "audio_cue": audio_cue(event_type),
    }
    if genre == "racing" and event_type == "boost":
        beat["post_speed_multiplier"] = 1.8
    if jump_height is not None:
        beat["jump_height_m"] = round(float(jump_height), 6)
    return beat


def choose_bypass(center, forward, clearance, previous_position):
    side = [-forward[1], forward[0], 0.0]
    options = [add3(center, mul3(side, clearance)), add3(center, mul3(side, -clearance))]
    return min(options, key=lambda value: distance3(value, previous_position))


def build_authored_route(placement_data, asset_data, genre="generic", camera_distance=None, camera_height=None):
    elements = placement_elements(placement_data)
    asset_by_placement = placement_asset_map(asset_data)
    enriched = []
    for element in elements:
        item = dict(element)
        asset = asset_by_placement.get(item["placement_id"], {})
        item["asset_id"] = asset.get("asset_id") or item.get("asset_id")
        item["expected_size_meters"] = asset_size(asset)
        enriched.append(item)
    anchors = [fixed_anchor(item) for item in enriched]
    base_camera = camera_defaults(genre, camera_distance, camera_height)
    beats = []
    for index, placement in enumerate(enriched):
        center = anchors[index]
        forward = horizontal_forward(anchors, index)
        mode = movement_mode(genre, placement)
        event = infer_event(placement, mode)
        phase_id, intensity = phase_for(index, len(enriched))
        size = placement["expected_size_meters"]
        radius = max(size[0], size[1]) * 0.5
        previous = beats[-1]["position"] if beats else add3(center, mul3(forward, -5.0))
        if index == 0 and event != "spawn":
            beats.append(make_beat(placement, previous, "spawn", mode, phase_id, intensity,
                                   base_camera, genre, "route start"))
        is_ground_jump = mode == "ground" and (
            event == "jump" or (placement.get("placement_mode") == "above_ground" and event in {"collect", "collect_required"})
        )
        if is_ground_jump:
            approach_distance = max(1.6, radius + 0.8)
            jump_event = "jump_collect" if event in {"collect", "collect_required"} else "jump"
            beats.append(make_beat(placement, add3(center, mul3(forward, -approach_distance)), "move", mode,
                                   phase_id, intensity, base_camera, genre, "approach jump"))
            height = max(float(placement.get("height_offset", 0.0)), size[2] + 0.45, 1.35)
            beats.append(make_beat(placement, center, jump_event, mode, phase_id, intensity,
                                   base_camera, genre, "clear gameplay obstacle", height))
            beats.append(make_beat(placement, add3(center, mul3(forward, approach_distance)), "move", mode,
                                   phase_id, intensity, base_camera, genre, "land and continue"))
        elif event in {"dodge", "hazard"} and mode != "flight":
            clearance = max(1.0, radius + 0.8)
            approach = add3(center, mul3(forward, -max(1.2, radius + 0.5)))
            bypass = choose_bypass(center, forward, clearance, previous)
            exit_position = add3(center, mul3(forward, max(1.2, radius + 0.5)))
            beats.append(make_beat(placement, approach, "move", mode, phase_id, intensity,
                                   base_camera, genre, "approach obstacle"))
            beats.append(make_beat(placement, bypass, event, mode, phase_id, intensity,
                                   base_camera, genre, "clear obstacle"))
            beats.append(make_beat(placement, exit_position, "move", mode, phase_id, intensity,
                                   base_camera, genre, "recover route"))
        elif event in {"interact", "reveal", "combat", "goal"} and mode != "flight":
            standoff = max(0.8, radius + (1.2 if event == "goal" else 0.7))
            beats.append(make_beat(placement, add3(center, mul3(forward, -standoff)), event, mode,
                                   phase_id, intensity, base_camera, genre))
        elif event in {"collect", "collect_required"} and mode != "flight":
            side = [-forward[1], forward[0], 0.0]
            collect_position = add3(center, mul3(side, min(0.45, radius + 0.1)))
            beats.append(make_beat(placement, collect_position, event, mode, phase_id, intensity,
                                   base_camera, genre))
        else:
            beats.append(make_beat(placement, center, event, mode, phase_id, intensity, base_camera, genre))
    for index, beat in enumerate(beats):
        beat["beat_id"] = "authored_%03d" % index
    return beats, enriched, base_camera


def route_tangents(beats, turn_smoothing):
    """Return bounded Hermite tangents, including a finite-radius U-turn."""
    positions = [beat["position"] for beat in beats]
    tangents = []
    for index, position in enumerate(positions):
        if index == 0:
            tangent = mul3(sub3(positions[1], position), turn_smoothing)
        elif index == len(positions) - 1:
            tangent = mul3(sub3(position, positions[-2]), turn_smoothing)
        else:
            incoming = sub3(position, positions[index - 1])
            outgoing = sub3(positions[index + 1], position)
            if beats[index - 1]["movement_mode"] != "flight" and beats[index]["movement_mode"] != "flight":
                incoming[2] = 0.0
            if beats[index]["movement_mode"] != "flight" and beats[index + 1]["movement_mode"] != "flight":
                outgoing[2] = 0.0
            incoming_length = distance3([0.0, 0.0, 0.0], incoming)
            outgoing_length = distance3([0.0, 0.0, 0.0], outgoing)
            if min(incoming_length, outgoing_length) <= 1e-6:
                tangent = [0.0, 0.0, 0.0]
            else:
                incoming_direction = normalize3(incoming)
                outgoing_direction = normalize3(outgoing)
                bisector = add3(incoming_direction, outgoing_direction)
                if distance3([0.0, 0.0, 0.0], bisector) <= 1e-5:
                    # A forward-to-backward route cannot turn at a point.
                    # Give it a deterministic lateral tangent so the Hermite
                    # path creates a compact hairpin instead of an instant flip.
                    tangent_direction = normalize3([-incoming_direction[1], incoming_direction[0], 0.0])
                else:
                    tangent_direction = normalize3(bisector)
                tangent = mul3(tangent_direction, min(incoming_length, outgoing_length) * turn_smoothing)
        tangents.append(tangent)
    return tangents


def hermite_position(a, b, tangent_a, tangent_b, t):
    t2, t3 = t * t, t * t * t
    h00 = 2.0 * t3 - 3.0 * t2 + 1.0
    h10 = t3 - 2.0 * t2 + t
    h01 = -2.0 * t3 + 3.0 * t2
    h11 = t3 - t2
    return [
        h00 * a[axis] + h10 * tangent_a[axis] + h01 * b[axis] + h11 * tangent_b[axis]
        for axis in range(3)
    ]


def densify_route(beats, maximum_step_m=3.0, turn_smoothing=0.35):
    if maximum_step_m <= 0.0:
        raise ValueError("maximum_step_m must be positive")
    if not 0.0 <= turn_smoothing <= 0.5:
        raise ValueError("turn_smoothing must be between 0.0 and 0.5")
    tangents = route_tangents(beats, turn_smoothing)
    dense = [dict(beats[0])]
    for segment_index, target in enumerate(beats[1:]):
        source = beats[segment_index]
        a, b = source["position"], target["position"]
        divisions = max(1, int(math.ceil(distance3(a, b) / maximum_step_m)))
        # Curves can be longer than their endpoint chord. Refine until every
        # emitted segment satisfies the same no-teleport spatial bound.
        while True:
            samples = [a] + [
                hermite_position(a, b, tangents[segment_index], tangents[segment_index + 1], float(step) / divisions)
                for step in range(1, divisions)
            ] + [b]
            if max(distance3(left, right) for left, right in zip(samples, samples[1:])) <= maximum_step_m + 1e-8:
                break
            divisions *= 2
        for step in range(1, divisions):
            t = float(step) / divisions
            mode = "flight" if "flight" in {source["movement_mode"], target["movement_mode"]} else target["movement_mode"]
            event = "flight" if mode == "flight" else "drive" if mode == "drive" else "move"
            position = [round(value, 6) for value in hermite_position(
                a, b, tangents[segment_index], tangents[segment_index + 1], t)]
            source_multiplier = float(source.get("post_speed_multiplier", source.get("speed_multiplier", 1.0)))
            target_multiplier = float(target.get("speed_multiplier", 1.0))
            cue = dict(source.get("camera_cue") or target.get("camera_cue") or {})
            cue["transition"] = "continuous"
            cue["look_at_world_xyz"] = target.get("look_at_world_xyz") or source.get("look_at_world_xyz")
            dense.append({
                "beat_id": "", "kind": "transit", "placement_id": None, "asset_id": None,
                "element_type": None, "event_type": event, "label": "%s transit" % mode,
                "phase_id": source["phase_id"] if t < 0.5 else target["phase_id"],
                "intensity": round(float(source["intensity"]) + (float(target["intensity"]) - float(source["intensity"])) * t, 4),
                "movement_mode": mode, "position": position, "character_position": list(position),
                "route_position_is_final_actor_target": True,
                "fixed_anchor_world_xyz": None, "actor_offset_from_fixed_anchor": None,
                "look_at_world_xyz": cue.get("look_at_world_xyz"), "terrain_sample": mode in {"ground", "drive"},
                "speed_multiplier": round(source_multiplier + (target_multiplier - source_multiplier) * t, 4),
                "dwell_seconds": 0.0, "action_cue": ACTION_BY_EVENT[event], "camera_cue": cue,
                "effect_cues": ["contrail"] if mode == "flight" else ["wheel_dust"] if mode == "drive" else [],
                "audio_cue": "adaptive_movement_mix",
            })
        dense.append(dict(target))
    for index, beat in enumerate(dense):
        beat["beat_id"] = "beat_%03d" % index
    return dense


def assign_timing(beats, fps, speed_by_mode, target_duration_seconds=0.0):
    if fps <= 0:
        raise ValueError("fps must be positive")
    current, total_distance = 1, 0.0
    beats[0]["frame"] = current
    for index in range(1, len(beats)):
        previous, beat = beats[index - 1], beats[index]
        segment = distance3(previous["position"], beat["position"])
        total_distance += segment
        speed = float(speed_by_mode.get(beat["movement_mode"], speed_by_mode["ground"]))
        speed *= max(0.1, float(beat.get("speed_multiplier", 1.0)))
        # Ceil rather than round: a segment may be slower than requested, but
        # never faster than its physical speed budget.
        current += max(1, int(math.ceil(segment / max(speed, 0.1) * fps)))
        current += max(0, int(round(float(previous.get("dwell_seconds", 0.0)) * fps)))
        beat["frame"] = current
    natural_end = current + max(fps * 2, int(round(float(beats[-1].get("dwell_seconds", 0.0)) * fps)))
    requested_end = 1 + int(round(float(target_duration_seconds) * fps)) if target_duration_seconds > 0.0 else natural_end
    # A requested presentation duration may stretch a route. It must never
    # compress a physically timed route and reintroduce teleport-like motion.
    target_end = max(requested_end, natural_end, len(beats) + 1)
    scale = float(target_end - 1) / max(1.0, float(natural_end - 1))
    previous_scaled = 0
    for index, beat in enumerate(beats):
        desired = int(round(1.0 + (float(beat["frame"]) - 1.0) * scale))
        maximum = target_end - (len(beats) - index)
        scaled = min(maximum, max(previous_scaled + 1, desired))
        beat["frame"] = scaled
        previous_scaled = scaled
    return target_end, total_distance, natural_end, scale


def route_forward_for_heading(positions, index):
    """Use a centered tangent and skip coincident event/hold positions."""
    previous = index - 1
    while previous >= 0 and distance3(positions[previous], positions[index]) <= 1e-6:
        previous -= 1
    following = index + 1
    while following < len(positions) and distance3(positions[following], positions[index]) <= 1e-6:
        following += 1
    if previous >= 0 and following < len(positions):
        direction = sub3(positions[following], positions[previous])
    elif following < len(positions):
        direction = sub3(positions[following], positions[index])
    elif previous >= 0:
        direction = sub3(positions[index], positions[previous])
    else:
        direction = [1.0, 0.0, 0.0]
    return normalize3([direction[0], direction[1], 0.0])


def unwrap_degrees(previous, current):
    while current - previous > 180.0:
        current -= 360.0
    while current - previous < -180.0:
        current += 360.0
    return current


def add_headings_and_enforce_turn_rate(beats, fps, maximum_turn_degrees_per_second):
    if maximum_turn_degrees_per_second <= 0.0:
        raise ValueError("maximum_turn_degrees_per_second must be positive")
    positions = [beat["position"] for beat in beats]
    headings = []
    for index in range(len(beats)):
        forward = route_forward_for_heading(positions, index)
        heading = math.degrees(math.atan2(forward[1], forward[0]))
        if headings:
            heading = unwrap_degrees(headings[-1], heading)
        headings.append(heading)
        beats[index]["heading_yaw_degrees"] = round(heading, 6)

    added_frames = 0
    previous_frame = int(beats[0]["frame"])
    for index in range(1, len(beats)):
        turn_degrees = abs(headings[index] - headings[index - 1])
        required_turn_frames = max(1, int(math.ceil(
            turn_degrees / maximum_turn_degrees_per_second * fps)))
        desired = int(beats[index]["frame"]) + added_frames
        minimum = previous_frame + required_turn_frames
        if desired < minimum:
            added_frames += minimum - desired
            desired = minimum
        beats[index]["frame"] = desired
        previous_frame = desired
    return added_frames


def add_legacy_camera_fields(beats, base_camera):
    positions = [beat["position"] for beat in beats]
    for index, beat in enumerate(beats):
        if index == 0:
            forward = normalize3(sub3(positions[min(1, len(beats) - 1)], positions[0]))
        elif index == len(beats) - 1:
            forward = normalize3(sub3(positions[-1], positions[-2]))
        else:
            forward = normalize3(sub3(positions[index + 1], positions[index - 1]))
        position = beat["position"]
        camera_position = add3(sub3(position, mul3(forward, float(base_camera["distance_m"]))),
                               [0.0, 0.0, float(base_camera["height_m"])])
        look_height = 0.0 if beat["movement_mode"] == "flight" else 1.15
        look_at = add3(position, add3(mul3(forward, 2.2), [0.0, 0.0, look_height]))
        beat["character_position"] = list(beat["position"])
        beat["camera_position"] = [round(value, 6) for value in camera_position]
        beat["look_at"] = [round(value, 6) for value in look_at]


def validate_output(beats, source_elements, maximum_step_m, fps, speed_by_mode,
                    maximum_turn_degrees_per_second):
    if not beats:
        raise ValueError("Director path has no beats")
    frames = [int(beat["frame"]) for beat in beats]
    if any(current <= previous for previous, current in zip(frames, frames[1:])):
        raise ValueError("Director beat frames must be strictly increasing")
    maximum_segment = max((distance3(a["position"], b["position"]) for a, b in zip(beats, beats[1:])), default=0.0)
    if maximum_segment > float(maximum_step_m) + 1e-5:
        raise ValueError("Director densification exceeded maximum_step_m")
    maximum_speed = 0.0
    maximum_turn_rate = 0.0
    for previous, current in zip(beats, beats[1:]):
        elapsed = (int(current["frame"]) - int(previous["frame"])) / float(fps)
        observed_speed = distance3(previous["position"], current["position"]) / max(elapsed, 1e-8)
        allowed_speed = float(speed_by_mode.get(current["movement_mode"], speed_by_mode["ground"]))
        allowed_speed *= max(0.1, float(current.get("speed_multiplier", 1.0)))
        if observed_speed > allowed_speed + 1e-5:
            raise ValueError("Director timing exceeds the movement speed budget")
        maximum_speed = max(maximum_speed, observed_speed)
        turn = abs(float(current["heading_yaw_degrees"]) - float(previous["heading_yaw_degrees"]))
        turn_rate = turn / max(elapsed, 1e-8)
        if turn_rate > maximum_turn_degrees_per_second + 1e-5:
            raise ValueError("Director timing exceeds the turn-rate budget")
        maximum_turn_rate = max(maximum_turn_rate, turn_rate)
    anchors = {item["placement_id"]: fixed_anchor(item) for item in source_elements}
    for beat in beats:
        placement_id = beat.get("placement_id")
        if placement_id and beat.get("fixed_anchor_world_xyz") is not None:
            if distance3(anchors[placement_id], beat["fixed_anchor_world_xyz"]) > 1e-6:
                raise ValueError("fixed placement anchors changed while building the Director path")
    return {
        "strictly_increasing_frames": True,
        "maximum_segment_m": round(maximum_segment, 6),
        "maximum_observed_speed_mps": round(maximum_speed, 6),
        "maximum_observed_turn_degrees_per_second": round(maximum_turn_rate, 6),
        "speed_budget_respected": True,
        "turn_rate_budget_respected": True,
        "fixed_anchor_mutation_count": 0,
    }


def build_director(placement_data, asset_data, args):
    authored, source_elements, base_camera = build_authored_route(
        placement_data, asset_data, args.genre, args.camera_distance_m, args.camera_height_m)
    beats = densify_route(authored, args.maximum_step_m, args.turn_smoothing)
    speed_by_mode = {"ground": args.run_speed_mps, "drive": args.drive_speed_mps, "flight": args.flight_speed_mps}
    frame_end, path_length, natural_end, timing_scale = assign_timing(
        beats, args.fps, speed_by_mode, args.target_duration_seconds)
    turn_added_frames = add_headings_and_enforce_turn_rate(
        beats, args.fps, args.maximum_turn_degrees_per_second)
    frame_end += turn_added_frames
    add_legacy_camera_fields(beats, base_camera)
    validation = validate_output(
        beats, source_elements, args.maximum_step_m, args.fps, speed_by_mode,
        args.maximum_turn_degrees_per_second)
    authored_beats = [beat for beat in beats if beat["kind"] == "authored"]
    referenced = list(dict.fromkeys(beat["placement_id"] for beat in authored_beats if beat.get("placement_id")))
    return {
        "schema_version": 3, "status": "generic_director_route_from_fixed_placements",
        "stage": "director_path", "genre": args.genre,
        "source_placement_plan": os.path.abspath(args.placement_plan),
        "source_asset_plan": os.path.abspath(args.asset_plan),
        "placement_policy": "read_only_fixed_anchors_no_asset_repositioning",
        "runtime_contract": {
            "fixed_asset_anchors_are_read_only": True,
            "environment_and_asset_meshes_remain_unchanged": True,
            "ground_and_drive_beats_follow_terrain": True,
            "flight_beats_use_3d_route_positions": True,
            "transit_segments_are_bounded_by_maximum_step": True,
            "corners_use_continuous_hermite_curves": True,
            "movement_speed_and_turn_rate_are_hard_limits": True,
            "requested_duration_may_not_compress_physical_timing": True,
            "camera_cues_may_not_move_assets": True,
        },
        "camera_system": base_camera["shot_type"], "actor_kind": actor_kind(args.genre),
        "fps": args.fps, "frame_start": 1, "frame_end": frame_end,
        "target_duration_seconds": round((frame_end - 1) / float(args.fps), 6),
        "requested_target_duration_seconds": max(0.0, float(args.target_duration_seconds)),
        "requested_target_duration_honored": (
            args.target_duration_seconds <= 0.0
            or abs((frame_end - 1) / float(args.fps) - args.target_duration_seconds) < 1.0 / args.fps
        ),
        "natural_duration_before_director_retime_seconds": round(
            (natural_end + turn_added_frames - 1) / float(args.fps), 6),
        "director_timing_scale": round(timing_scale, 6),
        "estimated_actor_path_length_m": round(path_length, 6),
        "speed_by_mode_mps": speed_by_mode, "maximum_step_m": args.maximum_step_m,
        "path_curve_model": "bounded_cubic_hermite",
        "turn_smoothing": args.turn_smoothing,
        "maximum_turn_degrees_per_second": args.maximum_turn_degrees_per_second,
        "referenced_fixed_placement_ids": referenced, "referenced_fixed_placement_count": len(referenced),
        "authored_beat_count": len(authored_beats), "transit_beat_count": len(beats) - len(authored_beats),
        "camera_track": [
            {"beat_id": beat["beat_id"], "frame": beat["frame"], "placement_id": beat["placement_id"],
             "camera_cue": beat["camera_cue"]} for beat in authored_beats
        ],
        "event_track": [
            {"beat_id": beat["beat_id"], "frame": beat["frame"], "placement_id": beat["placement_id"],
             "event_type": beat["event_type"], "action_cue": beat["action_cue"],
             "effect_cues": beat["effect_cues"], "audio_cue": beat["audio_cue"]}
            for beat in authored_beats
        ],
        "route_validation": validation, "beats": beats,
    }


def main():
    args = parse_args()
    if min(args.run_speed_mps, args.drive_speed_mps, args.flight_speed_mps, args.maximum_step_m) <= 0.0:
        raise ValueError("all route speeds and maximum_step_m must be positive")
    output = build_director(load_json(args.placement_plan), load_json(args.asset_plan), args)
    write_json(args.output, output)
    print("DIRECTOR_PATH", os.path.abspath(args.output))
    print("DIRECTOR_BEATS", len(output["beats"]))
    print("DIRECTOR_PATH_LENGTH_M", output["estimated_actor_path_length_m"])
    print("DIRECTOR_FRAME_RANGE", output["frame_start"], output["frame_end"])


if __name__ == "__main__":
    main()
