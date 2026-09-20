# Code2Games

Code2Games turns an existing Blender world into a staged gameplay demo. This
repository publishes the tested pipeline from a Code2Worlds-generated `.blend`
file to generated gameplay assets, a directed character route, and a rendered
video.

Only source code is versioned. Generated scenes, models, images, videos, model
weights, caches, and API credentials are intentionally excluded.

For the complete Chinese, stage-by-stage operating guide—including every LLM
JSON boundary and manual review point—see
[`SUCCESSFUL_PIPELINE_ZH.md`](SUCCESSFUL_PIPELINE_ZH.md).

## Pipeline

```text
input scene.blend
  -> default-camera packet
  -> scene understanding
  -> game-rule plan
  -> geometric placement candidates
  -> gameplay placement plan
  -> asset realization plan
  -> reference images and Hunyuan3D GLBs
  -> staged_scene.blend
  -> director_path.json
  -> staged_director_gameplay.blend and director_gameplay.mp4
```

The pipeline deliberately separates responsibilities:

- VLM/LLM stages make semantic decisions about the scene, rules, placements,
  and asset designs.
- Blender/Python stages extract geometry, resolve coordinates, import and scale
  assets, build animation, and render.
- `gameplay_placement_plan.json` and `asset_plan.json` are explicit review
  checkpoints and may be edited before continuing.

Global top-down layout packets, density maps, geometry heatmaps, and regional
beauty renders are not required by this tested default-camera pipeline.

## Included source files

The gaming-world implementation lives in `agent/game_staging/`:

1. `extract_default_camera_packet.py`
2. `run_scene_understanding_vlm.py`
3. `run_game_rule_agent.py`
4. `extract_placement_candidates.py`
5. `run_placement_pipeline.py`
6. `run_asset_realization.py`
7. `run_asset_generation_stage.py`
8. `stage_gameplay_assets.py`
After step 8, `staged_scene.blend` is the generated gaming world.

The final, bug-fixed demonstration layer is:

9. `build_genre_director_path.py`
10. `stage_genre_director_gameplay.py`

Genre wrappers provide the approved parameters for the four production demo
families:

- `stage_fps_showcase_gameplay.py`
- `stage_tps_showcase_gameplay.py`
- `stage_racing_showcase_gameplay.py`
- `stage_wingsuit_showcase_gameplay.py`

`build_gameplay_director_path.py` is the reusable, non-hard-coded builder. It
now carries the general Stage-11 fixes learned by the production builder:
strict placement validation, immutable fixed anchors, semantic event and
movement modes, bounded 3D transit segments, speed/dwell-aware timing,
continuous camera cues, compatibility camera fields, and atomic JSON output.
`stage_director_gameplay.py` is now the reusable Blender execution layer. It
contains the general terrain-contact, body-width swept collision solver,
anchor-preserving curved detours, dwell-aware speed/turn validation,
distance-synchronised locomotion, generated-asset support-point grounding,
geometry-aware interaction stand-off, evaluated actor turn-rate auditing,
FBX frame-rate restoration, atomic Blend output, and final per-frame
camera-obstruction audit without any production placement IDs. Lighting
freezing is available only as an explicit `--freeze_environment_lighting`
opt-in.
`build_genre_director_path.py` and `stage_genre_director_gameplay.py` remain
hand-authored showcase overrides for specialized combat choreography, vehicle
suspension, and aircraft motion.

The remaining tracked modules in that directory provide shared validation,
path handling, and LLM-call utilities.

## External dependencies

You need the following external projects and services:

- Blender 4.2 or a compatible release
- A Code2Worlds/Infinigen-generated Blender scene
- An OpenAI-compatible multimodal and text-model endpoint
- DashScope image generation, unless reference images already exist
- Hunyuan3D-2.1 for local image-to-3D generation
- A rigged animation FBX for the optional character/director stage

Install the lightweight non-Blender dependencies with:

```bash
pip install -r requirements-pipeline.txt
```

Blender scripts must run through Blender's Python runtime. Hunyuan3D should be
installed according to its upstream instructions in a separate environment.

## Environment

Run commands from the repository root and configure the provider without
putting secrets in source files:

```bash
export CODE2WORLDS_ROOT="$PWD"
export OPENAI_BASE_URL="https://your-openai-compatible-endpoint/v1"
export OPENAI_API_KEY="your-api-key"
export CODE2GAMES_VLM_MODEL="your-vision-model"
export CODE2GAMES_RULE_MODEL="your-rule-model"
export CODE2GAMES_PLACEMENT_MODEL="your-placement-model"
```

`DASHSCOPE_BASE_URL` and `DASHSCOPE_API_KEY` may be used instead of the OpenAI
variables. Never commit a real API key.

## End-to-end commands

Set the source scene and Blender executable:

```bash
export SOURCE_BLEND=/absolute/path/to/scene.blend
export BLENDER=/absolute/path/to/blender
export USER_PROMPT="describe the gameplay demo"
```

### 1. Extract the default-camera packet

```bash
"$BLENDER" -b "$SOURCE_BLEND" --python-exit-code 1 \
  --python ./agent/game_staging/extract_default_camera_packet.py
```

Outputs are written to `output/game_staging/default_camera_packet/`, including
the rendered view, grid view, metadata, and report.

### 2. Understand the scene

```bash
python ./agent/game_staging/run_scene_understanding_vlm.py \
  --user_prompt "$USER_PROMPT"
```

The principal output is
`output/game_staging/default_camera_packet/scene_understanding_default_camera.json`.

### 3. Design gameplay rules

```bash
python ./agent/game_staging/run_game_rule_agent.py \
  --user_prompt "$USER_PROMPT"
```

The principal output is
`output/game_staging/default_camera_packet/game_rule_plan.json`.

### 4. Extract real placement candidates

```bash
"$BLENDER" -b "$SOURCE_BLEND" --python-exit-code 1 \
  --python ./agent/game_staging/extract_placement_candidates.py -- \
  --blend "$SOURCE_BLEND"
```

The principal output is
`output/game_staging/default_camera_packet/placement_candidates.json`.

### 5. Select placements

```bash
python ./agent/game_staging/run_placement_pipeline.py \
  --user_prompt "$USER_PROMPT"
```

Review and, when necessary, edit
`output/game_staging/default_camera_packet/gameplay_placement_plan.json` before
continuing. Later stages treat it as the location source of truth.

### 6. Realize asset designs

```bash
python ./agent/game_staging/run_asset_realization.py \
  --user_prompt "$USER_PROMPT"
```

Review and, when necessary, edit
`output/game_staging/asset_realization/asset_plan.json`. It controls asset
prompts, expected dimensions, reuse, placement bindings, and GLB destinations.

### 7. Generate reference images and GLBs

For local Hunyuan3D generation without a separate Gradio server:

```bash
export HUNYUAN3D_REPO=/absolute/path/to/Hunyuan3D-2.1
export HUNYUAN3D_MODEL_PATH=/absolute/path/to/Hunyuan3D-2.1-model-cache
export CODE2GAMES_CACHE_DIR=/absolute/path/to/cache

CUDA_VISIBLE_DEVICES=0 python ./agent/game_staging/run_asset_generation_stage.py \
  --asset_plan ./output/game_staging/asset_realization/asset_plan.json \
  --hunyuan_backend local \
  --hunyuan_repo "$HUNYUAN3D_REPO" \
  --model_path "$HUNYUAN3D_MODEL_PATH" \
  --texgen_model_path tencent/Hunyuan3D-2.1 \
  --t2i_backend dashscope_sdk_qwen_image \
  --reuse_reference_images \
  --steps 20 \
  --octree_resolution 128 \
  --num_chunks 8000 \
  --export_texture \
  --skip_existing
```

Reference images and manifests are written under
`output/game_staging/asset_generation/`. GLB destinations come from each
asset's `expected_glb_path` in `asset_plan.json`.

### 8. Stage assets in Blender

```bash
"$BLENDER" -b "$SOURCE_BLEND" --python-exit-code 1 \
  --python ./agent/game_staging/stage_gameplay_assets.py -- \
  --scene_blend "$SOURCE_BLEND"
```

This produces `output/game_staging/staged_scene.blend`, placement reports, and
validation previews.

### 9. Build the reusable director route

```bash
python ./agent/game_staging/build_gameplay_director_path.py \
  --genre generic \
  --target_duration_seconds 50 \
  --turn_smoothing 0.35 \
  --maximum_turn_degrees_per_second 120
```

The output is `output/game_staging/director_path/director_path.json`. The route
derives events from the placement semantics, interpolates between immutable
gameplay anchors, and never moves assets. Use `--genre fps`, `tps`, `racing`,
or `wingsuit` to select a reusable actor/camera/movement profile without adding
scene coordinates or placement IDs to this script.

The route uses bounded cubic-Hermite curves rather than right-angle polyline
corners. Per-beat headings are unwrapped and constrained by a maximum angular
speed. The requested duration is allowed to stretch a physically timed route,
but it cannot compress the route beyond its movement-speed or turn-rate
budget; `requested_target_duration_honored` records the result.

### 10. Stage the reusable character, physical route, camera, and render

```bash
"$BLENDER" -b ./output/game_staging/staged_scene.blend \
  --python-exit-code 1 \
  --python ./agent/game_staging/stage_director_gameplay.py -- \
  --scene_blend ./output/game_staging/staged_scene.blend \
  --director_path ./output/game_staging/director_path/director_path.json \
  --npc_fbx /absolute/path/to/animated_character.fbx \
  --actor_radius_m 0.42 \
  --maximum_collision_detour_m 4.2 \
  --camera_collision_radius_m 1.0 \
  --render_video \
  --video_samples 16
```

The final outputs are written to `output/game_staging/director_gameplay/`:

- `staged_director_gameplay.blend`
- `director_gameplay_report.json`
- `director_gameplay.mp4`

`director_gameplay_report.json` records terrain support, inserted curved
detours, unresolved route collisions, evaluated speed/turn limits,
distance-synchronised animation segments, and the final per-frame camera
visibility audit. The executor restores the director FPS after FBX import, as
some FBX files otherwise silently switch a 24-fps scene to 30 fps.

## Data and model policy

This repository does not redistribute generated worlds, Blender scenes,
Hunyuan3D weights, generated GLBs, animation FBXs, rendered images, or videos.
Obtain external projects and model weights from their official sources and
follow their respective licenses.

## Status

This is a research prototype extracted from a successfully executed pipeline.
JSON schemas and prompts may evolve; preserve the explicit stage boundaries
when extending it.
