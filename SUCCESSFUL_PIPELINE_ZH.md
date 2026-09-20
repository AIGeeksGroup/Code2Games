# Code2Games：已跑通的 Scene-to-Video Pipeline 操作手册

> 注意：第九步的通用 Director builder 已吸收 production builder 中可泛化
> 的校验、事件/移动模式、路径密化、速度计时、相机 cue 和原子写入方法，
> 不含场景坐标或 placement ID。四类最终演示仍可用
> `build_genre_director_path.py` 作为人工编排覆盖。通用的脚底贴地、角色
> 体宽碰撞绕行、碰撞后速度/朝向重算、按路程同步步态和相机遮挡求解已
> 进入 `stage_director_gameplay.py`；通用执行器还会根据生成资产的分布式支撑点贴地、按真实
> 包围盒推导交互留距、审计最终角色转向连续性，并以原子方式写出 Blend；这些机制不依赖
> FPS 换弹、敌人编号或任何固定坐标。`stage_genre_director_gameplay.py` 仅保留四类演示的
> 战斗编排、车辆悬挂和飞机运动等 genre 专用逻辑。`staged_scene.blend` 在第八步后
> 已经是 Gaming World；后续 Director 仅用于角色演示与视频录制。

本文档记录的是已经实际跑通过的一条主链：从 **Code2Worlds 已生成的 Blender 场景** 开始，经过场景理解、玩法规划、位置选择、资产生成、Blender 放置、角色与跟随相机，最后导出视频。它不包含 Code2Worlds/Infinigen 本身如何生成初始场景，也不包含后来做过的全局俯视图、区域密度图等 layout packet 实验流程。

这条链的核心原则是：**LLM 做语义决策并写 JSON；Python/Blender 做坐标、几何、导入、缩放、动画与渲染。** LLM 不直接写 Blender 代码，也不应凭空写世界坐标。

## 0. 输入、目录与总流程

输入只有一个已经生成好的场景：

```text
SOURCE_BLEND = /absolute/path/to/scene.blend
```

以下命令都必须在 Code2Games 仓库根目录执行。为了避免路径分叉，**本流程不要设置 `CODE2GAMES_DEMO_NAME`**；所有默认输出会落在：

```text
output/game_staging/
├── default_camera_packet/       # 场景理解、规则、候选点、放置计划
├── asset_realization/           # LLM 资产计划
├── asset_generation/            # 文生图、Hunyuan3D、生成清单
├── asset_placement/             # 放置报告、预览、最终解析后的放置计划
├── staged_scene.blend           # 加入 GLB 后的场景
├── director_path/               # Python 生成的角色/相机路线
└── director_gameplay/           # 最终 Blend、报告与 mp4
```

完整依赖关系如下：

```text
scene.blend
  ├─ Blender ──> default_camera_packet/default_camera_*.png + metadata.json
  ├─ VLM ──────> scene_understanding_default_camera.json
  ├─ VLM + LLM ─> game_rule_visual_notes.json + game_rule_plan.json
  ├─ Blender ──> placement_candidates.json
  ├─ LLM ──────> gameplay_placement_plan.json
  ├─ LLM ──────> asset_realization/asset_plan.json
  ├─ T2I + Hunyuan3D ──> reference image + GLB
  ├─ Blender ──> staged_scene.blend
  ├─ Python ───> director_path.json
  └─ Blender ──> staged_director_gameplay.blend + director_gameplay.mp4
```

## 1. 一次性环境准备

先准备 Blender、LLM/VLM 服务、DashScope（文生图）和本地 Hunyuan3D。实际运行过的资产生成方式是 **local Hunyuan backend**：不需要单独开 Gradio/Hunyuan 服务，生成脚本自己在同一个进程中加载 Hunyuan。

```bash
cd /path/to/Code2Games

export SOURCE_BLEND=/absolute/path/to/scene.blend
export BLENDER=/absolute/path/to/blender-4.2.0-linux-x64/blender
export USER_PROMPT="describe the intended game"

# 场景理解 VLM；规则和放置也会回退到这些 OpenAI-compatible 配置。
export OPENAI_BASE_URL="https://your-openai-compatible-endpoint/v1"
export OPENAI_API_KEY="your-api-key"
export CODE2GAMES_VLM_MODEL="your-vision-model"
export CODE2GAMES_RULE_MODEL="your-rule-model"
export CODE2GAMES_PLACEMENT_MODEL="your-placement-model"

# DashScope 文生图需要；规则阶段的视觉 reader 也默认使用 DASHSCOPE_API_KEY。
export DASHSCOPE_API_KEY="your-dashscope-key"

# 仅第 7 步 local Hunyuan3D 需要。
export HUNYUAN3D_REPO=/absolute/path/to/Hunyuan3D-2.1
export HUNYUAN3D_MODEL_PATH=/absolute/path/to/hunyuan3d-model-cache
export CODE2GAMES_CACHE_DIR=/absolute/path/to/cache
```

如果 Hunyuan 模型已缓存且服务器离线运行，还可以按实际环境补充：

```bash
export HF_HOME=/absolute/path/to/huggingface-cache
export HUGGINGFACE_HUB_CACHE="$HF_HOME/hub"
export TRANSFORMERS_CACHE="$HF_HOME/transformers"
export TORCH_HOME=/absolute/path/to/torch-cache
export XDG_CACHE_HOME=/absolute/path/to/cache
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
```

`PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` 用于减少 PyTorch 显存碎片，不能凭空增加显存；`num_chunks`、八叉树分辨率和纹理生成仍会影响显存占用。

## 2. 第一步：从初始 Blend 提取默认相机包

运行：

```bash
"$BLENDER" -b "$SOURCE_BLEND" --python-exit-code 1 \
  --python ./agent/game_staging/extract_default_camera_packet.py
```

脚本：`agent/game_staging/extract_default_camera_packet.py`。

这一步不调用 LLM。它以源场景的 `DEFAULT_CAMERA` 为观察相机，渲染原图和带九宫格标注的图，同时记录相机及场景元数据。输出：

```text
output/game_staging/default_camera_packet/
├── default_camera_view.png
├── default_camera_view_grid.png
├── default_camera_metadata.json
└── default_camera_packet_report.json
```

这里的九宫格只服务于场景理解和位置选择时的视觉参照；它**不是**最终游戏相机。最终视频相机在第 10 步由角色后上方跟随相机生成。

## 3. 第二步：VLM 写场景理解 JSON

运行：

```bash
python ./agent/game_staging/run_scene_understanding_vlm.py \
  --user_prompt "$USER_PROMPT"
```

脚本：`agent/game_staging/run_scene_understanding_vlm.py`。

它将 `default_camera_view_grid.png` 和元数据送给 VLM，要求模型**只描述看见的场景**，不允许写玩法、路线、坐标、资产或 Blender 代码。模型的原始回复和经过校验的 JSON 分别是：

```text
scene_understanding_default_camera_prompt.md       # 实际发送给 VLM 的提示词
scene_understanding_default_camera_raw_response.md # VLM 原始输出
scene_understanding_default_camera.json            # 下游读取的正式 JSON
scene_understanding_default_camera_report.json     # 成功/失败报告
```

正式 JSON 结构必须是：

```json
{
  "view_status": {
    "selected_view": "DEFAULT_CAMERA",
    "usable": true,
    "confidence": 0.0,
    "reason": "..."
  },
  "scene_visual_context": {
    "overall_summary": "...",
    "environment_visual_type": "...",
    "terrain_structure": "...",
    "visible_elements": [],
    "surface_distribution": {
      "ground_regions": [],
      "water_regions": [],
      "vegetation_regions": [],
      "rock_or_slope_regions": [],
      "uncertain_regions": []
    },
    "spatial_relations": [],
    "traversability_visual_observation": {
      "visually_open_regions": [],
      "visually_blocked_or_complex_regions": [],
      "visually_discontinuous_regions": []
    },
    "visual_guidance_features": [],
    "uncertainty_notes": []
  }
}
```

人工介入方式：先看 `*_raw_response.md` 是否有幻觉或漏读；若仅是措辞/语义错误，可直接修改 `scene_understanding_default_camera.json` 使其符合上面的结构。若改动了这个正式 JSON，后续从第 3 步开始重新运行。

## 4. 第三步：LLM 写规则视觉备注与游戏规则 JSON

运行：

```bash
python ./agent/game_staging/run_game_rule_agent.py \
  --user_prompt "$USER_PROMPT" \
  --duration_seconds 30
```

脚本：`agent/game_staging/run_game_rule_agent.py`。它内部有 **两次模型调用**：

1. VLM 根据默认相机图、场景理解和元数据，写 `game_rule_visual_notes.json`；它只提取有利于设计规则的视觉事实，不能把空隙臆断成既定路线。
2. 文本 LLM 根据用户提示、场景理解和视觉备注，写 `game_rule_plan.json`；它定义玩法需求，但不能写任何具体坐标、路径点、GLB 提示词或 Blender 代码。

输出：

```text
game_rule_visual_prompt.md
game_rule_visual_raw_response.md
game_rule_visual_notes.json
game_rule_prompt.md
game_rule_raw_response.md
game_rule_plan.json
game_rule_report.json
```

`game_rule_visual_notes.json` 的结构：

```json
{
  "visual_rule_notes_status": {"usable": true, "confidence": 0.0, "reason": "..."},
  "visual_rule_notes": {
    "rule_relevant_scene_summary": "...",
    "scene_features_useful_for_rules": [],
    "movement_space_observation": "...",
    "visual_landmarks_for_rules": [],
    "rule_design_risks_from_visuals": [],
    "notes_for_rule_designer": []
  }
}
```

`game_rule_plan.json` 至少需要包含：

```json
{
  "game_rule_status": {"usable": true, "confidence": 0.0, "reason": "..."},
  "game_rule_plan": {
    "game_concept": {"title": "...", "description": "...", "why_it_fits_this_scene": "..."},
    "core_loop": "...",
    "objective": "...",
    "win_condition": "...",
    "fail_condition": "...",
    "time_limit_seconds": 45,
    "scoring_rules": [],
    "allowed_actions": ["move_forward", "move_backward", "move_left", "move_right", "jump"],
    "scene_element_usage": [],
    "randomness_policy": {
      "free_navigation": true,
      "fixed_path_required": false,
      "random_choice_allowed": true,
      "description": "..."
    },
    "required_gameplay_elements_for_placement": [
      {
        "element_type": "collectible",
        "purpose": "...",
        "placement_hint_from_scene": "...",
        "required_count": 5
      }
    ]
  }
}
```

可用的 `element_type` 只有：`collectible`、`obstacle`、`event_trigger`、`goal_area`、`hazard_zone`、`safe_zone`、`landmark`。`allowed_actions` 必须至少包含四向移动；规则要求自由探索，不应写成“自动向前跑、固定单一路线、仅坚持到计时结束”。

人工介入方式：这里最适合改的是 `game_rule_plan.json` 中的玩法数量和类别，例如收集物/障碍物数量、目标、时限和动作。改完必须从第 5 步重新开始，因为候选位置、放置、资产和导演路线都会依赖它。

## 5. 第四步：Blender 提取真实可放置候选点 JSON

运行：

```bash
"$BLENDER" -b "$SOURCE_BLEND" --python-exit-code 1 \
  --python ./agent/game_staging/extract_placement_candidates.py -- \
  --blend "$SOURCE_BLEND" \
  --sample_cols 48 --sample_rows 27 --max_candidates 450
```

脚本：`agent/game_staging/extract_placement_candidates.py`。

这一步不调用 LLM。Blender 对真实几何进行采样，输出候选点的：世界坐标、法线、地面/坡面/悬崖类别、可行走性、屏幕九宫格、离默认相机距离、附近树木/灌木的水平净空等事实。

输出：

```text
default_camera_packet/placement_candidates.json
default_camera_packet/placement_candidates_report.json
```

`placement_candidates.json` 是**坐标唯一事实来源**。后续 LLM 只能选择 `candidate_id`，不能发明 `world_xyz`。候选点附近已有树、灌木、岩石只是构图与风格上下文，不代表可以把现有树当成障碍物或直接复用为新资产。

人工介入方式：正常不要手改候选点坐标。如果候选点数量或区域明显不好，调整采样参数后重新跑本步，再从第 6 步继续。

## 6. 第五步：LLM 选择候选点，写放置计划 JSON

运行：

```bash
python ./agent/game_staging/run_placement_pipeline.py \
  --user_prompt "$USER_PROMPT"
```

脚本：`agent/game_staging/run_placement_pipeline.py`。这是简化后的**单次 LLM 选择**：不再有多轮 revision/retry 选择。它读取场景理解、规则视觉备注、游戏规则和真实候选点，要求模型为每个规则需求挑一个 `candidate_id`。

输出：

```text
default_camera_packet/gameplay_placement_prompt.md
default_camera_packet/gameplay_placement_raw_response.md
default_camera_packet/gameplay_placement_plan.json
```

模型原始输出的核心结构：

```json
{
  "selection_status": {"usable": true, "confidence": 0.0, "reason": "..."},
  "placements": [
    {
      "element_type": "collectible",
      "source_requirement_index": 0,
      "selected_candidate_id": "candidate_...",
      "placement_mode": "on_ground",
      "height_offset": 0,
      "gameplay_purpose": "...",
      "placement_reason": "...",
      "surface_alignment_mode": "auto",
      "max_tilt_degrees": 25,
      "surface_clearance_m": 0.02,
      "vegetation_clearance_radius_m": 0.25,
      "yaw_degrees": 0
    }
  ],
  "selection_notes": ["..."]
}
```

关键规则：

- `selected_candidate_id` 必须来自候选 JSON，且所有 placement 不可重复使用同一候选点。
- `placement_mode` 必须是该候选点允许的模式；普通地面物体用 `on_ground`，只有可达的悬空物才用 `above_ground`。
- `above_ground` 时 `height_offset` 必须为正数；其他模式必须为 `0`。
- `surface_alignment_mode` 推荐 `auto`。它让 Python 根据真实法线决定贴地、贴坡或贴墙；不是让 LLM 靠猜一个旋转角度。
- `yaw_degrees` 是可选的平面朝向微调，范围 `-180..180`；没有明确需要时可以省略。它不是“让物体竖直/平放”的主要机制。
- 选择点应在景物之间的真实空地，离周围植被足够近以融入画面、又不能重叠或挤进现有物体。候选事实中 `0.16m` 的树/灌木净空并不算开阔。

脚本把 `candidate_id` 确定性回填成 `anchor_world_xyz` 和 `world_xyz`，并生成正式的 `gameplay_placement_plan.json`。这份文件是之后**位置的唯一来源**。

人工介入方式：先看 `gameplay_placement_raw_response.md` 和正式计划。如果只是逻辑描述需要改，可编辑正式 JSON；但若换了 `candidate_id`，必须同步从对应 `placement_candidates.json` 复制该点的 `anchor_world_xyz`、`world_xyz`、法线和上下文，且保持唯一性。因此更稳妥的方式是改规则/提示后重跑本步，而不是手写坐标。改动放置计划后，从第 7 步重新执行。

## 7. 第六步：LLM 写资产实现计划 JSON

运行：

```bash
python ./agent/game_staging/run_asset_realization.py \
  --user_prompt "$USER_PROMPT"
```

脚本：`agent/game_staging/run_asset_realization.py`。它不会再选择位置，只读取已经固定的 placement，决定每个 placement 是否需要可见 GLB、资产是否可以复用、资产尺度、文生图/图生 3D 提示词和 GLB 输出路径。

输出：

```text
asset_realization/asset_realization_prompt.md
asset_realization/asset_realization_raw_response.md
asset_realization/asset_plan.json
```

这是最重要的人工 JSON 干预点。正式 JSON 结构为：

```json
{
  "assets": [
    {
      "asset_id": "asset_001",
      "asset_name": "...",
      "asset_role": "obstacle",
      "placement_ids": ["placement_003"],
      "generation_prompt_en": "one isolated, standalone ...",
      "negative_prompt_en": "...",
      "expected_glb_path": "./assets/generated_glb/asset_001.glb",
      "expected_size_meters": [2.5, 1.2, 1.1],
      "flat_base_policy": "reject",
      "flat_base_reason": "organic obstacle must not gain an artificial slab base"
    }
  ],
  "non_glb_placements": [
    {
      "placement_id": "placement_010",
      "implementation": "runtime logical trigger",
      "reason": "..."
    }
  ],
  "notes": ["..."]
}
```

编辑 `asset_plan.json` 时必须遵守：

- 每一个 `placement_id` 必须**恰好出现一次**：要么属于一个 GLB 资产的 `placement_ids`，要么在 `non_glb_placements` 中。
- `asset_role` 必须与对应 placement 的 `element_type` 相同；障碍物、地标、目标区等可见内容通常必须有 GLB，纯逻辑区可以是 `non_glb_placements`。
- `expected_size_meters` 的轴顺序是 `[X 宽, Y 深, Z 高]`，单位为米。这是最后导入 Blender 时的目标真实尺寸；不要把它写成 `[宽, 高, 深]`。
- `generation_prompt_en` 必须描述**一个独立物体**，并明确当前场景的材质、颜色、风化和风格；不要描述完整场景、人物、镜头、文字或 UI。
- 现有树林/灌木/岩石是场景基底，不能替代新增 gameplay obstacle。障碍物必须是玩家能跳、躲或绕过的独立游戏物体；不要生成孤立柱子、立柱、图腾、残墙碎片等无依托形态。
- 资产是否复用由 LLM/人工根据实际是否合适来决定，不强制“一物一个 GLB”，也不强制所有 placement 复用同一个 GLB。
- `flat_base_policy`：有意带平整底座的箱子、平台等可为 `allow`；有机岩石、根系、植被、收集物、雕塑物通常为 `reject`，以防生成一个不自然的扁平底盘。

若你先让外部 LLM 手工生成 JSON：把其**纯 JSON**保存到 `asset_realization_raw_response.md`，然后运行：

```bash
python ./agent/game_staging/run_asset_realization.py \
  --user_prompt "$USER_PROMPT" \
  --reuse_raw_response
```

`--reuse_raw_response` 的意思是：不再调用 LLM，而是读取已有的原始回复，做 schema 校验并产出 `asset_plan.json`。它适合“先修改 LLM response，再让脚本验证”。如果只改了已经生成的 `asset_plan.json`，无需使用该参数，直接从第 7 步继续。

## 8. 第七步：文生图并用本地 Hunyuan3D 生成 GLB

运行前，进入已装好 Hunyuan3D 的 conda 环境。已跑通的稳妥参数是 `steps=20`、`octree_resolution=128`、`num_chunks=8000`；单张 4090 D（24GB）上不要同时再常驻单独的 Hunyuan Gradio 服务，否则很容易 OOM。

```bash
CUDA_VISIBLE_DEVICES=0 python -u ./agent/game_staging/run_asset_generation_stage.py \
  --asset_plan ./output/game_staging/asset_realization/asset_plan.json \
  --hunyuan_backend local \
  --hunyuan_repo "$HUNYUAN3D_REPO" \
  --model_path "$HUNYUAN3D_MODEL_PATH" \
  --texgen_model_path tencent/Hunyuan3D-2.1 \
  --t2i_backend dashscope_sdk_qwen_image \
  --reuse_reference_images \
  --start 0 --limit 999 \
  --steps 20 \
  --octree_resolution 128 \
  --num_chunks 8000 \
  --export_texture \
  --skip_existing
```

脚本：`agent/game_staging/run_asset_generation_stage.py`。

它先读取 `asset_plan.json` 的 `generation_prompt_en`，生成纯白背景的单物体参考图，再调用 Hunyuan3D 生成并贴图，最后按 `expected_glb_path` 写入 GLB。主要输出：

```text
asset_generation/reference_images/<asset_id>.png
asset_generation/hunyuan_raw/...
asset_generation/asset_generation_manifest.json
asset_generation/asset_generation.log
assets/generated_glb/*.glb                 # 路径由 asset_plan 决定
```

`--reuse_reference_images` 只复用已经生成成功的参考图，不会重新文生图；`--skip_existing` 会跳过已有且有效的最终结果。若要只检查文生图，可附加 `--reference_images_only`。如果某一个 GLB 不理想，优先修改 `asset_plan.json` 的英文提示词/尺寸/资产拆分，仅对该资产使用 `--start` 和 `--limit 1` 重跑。

## 9. 第八步：把 GLB 解析并放进原始 Blend

运行：

```bash
"$BLENDER" -b "$SOURCE_BLEND" --python-exit-code 1 \
  --python ./agent/game_staging/stage_gameplay_assets.py -- \
  --scene_blend "$SOURCE_BLEND"
```

脚本：`agent/game_staging/stage_gameplay_assets.py`。

它读取以下三个 JSON：

```text
default_camera_packet/gameplay_placement_plan.json  # 固定位置、候选点与对齐信息
default_camera_packet/placement_candidates.json     # 原始表面事实
asset_realization/asset_plan.json                   # GLB 路径、大小、绑定关系
```

脚本导入 GLB，把其尺寸归一到 `expected_size_meters`，按真实采样表面放置/贴坡/贴墙，处理很小的地面间隙、收集物悬浮和旋转，并对会挡住新增资产的独立植被做必要清理。它不会为了修正视觉问题任意移动到另一处候选点。

输出：

```text
output/game_staging/staged_scene.blend
output/game_staging/asset_placement/asset_placement_report.json
output/game_staging/asset_placement/resolved_gameplay_placement_plan.json
output/game_staging/asset_placement/previews/*.png
```

先看 `previews/` 和 `asset_placement_report.json`，再在 Blender 中检查 `staged_scene.blend`。若尺寸太小、风格不搭、资产形态不对，回到第 7 步改 `asset_plan.json`，再重做第 8、9 步；若位置本身不对，回到第 6 步改/重跑放置，再从第 7 步开始。

## 10. 第九步：Python 从固定 placement 生成导演路线 JSON

运行：

```bash
python ./agent/game_staging/build_gameplay_director_path.py \
  --placement_plan ./output/game_staging/default_camera_packet/gameplay_placement_plan.json \
  --asset_plan ./output/game_staging/asset_realization/asset_plan.json \
  --output ./output/game_staging/director_path/director_path.json \
  --genre generic \
  --fps 24 \
  --target_duration_seconds 50 \
  --turn_smoothing 0.35 \
  --maximum_turn_degrees_per_second 120 \
  --run_speed_mps 7.0 \
  --camera_distance_m 7.5 \
  --camera_height_m 3.8
```

脚本：`agent/game_staging/build_gameplay_director_path.py`。这一步**不调用 LLM**，也不会重选或移动 gameplay asset。它按 `route_order`（没有则按 placement id）读取已固定的 placement，从 element type、gameplay purpose 和显式 event/movement 字段推导经过、收集、绕行、跳跃、交互和到达目标等 `beats`；随后使用有界三次 Hermite 曲线生成有弧度的转弯并进行 3D 定距插帧，按 ground/drive/flight 速度、事件停留时间和最大转向角速度计算帧号。路径同时输出连续展开的 `heading_yaw_degrees`，避免前后朝向瞬间翻转，并保留旧版 `character_position/camera_position/look_at` 兼容字段。目标时长只能拉长物理时间线，不能把它压缩到超速或瞬移；若 50 秒不足，输出中的 `requested_target_duration_honored` 会为 `false`。可用 `--genre fps|tps|racing|wingsuit` 选择通用运动/相机 profile，但脚本本身不含任何场景坐标或 placement ID。

输出：

```text
output/game_staging/director_path/director_path.json
```

可以人工检查/编辑 `director_path.json` 的 `beats`（如 `character_position`、`frame`、`event_type`、`camera_position`、`look_at`），但不要改成会穿过障碍物或脱离地形的路线。通用 builder 不再包含 `asset_003`、`asset_004` 或任何其他 production placement/asset ID 特例；跳跃、绕行和交互距离由 element type、gameplay purpose、movement mode 与资产尺寸推导。

## 11. 第十步：导入角色、创建后上方跟随相机并渲染视频

准备一个带动画的角色 FBX，例如 Mixamo 的跑步角色。运行：

```bash
export NPC_FBX=/absolute/path/to/animated_character.fbx

"$BLENDER" -b ./output/game_staging/staged_scene.blend --python-exit-code 1 \
  --python ./agent/game_staging/stage_director_gameplay.py -- \
  --scene_blend ./output/game_staging/staged_scene.blend \
  --director_path ./output/game_staging/director_path/director_path.json \
  --npc_fbx "$NPC_FBX" \
  --npc_height_m 1.7 \
  --actor_radius_m 0.42 \
  --ground_clearance_m 0.01 \
  --maximum_ground_step_m 0.85 \
  --maximum_slope_degrees 45 \
  --maximum_collision_detour_m 4.2 \
  --camera_distance_m 7.5 \
  --camera_height_m 3.8 \
  --camera_collision_radius_m 1.0 \
  --camera_min_ground_clearance_m 1.2 \
  --render_video \
  --video_samples 16
```

脚本：`agent/game_staging/stage_director_gameplay.py`。

它读取 `staged_scene.blend` 与 `director_path.json`，导入并以世界空间脚底包围盒校正角色；在真实场景上重新采样地面和五点足底支撑，用角色体宽扫掠路线，并在树干、岩石、建筑和 gameplay asset 周围插入不移动事件锚点的弧形绕行采样；碰撞修正后把事件停留时间纳入预算，再重新校验实际移动速度和转向角速度。FBX 导入若擅自把场景从 24 fps 改成 30 fps，脚本会恢复 director 指定的帧率，避免验证速度和最终播放速度不一致。角色循环步态按实际移动距离同步，事件 dwell 写成真正的静止关键帧；跟随相机除关键点求解外，还会对最终插值时间线逐帧检查镜头是否埋入几何体、是否被遮挡，并将机位平滑移动到可见候选位置。所有求解统计和未解决碰撞都会写入 report。输出：

```text
output/game_staging/director_gameplay/
├── staged_director_gameplay.blend
├── director_gameplay_report.json
└── director_gameplay.mp4
```

如果只想先检查相机与运动，不渲染视频，去掉 `--render_video`；也可加 `--render_previews` 查看关键帧预览。最终相机距离、高度、焦距和跟随平滑度可通过本脚本参数调整，不需要回到默认相机包。

## 12. JSON 人工干预的重跑规则

下面的表是最重要的“改什么、从哪里重跑”规则。

| 你修改的文件 | 典型修改 | 必须从哪一步重新跑 |
| --- | --- | --- |
| `scene_understanding_default_camera.json` | 场景类型、地面/水体/植被判断 | 第 3 步（规则） |
| `game_rule_visual_notes.json` | 视觉约束、规则设计风险 | 第 3 步的规则 LLM，或手动同步修改 `game_rule_plan.json` 后从第 5 步 |
| `game_rule_plan.json` | 元素类别/数量、目标、动作、时限 | 第 5 步（候选点可复用时从第 6 步也可，但推荐重新提取） |
| `placement_candidates.json` | 不建议手改；重新提取采样点 | 第 6 步 |
| `gameplay_placement_plan.json` | 更换位置/高度/表面对齐 | 第 7 步 |
| `asset_plan.json` | GLB 提示词、尺寸、复用关系、资产类型 | 第 8 步 |
| `director_path.json` | 角色节奏、镜头/角色路径 | 第 11 步 |

一句话总结：**玩法变了，重跑放置及以后；位置变了，重跑资产计划及以后；资产外观/尺寸变了，重跑生成与 Blender 放置；镜头/运动变了，只重跑最终导演渲染。**

## 13. 排错与检查顺序

每次出错优先看同阶段的 `*_prompt.md`、`*_raw_response.md` 和 `*_report.json`/manifest，而不是盲目重跑：

1. LLM 返回字段不合法：先看 raw response 是否不是纯 JSON、字段是否缺失、是否把坐标/路径写进不该写的阶段。
2. 位置不合理：检查 `placement_candidates.json` 中该 `candidate_id` 的地表、法线、植被净空和可行走性；不要只看默认相机截图。
3. GLB 不合理：检查 `asset_plan.json` 的 `generation_prompt_en`、`expected_size_meters` 和 `flat_base_policy`，再看 reference image。
4. Blender 放置不理想：检查 `asset_placement_report.json` 与预览图，先区分是“资产自身不对”还是“位置不对”。
5. 显存 OOM：确保没有另一个常驻 Hunyuan 服务占用同一张 GPU；缩小 batch 范围（`--start/--limit`），必要时降低 `octree_resolution` 或纹理质量；已有参考图时保留 `--reuse_reference_images`。

本手册描述的是已经验证的研究原型主链。输出 JSON 是明确的人工检查点：可以干预，但应保持 schema 与上下游绑定关系一致，而不是把所有决策硬编码进 Python。
