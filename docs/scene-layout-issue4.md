# 需求驱动的布局与视觉反馈（Issue #4）

实现依据：[Issue #4](https://github.com/C3Shirt/dgwICS/issues/4)，包括更新后的专业任务指导与动态上下文设计。此阶段只检查静态布局，不生成或运行 PLC。

## 流程与职责

```text
原始需求 → 独立 RequirementSpec → SceneDesign → 相对布局求解
  → 编译 DynamicCheckPlan → 构建前检查 → Factory I/O 真实加载
  → 原生实例检查 + top / iso_a / iso_b 截图 → 视觉逐项检查
  → 证据融合 → 通过 / 无法确认 / RepairTicket
  → Scene Agent 局部 Patch → 新版本加载、截图和完整复查
```

需求提取与设计是同一 Scene Agent 的两次独立调用；不增加 Planner、Supervisor 或群聊。提取时看原始文本、算子说明和可用部件名称，不看生成布局。数量、角色、关系和来源随任务变化。运行行为保存在 `deferred-tasks.json`，不以静态截图宣布运行成功。

`RequirementSpec` 保留原文引用与显式要求。模型假设不得成为硬约束；执行范围与非约束措辞放入有解释的 `context_fragments`。引用覆盖检查只能发现未引用文本，不能保证语义拆解绝对忠实；基准任务可用人工核对的 Spec。精确 prefab 选择器、能力名和算子参数在接受提取结果前校验，未知物理功能保留 `unsupported`。

CheckPlan 从独立要求绑定本轮角色与关系，不接受设计方的“已经正确”作为真值。通用算子覆盖数量、能力、空间关系、射线/体积、供料、输送交接、检测和接收的有限几何前提。没有 Emitter 的任务不需要供料检查，没有 Conveyor 的合法布置也不要求输送拓扑。注册白名单不执行模型提供的 Python。

## Prompt 与动态信息

英文静态提示词在 `factoryio_agents/simple_prompts.py`；三个角色上下文由 `factoryio_agents/scene_context.py:PromptContextBuilder` 分别生成，版本为 `issue4-guided-context-v3`。

| 调用 | 本轮信息 |
| --- | --- |
| 需求提取 | 原始文本、输出字段、注册算子及参数含义、真实 prefab 与测量能力名称 |
| 场景设计 | 原始需求、独立 Spec、必须绑定的角色、筛选部件及配置、选中候选的实测局部几何、布局接口、相关官方知识 |
| 视觉检查 | 原始需求、Spec、当前对象 ID/类型/功能、检查问题与预期关系、图像清单和真实图片、相关设备识别知识 |
| 局部修复 | 相关原始约束、Ticket、目标对象与冻结锚点、相关关系/局部测量、允许操作与对象、直接相关图片证据 |

视觉请求不注入生成坐标、`expected_position`、XML 或 PLC。每张图片绑定场景 SHA、图像 SHA、视角、采集时间及加载/截图请求；版本、文件哈希或相机回执不一致会拒绝检查。VLM 必须逐项返回 `satisfied / violated / unobservable`，说明对象、观察、原因和图片 ID；无法判别时最多补拍一次现有视角。

服务器响应返回非对象内容（如 `[]`）时，保留失败原始回复，最多一次改用 JSON object 传输并继续严格本地校验；不把网络错误当布局错误。格式校正也最多一次，不能无限重试。记录中的 `structured_output: null` 不是原始模型返回文本，原文应查看 `response_text`。

## 使用与恢复

在 PowerShell 中进入项目根目录；Factory I/O 应启动并加载现有桥接，模型使用本机配置。

```powershell
Set-Location 'F:\phd\project\digital-twin-factoryi-o'
& .\.venv\Scripts\python.exe run_simple_agents.py '构建两个独立输送工位，两条输送带分开放置，不需要供料器或移除器。只检查静态布局，不运行 PLC。' --scene-only --out runs\my-layout-001
```

新场景默认使用 `--scene-verification relations`：动态关系 + 原生几何 + 视觉。`relations_visual` 用于消融空间几何计算；`legacy` 保留原流程作对照。旧 PLC 流程继续使用 legacy，当前新布局流程不会隐式启动它。

```powershell
# 复用已经有效的需求、设计和布局；新目录保存本轮加载/截图证据
& .\.venv\Scripts\python.exe run_simple_agents.py --resume runs\my-layout-001 --scene-only --out runs\my-layout-002

# 可选：使用人工核对的独立需求，而不是在线提取
& .\.venv\Scripts\python.exe run_simple_agents.py '与 Spec 一致的原始需求' --requirement-spec verified-spec.json --scene-only

# 受控故障实验；仅在该布局有对应实测能力时适用
& .\.venv\Scripts\python.exe run_simple_agents.py --resume runs\my-layout-002 --scene-only --inject-fault emitter_gap --out runs\my-fault-001

# 相同历史图片对照；不重新加载、修改或修复 Factory I/O
& .\.venv\Scripts\python.exe run_simple_agents.py --compare-visual-prompts runs\my-fault-001 --comparison-revision 0 --out runs\my-comparison-001
```

恢复先核对原始需求、模板/协议与产物哈希。只完成需求提取的任务可从设计继续；完整任务复用设计和布局，重新加载与检查。历史提取结果若含现在能明确识别的非法绑定，只在尚无设计且原来源为模型提取时保留为 rejected 并重新提取原文；显式 supplied oracle 不会被自动替换。

故障选项是实验扰动，不是正常布局规则：`emitter_gap`、`sensor_parallel`、`height_offset`、`transfer_gap`、`missing_object`。缺少注入所需对象/能力会明确停止。局部修复冻结无关对象；结构改变需更新关系并重新编译覆盖检查。相同布局反复返回、无变化、能力不足或超过修复预算会停止。

## 记录与判定

- 根目录：`requirement.json`、`requirement-spec.json`、`requirements-source.json`、`design.json`、`scene-plan.json`、`run.json`、`result.json`、`execution.json`。
- 每个 `scene-NNN`：本版本需求/设计/布局、`check-plan.json`、构建前/加载后检查、加载回执、`.factoryio`、图像与 `image-index.json`、视觉回复、融合结果、Ticket、Patch、错误集合比较。
- `training-data/candidates/*.json`：每次最终 system/user 请求、脱敏载荷、图片副本、公开原始回复、响应格式/模型信息、Token 和错误；`trace.jsonl` 留存阶段与校验拒绝。失败轨迹默认待审核。
- `comparison.json`：A 单视角、A 同提示词多视角、B 动态检查、C 复用 B 回复结合原生几何。C 不再次采样模型，避免把随机差异当几何收益。

`scene_verified` 只表示本轮可执行的强制静态检查、实际对象注册及视觉审核通过。`unknown / unsupported / unobservable / not_applicable` 不当作通过；可靠几何反例不被视觉 pass 覆盖。有限几何检查不证明 mesh 无碰撞、实际工件交接、传感器电气响应或 PLC 行为。

对照记录的 `accuracy_metrics` 在独立真值审核前为 null。`scene_experiments.score_cases()` 可对审核后的 Check/Object 标签计算检出、误报、修复与保留指标；不能从模型自己的结论自动推导真值。对照来源标为 `visual_benchmark`，不会自动成为训练 gold。

## 2026-10-14 联机证据

| 框架 CLI 记录 | 实际结果 |
| --- | --- |
| `runs/issue4-line-layout-20261014-004` | 单段带 + 供料器 + 移除器：真实加载和三视角视觉检查通过；3 次模型调用，26.761 秒 |
| `runs/issue4-feed-fault-20261014-002` | 识别供料器偏移，局部修改后重新加载/截图并通过；4 次调用，45.381 秒 |
| `runs/issue4-independent-stations-20261014-004` | 无供料/移除器的两个独立工位通过；复用有效需求，2 次调用，17.621 秒 |
| `runs/issue4-column-layout-20261014-001` | 两个立柱、无输送/供料/移除器的布置通过；5 次调用（含两次格式纠正），43.740 秒 |
| `runs/issue4-visual-comparison-20261014-001` | 同一故障图片对照完成；3 次调用，33.724 秒；真值标签和准确率待审核 |
| `runs/issue4-feed-fault-20261014-003` | 最新冻结范围重测未通过：schema 与 JSON object 两次原始回复均为 `[]`，15.849 秒后有界停止，未进入 Patch |

以上均由命令行框架调用本机实际模型服务与桥接。场景实验检查静态布局，对照实验重放历史证据。直线示例的供料/接收要求本轮提取为视觉关系，因此不能称为原生几何已经证明了工艺连接。偏移修复前后，未修改的 Conveyor/Remover 布局行保持相同；早期 Ticket 允许的范围仍包含 Conveyor，随后已收紧视觉关系的对象定位与冻结范围。新结果另外报告 `native_registration_verified`，仅当原生检查独立覆盖所有本轮检查时才标记 `native_geometry_verified`，避免把注册成功扩大为连接证明。

目前有真实联机样例及离线回归，但尚未完成足量独立标注的 A/B/C 准确率统计，也不宣称 Issue #4 的量化验收已全部完成。失败轮次、重试和模型用量原样留存，不只汇报成功轮。

当前回归：523 项 Python 测试、12 项网页控制测试通过，其中 28 项覆盖本次布局 contracts/证据/修复/恢复/对照。测试的图片与载入适配是合成数据，不能替代以上真实联机记录。最新限定修复对象的行为通过回归测试，但最近真实模型重测因两次返回 `[]` 失败，不能称为已经重新完成联机验收。
