#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  bash scripts/generate_gaming_world.sh <scene.blend> <game-intent prompt> <animated-npc.fbx> [run-name]

Environment:
  BLENDER_BIN                         Blender executable (default: blender)
  PYTHON_BIN                          Python executable (default: python)
  CODE2GAMES_DURATION_SECONDS         Planned duration (default: 30)
  CODE2GAMES_SKIP_EXISTING_ASSETS     Reuse valid GLBs when set to 1
  CODE2GAMES_NPC_HEIGHT_M              NPC height in meters (default: 1.7)
  CODE2GAMES_NPC_YAW_OFFSET_DEGREES    NPC model-forward correction (default: 90)
  HUNYUAN_BACKEND                     gradio or local (default: gradio)
  HUNYUAN_SERVER                      Gradio endpoint (default: http://127.0.0.1:8080)
EOF
}

if [[ $# -lt 3 || $# -gt 4 ]]; then
  usage >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BLENDER_BIN="${BLENDER_BIN:-blender}"
PYTHON_BIN="${PYTHON_BIN:-python}"
DURATION_SECONDS="${CODE2GAMES_DURATION_SECONDS:-30}"
HUNYUAN_BACKEND="${HUNYUAN_BACKEND:-gradio}"
HUNYUAN_SERVER="${HUNYUAN_SERVER:-http://127.0.0.1:8080}"

CALLER_DIR="$PWD"
SCENE_BLEND="$1"
GAME_PROMPT="$2"
NPC_FBX="$3"
NPC_HEIGHT_M="${CODE2GAMES_NPC_HEIGHT_M:-1.7}"
NPC_YAW_OFFSET_DEGREES="${CODE2GAMES_NPC_YAW_OFFSET_DEGREES:-90}"
if [[ $# -eq 4 ]]; then
  export CODE2GAMES_DEMO_NAME="$4"
fi

if [[ "$SCENE_BLEND" != /* ]]; then
  SCENE_BLEND="$CALLER_DIR/$SCENE_BLEND"
fi
if [[ "$NPC_FBX" != /* ]]; then
  NPC_FBX="$CALLER_DIR/$NPC_FBX"
fi

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
if [[ ! -f "$SCENE_BLEND" ]]; then
  echo "ERROR: input scene not found: $SCENE_BLEND" >&2
  exit 1
fi
if [[ ! -f "$NPC_FBX" ]]; then
  echo "ERROR: animated NPC FBX not found: $NPC_FBX" >&2
  exit 1
fi
if [[ -z "${DASHSCOPE_API_KEY:-}" ]]; then
  echo "ERROR: DASHSCOPE_API_KEY is required by the current visual and asset-generation stages." >&2
  exit 1
fi
if [[ -z "${CODE2GAMES_VLM_MODEL:-${OPENAI_VLM_MODEL:-${OPENAI_MODEL:-}}}" ]]; then
  echo "ERROR: set CODE2GAMES_VLM_MODEL (recommended: qwen3-vl-plus)." >&2
  exit 1
fi
if [[ "$HUNYUAN_BACKEND" != "gradio" && "$HUNYUAN_BACKEND" != "local" ]]; then
  echo "ERROR: HUNYUAN_BACKEND must be gradio or local." >&2
  exit 1
fi

STAGING_ROOT="$REPO_ROOT/output/game_staging"
if [[ -n "${CODE2GAMES_DEMO_NAME:-}" ]]; then
  STAGING_ROOT="$STAGING_ROOT/$CODE2GAMES_DEMO_NAME"
fi
PACKET_DIR="$STAGING_ROOT/default_camera_packet"

require_file() {
  if [[ ! -s "$1" ]]; then
    echo "ERROR: required output was not created: $1" >&2
    exit 1
  fi
}

echo "[1/9] Extracting the default-camera scene packet"
"$BLENDER_BIN" -b "$SCENE_BLEND" \
  --python "$REPO_ROOT/scene_and_gameplay_planning/extract_default_camera_packet.py"
require_file "$PACKET_DIR/default_camera_metadata.json"
require_file "$PACKET_DIR/default_camera_view_grid.png"

echo "[2/9] Understanding the scene"
"$PYTHON_BIN" "$REPO_ROOT/scene_and_gameplay_planning/run_scene_understanding_vlm.py" \
  --user_prompt "$GAME_PROMPT" \
  --duration_seconds "$DURATION_SECONDS"
require_file "$PACKET_DIR/scene_understanding_default_camera.json"

echo "[3/9] Planning gameplay rules"
"$PYTHON_BIN" "$REPO_ROOT/scene_and_gameplay_planning/run_game_rule_agent.py" \
  --user_prompt "$GAME_PROMPT" \
  --duration_seconds "$DURATION_SECONDS"
require_file "$PACKET_DIR/game_rule_visual_notes.json"
require_file "$PACKET_DIR/game_rule_plan.json"

echo "[4/9] Extracting placement candidates"
"$BLENDER_BIN" -b "$SCENE_BLEND" \
  --python "$REPO_ROOT/game_realization/element_placement/extract_placement_candidates.py" \
  -- --blend "$SCENE_BLEND"
require_file "$PACKET_DIR/placement_candidates.json"

echo "[5/9] Selecting scene-constrained placements"
"$PYTHON_BIN" "$REPO_ROOT/game_realization/element_placement/run_placement_pipeline.py" \
  --user_prompt "$GAME_PROMPT"
require_file "$PACKET_DIR/gameplay_placement_plan.json"

echo "[6/9] Planning gameplay assets"
"$PYTHON_BIN" "$REPO_ROOT/game_realization/asset_instantiation/run_asset_realization.py" \
  --user_prompt "$GAME_PROMPT"
ASSET_PLAN="$STAGING_ROOT/asset_realization/asset_plan.json"
require_file "$ASSET_PLAN"

echo "[7/9] Generating gameplay assets"
ASSET_GENERATION_ARGS=(
  --asset_plan "$ASSET_PLAN"
  --hunyuan_backend "$HUNYUAN_BACKEND"
  --server "$HUNYUAN_SERVER"
)
if [[ "${CODE2GAMES_SKIP_EXISTING_ASSETS:-0}" == "1" ]]; then
  ASSET_GENERATION_ARGS+=(--skip_existing)
fi
"$PYTHON_BIN" "$REPO_ROOT/game_realization/asset_instantiation/run_asset_generation_stage.py" \
  "${ASSET_GENERATION_ARGS[@]}"

"$PYTHON_BIN" - "$ASSET_PLAN" "$REPO_ROOT" <<'PY'
import json
import os
import sys

plan_path, project_root = sys.argv[1:]
with open(plan_path, "r", encoding="utf-8") as handle:
    plan = json.load(handle)
missing = []
for asset in plan.get("assets", []):
    value = str(asset.get("expected_glb_path") or "")
    path = value if os.path.isabs(value) else os.path.join(project_root, value)
    if not os.path.isfile(path) or os.path.getsize(path) < 20:
        missing.append(path)
if missing:
    raise SystemExit("Asset generation is incomplete; missing GLBs:\n" + "\n".join(missing))
PY

echo "[8/9] Instantiating gameplay assets in Blender"
"$BLENDER_BIN" -b "$SCENE_BLEND" \
  --python "$REPO_ROOT/game_realization/asset_instantiation/stage_gameplay_assets.py" \
  -- --scene_blend "$SCENE_BLEND"

STAGED_SCENE="$STAGING_ROOT/staged_scene.blend"
require_file "$STAGED_SCENE"

echo "[9/9] Placing the gameplay NPC in the gaming world"
"$BLENDER_BIN" -b "$STAGED_SCENE" \
  --python "$REPO_ROOT/game_realization/asset_instantiation/stage_gameplay_npc.py" \
  -- \
  --scene_blend "$STAGED_SCENE" \
  --output_blend "$STAGED_SCENE" \
  --npc_fbx "$NPC_FBX" \
  --npc_height_m "$NPC_HEIGHT_M" \
  --npc_yaw_offset_degrees "$NPC_YAW_OFFSET_DEGREES"
require_file "$STAGED_SCENE"
require_file "$STAGING_ROOT/npc_staging/npc_staging_report.json"

echo "DONE: $STAGED_SCENE"
