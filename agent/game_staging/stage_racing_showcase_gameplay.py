"""Build the curated Racing path and stage its presentation-ready gameplay.

This is intentionally scene-specific.  It is the deterministic showcase layer
above the generic Code2Games stages 11 and 12: fixed placement/asset plans stay
read-only, while the car route, interactions and camera are authored for the
known racing valley rather than re-planned during rendering.
"""

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
    parser = argparse.ArgumentParser(description="Stage the curated Racing showcase")
    parser.add_argument("--placement_plan", default="./output/game_staging/game_racing/asset_placement_v6_route_fix4/gameplay_placement_plan.refined.json")
    parser.add_argument("--scene_blend", default="./output/game_staging/game_racing/staged_scene_v6_route_fix4.blend")
    parser.add_argument("--source_showcase", default="", help="open a showcase blend, strip Director objects, then stage in-process")
    parser.add_argument("--director_path", default="./output/game_staging/game_racing/director_path_showcase_v3/director_path.json")
    parser.add_argument("--vehicle_asset", default="./assets/gameplay/racing_vehicle/final/arkham_batmobile_game_ready.glb")
    parser.add_argument("--output_dir", default="./output/game_staging/game_racing/director_gameplay_showcase_v3")
    parser.add_argument("--output_blend", default="./output/game_staging/game_racing/staged_scene_v6_racing_showcase_v3.blend")
    parser.add_argument("--render_previews", action="store_true")
    parser.add_argument("--render_video", action="store_true")
    parser.add_argument("--render_spot_start", type=int, default=0)
    parser.add_argument("--render_spot_seconds", type=float, default=0.0)
    parser.add_argument("--render_spot_output", default="")
    parser.add_argument("--preview_samples", type=int, default=24)
    parser.add_argument("--video_samples", type=int, default=16)
    parser.add_argument("--resolution_x", type=int, default=960)
    parser.add_argument("--resolution_y", type=int, default=540)
    return parser.parse_args(script_args())


def reject_temporary_output(path):
    normalized = os.path.abspath(path).replace("\\", "/").lower()
    if normalized == "/tmp" or normalized.startswith("/tmp/"):
        raise ValueError("showcase outputs must not use /tmp: %s" % path)


def restore_referenced_racing_assets():
    """Undo visibility saved by an older Racing Director showcase.

    ``--source_showcase`` is used only because the clean V6 Stage-10 Blend is
    corrupt.  Earlier Director versions hid several fixed props and persisted
    those flags into the showcase Blend.  Restore only placements referenced
    by the new Stage-11 route; transforms and unrelated scenery stay untouched.
    """
    referenced = set()
    for raw in path_builder.RACING_ROUTE:
        placement_id = raw.get("placement_id") if isinstance(raw, dict) else raw[0]
        if placement_id:
            referenced.add(str(placement_id))
    restored = []
    for root in bpy.data.objects:
        placement_id = root.get("code2games_placement_id")
        if not placement_id or str(placement_id) not in referenced:
            continue
        pending = [root]
        changed = False
        while pending:
            obj = pending.pop()
            pending.extend(list(obj.children))
            if obj.hide_viewport or (obj.type == "MESH" and obj.hide_render):
                changed = True
            obj.hide_viewport = False
            if obj.type == "MESH":
                obj.hide_render = False
        if changed:
            restored.append(str(placement_id))
    print("SOURCE_RACING_ASSETS_RESTORED", len(restored), sorted(restored), flush=True)
    return restored


def _srt_time(seconds):
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    if millis >= 1000:
        millis -= 1000
        secs += 1
    return "%02d:%02d:%02d,%03d" % (hours, minutes, secs, millis)


def write_racing_hud(report_path, output_dir):
    """Write SRT + ASS captions of per-asset HUD labels from the staged report.

    Every gameplay anchor the car passes (boost / timing gate / hazard /
    steer barrier / finish) gets a short top-left caption so the viewer can
    read what the asset does even when the car reaction is subtle.  The ASS
    file bakes the top-left style in, so burning it needs no fragile
    force_style quoting (Alignment=7 is top-left; change to 9 for top-right).
    """
    report = json.load(open(report_path, encoding="utf-8"))
    events = report.get("racing_event_hud") or []
    labels = {
        "boost": "BOOST 加速",
        "checkpoint": "CHECKPOINT 计时门",
        "hazard": "HAZARD 碎石风险",
        "steer": "STEER 转向地标",
        "recover": "SAFE ZONE 安全区",
        "goal": "FINISH 终点",
        "countdown": "START 倒计时",
    }
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

    srt_lines = []
    ass_lines = []
    for index, event in enumerate(events, start=1):
        event_type = str(event.get("event_type", ""))
        text = labels.get(event_type, event_type.upper())
        # Caption appears well before the asset is reached and disappears as
        # the car passes it (QA: caption before the asset, gone once it is
        # behind the car).
        start_seconds = max(0.0, float(event.get("seconds", 0.0)) - 1.5)
        end_seconds = float(event.get("seconds", 0.0)) + 0.5
        srt_lines.append(str(index))
        srt_lines.append("%s --> %s" % (_srt_time(start_seconds), _srt_time(end_seconds)))
        srt_lines.append(text)
        srt_lines.append("")
        ass_lines.append(
            "Dialogue: 0,%s,%s,RacingHud,,0,0,0,,%s"
            % (_ass_time(start_seconds), _ass_time(end_seconds), text)
        )
    # Top-right speed readout, refreshed twice a second.  Stage 12 already
    # supplies the continuous gameplay display curve; never multiply it here
    # because that was what let a post-boost slowdown fall into the 100s.
    speed_samples = report.get("racing_speed_hud") or []
    speed_ass_lines = []
    for sample in speed_samples:
        frame = int(sample.get("frame", 0))
        if frame % 12 != 0:
            continue
        start = max(0.0, float(sample.get("seconds", 0.0)))
        end = start + 0.5
        kmh = float(sample.get("display_speed_kmh", sample.get("speed_kmh", 0.0)))
        speed_ass_lines.append(
            "Dialogue: 0,%s,%s,RacingSpeed,,0,0,0,,SPEED %d km/h"
            % (_ass_time(start), _ass_time(end), int(round(kmh)))
        )
    srt_path = os.path.join(output_dir, "racing_hud.srt")
    with open(srt_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(srt_lines))
    ass_path = os.path.join(output_dir, "racing_hud.ass")
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
            "Style: RacingHud,Noto Sans CJK SC,30,&H00FFFFFF,&H00FFFFFF,"
            "&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,2,0,7,20,20,30,1\n"
            "Style: RacingSpeed,Noto Sans CJK SC,24,&H00FFFFFF,&H00FFFFFF,"
            "&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,2,0,9,20,20,30,1\n"
            "\n"
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
            "MarginV, Effect, Text\n"
            + "\n".join(ass_lines + speed_ass_lines)
            + "\n"
        )
    return ass_path, len(events)


def main():
    args = parse_args()
    reject_temporary_output(args.director_path)
    reject_temporary_output(args.output_dir)
    reject_temporary_output(args.output_blend)
    os.environ["CODE2GAMES_DEMO_NAME"] = "game_racing"

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
        restore_referenced_racing_assets()
        bpy.context.view_layer.update()
        print("SOURCE_STRIPPED", len(targets), bpy.data.filepath)

    sys.argv = [
        "build_genre_director_path.py",
        "--genre", "racing",
        "--placement_plan", args.placement_plan,
        "--output", args.director_path,
    ]
    path_builder.main()

    stage_args = [
        "stage_genre_director_gameplay.py", "--",
        "--scene_blend", "" if args.source_showcase else args.scene_blend,
        "--director_path", args.director_path,
        "--racing_vehicle_asset", args.vehicle_asset,
        "--output_dir", args.output_dir,
        "--output_blend", args.output_blend,
        "--preview_samples", str(args.preview_samples),
        "--video_samples", str(args.video_samples),
        "--resolution_x", str(args.resolution_x),
        "--resolution_y", str(args.resolution_y),
        # Racing-specific tuning lives in this dedicated wrapper: the low
        # rear chase boom, per-asset reaction rings and camera-boom foliage
        # hiding are all configurable here instead of living in the generic
        # director.
        "--racing_camera_back", "13.5",
        "--racing_camera_height", "3.0",
        "--racing_camera_lookahead", "9.0",
        "--racing_camera_lens", "28.0",
        "--racing_prop_reactions",
        "--racing_hide_camera_foliage",
        # The purpose-built ~550 m speedway now earns the 50-second runtime
        # through distance.  Nine metres/second is real vehicle motion, not
        # the 2.54x slow-motion stretch used by the former 203 m grass route.
        "--racing_speed_mps", "9.0",
        "--racing_target_duration_seconds", "50.0",
        "--racing_min_display_speed_kmh", "200.0",
        "--racing_cruise_display_speed_kmh", "240.0",
        "--racing_peak_display_speed_kmh", "420.0",
    ]
    if args.render_previews:
        stage_args.append("--render_previews")
    if args.render_video:
        stage_args.append("--render_video")
    sys.argv = stage_args
    director.main()
    report_path = os.path.join(args.output_dir, "director_gameplay_report.json")
    if os.path.isfile(report_path):
        with open(report_path, encoding="utf-8") as handle:
            staged_report = json.load(handle)
        speedway_layout = staged_report.get("racing_speedway") or {}
        if speedway_layout.get("enabled"):
            layout_path = os.path.join(args.output_dir, "racing_speedway_layout.json")
            with open(layout_path, "w", encoding="utf-8") as handle:
                json.dump(speedway_layout, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            print("RACING_SPEEDWAY_LAYOUT", layout_path, flush=True)
        ass_path, event_count = write_racing_hud(report_path, args.output_dir)
        print("RACING_HUD_ASS", ass_path, "EVENTS", event_count)
    if args.render_spot_start and args.render_spot_output:
        director.render_spot_clip(
            bpy.context.scene,
            args.render_spot_start,
            args.render_spot_seconds,
            args.render_spot_output,
        )


if __name__ == "__main__":
    main()
