# Code2Games: Enabling Coding Agents for Gaming World Generation

This is the official repository for the paper:
> **Code2Games: Enabling Coding Agents for Gaming World Generation**
>
> [Wei Wu](https://github.com/weiwu-7)<sup>1*</sup>, [Ziyang Xu](https://github.com/xzy-Zayn)<sup>1*</sup>, [Zeyu Zhang](https://steve-zeyu-zhang.github.io/)<sup>1*†</sup>, [Yang Zhao](https://yangyangkiki.github.io/)<sup>2</sup>, and [Hao Tang](https://ha0tang.github.io/)<sup>1‡</sup>
>
> <sup>1</sup>School of Computer Science, Peking University  
> <sup>2</sup>La Trobe University
>
> \*Equal contribution. <sup>†</sup>Project lead. <sup>‡</sup>Corresponding author.
>
> ### [Paper](https://arxiv.org/abs/2600.00000) | [Website](https://aigeeksgroup.github.io/Code2Games)| [GameCode4D](https://huggingface.co/datasets/AIGeeksGroup/GameCode4D)

https://github.com/user-attachments/assets/d9be6a46-b422-4737-97f0-e9eda0694fb6

## Overview

Code2Games turns a natural-language game intent and a corresponding Code2Worlds Blender scene into a scene-grounded gaming world. The current repository contains the Blender-side pipeline for scene analysis, gameplay planning, constrained element placement, asset generation, world realization, NPC placement, and an optional visual-showcase stage.

The workflow is separated into two stages:

1. **Gaming-world generation:** generate gameplay rules and assets, place the NPC, and save the complete world as `staged_scene.blend`.
2. **Visual showcase (optional):** select a physically continuous presentation route, animate the NPC already present in the world, create a follow camera, and render an MP4.

```text
Code2Worlds scene.blend + game intent + animated NPC FBX
    -> scene and gameplay planning
    -> scene-constrained element placement
    -> asset realization and generation
    -> NPC placement
    -> staged_scene.blend (gaming world, including NPC)
    -> [optional] showcase route + follow camera + video
```

`staged_scene.blend` is the primary Blender gaming-world artifact and already contains the statically placed NPC. Route animation, the follow camera, and video rendering are presentation-only additions saved to a separate showcase Blend; they do not overwrite the gaming world.

## Repository Structure

```text
Code2Games/
├── shared_representation/          # Shared paths, I/O, and placement constraints
├── scene_and_gameplay_planning/    # Scene analysis and declarative gameplay planning
├── game_realization/
│   ├── element_placement/          # Candidate extraction and semantic selection
│   └── asset_instantiation/        # Asset generation, NPC placement, and Blender staging
├── visual_showcase/                # Optional route, follow camera, and video rendering
├── scripts/
│   ├── generate_gaming_world.sh    # scene.blend + NPC -> staged_scene.blend
│   └── generate_visual_showcase.sh # staged_scene.blend -> showcase Blend/MP4
└── requirements.txt
```

## Quick Start

### 1. Environment Setup

Clone the repository and create a Python environment:

```bash
git clone https://github.com/AIGeeksGroup/Code2Games.git
cd Code2Games
conda create -n code2games python=3.11
conda activate code2games
pip install -r requirements.txt
```

Install the following external components separately:

- [Blender 4.2](https://www.blender.org/download/)
- [Hunyuan3D 2.1](https://github.com/Tencent-Hunyuan/Hunyuan3D-2.1), either as a local installation or a Gradio service
- A Code2Worlds-generated `.blend` scene with an active camera

Blender provides `bpy` and `mathutils`; do not install them with `pip`.

### 2. Configure Models and Services

The current asset-generation stage uses DashScope for reference-image generation. Configure credentials through environment variables and never place API keys in source files:

```bash
export DASHSCOPE_API_KEY="your_api_key"
export DASHSCOPE_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
export CODE2GAMES_VLM_MODEL="qwen3-vl-plus"

# Optional executable overrides
export PYTHON_BIN="python"
export BLENDER_BIN="blender"

# Hunyuan3D: gradio (default) or local
export HUNYUAN_BACKEND="gradio"
export HUNYUAN_SERVER="http://127.0.0.1:8080"
```

For a local Hunyuan3D installation:

```bash
export HUNYUAN_BACKEND="local"
export HUNYUAN3D_REPO="/path/to/Hunyuan3D-2.1"
export HUNYUAN3D_MODEL_PATH="tencent/Hunyuan3D-2.1"
```

### 3. Obtain an NPC

The NPC is part of the generated gaming world, so the current end-to-end script requires an animated humanoid FBX before world realization.

Recommended sources:

- [Mixamo](https://www.mixamo.com/): choose a humanoid character, apply a walk/run animation, enable **In Place** when available, and download an animated FBX **With Skin**.
- [CGTrader character models](https://www.cgtrader.com/free-3d-models/character): filter for an FBX, rigged, game-ready humanoid. Check the license for every model. If the character is not animated, upload it to Mixamo for auto-rigging and animation.

For a custom CGTrader model, a typical preparation flow is:

```text
CGTrader humanoid model
    -> clean neutral/T-pose
    -> upload FBX/OBJ/ZIP to Mixamo
    -> auto-rig
    -> apply walk or run animation
    -> enable In Place
    -> download FBX With Skin
```

The current Blender executor is designed for an animated humanoid FBX. Mixamo auto-rigging supports bipedal humanoids; non-humanoid creatures require a separately prepared rig and compatible animation.

Place the downloaded file at the conventional path:

```text
assets/mixamo/mixamo_run.fbx
```

You may also keep it anywhere and pass its path directly to the script.

### 4. Generate the Gaming World

Run the complete Blender-world realization stage:

```bash
bash scripts/generate_gaming_world.sh \
  "/path/to/code2worlds/scene.blend" \
  "Create a third-person monster hunt in a dark forest. Track the creature, defeat it, and claim the trophy." \
  "assets/mixamo/mixamo_run.fbx" \
  "monster_hunt"
```

Arguments are:

```text
generate_gaming_world.sh <scene.blend> <game-intent prompt> <animated-npc.fbx> [run-name]
```

The optional run name isolates outputs from different games. The script performs scene understanding, gameplay planning, constrained placement, asset generation and instantiation, then places the NPC at a semantic spawn anchor. The primary result is:

```text
output/game_staging/monster_hunt/staged_scene.blend
```

This Blend is the gaming world. It contains the environment, generated gameplay elements, logical regions, and the statically placed NPC, but no showcase route or follow camera. Without a run name, outputs are written directly under `output/game_staging/`.

Existing generated GLBs can be reused:

```bash
export CODE2GAMES_SKIP_EXISTING_ASSETS=1
```

### 5. Generate an Optional Visual Showcase

After the gaming world exists, generate a route, NPC locomotion, follow camera, and video without importing another character:

```bash
bash scripts/generate_visual_showcase.sh "generic" "monster_hunt"
```

Arguments are:

```text
generate_visual_showcase.sh [genre] [run-name]
```

Supported route profiles are `generic`, `fps`, `tps`, `racing`, and `wingsuit`. The run name must match the one passed to `generate_gaming_world.sh`.

The optional presentation outputs are:

```text
output/game_staging/monster_hunt/showcase_route/showcase_route.json
output/game_staging/monster_hunt/visual_showcase/visual_showcase.blend
output/game_staging/monster_hunt/visual_showcase/visual_showcase.mp4
```

The showcase executor resamples the route against the actual Blender terrain, resolves local collisions, keeps actor turns continuous, synchronizes animation to traveled distance, and searches for follow-camera positions that avoid terrain and scene occlusion. It reads the NPC already embedded in `staged_scene.blend` and never overwrites that source world.

## Output Layout

```text
output/game_staging/<run-name>/
├── default_camera_packet/          # Scene analysis, rules, candidates, and placement plan
├── asset_realization/              # Asset plan and LLM response
├── asset_generation/               # Reference images, logs, and generation manifest
├── asset_placement/                # Placement reports and validation previews
├── npc_staging/                    # NPC placement report
├── staged_scene.blend              # Gaming world, including the placed NPC
├── showcase_route/                 # Optional physical presentation route
└── visual_showcase/                # Optional animated NPC/camera Blend and MP4
```

Generated GLBs are stored under:

```text
assets/generated_glb/<run-name>/
```

Each stage validates its required input artifact before the next stage begins. If the pipeline stops, inspect the corresponding JSON report under `output/game_staging/<run-name>/` and rerun after correcting the failed dependency.

## Acknowledgement

We thank the authors of [Code2Worlds](https://github.com/AIGeeksGroup/Code2Worlds), [Infinigen](https://github.com/princeton-vl/infinigen), [Hunyuan3D](https://github.com/Tencent-Hunyuan/Hunyuan3D-2.1), Blender, Adobe Mixamo, and the creators of the 3D assets used by this project.


