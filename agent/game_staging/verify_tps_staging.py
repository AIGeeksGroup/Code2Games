"""Fast non-rendering verification for the staged TPS showcase blend.

Checks, without rendering:
- scene frame_end / engine / camera / actor root
- camera and body rotation speed (fast turns + oscillation = shaking)
- body speed spikes (teleports)
- enemy grounding (straight-down ray under each TPS hostile root)
- enemy visibility: does each hostile project inside the camera frame at
  the combat probe frames?  (Answers "gunfire but no enemy visible".)

Run inside Blender:
  blender -b <blend> --python verify_tps_staging.py -- --probe_frames 104 411 659
"""

import argparse
import json
import math
import os
import sys

import bpy
from mathutils import Vector


def script_args():
    return sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []


def ground_z(x, y, scene, depsgraph):
    origin = Vector((float(x), float(y), 400.0))
    direction = Vector((0.0, 0.0, -1.0))
    for _ in range(48):
        hit, location, _normal, _face, obj, _matrix = scene.ray_cast(
            depsgraph,
            origin,
            direction,
            distance=900.0,
        )
        if not hit:
            return origin.z - 900.0
        if obj and obj.type == "MESH" and not obj.get("code2games_placement_id"):
            return float(location.z)
        origin = Vector((location.x, location.y, location.z - 0.03))
    return float(origin.z)


def project_into_camera(scene, depsgraph, camera, world_position):
    if camera is None:
        return None
    rel = camera.matrix_world.inverted() @ Vector(world_position)
    if rel.z >= -0.05:
        return None  # behind or at the camera
    res_x = int(scene.render.resolution_x * scene.render.resolution_percentage / 100.0)
    res_y = int(scene.render.resolution_y * scene.render.resolution_percentage / 100.0)
    focal_px = float(camera.data.lens) * res_x / 36.0
    depth = -rel.z
    screen_x = res_x / 2.0 + (rel.x / depth) * focal_px
    screen_y = res_y / 2.0 - (rel.y / depth) * focal_px
    inside = 0.0 <= screen_x <= res_x and 0.0 <= screen_y <= res_y
    return (screen_x, screen_y, inside)


def los_clear(scene, depsgraph, origin, target, ignore_prefixes=(), ignore_names=()):
    """True when the ray origin->target reaches the target before any solid."""
    origin = Vector(origin)
    target = Vector(target)
    delta = target - origin
    distance = delta.length
    if distance < 0.5:
        return False
    direction = delta / distance
    ray_origin = origin
    remaining = distance
    ignore_prefixes = tuple(ignore_prefixes)
    ignore_names = set(ignore_names)
    for _ in range(48):
        hit, location, _normal, _face, obj, _matrix = scene.ray_cast(
            depsgraph,
            ray_origin,
            direction,
            distance=max(0.0, remaining - 0.30),
        )
        if not hit:
            return True
        location = Vector(location)
        travelled = (location - ray_origin).length
        if travelled >= remaining - 1.0:
            return True  # reached the target volume
        source = obj.original if getattr(obj, "is_evaluated", False) else obj
        name = str(source.name)
        if name in ignore_names or any(name.startswith(prefix) for prefix in ignore_prefixes):
            ray_origin = location + direction * 0.04
            remaining = max(0.0, (target - ray_origin).length)
            continue
        # Match the staging visibility contract: temporary Director actors,
        # route-debug meshes, muzzle flashes and tracer effects do not make a
        # hostile physically hidden.  Fixed gameplay placements remain solid
        # and must still fail the LOS test.
        current = source
        is_fixed_placement = False
        for _ in range(32):
            if current is None:
                break
            if current.get("code2games_placement_id"):
                is_fixed_placement = True
                break
            current = current.parent
        in_director_collection = any(
            collection.name.startswith("Code2Games_") or collection.name.startswith("C2G_")
            for collection in source.users_collection
        )
        if in_director_collection and not is_fixed_placement:
            ray_origin = location + direction * 0.04
            remaining = max(0.0, (target - ray_origin).length)
            continue
        return False
    return False


def write_validation_markdown(path, validation, blend_path, report_path):
    route = validation["route"]
    motion = validation["motion"]
    camera = validation["camera"]
    rows = []
    for encounter in validation["combat"]["encounters"]:
        rows.append(
            "| %s | %s | %s | %s | %s | %s | %s |"
            % (
                encounter["enemy"],
                encounter["first_visible_frame"],
                encounter["enemy_attack_start_frame"],
                encounter["player_attack_start_frame"],
                encounter["first_player_hit_frame"],
                encounter["enemy_death_start_frame"],
                "PASS" if encounter["ok"] else "FAIL",
            )
        )
    hidden = ", ".join(str(name) for name in camera["hidden_tree_names"]) or "(none)"
    lines = [
        "# TPS Final Delivery (auto validation)",
        "",
        "## 版本",
        "- Final staged Blend: %s" % blend_path,
        "- Director report: %s" % (report_path or "n/a"),
        "",
        "## 路线覆盖",
        "- 覆盖 placement ID: %s"
        % ", ".join(str(value) for value in route["referenced_fixed_placement_ids"]),
        "- 覆盖数量: %s" % route["coverage_count"],
        "- 开场反向步数: %s" % route["opening_reverse_step_count"],
        "- 开场绕行数: %s" % route["opening_detour_count"],
        "- asset 最小中心距离(m): %s"
        % json.dumps(route["asset_min_distances_m"], ensure_ascii=False),
        "",
        "## 移动与相机验证",
        "- 最大角色转角(deg/frame): %s" % motion["max_body_turn_deg_per_frame"],
        "- 最大相机转角(deg/frame): %s" % camera["max_turn_deg_per_frame"],
        "- 最大速度(m/frame): %s" % motion["max_speed_m_per_frame"],
        "- 交替转向对: %s" % motion["alternating_turn_pair_count"],
        "- 未解决视锥遮挡数: %s" % camera["frustum_unresolved_count"],
        "- 隐藏 TreeFactory 实例: %s" % hidden,
        "",
        "## 战斗验证",
        "| Enemy | First visible | Enemy attack | Player attack | First hit | Death | Result |",
        "|---|---:|---:|---:|---:|---:|---|",
    ] + rows + [
        "",
        "## 结论",
        "- %s" % ("PASS" if validation["ok"] else "FAIL"),
    ]
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description="Verify staged TPS blend without rendering")
    parser.add_argument("--blend", default="", help="optional; defaults to the opened blend")
    parser.add_argument("--report", default="", help="director_gameplay_report.json from the same staging run")
    parser.add_argument("--validation_output", default="", help="tps_final_validation.json output path")
    parser.add_argument("--probe_frames", type=int, nargs="*", default=[])
    args = parser.parse_args(script_args())

    report = {}
    if args.report:
        if not os.path.isfile(args.report):
            raise FileNotFoundError("TPS staging report not found: %s" % args.report)
        with open(args.report, "r", encoding="utf-8") as handle:
            report = json.load(handle)
    failures = []

    blend_path = args.blend or bpy.data.filepath
    if not blend_path:
        raise SystemExit("no blend open and --blend not given")
    bpy.ops.wm.open_mainfile(filepath=blend_path)
    scene = bpy.context.scene
    print("FRAME_END", scene.frame_end, "FPS", scene.render.fps, "ENGINE", scene.render.engine)
    duration_seconds = (int(scene.frame_end) - int(scene.frame_start) + 1) / float(scene.render.fps)
    print("TPS_DURATION_SECONDS", "%.3f" % duration_seconds)
    if not 45.0 <= duration_seconds <= 60.0:
        failures.append("TPS duration %.3fs is outside the required 45-60s" % duration_seconds)
    camera = scene.camera
    print("CAMERA", camera.name if camera else None)
    actor_root = next(
        (obj for obj in bpy.data.objects if obj.name == "C2G_TPS_PLAYER_ROOT"),
        None,
    )
    print("ACTOR_ROOT_FOUND", bool(actor_root))

    camera_unresolved = int(
        camera.get("code2games_camera_unresolved_occlusions", -1)
        if camera is not None else -1
    )
    camera_shortened = int(
        camera.get("code2games_camera_shortened_sample_count", 0)
        if camera is not None else 0
    )
    print("CAMERA_SHORTENED_SAMPLE_COUNT", camera_shortened)
    print("CAMERA_UNRESOLVED_OCCLUSIONS", camera_unresolved)
    if camera_unresolved != 0:
        failures.append("TPS spring arm has %d unresolved camera occlusions" % camera_unresolved)
    camera_step = max(1, int(camera.get("code2games_camera_sample_step_frames", 2))) if camera else 2
    camera_sample_count = max(1, int(math.ceil((scene.frame_end - scene.frame_start) / camera_step)) + 1)
    shortened_ratio = camera_shortened / float(camera_sample_count)
    print("CAMERA_SHORTENED_SAMPLE_RATIO", "%.3f" % shortened_ratio)
    print("CAMERA_BLOCKING_OBJECTS", camera.get("code2games_camera_blocking_objects", "[]") if camera else "[]")
    print("CAMERA_HIDDEN_TREE_COUNT", int(camera.get("code2games_camera_hidden_tree_count", 0)) if camera else 0)
    print("CAMERA_HIDDEN_TREE_NAMES", camera.get("code2games_camera_hidden_tree_names", "[]") if camera else "[]")
    print("CAMERA_FRUSTUM_RAY_COUNT", int(camera.get("code2games_camera_frustum_ray_count", 0)) if camera else 0)
    print(
        "CAMERA_FRUSTUM_UNRESOLVED_COUNT",
        int(camera.get("code2games_camera_frustum_unresolved_count", -1)) if camera else -1,
    )

    probe_frames = list(args.probe_frames)
    if not probe_frames and report:
        probe_frames = [
            int(item["frame"])
            for item in (report.get("tps_combat") or {}).get("enemy_schedules", [])
            if item.get("frame") is not None
        ]
    if not probe_frames:
        probe_frames = [104, 411, 659]
    print("COMBAT_PROBE_FRAMES", probe_frames)

    # Enemy grounding + visibility at the probe (combat) frames.
    enemy_roots = [
        obj for obj in bpy.data.objects
        if obj.name.startswith("C2G_TPS_HOSTILE_") and obj.name.endswith("_ROOT")
    ]
    print("ENEMY_ROOTS", [root.name for root in enemy_roots])
    if len(enemy_roots) != 5:
        failures.append("TPS has %d enemy roots (expected exactly 5)" % len(enemy_roots))
    for root in enemy_roots:
        worst_gap = 0.0
        for frame in probe_frames:
            scene.frame_set(frame)
            depsgraph = bpy.context.evaluated_depsgraph_get()
            bpy.context.view_layer.update()
            position = root.evaluated_get(depsgraph).matrix_world.translation
            gap = float(position.z) - ground_z(position.x, position.y, scene, depsgraph)
            worst_gap = max(worst_gap, gap)
        print("ENEMY", root.name, "max_float_gap_m=%.3f" % worst_gap)

    for frame in probe_frames:
        scene.frame_set(frame)
        depsgraph = bpy.context.evaluated_depsgraph_get()
        bpy.context.view_layer.update()
        for root in enemy_roots:
            position = root.evaluated_get(depsgraph).matrix_world.translation
            projected = project_into_camera(scene, depsgraph, camera, position + Vector((0.0, 0.0, 1.5)))
            if projected is None:
                print("ENEMY_VIS", frame, root.name, "behind_or_nan")
            else:
                screen_x, screen_y, inside = projected
                print(
                    "ENEMY_VIS", frame, root.name,
                    "screen=(%.0f, %.0f) inside=%s" % (screen_x, screen_y, inside),
                )

    # Motion audit: camera/body rotation speed + body position speed.
    frames = list(range(max(1, scene.frame_start), scene.frame_end + 1))
    camera_rates = []
    root_rates = []
    root_speeds = []
    root_positions = []
    previous_camera_rotation = None
    previous_root_rotation = None
    previous_root_position = None
    for frame in frames:
        scene.frame_set(frame)
        depsgraph = bpy.context.evaluated_depsgraph_get()
        bpy.context.view_layer.update()
        if camera is not None:
            rotation = camera.evaluated_get(depsgraph).matrix_world.to_quaternion()
            if previous_camera_rotation is not None:
                # q and -q describe the same orientation.  Matrix conversion
                # is free to return either sign, so use the absolute dot
                # product and report the physically shortest rotation.
                dot = abs(sum(rotation[index] * previous_camera_rotation[index] for index in range(4)))
                angle = 2.0 * math.acos(max(-1.0, min(1.0, dot)))
                camera_rates.append((frame, math.degrees(angle)))
            previous_camera_rotation = rotation
        if actor_root is not None:
            evaluated = actor_root.evaluated_get(depsgraph).matrix_world
            rotation = evaluated.to_quaternion()
            position = evaluated.translation.copy()
            if previous_root_rotation is not None:
                dot = abs(sum(rotation[index] * previous_root_rotation[index] for index in range(4)))
                angle = 2.0 * math.acos(max(-1.0, min(1.0, dot)))
                root_rates.append((frame, math.degrees(angle)))
                root_speeds.append((frame, float((position - previous_root_position).length)))
            previous_root_rotation = rotation
            previous_root_position = position
            root_positions.append((frame, position.copy()))

    def audit_rates(name, rates, flag_degrees=3.0):
        if not rates:
            print(name, "NO_MOTION_DATA")
            return None, 0
        worst = max(rates, key=lambda item: item[1])
        print(name, "MAX_RATE_DEG_PER_FRAME %.2f at frame %d" % (worst[1], worst[0]))
        fast = [frame for frame, rate in rates if rate > flag_degrees]
        print(name, "FAST_TURN_FRAMES(%s):" % flag_degrees, fast[:40])
        shake = []
        for index in range(2, len(rates)):
            _, r0 = rates[index - 2]
            _, r1 = rates[index - 1]
            _, r2 = rates[index]
            if (r1 - r0) * (r2 - r1) < 0 and r1 > 1.0:
                shake.append(rates[index][0])
        print(name, "SHAKE_CANDIDATE_FRAMES:", shake[:40], "count", len(shake))
        return worst, len(shake)

    camera_worst, _camera_shake_count = audit_rates("CAMERA", camera_rates)
    body_worst, body_shake_count = audit_rates("BODY", root_rates)
    if camera_worst is not None and camera_worst[1] > 8.0:
        failures.append(
            "TPS camera rotates %.2f degrees in one frame at frame %d"
            % (camera_worst[1], camera_worst[0])
        )
    if root_speeds:
        max_speed_frame, max_speed = max(root_speeds, key=lambda item: item[1])
        print("BODY_MAX_SPEED_M_PER_FRAME %.3f at frame %d" % (max_speed, max_speed_frame))
        for second in (23.0, 30.0):
            centre = int(round(second * float(scene.render.fps)))
            window = [speed for frame, speed in root_speeds if abs(frame - centre) <= 12]
            average = sum(window) / float(len(window)) if window else 0.0
            stopped = sum(1 for speed in window if speed < 0.005)
            print(
                "TPS_NO_STOP_WINDOW", second,
                "average_m_per_frame=%.4f" % average,
                "stopped_frames", stopped,
            )
            if not window or average < 0.025 or stopped > 2:
                failures.append(
                    "TPS has an unexplained stop near %.0fs (avg %.4fm/frame, %d stopped frames)"
                    % (second, average, stopped)
                )
    if body_worst is not None and body_worst[1] > 8.05:
        failures.append(
            "TPS body rotates %.2f degrees in one frame at frame %d"
            % (body_worst[1], body_worst[0])
        )

    if report:
        placement_ids = report.get("tps_referenced_fixed_placement_ids") or []
        placement_count = int(report.get("tps_referenced_fixed_placement_count", len(placement_ids)))
        print("TPS_REFERENCED_FIXED_PLACEMENT_COUNT", placement_count)
        print("TPS_REFERENCED_FIXED_PLACEMENT_IDS", placement_ids)
        if placement_count != 14:
            failures.append("TPS route references %d fixed assets (expected exactly 14)" % placement_count)

        combat = report.get("tps_combat") or {}
        aim_policy = str(combat.get("player_aim_alignment_policy", ""))
        print("TPS_PLAYER_AIM_ALIGNMENT_POLICY", aim_policy)
        if aim_policy != "evaluated_final_scar_barrel_to_target":
            failures.append("TPS player aim was not solved from the final evaluated SCAR-H barrel")

        locomotion = combat.get("player_action_schedule") or {}
        print("TPS_CONTINUOUS_LOCOMOTION_STRIPS_MERGED", locomotion.get("continuous_locomotion_strips_merged"))
        print("TPS_STATIONARY_HOLDS_USE_ARMED_IDLE", locomotion.get("stationary_holds_use_armed_idle"))
        print("TPS_MINIMUM_FRACTIONAL_LOCOMOTION_REPEAT", locomotion.get("minimum_fractional_locomotion_repeat"))
        if not locomotion.get("continuous_locomotion_strips_merged"):
            failures.append("TPS consecutive locomotion strips were not merged")
        if not locomotion.get("stationary_holds_use_armed_idle"):
            failures.append("TPS stationary holds still use a moving foot cycle")
        if locomotion.get("missing"):
            failures.append("TPS player action library is missing: %s" % locomotion.get("missing"))
        if not locomotion.get("moving_combat_uses_locomotion_base"):
            failures.append("TPS moving combat still replaces the leg locomotion action")
        if int(report.get("ground_event_hold_count", -1)) != 0:
            failures.append("TPS still contains artificial event holds")
        unresolved_route_frames = report.get("ground_route_unresolved_frames") or []
        print("TPS_GROUND_ROUTE_UNRESOLVED_FRAMES", unresolved_route_frames)
        if int(report.get("ground_route_unresolved_count", -1)) != 0:
            failures.append("TPS route contains unresolved physical collision samples")
        relay_standoff = next(
            (
                item for item in (report.get("ground_interaction_standoffs") or [])
                if str(item.get("placement_id")) == "placement_001"
            ),
            None,
        )
        relay_clearance = float(relay_standoff.get("standoff_m", 0.0)) if relay_standoff else 0.0
        print("TPS_RELAY_STANDOFF_M", "%.3f" % relay_clearance)
        if relay_clearance < 2.0:
            failures.append("TPS relay standoff %.3fm is too small for the relay footprint" % relay_clearance)

    # ---- Spec 3 / 4.4: combat timing, visibility and validation artifacts ----
    hidden_tree_names = []
    if camera is not None:
        try:
            hidden_tree_names = json.loads(
                str(camera.get("code2games_camera_hidden_tree_names", "[]"))
            )
        except Exception:
            hidden_tree_names = []
    validation = {
        "timeline": {
            "frame_start": int(scene.frame_start),
            "frame_end": int(scene.frame_end),
            "fps": int(scene.render.fps),
            "duration_seconds": round(float(duration_seconds), 4),
        },
        "route": {
            "referenced_fixed_placement_ids": [],
            "coverage_count": 0,
            "opening_reverse_step_count": 0,
            "opening_detour_count": 0,
            "asset_min_distances_m": {},
        },
        "motion": {
            "max_speed_m_per_frame": 0.0,
            "max_body_turn_deg_per_frame": 0.0,
            "alternating_turn_pair_count": 0,
            "continuous_locomotion_strips_merged": True,
            "stationary_holds_use_armed_idle": True,
        },
        "camera": {
            "max_turn_deg_per_frame": 0.0,
            "unresolved_occlusion_count": 0,
            "frustum_ray_count": 0,
            "frustum_unresolved_count": 0,
            "hidden_tree_names": [],
        },
        "combat": {"encounters": []},
        "ok": False,
    }
    if camera_worst is not None:
        validation["camera"]["max_turn_deg_per_frame"] = round(float(camera_worst[1]), 4)
    validation["camera"]["unresolved_occlusion_count"] = max(0, int(camera_unresolved))
    validation["camera"]["frustum_ray_count"] = (
        max(0, int(camera.get("code2games_camera_frustum_ray_count", 0)))
        if camera is not None else 0
    )
    validation["camera"]["frustum_unresolved_count"] = (
        max(0, int(camera.get("code2games_camera_frustum_unresolved_count", 0)))
        if camera is not None else 0
    )
    validation["camera"]["hidden_tree_names"] = sorted(str(name) for name in hidden_tree_names)
    targeted_camera_clearance = {}
    if camera is not None and actor_root is not None:
        for second in (16, 26):
            frame = int(second * scene.render.fps)
            scene.frame_set(frame)
            depsgraph = bpy.context.evaluated_depsgraph_get()
            bpy.context.view_layer.update()
            camera_position = camera.evaluated_get(depsgraph).matrix_world.translation
            actor_position = actor_root.evaluated_get(depsgraph).matrix_world.translation
            clear = los_clear(
                scene,
                depsgraph,
                camera_position,
                actor_position + Vector((0.0, 0.0, 1.25)),
                ignore_prefixes=("C2G_TPS_PLAYER",),
                ignore_names=set(hidden_tree_names),
            )
            targeted_camera_clearance[str(second)] = bool(clear)
            print("TPS_TARGETED_CAMERA_CLEAR", second, clear)
            if not clear:
                failures.append("TPS camera view is obstructed at %ds" % second)
    validation["camera"]["targeted_clearance_seconds"] = targeted_camera_clearance
    if body_worst is not None:
        validation["motion"]["max_body_turn_deg_per_frame"] = round(float(body_worst[1]), 4)
    if root_speeds:
        _max_speed_frame, max_speed = max(root_speeds, key=lambda item: item[1])
        validation["motion"]["max_speed_m_per_frame"] = round(float(max_speed), 4)
    validation["motion"]["alternating_turn_pair_count"] = int(body_shake_count)

    opening_positions = [position for frame, position in root_positions if frame <= 40]
    if len(opening_positions) >= 3:
        main_direction = opening_positions[-1] - opening_positions[0]
        main_direction.z = 0.0
        if main_direction.length > 1e-5:
            main_direction.normalize()
            reverse_steps = 0
            for previous, current in zip(opening_positions, opening_positions[1:]):
                step = current - previous
                step.z = 0.0
                if step.length > 1e-5 and step.dot(main_direction) < 0.0:
                    reverse_steps += 1
            validation["route"]["opening_reverse_step_count"] = int(reverse_steps)

    combat = (report.get("tps_combat") or {}) if report else {}
    if report:
        validation["route"]["referenced_fixed_placement_ids"] = list(placement_ids)
        validation["route"]["coverage_count"] = int(placement_count)
        detours = report.get("ground_route_detours") or []
        validation["route"]["opening_detour_count"] = int(
            sum(1 for item in detours if int(item.get("frame", 999999)) <= 72)
        )
        min_distances = {}
        for placement_id in placement_ids:
            placement_root = next(
                (
                    obj for obj in bpy.data.objects
                    if obj.get("code2games_placement_id") == placement_id
                ),
                None,
            )
            if placement_root is None:
                continue
            anchor = placement_root.matrix_world.translation
            distances = [
                float((anchor - player_position).length)
                for _frame, player_position in root_positions[::4]
            ]
            if distances:
                min_distances[placement_id] = round(min(distances), 3)
        validation["route"]["asset_min_distances_m"] = min_distances

    locomotion = combat.get("player_action_schedule") or {}
    validation["motion"]["continuous_locomotion_strips_merged"] = bool(
        locomotion.get("continuous_locomotion_strips_merged", True)
    )
    validation["motion"]["stationary_holds_use_armed_idle"] = bool(
        locomotion.get("stationary_holds_use_armed_idle", True)
    )

    encounters = combat.get("encounters") or []
    print("COMBAT_ENCOUNTER_COUNT", len(encounters))
    if len(encounters) != 5:
        failures.append("TPS realizes %d combat encounters (expected exactly 5)" % len(encounters))
    ballistic_frame = int(combat.get("ballistic_check_frame", -1))
    print("TPS_BALLISTIC_CHECK_FRAME", ballistic_frame)
    if ballistic_frame != 264:
        failures.append("TPS ballistic approval frame is %d (expected 264)" % ballistic_frame)
    frame_264_tracers = [
        obj for obj in bpy.data.objects
        if str(obj.get("code2games_shooter", "")) == "tps_player"
        and int(obj.get("code2games_shot_frame", -1)) == 264
    ]
    print("TPS_FRAME_264_PLAYER_TRACERS", [obj.name for obj in frame_264_tracers])
    if not frame_264_tracers:
        failures.append("TPS has no player muzzle tracer at approval frame 264")
    weapon = (
        bpy.data.objects.get(str(actor_root.get("code2games_actor_weapon", "")))
        if actor_root is not None else None
    )
    ballistic_angles = []
    for tracer in [
        obj for obj in bpy.data.objects
        if str(obj.get("code2games_shooter", "")) == "tps_player"
    ]:
        frame = int(tracer.get("code2games_shot_frame", -1))
        origin = Vector(tracer.get("code2games_shot_origin", (0.0, 0.0, 0.0)))
        target = Vector(tracer.get("code2games_shot_target", (0.0, 0.0, 0.0)))
        shot_direction = target - origin
        shot_direction.z = 0.0
        if weapon is None or frame < 1 or shot_direction.length < 1e-5:
            continue
        scene.frame_set(frame)
        depsgraph = bpy.context.evaluated_depsgraph_get()
        bpy.context.view_layer.update()
        barrel = (
            weapon.evaluated_get(depsgraph).matrix_world.to_3x3()
            @ Vector((-1.0, 0.0, 0.0))
        )
        barrel.z = 0.0
        if barrel.length < 1e-5:
            continue
        angle = math.degrees(math.acos(max(-1.0, min(1.0, barrel.normalized().dot(shot_direction.normalized())))))
        ballistic_angles.append((frame, tracer.name, float(angle)))
    maximum_ballistic_angle = max((item[2] for item in ballistic_angles), default=180.0)
    print("TPS_MAX_BARREL_TRACER_ANGLE_DEG", "%.3f" % maximum_ballistic_angle)
    if maximum_ballistic_angle > 3.0:
        failures.append(
            "TPS final evaluated barrel/tracer angle is %.3f degrees (maximum 3)"
            % maximum_ballistic_angle
        )
    validation["combat"]["maximum_barrel_tracer_angle_degrees"] = round(maximum_ballistic_angle, 5)
    validation["combat"]["ballistic_samples"] = [
        {"frame": frame, "tracer": name, "angle_degrees": round(angle, 5)}
        for frame, name, angle in ballistic_angles
    ]
    actor_prefixes = (actor_root.name,) if actor_root is not None else ()
    player_hierarchy_names = set()
    if actor_root is not None:
        pending = [actor_root]
        while pending:
            obj = pending.pop()
            player_hierarchy_names.add(str(obj.name))
            pending.extend(list(obj.children))
    for encounter in encounters:
        enemy_name = str(encounter.get("enemy", ""))
        fv = int(encounter.get("first_visible_frame", -1))
        ea = int(encounter.get("enemy_attack_start_frame", -1))
        pa = int(encounter.get("player_attack_start_frame", -1))
        ph = int(encounter.get("first_player_hit_frame", -1))
        ds = int(encounter.get("enemy_death_start_frame", -1))
        enemy_attack_ok = 0 <= (ea - fv) <= 4
        player_attack_ok = 0 <= (pa - ea) <= 48
        hit_ok = 1 <= (ph - pa) <= 4
        death_ok = ds > ph
        aim_alignment = encounter.get("aim_alignment") or {}
        aim_residual = float(aim_alignment.get("residual_degrees", 180.0))
        aim_ok = bool(aim_alignment.get("verified")) and aim_residual <= 3.0
        verified_visible = False
        if fv >= 1 and camera is not None:
            scene.frame_set(fv)
            depsgraph = bpy.context.evaluated_depsgraph_get()
            bpy.context.view_layer.update()
            enemy_root = next(
                (obj for obj in enemy_roots if obj.name == "C2G_%s_ROOT" % enemy_name),
                None,
            )
            if enemy_root is not None:
                chest = (
                    enemy_root.evaluated_get(depsgraph).matrix_world.translation
                    + Vector((0.0, 0.0, 1.5))
                )
                projected = project_into_camera(scene, depsgraph, camera, chest)
                if projected is not None and projected[2] and actor_root is not None:
                    origin = (
                        actor_root.evaluated_get(depsgraph).matrix_world.translation
                        + Vector((0.0, 0.0, 1.35))
                    )
                    verified_visible = los_clear(
                        scene,
                        depsgraph,
                        origin,
                        chest,
                        ignore_prefixes=("C2G_TPS_HOSTILE_",) + actor_prefixes,
                        ignore_names=set(hidden_tree_names) | player_hierarchy_names,
                    )
        if not enemy_attack_ok:
            failures.append(
                "COMBAT %s: enemy attack %d not within 0-4 frames of first visible %d"
                % (enemy_name, ea, fv)
            )
        if not player_attack_ok:
            failures.append(
                "COMBAT %s: player attack %d not within 0-48 frames of enemy attack %d"
                % (enemy_name, pa, ea)
            )
        if not hit_ok:
            failures.append(
                "COMBAT %s: first player hit %d not 1-4 frames after player attack %d"
                % (enemy_name, ph, pa)
            )
        if not death_ok:
            failures.append(
                "COMBAT %s: death %d not after first hit %d" % (enemy_name, ds, ph)
            )
        if not aim_ok:
            failures.append(
                "COMBAT %s: final SCAR-H/tracer residual is %.3f degrees (maximum 3)"
                % (enemy_name, aim_residual)
            )
        if not verified_visible:
            failures.append(
                "COMBAT %s: not verifiably visible at first_visible_frame %d"
                % (enemy_name, fv)
            )
        encounter_ok = (
            enemy_attack_ok
            and player_attack_ok
            and hit_ok
            and death_ok
            and verified_visible
            and aim_ok
        )
        validation["combat"]["encounters"].append({
            "enemy": enemy_name,
            "combat_beat_frame": encounter.get("combat_beat_frame"),
            "first_visible_frame": fv,
            "enemy_attack_start_frame": ea,
            "player_attack_start_frame": pa,
            "first_player_hit_frame": ph,
            "enemy_death_start_frame": ds,
            "enemy_fire_frames": encounter.get("enemy_fire_frames"),
            "player_fire_frames": encounter.get("player_fire_frames"),
            "visibility_fallback": bool(encounter.get("visibility_fallback")),
            "verified_visible_at_first_visible": verified_visible,
            "enemy_attack_window_ok": enemy_attack_ok,
            "player_attack_window_ok": player_attack_ok,
            "hit_window_ok": hit_ok,
            "death_after_hit_ok": death_ok,
            "aim_alignment": aim_alignment,
            "aim_alignment_ok": aim_ok,
            "ok": encounter_ok,
        })
        print(
            "COMBAT_ENCOUNTER",
            enemy_name,
            "visible", fv,
            "enemy_attack", ea,
            "player_attack", pa,
            "first_hit", ph,
            "death", ds,
            "aim_residual", round(aim_residual, 3),
            "ok", encounter_ok,
        )

    if encounters:
        alpha = encounters[0]
        if int(alpha.get("enemy_attack_start_frame", -1)) != 240:
            failures.append("Alpha attacks at frame %s (required exactly 240)" % alpha.get("enemy_attack_start_frame"))
        if int(alpha.get("player_attack_start_frame", -1)) != 264:
            failures.append("Player returns fire at frame %s (required exactly 264)" % alpha.get("player_attack_start_frame"))

    validation["ok"] = not failures
    validation_output = args.validation_output or (
        os.path.join(os.path.dirname(args.report), "tps_final_validation.json")
        if args.report else ""
    )
    if validation_output:
        with open(validation_output, "w", encoding="utf-8") as handle:
            json.dump(validation, handle, ensure_ascii=False, indent=2)
        print("VALIDATION_JSON", validation_output)
        write_validation_markdown(
            os.path.splitext(validation_output)[0] + ".md",
            validation,
            blend_path,
            args.report,
        )

    if failures:
        for failure in failures:
            print("VERIFY_FAILURE", failure)
        raise RuntimeError("TPS verification failed: %s" % "; ".join(failures))

    print("VERIFY_DONE")


if __name__ == "__main__":
    main()
