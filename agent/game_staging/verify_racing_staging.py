"""No-render acceptance checks for the V6-based Racing rebuild."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys


EXPECTED_ROUTE_ASSET_IDS = {
    "racing_landmark_start_banner",
    "racing_barrier_gate1_right",
    "racing_event_start_countdown",
    "racing_boost_left_choice",
    "racing_barrier_start_left",
    "racing_boost_early_center",
    "racing_boost_mid_right",
    "racing_barrier_mid_right",
    "racing_barrier_mid_left",
}


def parse_args():
    values = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True)
    parser.add_argument("--placement_plan", required=True)
    parser.add_argument("--director_path", required=True)
    parser.add_argument("--summary", default="")
    return parser.parse_args(values)


def boost_windows(samples, threshold=400.0):
    windows = []
    active = None
    for sample in samples:
        frame = int(sample.get("frame", 0))
        value = float(sample.get("display_speed_kmh", sample.get("speed_kmh", 0.0)))
        if value >= threshold:
            if active is None:
                active = [frame, frame]
            else:
                active[1] = frame
        elif active is not None:
            windows.append(tuple(active))
            active = None
    if active is not None:
        windows.append(tuple(active))
    return windows


def backtrack_count(path):
    beats = path.get("beats") or []
    directions = []
    for previous, current in zip(beats, beats[1:]):
        a = current.get("staged_position") or current.get("position")
        b = previous.get("staged_position") or previous.get("position")
        if not isinstance(a, list) or not isinstance(b, list) or len(a) < 2 or len(b) < 2:
            continue
        delta = [float(a[0]) - float(b[0]), float(a[1]) - float(b[1])]
        length = math.hypot(delta[0], delta[1])
        if length >= 0.05:
            directions.append((delta[0] / length, delta[1] / length))
    return sum(
        1
        for previous, current in zip(directions, directions[1:])
        if previous[0] * current[0] + previous[1] * current[1] < -0.8
    )


def normalized_support_steps(samples, normalized_key, raw_key):
    """Return step rates on the solver's two-frame acceptance basis.

    Dense route samples do not have a constant frame gap.  Comparing a raw
    change accumulated across 10--25 frames with a two-frame limit produces a
    false failure even when the support solver stayed inside that limit.
    """
    values = []
    previous_frame = None
    for sample in samples:
        if normalized_key in sample:
            values.append(float(sample.get(normalized_key, 0.0)))
        else:
            frame = int(sample.get("frame", 0))
            frame_delta = int(sample.get("frame_delta", 0))
            if frame_delta <= 0 and previous_frame is not None:
                frame_delta = max(1, frame - previous_frame)
            raw_value = float(sample.get(raw_key, 0.0))
            values.append(raw_value * 2.0 / float(frame_delta) if frame_delta > 0 else 0.0)
        previous_frame = int(sample.get("frame", 0))
    return values


def main():
    args = parse_args()
    report_path = os.path.abspath(args.report)
    path_path = os.path.abspath(args.director_path)
    plan_path = os.path.abspath(args.placement_plan)
    failures = []
    for label, path in (("report", report_path), ("director path", path_path), ("placement plan", plan_path)):
        if not os.path.isfile(path):
            print("VERIFY_RACING_ERROR %s missing: %s" % (label, path))
            return 1
    report = json.load(open(report_path, encoding="utf-8"))
    path = json.load(open(path_path, encoding="utf-8"))

    frame_end = int(report.get("frame_end") or 0)
    seconds = frame_end / 24.0
    print("RACING_FRAME_END", frame_end, "= %.3fs" % seconds)
    if not 45.0 <= seconds <= 60.0:
        failures.append("duration %.3fs is outside 45-60s" % seconds)

    pass_report = report.get("racing_asset_passes") or {}
    safe_asset_count = int(pass_report.get("safe_asset_count") or 0)
    reaction_count = int(pass_report.get("event_reaction_count") or 0)
    passed_ids = set(pass_report.get("passed_placement_ids") or [])
    missing_expected = sorted(EXPECTED_ROUTE_ASSET_IDS - passed_ids)
    passed_without_reaction = list(pass_report.get("passed_without_reaction_ids") or [])
    print("RACING_SAFE_ASSET_COUNT", safe_asset_count)
    print("RACING_EVENT_REACTION_COUNT", reaction_count)
    print("RACING_PASSED_ASSETS", sorted(passed_ids))
    # The late grass run deliberately omits the former gate-one-left /
    # timing-gate-two pair as well as the three late clutter props.  Keep the
    # verifier aligned with the curated nine-asset route.
    if safe_asset_count < 9:
        failures.append("safe passed asset count %d < 9" % safe_asset_count)
    if missing_expected:
        failures.append("expected route assets not actually passed: %s" % ", ".join(missing_expected))
    if reaction_count != safe_asset_count or passed_without_reaction:
        failures.append(
            "asset reactions %d do not match passed assets %d (missing: %s)"
            % (reaction_count, safe_asset_count, ", ".join(passed_without_reaction))
        )
    feedback = report.get("racing_gameplay_feedback") or {}
    visible_reactions = int(feedback.get("prop_reaction_visual_count") or 0)
    print("RACING_VISIBLE_ASSET_REACTION_COUNT", visible_reactions)
    if visible_reactions < safe_asset_count:
        failures.append(
            "visible gameplay-asset reactions %d < passed assets %d"
            % (visible_reactions, safe_asset_count)
        )

    support = report.get("racing_vehicle_support") or {}
    support_samples = report.get("racing_vehicle_support_samples") or support.get("samples") or []
    unsafe_samples = [sample for sample in support_samples if not bool(sample.get("safe"))]
    minimum_clearance = min(
        (float(sample.get("minimum_ground_clearance_m", 0.0)) for sample in support_samples),
        default=-999.0,
    )
    maximum_clearance = max(
        (float(sample.get("maximum_ground_clearance_m", 999.0)) for sample in support_samples),
        default=999.0,
    )
    normalized_vertical_steps = normalized_support_steps(
        support_samples, "vertical_step_per_2_frames_m", "vertical_step_m"
    )
    normalized_rotation_steps = normalized_support_steps(
        support_samples, "pitch_roll_step_per_2_frames_degrees", "pitch_roll_step_degrees"
    )
    maximum_vertical_step = max(normalized_vertical_steps, default=999.0)
    maximum_rotation_step = max(normalized_rotation_steps, default=999.0)
    seventh_second_unsafe = [
        int(sample.get("frame", 0))
        for sample in support_samples
        if 157 <= int(sample.get("frame", 0)) <= 179 and not bool(sample.get("safe"))
    ]
    print("RACING_SUPPORT_UNSAFE_SAMPLES", len(unsafe_samples))
    print("RACING_MIN_GROUND_CLEARANCE_M", round(minimum_clearance, 6))
    print("RACING_MAX_GROUND_CLEARANCE_M", round(maximum_clearance, 6))
    print("RACING_MAX_VERTICAL_STEP_M_PER_2_FRAMES", round(maximum_vertical_step, 6))
    print("RACING_MAX_PITCH_ROLL_STEP_DEG_PER_2_FRAMES", round(maximum_rotation_step, 6))
    print("RACING_SEVENTH_SECOND_UNSAFE_FRAMES", seventh_second_unsafe)
    if not support_samples:
        failures.append("four-wheel support samples are missing")
    if unsafe_samples:
        failures.append("%d detailed four-wheel support samples are unsafe" % len(unsafe_samples))
    if minimum_clearance < -0.03:
        failures.append("minimum ground clearance %.4fm < -0.03m" % minimum_clearance)
    if maximum_clearance > 0.20:
        failures.append("maximum ground clearance %.4fm > 0.20m; vehicle is visibly hovering" % maximum_clearance)
    if maximum_vertical_step > 0.18001:
        failures.append("maximum two-frame vertical step %.4fm > 0.18m" % maximum_vertical_step)
    if maximum_rotation_step > 3.0001:
        failures.append("maximum two-frame pitch/roll step %.4fdeg > 3deg" % maximum_rotation_step)
    if seventh_second_unsafe:
        failures.append("seventh-second support failure at frames %s" % seventh_second_unsafe)

    mountain_seconds = float(support.get("mountain_drive_seconds") or 0.0)
    mountain_ratio = mountain_seconds / max(seconds, 1e-6)
    print("RACING_MOUNTAIN_DRIVE_SECONDS", round(mountain_seconds, 6))
    print("RACING_MOUNTAIN_DRIVE_RATIO", round(mountain_ratio, 6))
    if mountain_ratio > 0.15:
        failures.append("mountain driving ratio %.3f > 0.15" % mountain_ratio)

    speed_samples = report.get("racing_speed_hud") or []
    display_values = [
        float(sample.get("display_speed_kmh", sample.get("speed_kmh", 0.0)))
        for sample in speed_samples
    ]
    minimum_speed = min(display_values, default=0.0)
    maximum_speed = max(display_values, default=0.0)
    windows = boost_windows(speed_samples)
    print("RACING_MIN_DISPLAY_SPEED_KMH", round(minimum_speed, 3))
    print("RACING_MAX_DISPLAY_SPEED_KMH", round(maximum_speed, 3))
    print("RACING_BOOST_WINDOWS", windows)
    if minimum_speed < 200.0:
        failures.append("display speed falls below 200 km/h")
    if maximum_speed < 400.0 or maximum_speed > 450.0:
        failures.append("display peak %.2f km/h is outside 400-450" % maximum_speed)
    if not windows or max(end - start + 1 for start, end in windows) < 48:
        failures.append("no >=400 km/h boost window lasts at least 2.0 seconds")

    speed_by_frame = {
        int(sample.get("frame", 0)): float(sample.get("speed_mps", 0.0))
        for sample in speed_samples
    }
    boost_events = [
        event for event in (report.get("racing_event_hud") or [])
        if event.get("event_type") == "boost"
    ]
    physical_boost_gains = []
    for event in boost_events:
        frame = int(event.get("frame", 0))
        before = [speed_by_frame[value] for value in range(frame - 16, frame) if value in speed_by_frame]
        after = [speed_by_frame[value] for value in range(frame + 4, frame + 52) if value in speed_by_frame]
        before_speed = sum(before) / len(before) if before else 0.0
        after_speed = max(after, default=0.0)
        physical_boost_gains.append({
            "placement_id": event.get("placement_id"),
            "frame": frame,
            "before_speed_mps": round(before_speed, 4),
            "after_peak_speed_mps": round(after_speed, 4),
            "gain_ratio": round(after_speed / max(0.01, before_speed), 4),
        })
    effective_boosts = sum(
        1 for item in physical_boost_gains
        if item["after_peak_speed_mps"] >= item["before_speed_mps"] * 1.20
    )
    collectible_count = int(report.get("pickup_animation_count") or 0)
    print("RACING_BOOST_COLLECTIBLE_DISAPPEAR_COUNT", collectible_count)
    print("RACING_PHYSICAL_BOOST_GAINS", physical_boost_gains)
    if collectible_count < 3:
        failures.append("fewer than 3 boost collectibles disappear on collection")
    if effective_boosts < 3:
        failures.append("fewer than 3 boost pickups increase actual vehicle speed by >=20%")

    path_smoothing = report.get("racing_path_smoothing") or {}
    speedway = report.get("racing_speedway") or {}
    path_length = float(path_smoothing.get("actual_path_length_m") or 0.0)
    physical_speed = float(path_smoothing.get("actual_average_motion_speed_mps") or 0.0)
    timing_scale = float(path_smoothing.get("uniform_duration_scale") or 0.0)
    print("RACING_SPEEDWAY_ENABLED", bool(speedway.get("enabled")))
    print("RACING_RELOCATED_ASSET_COUNT", int(speedway.get("relocated_asset_count") or 0))
    print("RACING_TRACK_VISIBLE_TO_CAMERA", bool(speedway.get("road_visible_to_camera", True)))
    print("RACING_LOGICAL_SURFACE_GAP_M", float(speedway.get("maximum_logical_surface_gap_from_visible_ground_m") or 0.0))
    print("RACING_ACTUAL_PATH_LENGTH_M", round(path_length, 3))
    print("RACING_ACTUAL_AVERAGE_SPEED_MPS", round(physical_speed, 3))
    print("RACING_UNIFORM_DURATION_SCALE", round(timing_scale, 6))
    if not speedway.get("enabled"):
        failures.append("purpose-built racing speedway is missing")
    if int(speedway.get("relocated_asset_count") or 0) < 9:
        failures.append("fewer than 9 gameplay assets were distributed on the speedway")
    if bool(speedway.get("road_visible_to_camera", True)):
        failures.append("logical racing road is visible; original environment floor must remain visible")
    if not bool(speedway.get("original_environment_floor_visible")):
        failures.append("original environment floor visibility is not declared")
    if float(speedway.get("maximum_logical_surface_gap_from_visible_ground_m") or 0.0) > 0.08:
        failures.append("logical support surface sits more than 8cm above the visible terrain")
    relocated_assets = speedway.get("relocated_assets") or []
    boost_scales = [
        float(item.get("display_scale") or 0.0)
        for item in relocated_assets
        if str(item.get("placement_id", "")).startswith("racing_boost_")
    ]
    if len(boost_scales) < 3 or min(boost_scales, default=0.0) < 1.6:
        failures.append("boost collectibles are not all enlarged to at least 1.6x")
    boost_lateral_offsets = [
        abs(float(item.get("lateral_offset_m") or 0.0))
        for item in relocated_assets
        if str(item.get("placement_id", "")).startswith("racing_boost_")
    ]
    print("RACING_BOOST_LATERAL_OFFSETS_M", boost_lateral_offsets)
    if len(boost_lateral_offsets) < 3 or max(boost_lateral_offsets, default=999.0) > 0.20:
        failures.append("all boost collectibles must sit on the road centreline")
    if path_length < 400.0:
        failures.append("physical route %.2fm < 400m" % path_length)
    if physical_speed < 8.0:
        failures.append("actual average vehicle speed %.2fm/s < 8m/s" % physical_speed)
    if timing_scale > 1.15:
        failures.append("route is slowed by %.3fx; duration must come from distance" % timing_scale)
    unresolved_segments = int(path_smoothing.get("unresolved_blocked_segment_count") or 0)
    unresolved_details = path_smoothing.get("unresolved_blocked_segments") or []
    print("RACING_UNRESOLVED_BLOCKED_SEGMENTS", unresolved_details)
    if unresolved_segments:
        failures.append("%d racing route segments remain blocked" % unresolved_segments)
    backtracks = backtrack_count(path)
    print("RACING_BACKTRACK_COUNT", backtracks)
    if backtracks:
        failures.append("route contains %d backtracking direction changes" % backtracks)

    camera = report.get("racing_camera_visibility") or {}
    camera_ratio = float(camera.get("visible_ratio") or 0.0)
    print("RACING_CAMERA_VEHICLE_VISIBLE_RATIO", round(camera_ratio, 6))
    if camera_ratio < 0.70:
        failures.append("camera vehicle visible ratio %.3f < 0.70" % camera_ratio)

    dynamics = report.get("racing_vehicle_dynamics") or {}
    tangent_locked = bool(dynamics.get("tangent_locked_per_frame"))
    tangent_samples = int(dynamics.get("tangent_lock_sample_count") or 0)
    print("RACING_TANGENT_LOCKED_PER_FRAME", tangent_locked)
    print("RACING_TANGENT_LOCK_SAMPLE_COUNT", tangent_samples)
    if not tangent_locked or tangent_samples < frame_end:
        failures.append("vehicle orientation is not tangent-locked at every playback frame")

    summary = {
        "passed": not failures,
        "frame_end": frame_end,
        "duration_seconds": round(seconds, 6),
        "safe_asset_count": safe_asset_count,
        "event_reaction_count": reaction_count,
        "visible_asset_reaction_count": visible_reactions,
        "tangent_locked_per_frame": tangent_locked,
        "mountain_drive_seconds": round(mountain_seconds, 6),
        "mountain_drive_ratio": round(mountain_ratio, 6),
        "minimum_display_speed_kmh": round(minimum_speed, 6),
        "maximum_display_speed_kmh": round(maximum_speed, 6),
        "boost_windows": windows,
        "boost_collectible_disappear_count": collectible_count,
        "physical_boost_gains": physical_boost_gains,
        "support_unsafe_sample_count": len(unsafe_samples),
        "minimum_ground_clearance_m": round(minimum_clearance, 6),
        "maximum_ground_clearance_m": round(maximum_clearance, 6),
        "maximum_vertical_step_per_2_frames_m": round(maximum_vertical_step, 6),
        "maximum_pitch_roll_step_per_2_frames_degrees": round(maximum_rotation_step, 6),
        "seventh_second_unsafe_frames": seventh_second_unsafe,
        "backtrack_count": backtracks,
        "camera_vehicle_visible_ratio": round(camera_ratio, 6),
        "speedway_enabled": bool(speedway.get("enabled")),
        "relocated_asset_count": int(speedway.get("relocated_asset_count") or 0),
        "logical_track_visible_to_camera": bool(speedway.get("road_visible_to_camera", True)),
        "maximum_logical_surface_gap_from_visible_ground_m": float(speedway.get("maximum_logical_surface_gap_from_visible_ground_m") or 0.0),
        "boost_lateral_offsets_m": boost_lateral_offsets,
        "actual_path_length_m": round(path_length, 6),
        "actual_average_motion_speed_mps": round(physical_speed, 6),
        "uniform_duration_scale": round(timing_scale, 6),
        "failures": failures,
    }
    summary_path = os.path.abspath(args.summary) if args.summary else os.path.join(
        os.path.dirname(report_path), "verification_summary.json"
    )
    with open(summary_path, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print("RACING_VERIFICATION_SUMMARY", summary_path)

    if failures:
        print("VERIFY_RACING_FAIL")
        for failure in failures:
            print("  FAIL", failure)
        return 1
    print("VERIFY_RACING_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
