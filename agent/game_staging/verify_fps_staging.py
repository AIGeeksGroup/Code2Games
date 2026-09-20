"""Fast non-rendering verification for the staged FPS showcase blend.

Opens the staged blend and reports, without rendering a single frame:
- scene frame_end / engine / camera
- whether the SCAR view-model and muzzle exist
- where the muzzle projects in the camera frame at probe frames (gun visible?)
- how far each TACTICAL enemy floats above the terrain (straight-down ray)

Run inside Blender:
  blender -b <blend> --python verify_fps_staging.py -- --probe_frames 1 155 199 356 696
"""

import argparse
import math
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


def main():
    parser = argparse.ArgumentParser(description="Verify staged FPS blend without rendering")
    parser.add_argument("--blend", default="", help="optional; defaults to the opened blend")
    parser.add_argument("--probe_frames", type=int, nargs="*", default=[1, 155, 199, 356, 696])
    args = parser.parse_args(script_args())

    blend_path = args.blend or bpy.data.filepath
    if not blend_path:
        raise SystemExit("no blend open and --blend not given")
    bpy.ops.wm.open_mainfile(filepath=blend_path)
    scene = bpy.context.scene
    print("FRAME_END", scene.frame_end, "FPS", scene.render.fps, "ENGINE", scene.render.engine)
    camera = scene.camera
    print("CAMERA", camera.name if camera else None)
    actor_root = next(
        (obj for obj in bpy.data.objects if obj.name == "C2G_FPS_PLAYER"),
        None,
    )
    print("ACTOR_ROOT_FOUND", bool(actor_root))

    weapon = next(
        (obj for obj in bpy.data.objects if obj.name.startswith("C2G_FPS_SCAR_H_VIEWMODEL")),
        None,
    )
    muzzle = next(
        (obj for obj in bpy.data.objects if obj.name.startswith("C2G_FPS_MUZZLE")),
        None,
    )
    print("WEAPON_FOUND", bool(weapon), "MUZZLE_FOUND", bool(muzzle))

    res_x = int(scene.render.resolution_x * scene.render.resolution_percentage / 100.0)
    res_y = int(scene.render.resolution_y * scene.render.resolution_percentage / 100.0)
    focal_mm = camera.data.lens if camera and camera.data else 30.0
    focal_px = float(focal_mm) * res_x / 36.0

    for frame in args.probe_frames:
        scene.frame_set(frame)
        depsgraph = bpy.context.evaluated_depsgraph_get()
        bpy.context.view_layer.update()
        if actor_root is not None:
            root_pos = actor_root.evaluated_get(depsgraph).matrix_world.translation
            print("ROOT_POS", frame, "%.2f %.2f %.2f" % (root_pos.x, root_pos.y, root_pos.z))
        if camera is None or muzzle is None:
            print("PROBE", frame, "camera/muzzle missing")
            continue
        muzzle_world = muzzle.evaluated_get(depsgraph).matrix_world.translation
        rel = camera.matrix_world.inverted() @ muzzle_world
        if rel.z >= -0.05:
            print("PROBE", frame, "MUZZLE_BEHIND_OR_AT_CAMERA rel_z=", round(float(rel.z), 3))
            continue
        depth = -rel.z
        screen_x = res_x / 2.0 + (rel.x / depth) * focal_px
        screen_y = res_y / 2.0 - (rel.y / depth) * focal_px
        inside = 0.0 <= screen_x <= res_x and 0.0 <= screen_y <= res_y
        print(
            "PROBE", frame,
            "muzzle_screen=(%.0f, %.0f) inside=%s" % (screen_x, screen_y, inside),
        )

    roots = [
        obj for obj in bpy.data.objects
        if obj.name.startswith("C2G_TACTICAL_") and obj.name.endswith("_ROOT")
    ]
    for root in roots:
        worst = 0.0
        for frame in args.probe_frames:
            scene.frame_set(frame)
            depsgraph = bpy.context.evaluated_depsgraph_get()
            bpy.context.view_layer.update()
            position = root.evaluated_get(depsgraph).matrix_world.translation
            gap = float(position.z) - ground_z(position.x, position.y, scene, depsgraph)
            worst = max(worst, gap)
        print("ENEMY", root.name, "max_float_gap_m=%.3f" % worst)

    # Motion audit: sample the camera and body rotation every frame and flag
    # fast turns and high-frequency oscillation (shaking) numerically, without
    # rendering anything.
    frames = list(range(max(1, scene.frame_start), scene.frame_end + 1))
    camera_rates = []
    root_rates = []
    previous_camera_rotation = None
    previous_root_rotation = None
    previous_root_position = None
    root_speeds = []
    debug_frames = set([1, 2, 3, 4, 5, 100, 200, 300, 400, 500, 600, 700])
    for frame in frames:
        scene.frame_set(frame)
        depsgraph = bpy.context.evaluated_depsgraph_get()
        bpy.context.view_layer.update()
        if camera is not None:
            rotation = camera.evaluated_get(depsgraph).matrix_world.to_quaternion()
            if previous_camera_rotation is not None:
                angle = rotation.rotation_difference(previous_camera_rotation).angle
                camera_rates.append((frame, math.degrees(angle)))
            previous_camera_rotation = rotation
        if actor_root is not None:
            evaluated = actor_root.evaluated_get(depsgraph).matrix_world
            rotation = evaluated.to_quaternion()
            position = evaluated.translation.copy()
            if previous_root_rotation is not None:
                angle = rotation.rotation_difference(previous_root_rotation).angle
                root_rates.append((frame, math.degrees(angle)))
                speed = float((position - previous_root_position).length)
                root_speeds.append((frame, speed))
                if frame in debug_frames or len(root_speeds) <= 5:
                    print("AUDIT_SPEED", frame, "%.4f" % speed,
                          "pos %.2f %.2f %.2f" % (position.x, position.y, position.z))
            previous_root_rotation = rotation
            previous_root_position = position

    def audit_rates(name, rates, flag_degrees=3.0):
        if not rates:
            print(name, "NO_MOTION_DATA")
            return
        worst = max(rates, key=lambda item: item[1])
        print(name, "MAX_RATE_DEG_PER_FRAME %.2f at frame %d" % (worst[1], worst[0]))
        fast = [frame for frame, rate in rates if rate > flag_degrees]
        print(name, "FAST_TURN_FRAMES(%s):" % flag_degrees, fast[:40])
        # high-frequency oscillation: rate alternates up/down in a small band
        shake = []
        for index in range(2, len(rates)):
            _, r0 = rates[index - 2]
            _, r1 = rates[index - 1]
            _, r2 = rates[index]
            if (r1 - r0) * (r2 - r1) < 0 and r1 > 1.0:
                shake.append(rates[index][0])
        print(name, "SHAKE_CANDIDATE_FRAMES:", shake[:40], "count", len(shake))

    audit_rates("CAMERA", camera_rates)
    audit_rates("BODY", root_rates)
    if root_speeds:
        max_speed_frame, max_speed = max(root_speeds, key=lambda item: item[1])
        print("BODY_MAX_SPEED_M_PER_FRAME %.3f at frame %d" % (max_speed, max_speed_frame))

    # Dump the raw animation keyframes around the worst rotation and speed
    # frames so the actual jumping keys can be inspected.
    if actor_root is not None and actor_root.animation_data and actor_root.animation_data.action:
        action = actor_root.animation_data.action
        worst_rotation_frame = None
        if root_rates:
            worst_rotation_frame = max(root_rates, key=lambda item: item[1])[0]
        for curve in action.fcurves:
            points = list(curve.keyframe_points)
            if not points:
                continue
            frames_here = [int(round(point.co[0])) for point in points]
            if curve.data_path == "rotation_quaternion" and worst_rotation_frame is not None:
                window = [frame for frame in frames_here if abs(frame - worst_rotation_frame) <= 12]
                if window:
                    print(
                        "ROT_KEYS", curve.array_index,
                        [(frame, round(float(points[frames_here.index(frame)].co[1]), 3)) for frame in sorted(window)],
                    )
            if curve.data_path == "location":
                window = [frame for frame in frames_here if abs(frame - max_speed_frame) <= 12]
                if window:
                    print(
                        "LOC_KEYS", curve.array_index,
                        [(frame, round(float(points[frames_here.index(frame)].co[1]), 3)) for frame in sorted(window)],
                    )

    # Path geometry around the worst rotation frame: print the evaluated
    # player positions so a direction reversal / detour can be seen directly.
    if actor_root is not None and worst_rotation_frame is not None:
        print("PATH_DUMP", end=" ")
        for frame in range(max(1, worst_rotation_frame - 20), worst_rotation_frame + 21, 2):
            scene.frame_set(frame)
            depsgraph = bpy.context.evaluated_depsgraph_get()
            bpy.context.view_layer.update()
            pos = actor_root.evaluated_get(depsgraph).matrix_world.translation
            print("%d:(%.1f,%.1f,%.1f)" % (frame, pos.x, pos.y, pos.z), end=" ")
        print()

    print("VERIFY_DONE")


if __name__ == "__main__":
    main()
