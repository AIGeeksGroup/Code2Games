#!/usr/bin/env bash
set -euo pipefail

ROOT="${CODE2WORLDS_ROOT:-/chanxueyan/licong/Code2Worlds}"
BLENDER="${BLENDER_BIN:-$ROOT/blender-4.2.0-linux-x64/blender}"
PYTHON="${PYTHON_BIN:-python3}"
REQUESTED_GENRE="${1:-all}"

run_director() {
  local genre="$1"
  local staged_blend="$2"
  local placement_plan="$3"
  local game_root="$ROOT/output/game_staging/game_$genre"
  local path_dir="$game_root/director_path_v1"
  local output_dir="$game_root/director_gameplay_v1"
  local director_path="$path_dir/director_path.json"
  local output_blend="$game_root/staged_scene_v6_director_v1.blend"

  mkdir -p "$path_dir" "$output_dir"

  if [[ ! -f "$director_path" ]]; then
    echo "missing uploaded director path: $director_path" >&2
    exit 3
  fi

  "$BLENDER" -b --python-exit-code 1 \
    --python "$ROOT/agent/game_staging/stage_genre_director_gameplay.py" -- \
    --scene_blend "$staged_blend" \
    --director_path "$director_path" \
    --output_dir "$output_dir" \
    --output_blend "$output_blend" \
    --npc_fbx "$ROOT/assets/mixamo/mixamo_run.fbx" \
    --fps_combat_asset_dir "$ROOT/assets/gameplay/fps_realistic_combat/final" \
    --swat_combat_library "$ROOT/assets/gameplay/swat_combat/final/swat_combat_library.blend" \
    --racing_vehicle_asset "$ROOT/assets/gameplay/racing_vehicle/final/arkham_batmobile_game_ready.glb" \
    --wingsuit_aircraft_asset "$ROOT/assets/gameplay/wingsuit_aircraft/final/f104_starfighter_game_ready.glb" \
    --aircraft_target_length 16.7 \
    --combat_actor_scale 1.12 \
    --render_previews
}

run_if_requested() {
  local genre="$1"
  shift
  if [[ "$REQUESTED_GENRE" == "all" || "$REQUESTED_GENRE" == "$genre" ]]; then
    run_director "$genre" "$@"
  fi
}

run_if_requested fps \
  "$ROOT/output/game_staging/game_fps/staged_scene_v6_platform_fix5.blend" \
  "$ROOT/output/game_staging/game_fps/asset_placement_v6_platform_fix5/gameplay_placement_plan.refined.json"

run_if_requested tps \
  "$ROOT/output/game_staging/game_tps/staged_scene_v6_mountain_platform_fix5.blend" \
  "$ROOT/output/game_staging/game_tps/asset_placement_v6_mountain_platform_fix5/gameplay_placement_plan.refined.json"

run_if_requested racing \
  "$ROOT/output/game_staging/game_racing/staged_scene_v6_route_fix4.blend" \
  "$ROOT/output/game_staging/game_racing/asset_placement_v6_route_fix4/gameplay_placement_plan.refined.json"

run_if_requested wingsuit \
  "$ROOT/output/game_staging/game_wingsuit/staged_scene_v6_verified_fix6.blend" \
  "$ROOT/output/game_staging/game_wingsuit/asset_placement_v6_verified_fix6/gameplay_placement_plan.refined.json"

if [[ "$REQUESTED_GENRE" != "all" && "$REQUESTED_GENRE" != "fps" && "$REQUESTED_GENRE" != "tps" && "$REQUESTED_GENRE" != "racing" && "$REQUESTED_GENRE" != "wingsuit" ]]; then
  echo "unknown genre: $REQUESTED_GENRE" >&2
  exit 2
fi
