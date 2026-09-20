"""Build and stage the 52-second F-104 V8 second-mountain showcase.

The dedicated wrapper owns the summit systems, continuous launch, terrain and
forest sweeps, causal flight-cell collection and a low summit finish.  It
composes the existing stage-11/12 implementations.  V8 applies one audited
Stage-6 placement revision before Director staging; Director itself never
moves the newly confirmed fixed transforms.
"""

import argparse
import json
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
    parser = argparse.ArgumentParser(description="Stage the curated F-104 showcase")
    parser.add_argument("--placement_plan", default="./output/game_staging/game_wingsuit/asset_placement_v8_gameplay/gameplay_placement_plan.v8_confirmed.json")
    parser.add_argument("--scene_blend", default="./output/game_staging/game_wingsuit/staged_scene_v6_verified_fix6.blend")
    parser.add_argument("--source_showcase", default="", help="open a prior showcase, strip Director objects, then rebuild without overwriting it")
    parser.add_argument("--director_path", default="./output/game_staging/game_wingsuit/director_path_showcase_v8/director_path.json")
    parser.add_argument("--aircraft_asset", default="./assets/gameplay/wingsuit_aircraft/final/f104_starfighter_game_ready.glb")
    parser.add_argument("--aircraft_target_length", type=float, default=16.7)
    parser.add_argument("--output_dir", default="./output/game_staging/game_wingsuit/director_gameplay_aircraft_showcase_v8")
    parser.add_argument("--output_blend", default="./output/game_staging/game_wingsuit/staged_scene_v6_aircraft_showcase_v8.blend")
    parser.add_argument("--preflight_frames", type=int, default=72)
    parser.add_argument("--flight_frames", type=int, default=1116)
    parser.add_argument("--pullup_frames", type=int, default=60)
    parser.add_argument("--render_previews", action="store_true")
    parser.add_argument("--render_video", action="store_true")
    parser.add_argument("--preview_samples", type=int, default=24)
    parser.add_argument("--video_samples", type=int, default=16)
    parser.add_argument("--resolution_x", type=int, default=960)
    parser.add_argument("--resolution_y", type=int, default=540)
    return parser.parse_args(script_args())


def reject_temporary_output(path):
    normalized = os.path.abspath(path).replace("\\", "/").lower()
    if normalized == "/tmp" or normalized.startswith("/tmp/"):
        raise ValueError("showcase outputs must not use /tmp: %s" % path)


V8_REVISED_PLACEMENT_IDS = {
    "placement_001", "placement_002", "placement_003",
    "placement_012", "placement_013", "placement_019", "placement_020", "placement_023",
}


def apply_confirmed_gameplay_revision(plan_path):
    """Apply only the eight authorized V8 Stage-6 transform deltas to V4.

    Asset roots and their unparented gameplay proxies both carry the placement
    id.  Moving only top-level tagged objects avoids applying the same delta to
    children that already inherit an asset-root transform.
    """
    with open(plan_path, encoding="utf-8") as handle:
        plan = json.load(handle)
    elements = plan.get("placement_plan", {}).get("elements") or []
    authorized = {
        str(element.get("placement_id")): element
        for element in elements
        if element.get("director_relocation_authorized")
    }
    if set(authorized) != V8_REVISED_PLACEMENT_IDS:
        raise RuntimeError(
            "V8 placement plan must authorize exactly %s, got %s"
            % (sorted(V8_REVISED_PLACEMENT_IDS), sorted(authorized))
        )

    audit = []
    for placement_id in sorted(V8_REVISED_PLACEMENT_IDS):
        element = authorized[placement_id]
        old_xyz = Vector(element.get("relocated_from_world_xyz") or ())
        new_xyz = Vector(element.get("world_xyz") or ())
        if len(old_xyz) != 3 or len(new_xyz) != 3:
            raise RuntimeError("invalid V8 relocation coordinates: %s" % placement_id)
        delta = new_xyz - old_xyz
        objects = []
        for obj in bpy.data.objects:
            if str(obj.get("code2games_placement_id") or "") != placement_id:
                continue
            parent_id = str(obj.parent.get("code2games_placement_id") or "") if obj.parent else ""
            if parent_id == placement_id:
                continue
            objects.append(obj)
        if not objects:
            raise RuntimeError("V4 has no movable root/proxy for %s" % placement_id)
        nearest_old_xy = min(
            (Vector(obj.matrix_world.translation).xy - old_xyz.xy).length
            for obj in objects
        )
        if nearest_old_xy > 0.5:
            raise RuntimeError(
                "%s is not at its recorded pre-V8 XY (nearest %.3fm); refusing a double relocation"
                % (placement_id, nearest_old_xy)
            )
        before = {}
        for obj in objects:
            before[obj.name] = [round(float(value), 6) for value in obj.matrix_world.translation]
            matrix = obj.matrix_world.copy()
            matrix.translation = matrix.translation + delta
            obj.matrix_world = matrix
            obj["code2games_placement_revision"] = "wingsuit_v8_collectibles_and_second_summit"
        audit.append({
            "placement_id": placement_id,
            "from_world_xyz": [float(value) for value in old_xyz],
            "to_world_xyz": [float(value) for value in new_xyz],
            "delta_world_xyz": [round(float(value), 6) for value in delta],
            "moved_top_level_objects": sorted(before),
            "object_positions_before": before,
        })
        print(
            "V8_GAMEPLAY_RELOCATED", placement_id,
            "OBJECTS", len(objects), "DELTA", [round(float(value), 6) for value in delta],
        )
    bpy.context.view_layer.update()
    bpy.context.scene["code2games_wingsuit_v8_gameplay_relocation_count"] = len(audit)
    return audit


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


def write_wingsuit_hud(report_path, output_dir):
    """Write a restrained HUD with one causal four-cell progression."""
    report = json.load(open(report_path, encoding="utf-8"))
    events = report.get("wingsuit_event_hud") or []
    labels_by_placement = {
        "placement_000": "FLIGHT CELLS 1/4 // IGNITION",
        "placement_001": "FLIGHT CELLS 2/4 // POWER +16%",
        "placement_002": "FLIGHT CELLS 3/4 // POWER +32%",
        "placement_003": "FLIGHT CELLS 4/4 // SUMMIT UNLOCKED",
        "placement_019": "SECOND SUMMIT // WINDBREAK PASS",
        "placement_020": "SECOND SUMMIT // RECOVERY ZONE",
        "placement_022": "LAUNCH // AFTERBURNER",
        "placement_018": "PRESSURE FRONT // BANK CLEAR",
        "placement_017": "CLIFF IMPACT // EVADE",
        "placement_016": "ROCKFALL // TERRAIN SWEEP",
        "placement_023": "SECOND SUMMIT // HOMING LOCK",
        "placement_013": "SECOND SUMMIT // EXTRACTION DECK",
        "placement_012": "SECOND SUMMIT REACHED // COMPLETE",
    }
    lines = []
    for event in events:
        event_type = str(event.get("event_type", ""))
        placement_id = str(event.get("placement_id", ""))
        text = labels_by_placement.get(placement_id, event_type.replace("_", " ").upper())
        start_seconds = max(0.0, float(event.get("seconds", 0.0)) - 0.10)
        end_seconds = float(event.get("seconds", 0.0)) + 0.90
        lines.append(
            "Dialogue: 0,%s,%s,WingsuitHud,,0,0,0,,%s"
            % (_ass_time(start_seconds), _ass_time(end_seconds), text)
        )
    fps = 24.0
    duration = float(report.get("frame_end", 1248)) / fps
    lines.insert(
        0,
        "Dialogue: 0,0:00:00.00,%s,WingsuitMission,,0,0,0,,F-104 // STORMLINE RESCUE\\NTERRAIN LINK ACTIVE"
        % _ass_time(duration),
    )
    ass_path = os.path.join(output_dir, "wingsuit_hud.ass")
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
            "Style: WingsuitHud,Arial,28,&H00FFFFFF,&H00FFFFFF,"
            "&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,2,0,7,20,20,30,1\n"
            "Style: WingsuitMission,Arial,20,&H00E8F2FF,&H00FFFFFF,"
            "&H00000000,&H60000000,1,0,0,0,100,100,0,0,1,2,0,9,20,20,24,1\n"
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
    reject_temporary_output(args.director_path)
    reject_temporary_output(args.output_dir)
    reject_temporary_output(args.output_blend)
    os.environ["CODE2GAMES_DEMO_NAME"] = "game_wingsuit"

    if args.source_showcase:
        if not os.path.isfile(args.source_showcase):
            raise FileNotFoundError("source showcase blend not found: %s" % args.source_showcase)
        if os.path.abspath(args.output_blend) == os.path.abspath(args.source_showcase):
            raise ValueError("Wingsuit V8 output must not overwrite its V4 source")
        last_error = None
        for attempt in range(6):
            try:
                bpy.ops.wm.open_mainfile(filepath=args.source_showcase)
                break
            except RuntimeError as exc:
                last_error = exc
                print("SOURCE_OPEN_RETRY", attempt + 1, args.source_showcase, flush=True)
        else:
            raise RuntimeError("source showcase unreadable after retries: %s (%s)" % (args.source_showcase, last_error))
        targets = [
            obj for obj in bpy.data.objects
            if obj.get("code2games_director_owned")
            or obj.get("code2games_aircraft")
            or obj.get("code2games_camera_system")
        ]
        for obj in targets:
            bpy.data.objects.remove(obj, do_unlink=True)
        for key in [key for key in list(bpy.context.scene.keys()) if str(key).startswith("code2games_")]:
            del bpy.context.scene[key]
        bpy.context.view_layer.update()
        print("SOURCE_STRIPPED", len(targets), bpy.data.filepath)

    relocation_audit = apply_confirmed_gameplay_revision(args.placement_plan)

    sys.argv = [
        "build_genre_director_path.py",
        "--genre", "wingsuit",
        "--placement_plan", args.placement_plan,
        "--output", args.director_path,
    ]
    path_builder.main()

    stage_args = [
        "stage_genre_director_gameplay.py", "--",
        "--scene_blend", "" if args.source_showcase else args.scene_blend,
        "--director_path", args.director_path,
        "--wingsuit_aircraft_asset", args.aircraft_asset,
        "--aircraft_target_length", str(args.aircraft_target_length),
        "--output_dir", args.output_dir,
        "--output_blend", args.output_blend,
        "--preview_samples", str(args.preview_samples),
        "--video_samples", str(args.video_samples),
        "--resolution_x", str(args.resolution_x),
        "--resolution_y", str(args.resolution_y),
        # V8 timing: 3 s launch + 46.5 s circuit/low summit pass + 2.5 s
        # shallow exit = 1248 frames / 52 s at 24 fps.
        "--wingsuit_parked_back_m", "8.0",
        "--wingsuit_preflight_frames", str(args.preflight_frames),
        "--wingsuit_flight_frames", str(args.flight_frames),
        "--wingsuit_pullup_frames", str(args.pullup_frames),
    ]
    if args.render_previews:
        stage_args.append("--render_previews")
    if args.render_video:
        stage_args.append("--render_video")
    sys.argv = stage_args
    director.main()
    report_path = os.path.join(args.output_dir, "director_gameplay_report.json")
    if os.path.isfile(report_path):
        report = director.load_json(report_path)
        report["wingsuit_gameplay_relocation"] = {
            "placement_revision": "wingsuit_v8_collectibles_and_second_summit",
            "moved_placement_count": len(relocation_audit),
            "moved_placement_ids": sorted(item["placement_id"] for item in relocation_audit),
            "unlisted_placements_unchanged": True,
            "director_fixed_asset_positions_changed": False,
            "items": relocation_audit,
        }
        director.write_json(report_path, report)
        ass_path, event_count = write_wingsuit_hud(report_path, args.output_dir)
        print("WINGSUIT_HUD_ASS", ass_path, "EVENTS", event_count)


if __name__ == "__main__":
    main()
