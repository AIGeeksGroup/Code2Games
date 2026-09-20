"""Long, terrain-hugging landscape route for the Racing showcase.

This is deliberately an implied off-road rally line, not a visible circuit.
It leaves the V6 lowland vegetation, makes its distance on the flat exposed
desert floor, then returns to the lowland for the finish.
"""

import math


TRACK_WIDTH_M = 7.0
SAMPLE_COUNT = 256

# A one-way landscape traversal, not a circuit.  The first and final sections
# cross the original vegetated lowland.  The central two long, well-separated
# lanes use the verified flat desert floor, so duration comes from real path
# length rather than slow motion.  The extra intermediate controls at each
# change of direction are intentional: they make the vehicle begin steering
# early and trace a readable arc instead of sliding sideways through a corner.
LOWLAND_WAYPOINTS = [
    # Vegetated start: a progressive south-west departure.  The former
    # (-108, -2) -> (-104, -18) -> (-90, -22) elbow was the visible sharp
    # steering event at about eleven seconds.
    (-86.9, 58.5), (-92.0, 51.0), (-97.0, 42.0), (-102.0, 32.0),
    (-106.0, 22.0), (-108.0, 12.0), (-108.0, 1.0), (-106.0, -10.0),
    (-102.0, -18.0), (-95.0, -22.5),
    # First open lane stays on the flat sand but is pulled north toward the
    # vegetation belt.  It keeps a clear driving corridor rather than cutting
    # through the trees, while giving the chase view grass at the roadside.
    (-84.0, -16.5), (-68.0, -17.0), (-50.0, -17.0), (-34.0, -15.5),
    # The east turnaround is deliberately a compact 180-degree rally arc,
    # not a point-turn: all tangent changes are now incremental.  It remains
    # west of x=-14, whose terrain pocket previously lifted a wheel.
    (-27.0, -14.0), (-22.0, -11.0), (-18.5, -7.5), (-17.0, -4.0),
    (-17.0, 0.0), (-18.5, 3.5), (-22.0, 7.0), (-28.0, 9.5),
    (-35.0, 11.0),
    # Second long lane runs along the south edge of the grassland, rather
    # than across the empty centre of the desert.
    (-44.0, 11.5), (-60.0, 11.5), (-78.0, 11.5), (-94.0, 13.0),
    # Broad west turnaround and an eastbound transition.  This replaces the
    # old hard northward kink before the grass, which made the car look as if
    # it slid sideways into vegetation around 32 seconds.
    (-103.0, 17.0), (-107.0, 21.0), (-108.5, 26.0), (-107.0, 30.5),
    (-103.0, 34.0),
    (-94.0, 37.0), (-80.0, 38.0), (-64.0, 38.0), (-50.0, 38.0),
    (-40.0, 39.0),
    # One continuous, forward grass entry and lowland finish.  The previous
    # S-shaped final controls made the car visibly side-step at 38--40 s and
    # again near 46 s.  This is now a single north-east arc all the way to
    # the finish: no reversal, no camera-facing lateral move.
    (-36.0, 43.0), (-33.0, 49.0), (-31.0, 55.0), (-29.0, 62.0),
    (-27.0, 69.0), (-25.0, 75.0), (-23.0, 79.0), (-20.0, 82.0),
]


def _segments():
    result = []
    running = 0.0
    for first, second in zip(LOWLAND_WAYPOINTS, LOWLAND_WAYPOINTS[1:]):
        dx, dy = second[0] - first[0], second[1] - first[1]
        length = max(1e-6, math.hypot(dx, dy))
        result.append((running, length, first, second))
        running += length
    return result, running


_SEGMENTS, ROUTE_LENGTH_M = _segments()


ASSET_SPECS = [
    (0.000, "racing_landmark_start_banner", "establish", "frame the lowland rally start", -4.0),
    (0.024, "racing_event_start_countdown", "countdown", "start the lowland exit", 2.0),
    (0.105, "racing_barrier_start_left", "steer", "thread the first lowland marker", -2.5),
    (0.185, "racing_barrier_gate1_right", "steer", "break out onto the desert straight", 2.5),
    (0.270, "racing_boost_early_center", "boost", "collect the first centre boost", 0.0),
    (0.350, "racing_barrier_mid_right", "steer", "hold the open desert line", 2.5),
    (0.435, "racing_boost_mid_right", "boost", "collect the desert boost", 0.0),
    # gate1_left and timing_gate_02 were the two unwanted props seen at
    # roughly 26 s and 30 s.  They are intentionally absent from this route;
    # Stage 12 consequently keeps them out of the showcase without altering
    # the source placement plan or their original world transforms.
    (0.675, "racing_barrier_mid_left", "steer", "leave the desert for the lowland", -2.5),
    (0.735, "racing_boost_left_choice", "boost", "collect the return centre boost", 0.0),
    # The final grass run is deliberately scenery-led.  The trapezoid branch
    # marker and the two late props previously visible at roughly 39 s / 43 s
    # are omitted by request, so they cannot appear as a cluttered stack.
]


def _segment_for_fraction(fraction):
    distance = max(0.0, min(1.0, float(fraction))) * ROUTE_LENGTH_M
    for start_distance, length, first, second in _SEGMENTS:
        if distance <= start_distance + length:
            return start_distance, length, first, second, distance
    start_distance, length, first, second = _SEGMENTS[-1]
    return start_distance, length, first, second, distance


def centreline(fraction, z=0.0):
    start_distance, length, first, second, distance = _segment_for_fraction(fraction)
    amount = max(0.0, min(1.0, (distance - start_distance) / length))
    return [
        first[0] + (second[0] - first[0]) * amount,
        first[1] + (second[1] - first[1]) * amount,
        float(z),
    ]


def tangent(fraction):
    _start_distance, _length, first, second, _distance = _segment_for_fraction(fraction)
    dx, dy = second[0] - first[0], second[1] - first[1]
    length = max(1e-9, math.hypot(dx, dy))
    return [dx / length, dy / length, 0.0]


def offset_position(fraction, lateral_m, z=0.0):
    point = centreline(fraction, z)
    direction = tangent(fraction)
    point[0] += direction[1] * float(lateral_m)
    point[1] -= direction[0] * float(lateral_m)
    return point


def nearest_fraction(x, y):
    """Arc-length fraction of the nearest point on the lowland polyline."""
    best_distance_sq = float("inf")
    best_fraction = 0.0
    for start_distance, length, first, second in _SEGMENTS:
        dx, dy = second[0] - first[0], second[1] - first[1]
        denom = max(1e-9, dx * dx + dy * dy)
        amount = max(0.0, min(1.0, ((x - first[0]) * dx + (y - first[1]) * dy) / denom))
        px, py = first[0] + dx * amount, first[1] + dy * amount
        distance_sq = (x - px) ** 2 + (y - py) ** 2
        if distance_sq < best_distance_sq:
            best_distance_sq = distance_sq
            best_fraction = (start_distance + length * amount) / ROUTE_LENGTH_M
    return best_fraction


def asset_layout():
    return {
        placement_id: {
            "fraction": fraction,
            "event_type": event_type,
            "label": label,
            "lateral_offset_m": lateral,
            "route_position": centreline(fraction),
            "asset_position": offset_position(fraction, lateral),
            "tangent": tangent(fraction),
        }
        for fraction, placement_id, event_type, label, lateral in ASSET_SPECS
    }


def build_route():
    assets = asset_layout()
    entries = []
    for fraction, placement_id, event_type, label, _lateral in ASSET_SPECS:
        entries.append((fraction, 0, {
            "placement_id": placement_id,
            "event_type": event_type,
            "label": label,
            "movement_mode": "drive",
            "actor_position": list(assets[placement_id]["route_position"]),
            "asset_anchor_position": list(assets[placement_id]["asset_position"]),
            "racing_track_fraction": float(fraction),
            "racing_speedway_layout": True,
        }))
    for index in range(1, 80):
        fraction = index / 80.0
        entries.append((fraction, 1, {
            "beat_id": "racing_lowland_transit_%02d" % index,
            "event_type": "drive",
            "label": "sustain speed through the landscape rally line",
            "movement_mode": "drive",
            "position": centreline(fraction),
            "racing_track_fraction": float(fraction),
            "racing_speedway_layout": True,
        }))
    entries.append((0.99, 2, {
        "beat_id": "racing_lowland_finish",
        "event_type": "goal",
        "label": "finish the long landscape rally run",
        "movement_mode": "drive",
        "position": centreline(1.0),
        "speed_multiplier": 1.0,
        "racing_track_fraction": 1.0,
        "racing_speedway_layout": True,
    }))
    return [entry for _fraction, _priority, entry in sorted(entries, key=lambda item: (item[0], item[1]))]
