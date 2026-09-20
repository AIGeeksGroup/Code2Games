#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  bash scripts/generate_visual_showcase.sh [genre] [run-name]

Genres:
  generic, fps, tps, racing, wingsuit

This optional presentation stage reads the NPC already stored in
staged_scene.blend. It does not import or place an NPC.

Environment:
  BLENDER_BIN                    Blender executable (default: blender)
  PYTHON_BIN                     Python executable (default: python)
  CODE2GAMES_RENDER_PREVIEWS     Render preview frames when set to 1
  CODE2GAMES_ADD_EVENTS          Add optional showcase VFX when set to 1
EOF
}

if [[ $# -gt 2 ]]; then
  usage >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BLENDER_BIN="${BLENDER_BIN:-blender}"
PYTHON_BIN="${PYTHON_BIN:-python}"
GENRE="${1:-generic}"
if [[ $# -eq 2 ]]; then
  export CODE2GAMES_DEMO_NAME="$2"
fi

case "$GENRE" in
  generic|fps|tps|racing|wingsuit) ;;
  *)
    echo "ERROR: unsupported genre: $GENRE" >&2
    usage >&2
    exit 2
    ;;
esac

cd "$REPO_ROOT"
export CODE2WORLDS_ROOT="$REPO_ROOT"

if ! command -v "$BLENDER_BIN" >/dev/null 2>&1; then
  echo "ERROR: Blender executable not found: $BLENDER_BIN" >&2
  exit 1
fi
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "ERROR: Python executable not found: $PYTHON_BIN" >&2
  exit 1
fi

STAGING_ROOT="$REPO_ROOT/output/game_staging"
if [[ -n "${CODE2GAMES_DEMO_NAME:-}" ]]; then
  STAGING_ROOT="$STAGING_ROOT/$CODE2GAMES_DEMO_NAME"
fi
STAGED_SCENE="$STAGING_ROOT/staged_scene.blend"
PLACEMENT_PLAN="$STAGING_ROOT/default_camera_packet/gameplay_placement_plan.json"
ASSET_PLAN="$STAGING_ROOT/asset_realization/asset_plan.json"
NPC_REPORT="$STAGING_ROOT/npc_staging/npc_staging_report.json"
SHOWCASE_ROUTE="$STAGING_ROOT/showcase_route/showcase_route.json"
OUTPUT_DIR="$STAGING_ROOT/visual_showcase"

require_file() {
  if [[ ! -s "$1" ]]; then
    echo "ERROR: required input/output was not found: $1" >&2
    exit 1
  fi
}

require_file "$STAGED_SCENE"
require_file "$PLACEMENT_PLAN"
require_file "$ASSET_PLAN"
require_file "$NPC_REPORT"

echo "[1/2] Building an optional physics-aware showcase route"
"$PYTHON_BIN" "$REPO_ROOT/visual_showcase/build_showcase_route.py" \
  --placement_plan "$PLACEMENT_PLAN" \
  --asset_plan "$ASSET_PLAN" \
  --output "$SHOWCASE_ROUTE" \
  --genre "$GENRE"
require_file "$SHOWCASE_ROUTE"

echo "[2/2] Animating the staged NPC and creating the follow-camera video"
SHOWCASE_ARGS=(
  --scene_blend "$STAGED_SCENE"
  --showcase_route "$SHOWCASE_ROUTE"
  --output_dir "$OUTPUT_DIR"
  --render_video
)
if [[ "${CODE2GAMES_RENDER_PREVIEWS:-0}" == "1" ]]; then
  SHOWCASE_ARGS+=(--render_previews)
fi
if [[ "${CODE2GAMES_ADD_EVENTS:-0}" == "1" ]]; then
  SHOWCASE_ARGS+=(--add_events)
fi

"$BLENDER_BIN" -b "$STAGED_SCENE" \
  --python "$REPO_ROOT/visual_showcase/stage_visual_showcase.py" \
  -- "${SHOWCASE_ARGS[@]}"

require_file "$OUTPUT_DIR/visual_showcase.blend"
require_file "$OUTPUT_DIR/visual_showcase.mp4"
echo "SHOWCASE: $OUTPUT_DIR/visual_showcase.blend"
echo "VIDEO: $OUTPUT_DIR/visual_showcase.mp4"
