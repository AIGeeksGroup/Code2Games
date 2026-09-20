"""Build the direct-objective FPS path and stage the persistent-enemy demo."""

import argparse
import json
import math
import os
import sys

import bpy
from mathutils import Vector

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import build_genre_director_path as path_builder
import stage_genre_director_gameplay as director


def script_args():
    return sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []


def parse_args():
    parser = argparse.ArgumentParser(description="Stage the curated FPS showcase")
    parser.add_argument("--placement_plan", default="./output/game_staging/game_fps/asset_placement_v6_platform_fix5/gameplay_placement_plan.refined.json")
    parser.add_argument("--scene_blend", default="./output/game_staging/game_fps/staged_scene_v6_platform_fix5.blend")
    parser.add_argument("--source_showcase", default="", help="open a showcase blend, strip Director objects, then stage in-process")
    parser.add_argument("--director_path", default="./output/game_staging/game_fps/director_path_showcase_v5/director_path.json")
    parser.add_argument("--swat_library", default="./assets/gameplay/swat_combat/final/swat_combat_library.blend")
    parser.add_argument("--output_dir", default="./output/game_staging/game_fps/director_gameplay_showcase_v5")
    parser.add_argument("--output_blend", default="./output/game_staging/game_fps/staged_scene_v6_fps_showcase_v5.blend")
    parser.add_argument("--combat_actor_scale", type=float, default=1.12)
    parser.add_argument("--render_previews", action="store_true")
    parser.add_argument("--render_spot_start", type=int, default=0)
    parser.add_argument("--render_spot_seconds", type=float, default=0.0)
    parser.add_argument("--render_spot_output", default="")
    parser.add_argument("--render_spot_probe_of", default="")
    parser.add_argument("--render_spot_probe_output", default="")
    return parser.parse_args(script_args())


def reject_temporary_output(path):
    normalized = os.path.abspath(path).replace("\\", "/").lower()
    if normalized == "/tmp" or normalized.startswith("/tmp/"):
        raise ValueError("showcase outputs must not use /tmp: %s" % path)


def _world_bounds(obj):
    points = []
    pending = list(obj.children_recursive)
    pending.append(obj)
    for child in pending:
        if child.type == "MESH":
            points.extend(child.matrix_world @ Vector(corner) for corner in child.bound_box)
    if not points:
        return None
    minimum = Vector((
        min(point.x for point in points),
        min(point.y for point in points),
        min(point.z for point in points),
    ))
    maximum = Vector((
        max(point.x for point in points),
        max(point.y for point in points),
        max(point.z for point in points),
    ))
    return minimum, maximum


def _ground_z_under(x, y, fallback, exclude_hierarchy=None):
    scene = bpy.context.scene
    depsgraph = bpy.context.evaluated_depsgraph_get()
    origin = Vector((float(x), float(y), 400.0))
    direction = Vector((0.0, 0.0, -1.0))
    excluded = set()
    if exclude_hierarchy is not None:
        pending = [exclude_hierarchy]
        while pending:
            obj = pending.pop()
            pending.extend(list(obj.children))
            excluded.add(obj.name)
    for _ in range(48):
        hit, location, _normal, _face, obj, _matrix = scene.ray_cast(
            depsgraph,
            origin,
            direction,
            distance=900.0,
        )
        if not hit:
            break
        if (
            obj
            and obj.type == "MESH"
            and not obj.get("code2games_placement_id")
            and obj.name not in excluded
        ):
            return float(location.z)
        origin = Vector((location.x, location.y, location.z - 0.03))
    return float(fallback)


def post_stage_fps_showcase():
    """FPS-specific fixes applied to the staged blend, in this wrapper only.

    1) Extraction platform: seat its geometry on the ground and lift the
       final route climb onto the platform top (the player walks up the ramp).
    """
    scene = bpy.context.scene
    depsgraph = bpy.context.evaluated_depsgraph_get()
    player = bpy.data.objects.get("C2G_FPS_PLAYER")
    result = {}

    platform = next(
        (
            obj for obj in bpy.data.objects
            if obj.get("code2games_placement_id") == "fps_final_extraction_goal"
        ),
        None,
    )
    if platform is not None:
        bounds = _world_bounds(platform)
        if bounds:
            minimum, maximum = bounds
            corners = [
                (minimum.x, minimum.y),
                (maximum.x, minimum.y),
                (minimum.x, maximum.y),
                (maximum.x, maximum.y),
                ((minimum.x + maximum.x) * 0.5, (minimum.y + maximum.y) * 0.5),
            ]
            terrain_z = [
                _ground_z_under(x, y, minimum.z, exclude_hierarchy=platform)
                for x, y in corners
            ]
            # Seat the lowest side: the platform's own mesh used to be hit by
            # the ground ray (its meshes carry no placement id), so the gap
            # read negative and the platform was never lowered at all.
            gap = float(minimum.z) - min(terrain_z)
            if gap > 0.05:
                lower = gap - 0.01
                for child in list(platform.children):
                    matrix = child.matrix_world.copy()
                    matrix.translation.z -= lower
                    child.matrix_world = matrix
                bpy.context.view_layer.update()
                result["platform_lowered_m"] = round(lower, 3)
                bounds = _world_bounds(platform)
                if bounds:
                    minimum, maximum = bounds
            # Raise the final route climb onto the platform top.
            if player is not None and player.animation_data and player.animation_data.action:
                top_z = float(maximum.z)
                action = player.animation_data.action
                z_curve = next(
                    (curve for curve in action.fcurves if curve.data_path == "location" and curve.array_index == 2),
                    None,
                )
                if z_curve is not None and len(z_curve.keyframe_points) >= 4:
                    points = sorted(z_curve.keyframe_points, key=lambda point: point.co[0])
                    climb = points[-16:]
                    start_z = float(climb[0].co[1])
                    for index, point in enumerate(climb):
                        fraction = (index + 1) / float(len(climb))
                        point.co[1] = start_z + (top_z + 0.05 - start_z) * fraction
                    result["final_climb_raised_to_m"] = round(top_z + 0.05, 3)
        else:
            result["platform_skipped"] = "no mesh bounds"
    else:
        result["platform_skipped"] = "not found"

    # The uplink mast with the radar dish is removed from the showcase (QA:
    # "delete the radar").  Its staging event is already gone; hide the asset
    # itself so the landmark no longer appears beside the route.
    radar = next(
        (
            obj for obj in bpy.data.objects
            if obj.get("code2games_placement_id") == "fps_landmark_uplink_mast"
        ),
        None,
    )
    if radar is not None:
        pending = [radar]
        while pending:
            obj = pending.pop()
            pending.extend(list(obj.children))
            obj.hide_viewport = True
            if obj.type == "MESH":
                obj.hide_render = True
        result["radar_hidden"] = True

    return result


def _ass_time(seconds):
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    centis = int(round((seconds - int(seconds)) * 100))
    if centis >= 100:
        centis -= 100
        secs += 1
    return "%d:%02d:%02d.%02d" % (hours, minutes, secs, centis)


def write_fps_hud(report_path, output_dir, extra_events=None):
    """Write FPS HUD: top-left status, bottom-right player HP, top-right enemy HP."""
    report = json.load(open(report_path, encoding="utf-8"))
    events = report.get("fps_event_hud") or []
    if extra_events:
        events = list(events) + list(extra_events)
    lines = []

    def hp_bar(hp):
        filled = int(round(max(0, min(100, hp)) / 10.0))
        if hp > 0 and filled < 1:
            # The final sliver (~5%) must still read as alive, not empty
            # (QA: "残血跑到终点", never an empty/dead-looking bar).
            filled = 1
        return "█" * filled + "░" * (10 - filled)

    # Top-left: live status (the only subtitle; QA: too many captions before).
    status_labels = {
        "collect": "收集弹药",
        "collect_required": "收集核心",
        "interact": "激活中继",
        "combat": "交火中",
        "goal": "撤离",
    }
    sorted_events = sorted(events, key=lambda event: float(event.get("seconds", 0.0)))
    frame_end_seconds = float(report.get("frame_end", 0)) / 24.0
    for index, event in enumerate(sorted_events):
        event_type = str(event.get("event_type", ""))
        status = status_labels.get(event_type)
        if not status:
            continue
        seconds = float(event.get("seconds", 0.0))
        next_seconds = (
            float(sorted_events[index + 1].get("seconds", 0.0))
            if index + 1 < len(sorted_events)
            else frame_end_seconds
        )
        start_seconds = max(0.0, seconds - 0.5)
        end_seconds = max(start_seconds + 0.5, next_seconds - 0.2)
        lines.append(
            "Dialogue: 0,%s,%s,FpsStatus,,0,0,0,,状态：%s"
            % (_ass_time(start_seconds), _ass_time(end_seconds), status)
        )
        if event_type == "combat":
            reload_start = seconds + 1.4
            reload_end = min(reload_start + 1.2, next_seconds - 0.2)
            if reload_end > reload_start:
                lines.append(
                    "Dialogue: 0,%s,%s,FpsStatus,,0,0,0,,状态：换弹"
                    % (_ass_time(reload_start), _ass_time(reload_end))
                )

    # Bottom-right: player HP -- full, drops live under enemy fire, never
    # recovers.  Damage follows the ENEMIES' fire windows (including the
    # hilltop hostile's harassment from 10 s), not just the kill events.
    player_end = max([float(event.get("seconds", 0.0)) for event in sorted_events] + [0.0]) + 6.0
    fps = 24.0
    combat = report.get("fps_combat") or {}
    schedules = combat.get("enemy_schedules") or []
    # QA: A and B drain only a little so the NPC keeps a healthy reserve;
    # C hammers it down to a sliver that survives to the finish (never heals).
    enemy_damage = {"ALPHA": 15, "BRAVO": 15, "CHARLIE": 65}
    hp_segments = []
    cursor = 0.0
    current_hp = 100
    for schedule in sorted(schedules, key=lambda item: item.get("frame", 0)):
        enemy = str(schedule.get("enemy", ""))
        key = enemy.split("_")[-1]
        damage = enemy_damage.get(key, 40)
        fire_frames = [
            (int(action.get("frame_start", 0)), int(action.get("frame_end", 0)))
            for action in (schedule.get("installed_actions") or [])
            if action.get("action") == "fire"
        ]
        # The hilltop hostile fires a non-damaging suppression burst when he
        # is first seen (~4 s).  Only the 10 s harassment engagement onward
        # drains the player (QA: "third enemy attacks from 10 s"), so the
        # NPC still meets enemy C with the A/B reserve intact.
        if key == "CHARLIE":
            fire_frames = [
                (start, end) for start, end in fire_frames if start >= 240
            ]
        if not fire_frames:
            continue
        total_frames = max(1, sum(end - start for start, end in fire_frames))
        for start_frame, end_frame in fire_frames:
            window_frames = max(1, end_frame - start_frame)
            drop = damage * window_frames / total_frames
            start_s = start_frame / fps
            end_s = end_frame / fps
            if start_s > cursor + 0.05:
                hp_segments.append((cursor, start_s, current_hp))
            target_hp = max(5, current_hp - drop)
            steps = 6
            for step in range(steps):
                t0 = start_s + (end_s - start_s) * step / steps
                t1 = start_s + (end_s - start_s) * (step + 1) / steps
                progress = (step + 1) / steps
                if step == 0:
                    # Front-load the first step so the bar reacts the moment
                    # the hostile opens fire with a clearly visible chunk
                    # (QA: the early drops were one block at a time and read
                    # as "HP only starts dropping at 13 s").
                    progress = 0.55
                else:
                    # Keep the bar monotonic: after the 55% front-load the
                    # remaining 45% continues smoothly to 100% (QA: without
                    # this the second step's progress 1/3 < 0.55 made the
                    # bar visibly jump back UP).
                    progress = 0.55 + 0.45 * step / (steps - 1)
                hp_value = current_hp - (current_hp - target_hp) * progress
                hp_segments.append((t0, t1, round(hp_value)))
            cursor = end_s
            current_hp = target_hp
    if player_end > cursor + 0.05:
        hp_segments.append((cursor, player_end, current_hp))
    previous_end = 0.0
    for start, end, hp in hp_segments:
        start = max(start, previous_end)
        if end <= start:
            continue
        previous_end = end
        if hp > 60:
            color = "00FF00"
        elif hp > 30:
            color = "00FFFF"
        else:
            color = "0000FF"
        lines.append(
            "Dialogue: 0,%s,%s,FpsPlayerHp,,0,0,0,,玩家 {\\1c&H%s&}%s"
            % (_ass_time(start), _ass_time(end), color, hp_bar(hp))
        )

    # Top-right: ONE hostile HP bar at a time (the current target), tiling the
    # timeline A -> B -> C; each bar decays live and turns to 已消灭.
    combat = report.get("fps_combat") or {}
    schedules = combat.get("enemy_schedules") or []
    enemy_letters = {"ALPHA": "A", "BRAVO": "B", "CHARLIE": "C"}
    fps = 24.0
    frame_end = max(1, int(report.get("frame_end", 0)))
    meta = []
    for schedule in schedules:
        enemy = str(schedule.get("enemy", ""))
        letter = enemy_letters.get(enemy.split("_")[-1], enemy)
        combat_frame = int(schedule.get("frame", 0))
        actions = schedule.get("installed_actions") or []
        hit_start = None
        death_end = None
        for action in actions:
            action_name = str(action.get("action", ""))
            frame_start = int(action.get("frame_start", 0))
            action_end = int(action.get("frame_end", 0))
            if action_name == "hit" and hit_start is None:
                hit_start = frame_start
            if action_name == "death":
                death_end = max(death_end or 0, action_end)
        if hit_start is None:
            hit_start = combat_frame + 11
        if death_end is None:
            death_end = hit_start + 78
        # The death ACTION collapses in ~22 frames; the schedule segment's
        # frame_end is the whole scene tail, so cap the visible HP decay to
        # the collapse (QA: the bar should drain fast, not run the full clip).
        death_end = min(death_end, hit_start + 28)
        meta.append({
            "letter": letter,
            "combat_frame": combat_frame,
            "hit_start": hit_start,
            "death_end": death_end,
        })
    meta.sort(key=lambda item: item["combat_frame"])
    previous_death = 0
    for meta_index, item in enumerate(meta):
        window_start = previous_death
        window_end = item["death_end"]
        hit_start = item["hit_start"]
        letter = item["letter"]
        if hit_start > window_start:
            lines.append(
                "Dialogue: 0,%s,%s,FpsEnemyHp,,0,0,40,,{\\1c&H00FF00&}敌人%s %s"
                % (_ass_time(window_start / fps), _ass_time(hit_start / fps), letter, hp_bar(100))
            )
        decay_frames = list(range(hit_start, window_end, 3))
        for frame_index, frame in enumerate(decay_frames):
            next_frame = min(frame + 3, window_end)
            fraction = (frame - hit_start) / max(1.0, float(window_end - hit_start))
            hp = int(round(100.0 * (1.0 - fraction)))
            hp = max(0, min(100, hp))
            if hp > 50:
                color = "00FF00"
            elif hp > 20:
                color = "00FFFF"
            else:
                color = "0000FF"
            lines.append(
                "Dialogue: 0,%s,%s,FpsEnemyHp,,0,0,40,,{\\1c&H%s&}敌人%s %s"
                % (
                    _ass_time(frame / fps),
                    _ass_time(next_frame / fps),
                    color,
                    letter,
                    hp_bar(hp),
                )
            )
        # No 已消灭 label: the bar simply ends, and the next hostile's bar
        # takes the slot (QA: A -> B -> C, one bar at a time).
        previous_death = window_end
    ass_path = os.path.join(output_dir, "fps_hud.ass")
    with open(ass_path, "w", encoding="utf-8") as handle:
        handle.write(
            "[Script Info]\n"
            "ScriptType: v4.00+\n"
            "PlayResX: 960\n"
            "PlayResY: 540\n"
            "\n"
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding\n"
            "Style: FpsStatus,Noto Sans CJK SC,26,&H00FFFFFF,&H00FFFFFF,"
            "&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,2,0,7,20,20,70,1\n"
            "Style: FpsPlayerHp,Noto Sans CJK SC,24,&H00FFFFFF,&H00FFFFFF,"
            "&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,2,0,3,20,20,40,1\n"
            "Style: FpsEnemyHp,Noto Sans CJK SC,24,&H00FFFFFF,&H00FFFFFF,"
            "&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,2,0,9,20,20,40,1\n"
            "\n"
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
            "MarginV, Effect, Text\n"
            + "\n".join(lines)
            + "\n"
        )
    print("FPS_HUD_LINES", len(lines), flush=True)
    return ass_path, len(events)


def main():
    args = parse_args()
    for path in (args.director_path, args.output_dir, args.output_blend):
        reject_temporary_output(path)
    os.environ["CODE2GAMES_DEMO_NAME"] = "game_fps"

    if args.source_showcase:
        if not os.path.isfile(args.source_showcase):
            raise FileNotFoundError("source showcase blend not found: %s" % args.source_showcase)
        if os.path.abspath(args.output_blend) == os.path.abspath(args.source_showcase):
            # Never overwrite the source showcase: the shared mount's saves
            # are unreliable, and a corrupt save over the source destroys the
            # only reusable base scene.
            args.output_blend = args.source_showcase + ".staged"
            print("OUTPUT_BLEND_AVOIDED_SOURCE", args.output_blend)
        last_error = None
        for attempt in range(6):
            try:
                bpy.ops.wm.open_mainfile(filepath=args.source_showcase)
                break
            except RuntimeError as exc:
                last_error = exc
                print("SOURCE_OPEN_RETRY", attempt + 1, args.source_showcase, flush=True)
        else:
            raise RuntimeError(
                "source showcase unreadable after retries: %s (%s)"
                % (args.source_showcase, last_error)
            )
        targets = [
            obj for obj in bpy.data.objects
            if obj.get("code2games_director_owned")
            or obj.get("code2games_racing_vehicle")
            or obj.get("code2games_vehicle_asset")
            or obj.get("code2games_camera_system")
            or obj.get("code2games_swat_actor")
            or obj.get("code2games_fps_enemy")
        ]
        for obj in targets:
            bpy.data.objects.remove(obj, do_unlink=True)
        for key in [key for key in list(bpy.context.scene.keys()) if str(key).startswith("code2games_")]:
            del bpy.context.scene[key]
        bpy.context.view_layer.update()
        print("SOURCE_STRIPPED", len(targets), bpy.data.filepath)
    sys.argv = [
        "build_genre_director_path.py",
        "--genre", "fps",
        "--placement_plan", args.placement_plan,
        "--output", args.director_path,
    ]
    path_builder.main()
    sys.argv = [
        "stage_genre_director_gameplay.py", "--",
        "--scene_blend", "" if args.source_showcase else args.scene_blend,
        "--director_path", args.director_path,
        "--swat_combat_library", args.swat_library,
        "--combat_actor_scale", str(args.combat_actor_scale),
        "--output_dir", args.output_dir,
        "--output_blend", args.output_blend,
        # FPS-specific tuning lives in this dedicated wrapper, not in the
        # generic director: view-model nose-up pitch (keeps the gun in frame
        # with the barrel at the crosshair), camera slope-pitch clamps, event
        # hold frames (only firefights stop the run) and the enemy death pose
        # timing after the kill burst.
        "--fps_viewmodel_pitch_degrees", "5",
        "--fps_viewmodel_scale", "1.0",
        "--fps_viewmodel_offset", "0.42", "0.70", "-0.28",
        "--fps_camera_max_up_pitch_degrees", "26",
        "--fps_camera_max_down_pitch_degrees", "3",
        # Combat holds 44 frames so the fast collapse (death starts ~0.6 s
        # in, fully down ~1.5 s in) is visible before the player moves on.
        "--fps_hold_frames", "3", "3", "4", "44", "10",
        "--enemy_death_start_offset", "14",
        "--fps_walk_speed_mps", "5.5",
        "--fps_smooth_turn_window", "5",
        "--ground_sample_step_frames", "2",
    ] + (["--render_previews"] if args.render_previews else [])
    director.main()
    post_result = post_stage_fps_showcase()
    if post_result:
        print("FPS_POST_STAGE", json.dumps(post_result, ensure_ascii=False), flush=True)
    if any(key in post_result for key in ("platform_lowered_m", "final_climb_raised_to_m")):
        # post_stage moved the platform / player route or added the radar
        # pulse; persist those edits back into the showcase blend.
        # The shared mount rejects a second direct save onto the open file,
        # so write a sibling temp file and atomically replace the target.
        temp_blend = args.output_blend + ".tmp"
        try:
            bpy.ops.wm.save_as_mainfile(filepath=temp_blend)
            os.replace(temp_blend, args.output_blend)
        except Exception as save_error:
            print("FPS_POST_SAVE_SKIPPED", save_error, flush=True)
    report_path = os.path.join(args.output_dir, "director_gameplay_report.json")
    if os.path.isfile(report_path):
        ass_path, event_count = write_fps_hud(report_path, args.output_dir)
        print("FPS_HUD_ASS", ass_path, "EVENTS", event_count)
    if args.render_spot_start and args.render_spot_output:
        director.render_spot_clip(
            bpy.context.scene,
            args.render_spot_start,
            args.render_spot_seconds,
            args.render_spot_output,
            probe_of=args.render_spot_probe_of,
            probe_output=args.render_spot_probe_output,
        )


if __name__ == "__main__":
    main()
