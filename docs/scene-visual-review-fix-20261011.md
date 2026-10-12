# 2026-10-11 场景错位与视觉误放行排查

已确认两件事：Agent3 对有明显可疑连接的场景返回了空问题列表；确定性几何检查没有执行连接检查，却报告通过。当前修复阻止了后一种误放行。视觉模型本身的漏检尚未解决，当前软件里的场景也尚未修复或重新验收。

## 原始证据

对应场景是 `runs/20261011-211847-d3f4b8e2/scene-003`。这里的截图和坐标可与用户截图核对；不是后来停在 planning 的 `runs/20261011-222758-ab15c9bc` 生成的场景。

- `scene-feedback.json`：`verdict=pass`，`findings=[]`。
- `training-data/candidates/9a8e82fe43944f5da47af51e3b14e308.json`：真实视觉模型调用，传入三张场景图片，返回上述结论。
- `scene-003/geometry-validation.json`：`passed=true`，但 `scope=unsupported_topology`、`spatial_checks_applied=false`。
- `scene-003/load-receipt.json`：BeltConveyor4M 的 `component_geometry` 为空。两个传感器射线均近似沿世界 Z 正方向；皮带长轴也沿 Z。射线所在 X 约为 4.5621 m，供料原点 X 为 4.3125 m。

左侧橙色物体是移除器，绿色物体是供料器。移除器位于输送带末端之外可以是合理设计。应检查供料原点、检测射线和移除体积是否与实际物料路径相接，不能仅按屏幕左右或颜色判断。截图和当前回执未证明实际物料尺寸、运动轨迹或有效检测；不据此编造精确修正坐标。

## 根因与修改

1. 原桥接层只提取 RollerConveyor 的几何信息，没有提取 BeltConveyor。补充读取实际 `BeltConveyor.beltRenderer` 的表面包围盒，以及物理组件 `BtLinearConveyorBelt.transform.forward` 的正速度方向。后者已通过本机安装版本的组件代码核对，未按物体名称猜测流向。
2. Python 检查器在不支持的拓扑上返回通过，且 `loaded_geometry()` 再以“问题列表为空”覆盖结论。现在缺失几何、不支持拓扑、缺少回执均返回未通过；健康的原生注册不会把未检查变成通过。
3. 工作流新增独立门槛：`spatial_checks_applied=false` 时转为 `needs_attention/scene_geometry/tools`，不调用视觉、PLC 或编译器。已经执行并失败的连接检查仍走场景修复。通过检查时，把检查结果及其范围加入视觉 handoff。
4. 视觉提示词改为检查物体之间的功能连接，包括供料、射线、物料路径、高度与移除区域。明确 XZ 可达性不足以证明物料检测、垂直对齐和下游移除。

当前确定性连接检查仅支持一条具备测量信息的输送机和一个供料器，检查范围仍是 XZ 必要可达条件。不支持的拓扑会明确要求补充工具能力；这不是已完成全部工业拓扑的几何验收。运行时物理验收仍需独立执行。

## 实际复核结果

调用框架的 `VisionAgent`，用当前提示词重新审查同一版本的三张原始图片。没有把新检查器的失败结果注入模型，便于观察视觉审查本身的变化。

结果：第一轮输出格式错误；第二轮仍返回 `pass`、空 findings。提示词修改不足以修复这次漏检。新几何检查器对同一份原生回执返回 `passed=false/scope=missing_conveyor_geometry`；新工作流会阻止继续推进。不能把这次视觉复核记为成功纠错或训练 gold。

产物在 `artifacts/scene-visual-diagnosis-20261011-001/vision-recheck`：

- `scene-review-result.json`：复核结果、输入图像哈希、调用数量、source_unchanged 等。
- `scene-feedback.json`：新的视觉输出，仍为 pass。
- `geometry-recheck.json`：新的几何结论。
- `progress.log`、`training-data/candidates`：模型公开输出和完整候选记录。候选默认待审核，不进入 SFT/DPO gold。

复核工具可用于其他归档场景，运行目录是项目根目录 `F:\phd\project\digital-twin-factoryi-o`：

```powershell
$reviewOut = Join-Path 'artifacts' ('scene-review-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
& .\.venv\Scripts\python.exe tools\review_archived_scene.py `
  --source runs\20261011-211847-d3f4b8e2 `
  --scene-revision scene-003 `
  --out $reviewOut
```

该工具复用框架的 VisionAgent、调用预算、公开进度和候选记录。只读取归档输入并调用模型；不会操作 Factory I/O 或 PLC，不复制整套框架源代码。它保留原始场景/任务/I/O 身份，并把新的模型策略和配置身份记在新的诊断输出中。复核完成不代表当前软件状态或整个流程验收通过。

## 验证范围与剩余工作

Python 回归验证覆盖未执行检查的阻断、已执行失败检查的场景修复、检查报告的视觉传递，以及原有工作流。桥接补丁已在 `artifacts/scene-visual-diagnosis-20261011-001/bridge-build` 编译成功，未改安装目录的 DLL、未修改当前软件状态、未加载到运行中的 Factory I/O。

剩余工作是加载并验证新桥接层的真实皮带几何回执，再由 CLI 框架执行连接修复与物理验收。视觉检查还需更聚焦的连接级证据和实际漏检评测；不能继续依赖一次空 findings 作为充分场景验收。
