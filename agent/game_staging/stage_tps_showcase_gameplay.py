"""Build the direct-objective TPS path and stage its stable spring-arm demo."""

import argparse
import json
import os
import sys
import bpy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import build_genre_director_path as path_builder
import stage_genre_director_gameplay as director


def script_args():
    return sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []


def parse_args():
    parser = argparse.ArgumentParser(description="Stage the curated TPS showcase")
    parser.add_argument("--placement_plan", default="./output/game_staging/game_tps/asset_placement_v6_mountain_platform_fix5/gameplay_placement_plan.refined.json")
    parser.add_argument("--scene_blend", default="./output/game_staging/game_tps/staged_scene_v6_mountain_platform_fix5.blend")
    parser.add_argument("--source_showcase", default="", help="open a showcase blend, strip Director objects, then stage in-process")
    parser.add_argument("--director_path", default="./output/game_staging/game_tps/director_path_showcase_v5/director_path.json")
    parser.add_argument("--swat_library", default="./assets/gameplay/swat_combat/final/swat_combat_library.blend")
    parser.add_argument("--output_dir", default="./output/game_staging/game_tps/director_gameplay_showcase_v5")
    parser.add_argument("--output_blend", default="./output/game_staging/game_tps/staged_scene_v6_tps_showcase_v5.blend")
    parser.add_argument("--combat_actor_scale", type=float, default=1.12)
    parser.add_argument("--render_previews", action="store_true")
    parser.add_argument("--render_spot_start", type=int, default=0)
    parser.add_argument("--render_spot_seconds", type=float, default=0.0)
    parser.add_argument("--render_spot_output", default="")
    return parser.parse_args(script_args())


def reject_temporary_output(path):
    normalized = os.path.abspath(path).replace("\\", "/").lower()
    if normalized == "/tmp" or normalized.startswith("/tmp/"):
        raise ValueError("showcase outputs must not use /tmp: %s" % path)


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


def write_tps_hud(report_path, output_dir):
    """Top-left caption per TPS gameplay event (QA: label the assets)."""
    report = json.load(open(report_path, encoding="utf-8"))
    events = report.get("tps_event_hud") or []
    labels = {
        "collect": "弹药补给",
        "collect_required": "收集核心",
        "interact": "激活中继",
        "combat": "交火",
        "goal": "到达撤离点",
    }
    lines = []
    for event in events:
        event_type = str(event.get("event_type", ""))
        text = labels.get(event_type, event_type.upper())
        start_seconds = max(0.0, float(event.get("seconds", 0.0)) - 1.0)
        end_seconds = float(event.get("seconds", 0.0)) + 1.0
        lines.append(
            "Dialogue: 0,%s,%s,TpsHud,,0,0,0,,%s"
            % (_ass_time(start_seconds), _ass_time(end_seconds), text)
        )
    ass_path = os.path.join(output_dir, "tps_hud.ass")
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
            "Style: TpsHud,Noto Sans CJK SC,30,&H00FFFFFF,&H00FFFFFF,"
            "&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,2,0,7,20,20,30,1\n"
            "\n"
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
            "MarginV, Effect, Text\n"
            + "\n".join(lines)
            + "\n"
        )
    return ass_path, len(events)


def main():
    args = parse_args()
    for path in (args.director_path, args.output_dir, args.output_blend):
        reject_temporary_output(path)
    os.environ["CODE2GAMES_DEMO_NAME"] = "game_tps"

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
        "--genre", "tps",
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
        # TPS-specific tuning lives in this dedicated wrapper, not in the
        # generic director: smooth post-hold body turns, two-frame locomotion
        # blends (stops the opening "crazy shaking"), wider tree detours, and
        # an earlier enemy death pose after the kill.
        "--tps_turn_easing",
        "--tps_always_run",
        "--tps_target_motion_end_frame", "1176",
        "--tps_first_enemy_attack_frame", "240",
        "--tps_enemy_count", "5",
        "--locomotion_blend_frames", "5",
        "--fps_smooth_turn_window", "5",
        "--max_turn_deg_per_frame", "8",
        "--ground_detour_radius", "5.5",
        "--enemy_death_start_offset", "20",
        "--ground_sample_step_frames", "2",
    ] + (["--render_previews"] if args.render_previews else [])
    director.main()
    report_path = os.path.join(args.output_dir, "director_gameplay_report.json")
    if os.path.isfile(report_path):
        ass_path, event_count = write_tps_hud(report_path, args.output_dir)
        print("TPS_HUD_ASS", ass_path, "EVENTS", event_count)
    if args.render_spot_start and args.render_spot_output:
        director.render_spot_clip(
            bpy.context.scene,
            args.render_spot_start,
            args.render_spot_seconds,
            args.render_spot_output,
        )


if __name__ == "__main__":
    main()
