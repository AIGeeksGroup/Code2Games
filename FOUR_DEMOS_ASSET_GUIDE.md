# Code2Games 四个 Demo：资产用途与 JSON 索引

本文记录 FPS、TPS、Racing、Wingsuit/F-104 四个 Demo 的已确认游戏资产语义。
资产位置以各自的 `gameplay_placement_plan.refined.json` 为唯一真值；后续 Director、角色、车辆、飞机、相机和特效不得移动这些固定资产。

## JSON 路径

### Stage 6 固定放置方案

| Demo | 服务器路径 |
| --- | --- |
| FPS | `output/game_staging/game_fps/asset_placement_v6_platform_fix5/gameplay_placement_plan.refined.json` |
| TPS | `output/game_staging/game_tps/asset_placement_v6_mountain_platform_fix5/gameplay_placement_plan.refined.json` |
| Racing | `output/game_staging/game_racing/asset_placement_v6_route_fix4/gameplay_placement_plan.refined.json` |
| Wingsuit | `output/game_staging/game_wingsuit/asset_placement_v6_verified_fix6/gameplay_placement_plan.refined.json` |

### Stage 11 Director 路线方案

| Demo | 服务器路径 |
| --- | --- |
| FPS | `output/game_staging/game_fps/director_path_showcase_v5/director_path.json` |
| TPS | `output/game_staging/game_tps/director_path_showcase_v5/director_path.json` |
| Racing | `output/game_staging/game_racing/director_path_showcase_v3/director_path.json` |
| Wingsuit | `output/game_staging/game_wingsuit/director_path_showcase_v4/director_path.json` |

本地下载的固定放置方案位于 `finish_experiments/` 对应资产放置目录。

## FPS：Red Valley Last Uplink

### 主目标与补给

| 资产 ID | 类型 | 用途 |
| --- | --- | --- |
| `fps_required_cell_west` | collectible | 西侧战斗口袋的必需 uplink 能源电池。 |
| `fps_required_cell_east` | collectible | 东侧中距离战斗口袋的必需 uplink 能源电池。 |
| `fps_optional_ammo_near` | collectible | 开局交战用的可选弹药箱。 |
| `fps_optional_medkit_right` | collectible | 暴露右侧侧翼的可选医疗补给。 |
| `fps_optional_ammo_mid` | collectible | 向 uplink 过渡时的中场可选弹药。 |
| `fps_emergency_uplink_trigger` | event_trigger | 消耗两块必需能源电池、启动 uplink 与最终攻势的互动终端。 |
| `fps_final_extraction_goal` | goal_area | uplink 启动、最终战斗解决后可进入的撤离区。 |

### 掩体、威胁与恢复

| 资产 ID | 类型 | 用途 |
| --- | --- | --- |
| `fps_cover_near_center_a` | obstacle | 开局射击通道的第一块人工掩体。 |
| `fps_cover_near_center_b` | obstacle | 与前者错位的第二块掩体，形成开局选择。 |
| `fps_cover_right_flank` | obstacle | 保护穿越暴露右侧侧翼的掩体。 |
| `fps_cover_west_a` / `fps_cover_west_b` | obstacle | 西侧能源电池周围的主/次掩体。 |
| `fps_cover_mid_center` | obstacle | 中场掩体，切断最长暴露射线。 |
| `fps_cover_east_mid` | obstacle | 东侧能源电池附近的推进掩体。 |
| `fps_cover_uplink_approach` | obstacle | 进入 uplink 攻坚区前的前方掩体。 |
| `fps_hazard_opening_suppression` | hazard_zone | 开局压制火力预警，要求横向进入掩体。 |
| `fps_hazard_right_impact` | hazard_zone | 右侧补给附近的预警落点。 |
| `fps_hazard_west_impact` | hazard_zone | 西侧推进中的中段落点危险。 |
| `fps_hazard_final_crossfire` | hazard_zone | 中场至 uplink 的最终交叉火力区。 |
| `fps_safe_rear_recovery` | safe_zone | 开局后方回血/换弹的安全口袋。 |
| `fps_safe_forward_recovery` | safe_zone | uplink 最终战前的前方恢复区。 |

### 导航地标

| 资产 ID | 类型 | 用途 |
| --- | --- | --- |
| `fps_landmark_combat_beacon` | landmark | 中场定向，关联两个能源电池与 uplink。 |
| `fps_landmark_uplink_mast` | landmark | 标识 uplink 区的高耸发光桅杆。 |
| `fps_landmark_extraction_beacon` | landmark | 标识最终撤离区的大型信标。 |

Showcase 主线：开局掩体 → 弹药 → 右侧敌人 → 东侧能源电池 → 最终交叉火力 → uplink 互动 → 撤离。

## TPS：Woodland Signal Holdout

### Relay、补给与终点

| 资产 ID | 类型 | 用途 |
| --- | --- | --- |
| `placement_000` | event_trigger | 第一座紧急信号 relay，触发开局战斗。 |
| `placement_001` | event_trigger | 第二座紧急信号 relay，触发最终战斗。 |
| `placement_009` | collectible | 开局战斗的可选弹药。 |
| `placement_010` | collectible | 开局战斗的可选医疗补给。 |
| `placement_011` | collectible | 右侧风险绕路上的可选弹药。 |
| `placement_012` | collectible | 第二 relay / 撤离战前的可选医疗补给。 |
| `placement_021` | goal_area | 两个 relay 完成后解锁的林地撤离区。 |

### 掩体、威胁与恢复

| 资产 ID | 类型 | 用途 |
| --- | --- | --- |
| `placement_002` / `placement_003` | obstacle | 第一 relay 遭遇战的近距离掩体组合。 |
| `placement_004` / `placement_005` | obstacle | 两个 relay 之间的中场掩体组合。 |
| `placement_006` | obstacle | 第二 relay 战区的入口掩体。 |
| `placement_007` | obstacle | 最终战区的主掩体。 |
| `placement_008` | obstacle | 最终战区的侧翼掩体。 |
| `placement_013` | hazard_zone | 开局压制/冲击危险区。 |
| `placement_014` | hazard_zone | 连接两个战区的中远距离危险区。 |
| `placement_015` | hazard_zone | 最终战区危险区，促使玩家在右侧掩体间移动。 |
| `placement_016` | safe_zone | 开局恢复区。 |
| `placement_017` | safe_zone | 两场 relay 遭遇间的中场恢复区。 |

### 导航地标

| 资产 ID | 类型 | 用途 |
| --- | --- | --- |
| `placement_018` | landmark | 开局战区的近距离视觉地标。 |
| `placement_019` | landmark | 密林短视距中的远方定向地标。 |
| `placement_020` | landmark | 最终 relay 与撤离方向的右侧地标。 |

Showcase 主线：林间开局 → 医疗补给 → 掩体交火 → 中场推进 → 东侧战区 → 激活 relay → 撤离。未走到的 relay、补给和掩体是替代路线或环境语义，不应被无故隐藏。

## Racing：Dustline Valley Rally

### 计时、加速与终点

| 资产 ID | 类型 | 用途 |
| --- | --- | --- |
| `racing_event_start_countdown` | event_trigger | 倒计时、引擎启动和发车锚点。 |
| `racing_boost_early_center` | collectible | 干净发车后可拿到的早期 boost。 |
| `racing_boost_left_choice` | collectible | 宽阔左侧路线的 boost 奖励。 |
| `racing_boost_mid_right` | collectible | 高风险右侧路线的 boost 奖励。 |
| `racing_boost_grass_bonus` | collectible | 草地开放通道的 bonus boost，不能放在树下。 |
| `racing_boost_late_risk` | collectible | 终点前的高风险加速机会。 |
| `racing_event_timing_gate_01` | event_trigger | 发车后的第一计时门。 |
| `racing_event_timing_gate_02` | event_trigger | 中段分支区的第二计时门。 |
| `racing_event_timing_gate_03` | event_trigger | 早期路线汇合处的第三计时门。 |
| `racing_event_timing_gate_04` | event_trigger | 终点前最后一个计时门。 |
| `racing_goal_finish` | goal_area | 完成全部计时门验证后的比赛终点。 |

### 路线边界、风险与恢复

| 资产 ID | 类型 | 用途 |
| --- | --- | --- |
| `racing_barrier_start_left` / `racing_barrier_start_right` | obstacle | 起跑线两侧护栏，保持中央发车通路。 |
| `racing_barrier_gate1_left` / `racing_barrier_gate1_right` | obstacle | 第一门的左右边界，形成转向开口。 |
| `racing_barrier_mid_left` / `racing_barrier_mid_right` | obstacle | 中段安全与高风险路线分叉边界。 |
| `racing_barrier_left_branch` | obstacle | 左侧宽路线的外侧边界。 |
| `racing_barrier_finish_edge` | obstacle | 终点侧边视觉导向护栏。 |
| `racing_barrier_cliff_edge_outer` | obstacle | 高台山崖外侧轮胎/护栏标记，定义可驾驶边缘。 |
| `racing_hazard_mid_right_mud` | hazard_zone | 右侧泥地：更快但抓地较差。 |
| `racing_hazard_mid_left_rough` | hazard_zone | 左侧粗糙地面：另一种速度惩罚。 |
| `racing_hazard_late_scree` | hazard_zone | 终点前松散碎石地表。 |
| `racing_safe_start_recovery` | safe_zone | 起跑侧车辆恢复点，代价是时间。 |
| `racing_safe_late_recovery` | safe_zone | 终点前恢复点。 |

### 导航地标

| 资产 ID | 类型 | 用途 |
| --- | --- | --- |
| `racing_landmark_start_banner` | landmark | 起跑 banner，建立赛车身份与方向。 |
| `racing_landmark_decision_tower` | landmark | 标识中段风险选择区的 rally tower。 |
| `racing_landmark_finish_approach` | landmark | 宣告终点冲刺方向的地标。 |

Showcase 主线：发车 → early boost → Gate 1 → 草地 bonus → 北侧草地走廊 → 碎石风险区 → 山崖边受支撑平台爬升 → 终点。未接入安全车宽通路的山顶资产保留为可见场景信息，不强迫赛车穿越。

## Wingsuit / F-104：Riftwing Stormline Extraction

### 山顶准备与发射

| 资产 ID | 类型 | 用途 |
| --- | --- | --- |
| `placement_021` | landmark | 山顶坠机点；建立开场与起飞背景。 |
| `placement_000` | collectible | 山顶第一块必需稳定器电池。 |
| `placement_001` | collectible | 山顶第二块必需稳定器/风力符文。 |
| `placement_003` | collectible | 小幅抬高的飞行信标，用于可达小跳跃。 |
| `placement_004` | obstacle | 山顶低矮 storm barrier，形成一次明确绕行。 |
| `placement_005` | obstacle | 坠毁调查无人机，形成短跳/闪避选择。 |
| `placement_006` / `placement_007` | obstacle | 山顶遗迹/障碍，强调觉醒事件的规模。 |
| `placement_008` | obstacle | 折断桅杆碎片，定义山顶 scramble 外边界。 |
| `placement_009` | obstacle | 通往发射区前的最后地面阻碍。 |
| `placement_010` | event_trigger | 巨物/风暴觉醒触发器。 |
| `placement_011` | event_trigger | 追击、落石与地面冲击升级触发器。 |
| `placement_014` / `placement_015` / `placement_018` | hazard_zone | 山顶风暴、stomp 与发射前冲击预警。 |
| `placement_019` | safe_zone | 山顶风障安全区，用于校准与起飞准备。 |
| `placement_022` | landmark | 山崖发射架；由地面阶段切换至飞行阶段的关键地标。 |

### 山谷飞行与撤离

| 资产 ID | 类型 | 用途 |
| --- | --- | --- |
| `placement_017` / `placement_018` | hazard_zone | 山体旁的早期飞行危险标记，用于近崖闪避展示。 |
| `placement_002` | collectible | 谷底附近第三块必需稳定器，飞机低空掠过时获取。 |
| `placement_016` | hazard_zone | 最终进场前低空落石预警。 |
| `placement_023` | landmark | 救援 homing mast，指示最终撤离方向。 |
| `placement_012` | event_trigger | 最终控制门/飞行控制权交接触发器。 |
| `placement_013` | goal_area | 紧急着陆或撤离平台目标区。 |
| `placement_020` | safe_zone | 着陆端恢复安全区。 |

Showcase 主线：坠机点建立 → 山顶稳定器充能与发射架准备 → 飞机向前起飞离崖 → 单向山谷掠过并收集谷底稳定器 → 避开落石 → 锁定救援桅杆 → 穿越最终控制门 → 拉升收尾。

山顶资产解释“为何能起飞”；谷底资产解释“飞行任务”；救援桅杆和控制门解释“为何结束”。飞机不得把它们当无意义装饰绕圈。

## 专用 Showcase 脚本

| Demo | 脚本 | 预期最终 Blend |
| --- | --- | --- |
| FPS | `agent/game_staging/stage_fps_showcase_gameplay.py` | `output/game_staging/game_fps/staged_scene_v6_fps_showcase_v5.blend` |
| TPS | `agent/game_staging/stage_tps_showcase_gameplay.py` | `output/game_staging/game_tps/staged_scene_v6_tps_showcase_v5.blend` |
| Racing | `agent/game_staging/stage_racing_showcase_gameplay.py` | `output/game_staging/game_racing/staged_scene_v6_racing_showcase_v3.blend` |
| Wingsuit | `agent/game_staging/stage_wingsuit_showcase_gameplay.py` | `output/game_staging/game_wingsuit/staged_scene_v6_aircraft_showcase_v4.blend` |

四个专用脚本都先调用 `build_genre_director_path.py` 生成场景专属 Stage 11 JSON，再调用 `stage_genre_director_gameplay.py` 执行 Stage 12。它们允许定制角色、车、飞机、相机和事件，但不允许重新放置已确认资产。
