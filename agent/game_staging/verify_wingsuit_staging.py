"""Fast no-render verification for the F-104 Stormline Rescue showcase.

Run after ``stage_wingsuit_showcase_gameplay.py`` and before any spot/full
render.  The checks use detailed route/event records rather than trusting only
aggregate counters.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys


SUMMIT_EVENT_IDS = {
    "placement_000",  # installed ignition flight cell 1/4
    "placement_022",  # launch gantry
}
AIRBORNE_EVENT_IDS = {
    "placement_018",
    "placement_017",
    "placement_001",
    "placement_002",
    "placement_016",
    "placement_003",
    "placement_023",
    "placement_019",
    "placement_020",
    "placement_013",
    "placement_012",
}
EXPECTED_RESPONSE_IDS = SUMMIT_EVENT_IDS | AIRBORNE_EVENT_IDS
COLLECTIBLE_IDS = {"placement_000", "placement_001", "placement_002", "placement_003"}
DESTINATION_IDS = {"placement_023", "placement_019", "placement_020", "placement_013", "placement_012"}
REVISED_WORLD_XYZ = {
    "placement_001": [-48.0, -70.0, 7.0],
    "placement_002": [-386.64621, -17.439356, 46.520142],
    "placement_003": [-225.0, -28.0, 39.0],
    "placement_023": [-272.804291, -13.96813, 27.521133],
    "placement_019": [-277.196136, -18.102955, 27.521179],
    "placement_020": [-287.207855, -19.655741, 27.518433],
    "placement_013": [-298.64621, -17.439356, 27.520142],
    "placement_012": [-302.471069, -12.043131, 27.516144],
}


def parse_args():
    values = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--report",
        default="./output/game_staging/game_wingsuit/director_gameplay_aircraft_showcase_v8/director_gameplay_report.json",
    )
    parser.add_argument(
        "--placement_plan",
        default="./output/game_staging/game_wingsuit/asset_placement_v8_gameplay/gameplay_placement_plan.v8_confirmed.json",
    )
    parser.add_argument(
        "--director_path",
        default="./output/game_staging/game_wingsuit/director_path_showcase_v8/director_path.json",
    )
    return parser.parse_args(values)


def load_json(path, label, failures):
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        failures.append("%s missing: %s" % (label, path))
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def xyz_distance(first, second):
    return math.sqrt(sum((float(a) - float(b)) ** 2 for a, b in zip(first, second)))


def main():
    args = parse_args()
    failures = []
    report = load_json(args.report, "report", failures)
    plan = load_json(args.placement_plan, "placement plan", failures)
    path = load_json(args.director_path, "director path", failures)
    if failures:
        for failure in failures:
            print("VERIFY_WINGSUIT_FAIL", failure)
        return 1

    if not report.get("ok"):
        failures.append("staging report ok is not true")
    if report.get("genre") != "wingsuit":
        failures.append("report genre is not wingsuit")
    if report.get("actor_kind") != "f104_starfighter_aircraft":
        failures.append("runtime actor is not the authored F-104")

    # Timeline contract: V8 is exactly 52 seconds and remains inside the user
    # requested 45--60 second delivery window.
    frame_start = int(report.get("frame_start") or 0)
    frame_end = int(report.get("frame_end") or 0)
    duration = frame_end / 24.0
    contract = report.get("wingsuit_timeline_contract") or {}
    print("WINGSUIT_TIMELINE", frame_start, frame_end, "seconds=%.3f" % duration)
    if not 45.0 <= duration <= 60.0:
        failures.append("duration %.3fs is outside 45--60s" % duration)
    if frame_end != 1248 or int(contract.get("target_video_frames") or 0) != 1248:
        failures.append("V8 timeline is not the 1248-frame contract")
    if [contract.get(key) for key in ("preflight_frames", "terrain_flight_frames", "pullup_frames")] != [72, 1116, 60]:
        failures.append("timeline phase lengths are not 72/1116/60")

    route = report.get("aircraft_route") or {}
    print(
        "WINGSUIT_ROUTE",
        "sweeps", route.get("terrain_sweep_count"),
        "preflight", route.get("summit_assets_used_as_preflight_events"),
        "airborne", route.get("flight_assets_used_as_near_pass_events"),
    )
    summit_route_ids = set(route.get("summit_assets_used_as_preflight_events") or [])
    airborne_route_ids = set(route.get("flight_assets_used_as_near_pass_events") or [])
    if not SUMMIT_EVENT_IDS <= summit_route_ids:
        failures.append("summit systems omitted from route: %s" % sorted(SUMMIT_EVENT_IDS - summit_route_ids))
    if airborne_route_ids != AIRBORNE_EVENT_IDS:
        failures.append("airborne asset route mismatch: %s" % sorted(AIRBORNE_EVENT_IDS ^ airborne_route_ids))
    if int(route.get("terrain_sweep_count") or 0) < 5:
        failures.append("route has too few mountain/forest sweep controls")
    if int(route.get("destination_orbit_count") or 0) < 10:
        failures.append("route does not complete the ten-control destination-mountain circuit")
    if route.get("route_shape") != "complete_destination_mountain_circuit_then_low_second_summit_finish":
        failures.append("route is not the confirmed second-mountain loop/finish shape")
    if int(route.get("destination_finale_start_frame") or 0) != 936:
        failures.append("second-summit finale does not begin at frame 936 / 39 seconds")
    if float(route.get("final_exit_climb_m") or 999.0) > 8.0:
        failures.append("final exit climbs too far to retain the summit in frame")
    if route.get("fixed_asset_positions_changed") is not False:
        failures.append("route reports fixed asset movement")
    if int(route.get("reversal_assets_left_as_scenery") or 0) != 0:
        failures.append("one or more mission assets were discarded as scenery")

    # Detailed HUD and feedback IDs must agree; this catches a stale aggregate
    # count that claims responses while a specific asset never participates.
    hud_events = report.get("wingsuit_event_hud") or []
    hud_ids = {str(event.get("placement_id")) for event in hud_events if event.get("placement_id")}
    feedback = report.get("aircraft_gameplay_feedback") or {}
    response_ids = set(feedback.get("responsive_placement_ids") or [])
    print("WINGSUIT_HUD_EVENTS", len(hud_events), sorted(hud_ids))
    print("WINGSUIT_LOCAL_RESPONSES", len(response_ids), sorted(response_ids))
    missing_hud = EXPECTED_RESPONSE_IDS - hud_ids
    missing_feedback = EXPECTED_RESPONSE_IDS - response_ids
    if missing_hud:
        failures.append("HUD omits asset events: %s" % sorted(missing_hud))
    if missing_feedback:
        failures.append("localized feedback omits assets: %s" % sorted(missing_feedback))
    if int(feedback.get("localized_asset_response_count") or 0) != len(response_ids):
        failures.append("localized response aggregate disagrees with detailed IDs")
    for forbidden in ("world_space_ring_count", "airborne_gate_count", "anchor_beam_count"):
        if int(feedback.get(forbidden) or 0) != 0:
            failures.append("forbidden flight feedback remains: %s=%s" % (forbidden, feedback.get(forbidden)))
    collected_ids = set(feedback.get("collected_placement_ids") or [])
    collection_frames = [int(value) for value in feedback.get("flight_cell_collection_frames") or []]
    print("WINGSUIT_COLLECTION", sorted(collected_ids), collection_frames, feedback.get("collection_counter_final"))
    if collected_ids != COLLECTIBLE_IDS:
        failures.append("flight-cell collection IDs mismatch")
    if collection_frames != sorted(collection_frames) or len(set(collection_frames)) != 4:
        failures.append("four flight cells are not collected at four ordered moments")
    if len(collection_frames) == 4:
        gaps = [second - first for first, second in zip(collection_frames, collection_frames[1:])]
        if min(gaps) < 120 or collection_frames[-1] - collection_frames[0] < 900:
            failures.append("flight-cell progression is not distributed across the complete run")
    if feedback.get("collection_counter_final") != "4/4":
        failures.append("flight-cell counter does not finish at 4/4")
    if feedback.get("collection_unlocks_destination") is not True:
        failures.append("collection does not causally unlock the destination")
    if int(report.get("pickup_animation_count") or 0) != 4:
        failures.append("exactly four flight-cell consumption animations are required")
    afterburner = report.get("aircraft_afterburner") or {}
    if afterburner.get("collection_changes_engine_output") is not True:
        failures.append("flight cells do not change afterburner output")
    if len(afterburner.get("flight_cell_power_levels") or []) != 4:
        failures.append("afterburner does not expose four collection power levels")

    # Confirm Stage 11 copied every fixed anchor exactly from the approved
    # placement plan.  Director transit controls are intentionally ignored.
    elements = plan.get("placement_plan", {}).get("elements") or []
    approved = {
        str(item.get("placement_id")): item.get("world_xyz")
        for item in elements
        if item.get("placement_id") and item.get("world_xyz")
    }
    checked = set()
    for beat in path.get("beats") or []:
        placement_id = str(beat.get("placement_id") or "")
        fixed = beat.get("fixed_anchor_world_xyz")
        if not placement_id or not fixed or placement_id not in approved:
            continue
        checked.add(placement_id)
        if xyz_distance(fixed, approved[placement_id]) > 1e-5:
            failures.append("fixed anchor changed for %s" % placement_id)
    missing_anchor_checks = EXPECTED_RESPONSE_IDS - checked
    print("WINGSUIT_FIXED_ANCHORS_CHECKED", len(checked))
    if missing_anchor_checks:
        failures.append("mission anchors absent from Director JSON: %s" % sorted(missing_anchor_checks))

    # V8 deliberately revises Stage 6 before Director starts.  Verify both the
    # confirmed coordinates and the relocation audit, while still requiring
    # Director itself to report zero fixed-asset movement.
    for placement_id, expected_xyz in REVISED_WORLD_XYZ.items():
        if placement_id not in approved:
            failures.append("V8 revised placement absent: %s" % placement_id)
        elif xyz_distance(approved[placement_id], expected_xyz) > 1e-5:
            failures.append("V8 revised coordinate changed: %s" % placement_id)
    revision = plan.get("wingsuit_v8_gameplay_revision") or {}
    if int(revision.get("moved_placement_count") or 0) != 8:
        failures.append("V8 confirmed plan does not record eight gameplay moves")
    if set(revision.get("airborne_collectible_ids") or []) != {"placement_001", "placement_002", "placement_003"}:
        failures.append("V8 plan does not identify the three airborne collectibles")
    relocation = report.get("wingsuit_gameplay_relocation") or {}
    relocated_ids = set(relocation.get("moved_placement_ids") or [])
    print("WINGSUIT_DESTINATION", sorted(relocated_ids), route.get("destination_mountain_center_world_xyz"))
    if relocated_ids != set(REVISED_WORLD_XYZ):
        failures.append("staged V8 gameplay relocation IDs mismatch")
    if int(relocation.get("moved_placement_count") or 0) != 8:
        failures.append("staging report does not record eight gameplay moves")
    if relocation.get("unlisted_placements_unchanged") is not True:
        failures.append("staging report does not preserve unlisted placements")
    if relocation.get("director_fixed_asset_positions_changed") is not False:
        failures.append("Director reports moving newly confirmed fixed assets")

    readability = report.get("wingsuit_destination_readability") or {}
    readable_ids = {
        str(item.get("placement_id"))
        for item in readability.get("samples") or []
        if item.get("in_safe_frame")
    }
    print(
        "WINGSUIT_DESTINATION_READABILITY",
        readability.get("in_safe_frame_count"),
        readability.get("sample_count"),
        "gap", readability.get("minimum_event_gap_frames"),
    )
    for item in readability.get("samples") or []:
        print(
            "WINGSUIT_DESTINATION_FRAME",
            item.get("placement_id"), item.get("frame"),
            "uv", item.get("screen_uv"),
            "depth", item.get("camera_depth"),
            "safe", item.get("in_safe_frame"),
        )
    if readable_ids != DESTINATION_IDS:
        failures.append("not every second-summit asset anchor is framed at its event")
    if readability.get("all_destination_anchors_in_safe_frame") is not True:
        failures.append("destination readability audit failed")
    if int(readability.get("minimum_event_gap_frames") or 0) < 24:
        failures.append("second-summit events remain too tightly stacked")

    dynamics = report.get("aircraft_dynamics") or {}
    print(
        "WINGSUIT_DYNAMICS",
        "clearance", dynamics.get("minimum_terrain_clearance_m"),
        "bank", dynamics.get("maximum_coordinated_bank_degrees"),
        "pitch", dynamics.get("maximum_pitch_degrees"),
        "final", dynamics.get("final_frame"),
    )
    if not dynamics.get("enabled"):
        failures.append("aircraft dynamics missing")
    if float(dynamics.get("minimum_terrain_clearance_m") or 0.0) < 0.25:
        failures.append("aircraft penetrates terrain/support")
    if float(dynamics.get("maximum_coordinated_bank_degrees") or 0.0) < 8.0:
        failures.append("terrain route contains no readable banking")
    if float(dynamics.get("path_pitch_limit_degrees") or 0.0) != 58.0:
        failures.append("V8 path pitch limiter is not the required 58 degrees")
    if float(dynamics.get("maximum_pitch_degrees") or 999.0) > 65.0:
        failures.append("aircraft evaluated pitch exceeds 65 degrees")
    if int(dynamics.get("final_frame") or 0) != 1248:
        failures.append("aircraft animation does not reach frame 1248")

    camera_system = str(report.get("camera_system") or "")
    unresolved = int(report.get("camera_unresolved_occlusions") or 0)
    print("WINGSUIT_CAMERA", camera_system, "unresolved", unresolved)
    if camera_system != "rigid_left_rear_flight_path_boom":
        failures.append("camera is not the continuous left-rear F-104 boom")
    if unresolved:
        failures.append("camera has unresolved occlusions: %d" % unresolved)

    if failures:
        print("VERIFY_WINGSUIT_FAIL")
        for failure in failures:
            print("  FAIL", failure)
        return 1
    print("VERIFY_WINGSUIT_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
