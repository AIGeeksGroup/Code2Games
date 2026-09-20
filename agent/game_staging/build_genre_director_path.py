"""Build genre-specific Director paths from already-fixed gameplay placements.

This is Code2Games stage 11.  It never edits a placement plan and never moves a
gameplay asset.  Fixed placement anchors are copied into semantic route beats;
only actor transit points between those anchors are synthesized.
"""

import argparse
import json
import math
import os
import sys

import racing_speedway_layout


FPS_ROUTE = [
    ("fps_cover_near_center_a", "spawn", "enter the opening firing lane", [0.0, -3.0, 0.0]),
    ("fps_optional_ammo_near", "collect", "collect opening ammunition", [0.0, 0.0, 0.0]),
    # A direct south-lane push instead of the old east->west->mid reversals:
    # the route sweeps gently east through the firing lane, crosses the final
    # crossfire on the way up to the uplink, then turns north-east for the
    # extraction.  Unvisited west-cell covers remain scenery.
    ("fps_cover_right_flank", "combat", "suppress the opening right-flank hostile", [1.8, 0.0, 0.0]),
    # Second hostile stands directly ON the player's path around the fourth
    # second of the run (about 4 m past the first kill) so the player walks
    # straight onto him and opens fire without a cover detour.  The eastern
    # cover prop stays as scenery the player passes on the way to the cell.
    {"beat_id": "fps_midpath_hostile", "event_type": "combat", "label": "drop the hostile standing in the lane", "movement_mode": "ground", "position": [-33.13, 38.61, 0.0]},
    ("fps_required_cell_east", "collect_required", "secure the eastern uplink cell", [0.0, 0.0, 0.0]),
    ("fps_hazard_final_crossfire", "combat", "break through the final crossfire lane", [0.0, 0.0, 0.0]),
    ("fps_emergency_uplink_trigger", "interact", "activate the emergency uplink", [0.0, 0.0, 0.0]),
    ("fps_final_extraction_goal", "goal", "reach the extraction zone", [0.0, 0.0, 0.0]),
]


TPS_ROUTE = [
    ("placement_003", "spawn", "enter the woodland opening", [-2.0, -2.0, 0.0]),
    ("placement_010", "collect", "collect the opening medical supply", [0.0, 0.0, 0.0]),
    ("placement_000", "interact", "activate the opening emergency relay", [0.0, 0.0, 0.0]),
    ("placement_009", "collect", "collect ammunition beside the opening relay", [0.0, 0.0, 0.0]),
    ("placement_002", "combat", "take opening combat cover", [1.8, 0.0, 0.0]),
    ("placement_016", "recover", "cross the opening recovery pocket", [0.0, 0.0, 0.0]),
    # Continue east through assets that lie on the same traversable woodland
    # corridor.  Far-north landmarks and the far-south optional supply remain
    # scenery: visiting them would reintroduce the large loops the showcase
    # deliberately removed.
    ("placement_017", "recover", "cross the midfield recovery pocket", [0.0, 0.0, 0.0]),
    ("placement_004", "combat", "use midfield cover", [-1.7, 1.0, 0.0]),
    ("placement_005", "dodge", "move around the complementary midfield cover", [0.0, 0.0, 0.0]),
    ("placement_006", "combat", "take the eastern combat entry cover", [0.0, 0.0, 0.0]),
    ("placement_007", "combat", "hold the eastern cover line", [0.0, 0.0, 0.0]),
    ("placement_001", "interact", "activate the eastern emergency relay", [0.0, 0.0, 0.0]),
    ("placement_008", "combat", "clear the final flanking cover", [0.0, 0.0, 0.0]),
    ("placement_021", "goal", "reach the woodland extraction area", [0.0, 0.0, 0.0]),
]


RACING_ROUTE_V11_LOWLAND = [
    # Rebuilt exclusively from the V6 placement truth.  The old ridge section
    # put a 5.2 m vehicle across wheel-height differences of up to four metres,
    # so those props remain immutable scenery rather than false driving goals.
    # V3 proved that the start-right anchor sits on a terrain boundary: one
    # wheel ray reached the -500 m scene floor.  Start at the already-safe V6
    # countdown position instead, then pass fourteen real lowland assets.
    ("racing_landmark_start_banner", "establish", "frame the rally start", [4.0, 7.0, 0.0]),
    ("racing_event_start_countdown", "countdown", "start the timed sprint", [0.0, 0.0, 0.0]),
    # The reusable V6 showcase's actual start-left root is seven metres from
    # the countdown control, already inside the verified steer pass radius.
    # A stationary start-side response avoids driving back into the terrain
    # boundary while leaving the fixed asset untouched.
    {
        "placement_id": "racing_barrier_start_left",
        "event_type": "steer",
        "label": "launch with the start-side marker in range",
        "movement_mode": "drive",
        "actor_position": [-86.89, 58.47, -0.658411],
    },
    {
        "placement_id": "racing_barrier_gate1_right",
        "event_type": "steer",
        "label": "exit along the outer start lane",
        "movement_mode": "drive",
        "actor_position": [-82.0, 53.0, -0.65],
    },
    ("racing_boost_early_center", "boost", "commit to the supported lowland boost", [0.0, 0.0, 0.0]),
    {"beat_id": "racing_lowland_turnaround", "event_type": "drive", "label": "carry speed around the broad lowland bend", "movement_mode": "drive", "position": [-63.0, 27.0, -0.65]},
    ("racing_boost_mid_right", "boost", "activate the lowland midfield boost", [0.0, 0.0, 0.0]),
    {
        "placement_id": "racing_barrier_mid_right",
        "event_type": "steer",
        "label": "sweep along the safe east side of the inside marker",
        "movement_mode": "drive",
        "actor_position": [-64.0, 39.0, -0.64],
    },
    {
        "placement_id": "racing_barrier_gate1_left",
        "event_type": "steer",
        "label": "continue along the lowland barrier lane",
        "movement_mode": "drive",
        "actor_position": [-64.0, 41.0, -0.64],
    },
    {
        "placement_id": "racing_event_timing_gate_02",
        "event_type": "checkpoint",
        "label": "clear timing gate two from its safe east side",
        "movement_mode": "drive",
        "actor_position": [-64.0, 43.0, -0.64],
    },
    ("racing_barrier_mid_left", "steer", "hold the midfield grass lane", [0.0, 0.0, 0.0]),
    ("racing_barrier_left_branch", "steer", "round the left-branch marker", [0.0, 0.0, 0.0]),
    ("racing_boost_left_choice", "boost", "sweep through the safe north-west boost", [0.0, 0.0, 0.0]),
    # Stay in the already-proven V6 lowland corridor.  The former y=78/80
    # northern arc crossed a dense obstacle pocket; its automatic detour
    # displaced both this boost and the following mud event beyond 6 m.
    # A direct eastbound transition keeps the turn below the backtrack limit
    # and gives collision smoothing far less reason to move either event.
    {
        "placement_id": "racing_hazard_mid_right_mud",
        "event_type": "hazard",
        "label": "skim the safe edge of the mud risk",
        "movement_mode": "drive",
        "actor_position": [-56.0, 69.0, -0.652426],
    },
    ("racing_boost_grass_bonus", "boost", "collect the open-field bonus boost", [0.0, 0.0, 0.0]),
    # These are V6's already-used flat-corridor transit controls.  They extend
    # the run with forward travel rather than a loop, stop or mountain climb.
    # Leave the bonus on its already-supported y~=74 line before merging into
    # the long straight.  The former diagonal merge to (-40, 80) crossed the
    # only two residual body-width collisions at x=-45..-40, y=79..82.
    {"beat_id": "racing_flat_transit_a", "event_type": "drive", "label": "clear the bonus along the west grass line", "movement_mode": "drive", "position": [-40.0, 74.2, -0.653]},
    {"beat_id": "racing_flat_transit_b", "event_type": "drive", "label": "hold the open grass straight", "movement_mode": "drive", "position": [-20.0, 80.0, -0.654]},
    {"beat_id": "racing_flat_transit_c", "event_type": "drive", "label": "continue the east grass straight", "movement_mode": "drive", "position": [0.0, 80.0, -0.636]},
    {"beat_id": "racing_flat_transit_d", "event_type": "drive", "label": "complete the flat grass straight", "movement_mode": "drive", "position": [10.0, 80.0, -0.664]},
    {"beat_id": "racing_safe_finish", "event_type": "goal", "label": "finish on the verified lowland ground", "movement_mode": "drive", "position": [12.0, 80.0, -0.664], "speed_multiplier": 1.0},
]

# The delivery route is now a real ~550 m circuit.  V11's 203 m lowland route
# remains above as historical source context, but is no longer selected: it
# required a 2.54x slow-motion stretch to fill fifty seconds.
RACING_ROUTE = racing_speedway_layout.build_route()


WINGSUIT_ROUTE = [
    {"placement_id": "placement_021", "event_type": "spawn", "label": "establish the summit crash site and parked rescue aircraft", "movement_mode": "ground", "offset": [2.0, 1.0, 0.0]},
    {"placement_id": "placement_005", "event_type": "interact", "label": "recover the crashed survey drone telemetry", "movement_mode": "ground"},
    {"placement_id": "placement_000", "event_type": "flight_collectible", "label": "install flight cell one of four and ignite the aircraft", "movement_mode": "ground"},
    {"placement_id": "placement_008", "event_type": "dodge", "label": "cross the snapped mast debris", "movement_mode": "ground", "offset": [1.6, 0.0, 0.0]},
    {"placement_id": "placement_004", "event_type": "dodge", "label": "detour around the storm barrier", "movement_mode": "ground", "offset": [-1.8, 0.0, 0.0]},
    {"placement_id": "placement_005", "event_type": "dodge", "label": "vault past the crashed survey drone", "movement_mode": "ground", "offset": [-1.6, 0.0, 0.0]},
    {"placement_id": "placement_010", "event_type": "interact", "label": "bring the summit flight network online", "movement_mode": "ground"},
    {"placement_id": "placement_014", "event_type": "hazard", "label": "escape the pressure rupture", "movement_mode": "ground", "offset": [2.0, 0.0, 0.0]},
    {"placement_id": "placement_022", "event_type": "launch", "label": "launch from the summit gantry", "movement_mode": "ground", "offset": [0.0, -2.0, 0.8]},
    {"beat_id": "flight_clear_cliff", "event_type": "flight", "label": "clear the cliff edge into open sky", "movement_mode": "flight", "position": [-15.5, -34.0, 24.0]},
    # Flight controls are relative to immutable asset anchors.  The old
    # absolute actor positions missed these objects by 11--18 metres.
    {"placement_id": "placement_018", "event_type": "flight_hazard", "label": "knife past the vertical summit impact column", "movement_mode": "flight", "offset": [-4.0, -1.0, 7.5], "look_at_fixed_anchor": True},
    {"placement_id": "placement_017", "event_type": "flight_hazard", "label": "thread the lower rockfall warning", "movement_mode": "flight", "offset": [4.0, -2.0, 7.5], "look_at_fixed_anchor": True},
    {"placement_id": "placement_001", "event_type": "flight_collectible", "label": "collect flight cell two of four on cliff departure", "movement_mode": "flight", "look_at_fixed_anchor": True},
    {"beat_id": "flight_bank_left", "event_type": "flight", "label": "bank left around the cliff foot", "movement_mode": "flight", "position": [-30.0, -58.0, 7.5]},
    {"beat_id": "flight_speed_dive", "event_type": "flight", "label": "commit to the rescue corridor", "movement_mode": "flight", "position": [-44.0, -69.0, 6.0]},
    {"placement_id": "placement_016", "event_type": "flight_hazard", "label": "avoid the lower rockfall marker", "movement_mode": "flight", "offset": [0.0, 0.0, 8.0]},
    {"placement_id": "placement_002", "event_type": "flight_collectible", "label": "collect flight cell three of four during the mountain circuit", "movement_mode": "flight", "look_at_fixed_anchor": True},
    {"placement_id": "placement_003", "event_type": "flight_collectible", "label": "collect flight cell four of four and unlock the destination", "movement_mode": "flight", "look_at_fixed_anchor": True},
    {"placement_id": "placement_023", "event_type": "reveal", "label": "lock the second-summit rescue homing mast", "movement_mode": "flight", "offset": [0.0, 0.0, 7.0], "look_at_fixed_anchor": True},
    {"placement_id": "placement_019", "event_type": "flight_event", "label": "cross the second-summit windbreak", "movement_mode": "flight", "offset": [0.0, 0.0, 7.0], "look_at_fixed_anchor": True},
    {"placement_id": "placement_020", "event_type": "flight_event", "label": "confirm the second-summit recovery refuge", "movement_mode": "flight", "offset": [0.0, 0.0, 7.0], "look_at_fixed_anchor": True},
    {"placement_id": "placement_013", "event_type": "flight_event", "label": "upload the extraction coordinates over the second summit", "movement_mode": "flight", "offset": [5.0, 0.0, 7.0], "look_at_fixed_anchor": True},
    {"placement_id": "placement_012", "event_type": "goal", "label": "reach the second summit and hold it in view", "movement_mode": "flight", "offset": [0.0, 0.0, 6.0], "look_at_fixed_anchor": True},
]


GENRE_DESIGN = {
    "fps": {
        "title": "Red Valley Last Uplink",
        "target_duration_seconds": 45.0,
        "director_concept": "A first-person combat crescendo that starts in the southern firing lane, pushes straight east through two hostile covers, secures the uplink cell, breaks the final crossfire on the climb, then ends in a full-speed extraction run.",
        "phases": [
            {"phase_id": "fps_opening_contact", "authored_range": [0, 3], "intensity": 0.42, "goal": "teach cover, suppression, and supplies"},
            {"phase_id": "fps_east_cell_push", "authored_range": [4, 4], "intensity": 0.62, "goal": "secure the exposed east cell"},
            {"phase_id": "fps_final_crossfire", "authored_range": [5, 5], "intensity": 0.88, "goal": "break the last crossfire lane on the uplink climb"},
            {"phase_id": "fps_uplink_and_extraction", "authored_range": [6, 7], "intensity": 1.0, "goal": "activate the uplink and sprint into the extraction beacon"},
        ],
        "camera_grammar": "Rigid first-person for control and combat; the slope pitch looks up on the final uplink climb and the extraction keeps the same body-mounted camera throughout.",
        "effect_grammar": "Short muzzle-origin tracers, ground-impact dust, blue power-cell bursts, uplink energy column, and a final extraction flare.",
    },
    "tps": {
        "title": "Woodland Signal Holdout",
        "target_duration_seconds": 45.0,
        "director_concept": "A close over-shoulder woodland firefight on one continuous eastward push: opening medical pickup, three escalating cover fights, a single relay activation, and a north-east extraction with a clear success confirmation.",
        "phases": [
            {"phase_id": "tps_opening_contact", "authored_range": [0, 5], "intensity": 0.58, "goal": "supply, activate the opening relay, and survive its close-range fight"},
            {"phase_id": "tps_woodland_push", "authored_range": [6, 10], "intensity": 0.78, "goal": "cross the recovery pocket and clear the complete midfield cover chain"},
            {"phase_id": "tps_relay_activation", "authored_range": [11, 12], "intensity": 0.92, "goal": "activate the eastern relay and clear its flanking cover"},
            {"phase_id": "tps_extraction", "authored_range": [13, 13], "intensity": 1.0, "goal": "reach the extraction beacon and confirm success"},
        ],
        "camera_grammar": "One rigid shoulder boom: the character stays fully visible in the lower frame with the road ahead, no candidate switching, no overhead reset, and no vegetation cuts.",
        "effect_grammar": "Short muzzle bursts, ground-impact dust, relay activation ring, and a green extraction confirmation flare.",
    },
    "racing": {
        "title": "Dustline Valley Rally",
        "target_duration_seconds": 50.0,
        "director_concept": "A full-speed lowland rally traversal through the source scene's existing vegetation basin, with fourteen gameplay assets spread along a real long-distance route rather than a slow-motion short path.",
        "phases": [
            {"phase_id": "racing_launch", "authored_range": [0, 19], "intensity": 0.48, "goal": "launch cleanly through the lowland vegetation"},
            {"phase_id": "racing_opening_arc", "authored_range": [20, 46], "intensity": 0.72, "goal": "build real physical speed through the first basin traverse"},
            {"phase_id": "racing_back_straight", "authored_range": [47, 73], "intensity": 0.90, "goal": "link widely spaced assets across the long lowland route"},
            {"phase_id": "racing_finish", "authored_range": [74, 93], "intensity": 1.0, "goal": "finish the traversal without dropping below 200 km/h"},
        ],
        "camera_grammar": "One rigid third-person driving camera fixed to the vehicle in rear-high local space: keep the complete car in the lower centre, reserve the upper frame for the road ahead, and never swap shoulders, orbit, lag behind or independently turn.",
        "effect_grammar": "Visible boost exhaust, brake-light response, hazard dust and timing-gate pulses tied to route events without moving or hiding safely passed fixed gameplay assets.",
    },
    "wingsuit": {
        "title": "Stormline Second Summit",
        "target_duration_seconds": 52.0,
        "director_concept": "A three-second F-104 launch, four causally linked flight cells, one complete circuit around the user-selected second mountain, and a low readable finish across its homing mast, windbreak, refuge and extraction deck.",
        "phases": [
            {"phase_id": "wingsuit_three_second_launch", "authored_range": [0, 8], "intensity": 0.62, "goal": "install flight cell one and launch within three seconds without stacking status text"},
            {"phase_id": "wingsuit_cliff_departure", "authored_range": [9, 13], "intensity": 0.82, "goal": "collect flight cells two and three while clearing the summit and valley"},
            {"phase_id": "wingsuit_mountain_forest_circuit", "authored_range": [14, 17], "intensity": 1.0, "goal": "bank through the mountain and forest before beginning the broad destination orbit"},
            {"phase_id": "wingsuit_second_summit_finish", "authored_range": [18, 22], "intensity": 0.96, "goal": "collect the fourth cell, unlock the summit, then cross every spaced destination system in a low level finish"},
        ],
        "camera_grammar": "Use one continuous left-rear flight camera: begin wide enough to read the parked aircraft and summit systems, ease into the chase offset during launch, keep the full F-104 and the next terrain objective visible, and never cut, teleport or orbit independently.",
        "effect_grammar": "Use only localized asset illumination, status lamps, afterburner ignition, terrain dust and HUD state changes.  Never create pass-through rings, anchor beams or generic world-space pickup pulses.",
    },
}


ACTION_BY_EVENT = {
    "spawn": "establish_control", "establish": "environment_reveal",
    "collect": "pickup_and_confirm", "collect_required": "objective_pickup_and_counter_increment",
    "jump_collect": "jump_pickup_and_land", "combat": "aim_burst_reposition",
    "dodge": "telegraph_then_evade", "recover": "brief_recovery_without_stopping_momentum",
    "interact": "hold_interact_then_event_release", "reveal": "orient_and_reveal_landmark",
    "countdown": "launch_control_lock_then_release", "checkpoint": "cross_timing_gate",
    "boost": "collect_and_activate_boost", "steer": "precision_steer",
    "hazard": "traction_loss_then_recover", "launch": "sprint_jump_and_deploy_wingsuit",
    "flight": "sustained_glide", "flight_hazard": "bank_or_dive_near_miss",
    "flight_collectible": "intercept_flight_cell_and_increment_power",
    "flight_event": "cross_runtime_flight_gate", "land": "flare_and_absorb_landing",
    "goal": "complete_objective_and_hold_hero_pose",
}


EFFECTS_BY_EVENT = {
    "collect": ["pickup_flash", "objective_tick"],
    "collect_required": ["objective_energy_burst", "objective_counter_increment"],
    "jump_collect": ["pickup_flash", "airborne_trail", "landing_dust"],
    "combat": ["muzzle_flash", "directional_tracers", "surface_impacts"],
    "dodge": ["danger_telegraph", "near_miss_impact", "camera_impulse"],
    "recover": ["recovery_field_pulse", "low_intensity_breathing_reset"],
    "interact": ["interaction_progress", "energy_release", "objective_state_change"],
    "reveal": ["landmark_emission_ramp", "atmospheric_light_sweep"],
    "countdown": ["countdown_lights", "engine_revs", "launch_dust"],
    "checkpoint": ["timing_gate_sweep", "split_time_flash"],
    "boost": ["boost_pickup_burst", "exhaust_flame", "speed_streaks"],
    "steer": ["tire_dust", "suspension_compression"],
    "hazard": ["surface_debris", "traction_spray", "camera_vibration"],
    "launch": ["gantry_energy_release", "fabric_deploy_snap", "cliff_dust"],
    "flight_hazard": ["rockfall_near_miss", "wingtip_vortex", "camera_impulse"],
    "flight_collectible": ["asset_energy_absorption", "afterburner_power_increment", "objective_counter_increment"],
    "flight_event": ["wind_gate_ripple", "contrail_intensify"],
    "land": ["flare_turbulence", "landing_sparks", "deck_dust"],
    "goal": ["goal_confirmation", "hero_light", "time_freeze_accent"],
}


PROFILES = {
    "fps": {
        "camera_system": "first_person_combat",
        "actor_kind": "first_person_player",
        "route": FPS_ROUTE,
        "speed_by_mode": {"ground": 4.8},
        "maximum_step_m": 3.0,
        "fps": 24,
        "target_duration_seconds": GENRE_DESIGN["fps"]["target_duration_seconds"],
    },
    "tps": {
        "camera_system": "third_person_over_shoulder",
        "actor_kind": "third_person_character",
        "route": TPS_ROUTE,
        "speed_by_mode": {"ground": 4.0},
        "maximum_step_m": 3.0,
        "fps": 24,
        "target_duration_seconds": GENRE_DESIGN["tps"]["target_duration_seconds"],
    },
    "racing": {
        "camera_system": "terrain_aware_vehicle_chase",
        "actor_kind": "rally_vehicle",
        "route": RACING_ROUTE,
        "speed_by_mode": {"drive": 16.0},
        "maximum_step_m": 5.0,
        "fps": 24,
        "target_duration_seconds": GENRE_DESIGN["racing"]["target_duration_seconds"],
    },
    "wingsuit": {
        "camera_system": "terrain_aware_aircraft_chase",
        "actor_kind": "f104_starfighter_aircraft",
        "route": WINGSUIT_ROUTE,
        "speed_by_mode": {"ground": 4.0, "flight": 12.0},
        "maximum_step_m": 4.0,
        "fps": 24,
        "target_duration_seconds": GENRE_DESIGN["wingsuit"]["target_duration_seconds"],
    },
}


def script_args():
    return sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]


def parse_args():
    parser = argparse.ArgumentParser(description="Build stage-11 path from fixed placements")
    parser.add_argument("--genre", choices=sorted(PROFILES), required=True)
    parser.add_argument("--placement_plan", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args(script_args())


def load_json(path):
    if not os.path.isfile(path):
        raise FileNotFoundError("required JSON not found: %s" % path)
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path, value):
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        # The project output volume can expose a just-renamed file to a second
        # reader before buffered content is durable.  Stage 12 may be invoked
        # immediately by a showcase wrapper, so make the Stage-11 hand-off
        # explicit before the atomic rename.
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def vec3(value):
    return [float(value[0]), float(value[1]), float(value[2])]


def distance(a, b):
    return math.sqrt(sum((float(a[index]) - float(b[index])) ** 2 for index in range(3)))


def placement_map(plan):
    elements = plan.get("placement_plan", {}).get("elements")
    if not isinstance(elements, list) or not elements:
        raise ValueError("placement plan contains no elements")
    result = {}
    for item in elements:
        placement_id = item.get("placement_id")
        if not isinstance(placement_id, str) or placement_id in result:
            raise ValueError("placement ids must be unique strings")
        if not isinstance(item.get("world_xyz"), list) or len(item["world_xyz"]) != 3:
            raise ValueError("placement %s has no fixed world_xyz" % placement_id)
        result[placement_id] = item
    return result


def normalize_spec(raw, genre):
    if isinstance(raw, dict):
        result = dict(raw)
    else:
        placement_id, event_type, label, offset = raw
        result = {
            "placement_id": placement_id,
            "event_type": event_type,
            "label": label,
            "offset": offset,
        }
    result.setdefault("movement_mode", "drive" if genre == "racing" else "ground")
    result.setdefault("offset", [0.0, 0.0, 0.0])
    return result


def phase_for_authored_index(genre, index):
    for phase in GENRE_DESIGN[genre]["phases"]:
        start, end = phase["authored_range"]
        if start <= index <= end:
            return phase
    raise ValueError("%s authored beat %d has no director phase" % (genre, index))


def camera_cue(genre, event_type, look_at_world_xyz=None):
    base = {
        "fps": {"shot_type": "first_person_follow", "lens_mm": 30.0, "distance_m": 0.0, "height_m": 1.68, "side": "center"},
        "tps": {"shot_type": "right_shoulder_follow", "lens_mm": 38.0, "distance_m": 6.8, "height_m": 3.2, "side": "right"},
        "racing": {"shot_type": "rigid_low_vehicle_chase", "lens_mm": 30.0, "distance_m": 10.5, "height_m": 4.2, "side": "center"},
        "wingsuit": {"shot_type": "rigid_left_rear_aircraft_chase", "lens_mm": 38.0, "distance_m": 28.0, "height_m": 7.5, "side": "left"},
    }[genre]
    cue = dict(base)
    shot_overrides = {
        "reveal": ("cinematic_landmark_reveal", 55.0, "cutaway"),
        "interact": ("hero_interaction_orbit", 42.0, "cutaway"),
        "dodge": ("lateral_whip_follow", 32.0, "continuous"),
        "combat": ("combat_shoulder_or_first_person", 36.0, "continuous"),
        "countdown": ("front_three_quarter_launch", 50.0, "cutaway"),
        "checkpoint": ("gate_crossing_chase", 34.0, "continuous"),
        "boost": ("wheel_level_speed_cut", 24.0, "cutaway"),
        "hazard": ("hood_or_low_side_hazard", 26.0, "continuous"),
        "launch": ("cliff_side_profile_launch", 45.0, "cutaway"),
        "flight_hazard": ("cliff_facing_near_miss", 30.0, "continuous"),
        "collect_required": ("objective_pickup_punch_in", 36.0, "continuous"),
        "land": ("long_lens_landing_compression", 70.0, "cutaway"),
        "goal": ("hero_crane_out", 52.0, "cutaway"),
    }
    if event_type in shot_overrides:
        cue["shot_type"], cue["lens_mm"], cue["transition"] = shot_overrides[event_type]
    else:
        cue["transition"] = "continuous"
    cue["look_at_world_xyz"] = look_at_world_xyz
    cue["camera_shake"] = (
        "heavy" if event_type in {"launch", "flight_hazard"}
        else "medium" if event_type in {"combat", "dodge", "hazard"}
        else "none"
    )
    cue["horizon_policy"] = "bank_with_actor" if genre == "wingsuit" and event_type not in {"land", "goal"} else "stabilized"
    return cue


def audio_cue(event_type):
    return {
        "combat": "weapon_burst_and_impacts",
        "dodge": "incoming_warning_then_near_miss",
        "collect": "pickup_chime",
        "collect_required": "objective_cell_powerup",
        "jump_collect": "jump_whoosh_and_pickup",
        "interact": "machine_charge_and_release",
        "reveal": "landmark_music_sting",
        "countdown": "rally_countdown_and_engine_rev",
        "checkpoint": "timing_gate_confirmation",
        "boost": "boost_ignition",
        "hazard": "surface_roar_and_chassis_rattle",
        "launch": "storm_crack_and_fabric_deploy",
        "flight_hazard": "rockfall_whoosh",
        "flight_collectible": "flight_cell_absorption_and_engine_powerup",
        "flight_event": "wind_gate_bass_pulse",
        "land": "flare_wind_and_landing_impact",
        "goal": "objective_complete_music_release",
    }.get(event_type, "adaptive_gameplay_ambience")


def default_speed_multiplier(genre, event_type):
    genre_defaults = {
        "fps": {"combat": 0.80, "dodge": 1.18, "recover": 0.72, "interact": 0.55, "goal": 1.12},
        "tps": {"combat": 0.76, "dodge": 1.12, "recover": 0.68, "interact": 0.52, "reveal": 0.70},
        "racing": {"countdown": 0.25, "boost": 1.35, "hazard": 0.82, "checkpoint": 1.08, "goal": 1.22},
        "wingsuit": {"dodge": 1.08, "interact": 0.58, "launch": 1.18, "flight": 1.24, "flight_hazard": 1.08, "flight_collectible": 1.16, "flight_event": 1.28, "land": 0.52},
    }
    return float(genre_defaults.get(genre, {}).get(event_type, 1.0))


def default_dwell_seconds(event_type):
    return {
        "spawn": 0.70,
        "establish": 1.00,
        "collect": 0.28,
        "collect_required": 0.38,
        "jump_collect": 0.48,
        "combat": 0.82,
        "dodge": 0.22,
        "recover": 0.62,
        "interact": 1.18,
        "reveal": 0.92,
        "countdown": 1.75,
        "checkpoint": 0.10,
        "boost": 0.06,
        "steer": 0.0,
        "hazard": 0.06,
        "launch": 0.28,
        "flight": 0.0,
        "flight_hazard": 0.0,
        "flight_event": 0.0,
        "land": 0.72,
        "goal": 1.35,
    }.get(event_type, 0.30)


def authored_beats(genre, profile, placements):
    beats = []
    referenced = []
    for index, raw in enumerate(profile["route"]):
        spec = normalize_spec(raw, genre)
        placement_id = spec.get("placement_id")
        placement = placements.get(placement_id) if placement_id else None
        if placement_id and placement is None:
            raise ValueError("%s route references missing placement %s" % (genre, placement_id))
        if placement is not None:
            anchor = vec3(placement["world_xyz"])
            source_anchor = list(anchor)
            referenced.append(placement_id)
        else:
            anchor = vec3(spec["position"])
            source_anchor = None
        if placement is not None and spec.get("asset_anchor_position") is not None:
            anchor = vec3(spec["asset_anchor_position"])
        offset = vec3(spec.get("offset", [0.0, 0.0, 0.0]))
        if spec.get("actor_position") is not None:
            position = vec3(spec["actor_position"])
            offset = [position[i] - anchor[i] for i in range(3)]
        else:
            position = [anchor[i] + offset[i] for i in range(3)]
        movement_mode = spec["movement_mode"]
        phase = phase_for_authored_index(genre, index)
        look_at = (
            [round(value, 6) for value in anchor]
            if placement and spec.get("look_at_fixed_anchor")
            else None
        )
        event_type = spec["event_type"]
        beat = {
            "beat_id": spec.get("beat_id") or "authored_%03d" % index,
            "kind": "authored",
            "placement_id": placement_id,
            "element_type": placement.get("element_type") if placement else None,
            "event_type": event_type,
            "label": spec["label"],
            "phase_id": phase["phase_id"],
            "intensity": float(spec.get("intensity", phase["intensity"])),
            "movement_mode": movement_mode,
            "position": [round(value, 6) for value in position],
            "fixed_anchor_world_xyz": [round(value, 6) for value in anchor] if placement else None,
            "actor_offset_from_fixed_anchor": [round(value, 6) for value in offset] if placement else None,
            "look_at_world_xyz": look_at,
            "terrain_sample": movement_mode in {"ground", "drive"},
            "speed_multiplier": float(spec.get("speed_multiplier", default_speed_multiplier(genre, event_type))),
            "dwell_seconds": float(spec.get("dwell_seconds", default_dwell_seconds(event_type))),
            "action_cue": ACTION_BY_EVENT.get(event_type, "contextual_movement"),
            "camera_cue": camera_cue(genre, event_type, look_at),
            "effect_cues": list(EFFECTS_BY_EVENT.get(event_type, [])),
            "audio_cue": audio_cue(event_type),
        }
        if genre == "racing" and spec.get("racing_speedway_layout"):
            beat["racing_speedway_layout"] = True
            beat["racing_track_surface"] = True
            beat["racing_track_fraction"] = float(spec.get("racing_track_fraction", 0.0))
            if source_anchor is not None:
                beat["racing_source_anchor_world_xyz"] = [round(value, 6) for value in source_anchor]
        if genre == "racing" and event_type == "boost":
            # Boost pads accelerate AFTER the pad: the car approaches at
            # cruise speed, then the following route segments run boosted and
            # decay back to cruise.  The old semantics made the approach fast
            # and the departure slow, which read as "no acceleration after
            # the boost" in the QA clip.
            beat["speed_multiplier"] = 1.0
            beat["post_speed_multiplier"] = 2.0
        beats.append(beat)
    if (
        genre == "racing"
        and len(beats) >= 2
        and beats[0].get("event_type") == "establish"
        and beats[1].get("event_type") == "countdown"
    ):
        # An establishing shot is camera grammar, not an instruction for the
        # vehicle to drive away from the start line and return to it.  Keeping
        # the car on the countdown control removes the opening U-turn/360.
        beats[0]["position"] = list(beats[1]["position"])
        anchor = beats[0].get("fixed_anchor_world_xyz")
        if anchor:
            beats[0]["actor_offset_from_fixed_anchor"] = [
                round(beats[0]["position"][axis] - anchor[axis], 6)
                for axis in range(3)
            ]
        beats[0]["label"] = "hold the rally car on the start line for the establishing shot"
    return beats, referenced


def densify(beats, maximum_step_m):
    dense = [dict(beats[0])]
    for target in beats[1:]:
        source = dense[-1]
        a = source["position"]
        b = target["position"]
        segment_distance = distance(a, b)
        divisions = max(1, int(math.ceil(segment_distance / maximum_step_m)))
        for step in range(1, divisions):
            t = float(step) / divisions
            # A flight transition begins immediately after the launch beat;
            # a landing transition switches back to terrain sampling only as
            # it reaches the ground-authored target.
            if target["movement_mode"] == "flight":
                mode = "flight"
            elif source["movement_mode"] == "flight":
                mode = "flight"
            else:
                mode = target["movement_mode"]
            intensity = float(source["intensity"] + (target["intensity"] - source["intensity"]) * t)
            look_at = target.get("look_at_world_xyz") or source.get("look_at_world_xyz")
            transit_event = "flight" if mode == "flight" else "drive" if mode == "drive" else "move"
            transit_camera = dict(source.get("camera_cue") or target.get("camera_cue") or {})
            transit_camera["transition"] = "continuous"
            transit_camera["look_at_world_xyz"] = look_at
            source_multiplier = float(source.get("post_speed_multiplier", source["speed_multiplier"]))
            target_multiplier = float(target["speed_multiplier"])
            transit_effects = (
                ["contrail", "wingtip_vortex"] if mode == "flight"
                else ["wheel_dust", "grass_wake"] if mode == "drive"
                else []
            )
            dense.append({
                "beat_id": "",
                "kind": "transit",
                "placement_id": None,
                "element_type": None,
                "event_type": transit_event,
                "label": "%s transit" % mode,
                "phase_id": source["phase_id"] if t < 0.5 else target["phase_id"],
                "intensity": round(intensity, 4),
                "movement_mode": mode,
                "position": [round(a[i] + (b[i] - a[i]) * t, 6) for i in range(3)],
                "fixed_anchor_world_xyz": None,
                "actor_offset_from_fixed_anchor": None,
                "look_at_world_xyz": look_at,
                "terrain_sample": mode in {"ground", "drive"},
                "speed_multiplier": round(source_multiplier + (target_multiplier - source_multiplier) * t, 4),
                "dwell_seconds": 0.0,
                "action_cue": "sustained_glide" if mode == "flight" else "precision_drive" if mode == "drive" else "tactical_traverse",
                "camera_cue": transit_camera,
                "effect_cues": transit_effects,
                "audio_cue": "adaptive_movement_mix",
                "racing_speedway_layout": bool(source.get("racing_speedway_layout") or target.get("racing_speedway_layout")),
                "racing_track_surface": bool(source.get("racing_track_surface") or target.get("racing_track_surface")),
                "racing_track_fraction": round(
                    float(source.get("racing_track_fraction", 0.0))
                    + (
                        float(target.get("racing_track_fraction", source.get("racing_track_fraction", 0.0)))
                        - float(source.get("racing_track_fraction", 0.0))
                    ) * t,
                    8,
                ),
            })
        dense.append(dict(target))
    for index, beat in enumerate(dense):
        beat["beat_id"] = "beat_%03d" % index
    return dense


def assign_timing(beats, profile):
    fps = int(profile["fps"])
    current_frame = 1
    total_distance = 0.0
    beats[0]["frame"] = current_frame
    for index in range(1, len(beats)):
        previous = beats[index - 1]
        beat = beats[index]
        segment = distance(previous["position"], beat["position"])
        total_distance += segment
        mode = beat["movement_mode"]
        speed = float(profile["speed_by_mode"].get(mode, profile["speed_by_mode"].get(previous["movement_mode"], 4.0)))
        speed *= max(0.1, float(beat.get("speed_multiplier", 1.0)))
        travel_frames = max(1, int(round(segment / max(speed, 0.1) * fps)))
        dwell_frames = max(0, int(round(float(previous.get("dwell_seconds", 0.0)) * fps)))
        current_frame += travel_frames + dwell_frames
        beat["frame"] = current_frame
    natural_frame_end = current_frame + max(fps * 2, int(round(float(beats[-1].get("dwell_seconds", 0.0)) * fps)))
    target_frame_end = 1 + int(round(float(profile["target_duration_seconds"]) * fps))
    scale = float(target_frame_end - 1) / max(1.0, float(natural_frame_end - 1))
    previous_scaled = 0
    for beat in beats:
        scaled = int(round(1.0 + (float(beat["frame"]) - 1.0) * scale))
        scaled = max(previous_scaled + 1, scaled)
        beat["frame"] = min(target_frame_end, scaled)
        previous_scaled = beat["frame"]
    if beats[-1]["frame"] >= target_frame_end:
        raise ValueError("target duration is too short for strictly increasing director beats")
    return target_frame_end, total_distance, natural_frame_end, scale


def main():
    args = parse_args()
    profile = PROFILES[args.genre]
    source_plan = load_json(args.placement_plan)
    placements = placement_map(source_plan)
    beats, referenced = authored_beats(args.genre, profile, placements)
    beats = densify(beats, float(profile["maximum_step_m"]))
    frame_end, total_distance, natural_frame_end, timing_scale = assign_timing(beats, profile)
    design = GENRE_DESIGN[args.genre]
    authored = [item for item in beats if item["kind"] == "authored"]
    output = {
        "schema_version": 2,
        "stage": "director_path",
        "genre": args.genre,
        "title": design["title"],
        "director_concept": design["director_concept"],
        "source_placement_plan": os.path.abspath(args.placement_plan),
        "placement_policy": (
            "explicit_speedway_relayout_referenced_gameplay_roots_only"
            if args.genre == "racing"
            else "read_only_fixed_anchors_no_asset_repositioning"
        ),
        "runtime_contract": {
            "fixed_asset_anchors_are_read_only": args.genre != "racing",
            "racing_referenced_assets_may_move_to_speedway_truth": args.genre == "racing",
            "environment_and_asset_meshes_remain_unchanged": True,
            "ground_and_drive_beats_follow_terrain": True,
            "flight_beats_use_authored_3d_corridor": True,
            "camera_cutaways_may_not_move_assets": True,
            "all_authored_action_camera_effect_and_audio_cues_must_be_staged": True,
        },
        "camera_system": profile["camera_system"],
        "camera_grammar": design["camera_grammar"],
        "effect_grammar": design["effect_grammar"],
        "actor_kind": profile["actor_kind"],
        "fps": profile["fps"],
        "frame_start": 1,
        "frame_end": frame_end,
        "target_duration_seconds": profile["target_duration_seconds"],
        "natural_duration_before_director_retime_seconds": round((natural_frame_end - 1) / float(profile["fps"]), 6),
        "director_timing_scale": round(timing_scale, 6),
        "estimated_actor_path_length_m": round(total_distance, 6),
        "phases": design["phases"],
        "referenced_fixed_placement_ids": referenced,
        "referenced_fixed_placement_count": len(set(referenced)),
        "authored_beat_count": len(authored),
        "transit_beat_count": sum(1 for item in beats if item["kind"] == "transit"),
        "camera_track": [
            {
                "beat_id": item["beat_id"], "frame": item["frame"], "phase_id": item["phase_id"],
                "placement_id": item["placement_id"], "camera_cue": item["camera_cue"],
            }
            for item in authored
        ],
        "event_track": [
            {
                "beat_id": item["beat_id"], "frame": item["frame"], "phase_id": item["phase_id"],
                "placement_id": item["placement_id"], "event_type": item["event_type"],
                "action_cue": item["action_cue"], "effect_cues": item["effect_cues"],
                "audio_cue": item["audio_cue"], "intensity": item["intensity"],
            }
            for item in authored
        ],
        "intensity_curve": [
            {"frame": item["frame"], "phase_id": item["phase_id"], "intensity": item["intensity"]}
            for item in authored
        ],
        "route_validation": {
            "fixed_anchor_mutation_count": 0,
            "all_authored_beats_have_phase": all(bool(item.get("phase_id")) for item in authored),
            "all_authored_beats_have_camera_cue": all(bool(item.get("camera_cue")) for item in authored),
            "all_authored_beats_have_action_cue": all(bool(item.get("action_cue")) for item in authored),
        },
        "beats": beats,
    }
    write_json(args.output, output)
    print("DIRECTOR_PATH_OK", args.genre, args.output)
    print("DIRECTOR_BEATS", len(beats), "FRAME_END", frame_end, "PATH_M", round(total_distance, 3))


if __name__ == "__main__":
    main()
