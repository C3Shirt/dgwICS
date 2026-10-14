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

英文静态提示词在 `factoryio_agents/simple_prompts.py`；三个角色上下文由 `factoryio_agents/scene_context.py:PromptContextBuilder` 分别生成，版本为 `issue4-explicit-layout-summary-v5`。

| 调用 | 本轮信息 |
| --- | --- |
| 需求提取 | 原始文本、输出字段、注册算子及参数含义、真实 prefab 与测量能力名称 |
| 场景设计 | 原始需求、独立 Spec、必须绑定的角色、筛选部件及配置、选中候选的实测局部几何、布局接口、相关官方知识 |
| 视觉检查 | 独立需求形成的详细场景说明、逐实例官方功能/外观/标记、当前加载姿态与所需空间信息、预期相对关系、检查问题、真实图片 |
| 局部修复 | 相关原始约束、Ticket、目标对象与冻结锚点、相关关系/局部测量、允许操作与对象、直接相关图片证据 |

需求提取同时产生英文 `scene_intent`（goal、process_description、process_stages），引用原始 requirement IDs；设计方的 scene_goal 不作为检查真值。旧任务缺少 scene_intent 时，按原始要求和官方功能生成背景，不重新改写它的 Spec。

视觉请求的 `objects` 按实例给出官方功能、识别特征及来源、角色、场景文件配置、预期关系。`actual_pose` 来自本轮加载回执，不回填计划坐标；轴为 `[X,Y,Z]`，Y 竖直，比例来自回执。外包围盒不作为发射、感知或移除功能区域的等价测量；移除器缺少独立活动区域测量时不会获得虚假的几何通过。模型的空间解释仍是 VLM 反馈，原生检查另行保存。

每张图片绑定场景 SHA、图像 SHA、视角、采集时间及加载/截图请求；版本、文件哈希或相机回执不一致会拒绝检查。这些审计元数据保存在 `vision-context-audit-N.json`，不重复塞进模型上下文。请求不包含完整 Spec、XML、PLC 或设计方自评。

VLM 必须检查全部任务，但只输出异常：根对象为 `{"inspection_complete":true,"summary":"Layout correct.","issues":[]}`。summary 必须非空；无异常也明确给出“布局正确”的模型判断，不能只返回数组或沉默。每项 issue 包含 check_id、violated/unobservable、suspect_object_ids、图像观察、单独的测量解释、原因、修复意图、图片 ID 和可选补拍视角。正常对象和满足的项不输出。无法判断时指出对象与证据缺口；未绑定原始检查的额外担忧不能变为自动修复约束。最多补拍一次现有视角。

程序校验当前图像及对象引用，再用 CheckPlan 恢复内部映射。空异常只有在检查完成、原生注册和所有独立强制检查通过时才表示静态通过；缺失/未知/不支持不算通过。原生数量与视觉数量冲突需要核对身份，不允许据此直接删除设备。局部修复优先采用明确 suspect IDs，原生反例保留算子的可移动端点，参照物冻结。

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
- 每个 `scene-NNN`：本版本需求/设计/布局、检查与加载回执、`.factoryio`、真实图片；`vision-input-N.json` 是最终动态载荷，`visual-review-N.json` 是仅异常原始回复，`visual-review-normalized-N.json` 和 `verification.json` 是程序映射与独立检查，另存 Ticket/Patch。
- `training-data/candidates/*.json`：每次最终 system/user 请求、脱敏载荷、图片副本、公开原始回复、响应格式/模型信息、Token 和错误；`trace.jsonl` 留存阶段与校验拒绝。失败轨迹默认待审核。
- `comparison.json`：A 保留旧提示词的单/多视角；B 是详细背景与官方逐实例资料、不带姿态；C 在 B 的基础上加本轮实际空间资料；D 原样复用 C 做程序融合，不再次调用模型。B/C 使用相同提示词、图片和输出协议，A 的旧协议不同，仅作定性参考。
- `--probe-vision-protocol <candidate.json>`：冻结保存的 system/user 和图片，做流式/非流式 × JSON object/简单 schema/完整 schema 六次有界诊断，不修改软件，也不产生布局通过结论。记录标为 protocol_probe，不能成为自动训练 gold。

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
| `runs/issue4-feed-fault-20261014-003` | 较早冻结范围重测未通过：schema 与 JSON object 两次原始回复均为 `[]`，15.849 秒后有界停止，未进入 Patch |

以上均由命令行框架调用本机实际模型服务与桥接。场景实验检查静态布局，对照实验重放历史证据。直线示例的供料/接收要求本轮提取为视觉关系，因此不能称为原生几何已经证明了工艺连接。偏移修复前后，未修改的 Conveyor/Remover 布局行保持相同；早期 Ticket 允许的范围仍包含 Conveyor，随后已收紧视觉关系的对象定位与冻结范围。新结果另外报告 `native_registration_verified`，仅当原生检查独立覆盖所有本轮检查时才标记 `native_geometry_verified`，避免把注册成功扩大为连接证明。

目前有真实联机样例及离线回归，但尚未完成足量独立标注的 A/B/C 准确率统计，也不宣称 Issue #4 的量化验收已全部完成。失败轮次、重试和模型用量原样留存，不只汇报成功轮。

当前回归：535 项 Python 测试、12 项网页控制测试通过，其中 40 项覆盖布局 contracts/证据/修复/恢复/对照和新异常协议。测试的图片与载入适配是合成数据，不能替代真实联机记录。新的实机结果与仍存在的问题见下节。


## Vision v4 实际验证

全部实验由 `run_simple_agents.py` 执行，旁观者只检查记录并修改框架；没有手动生成/修改场景、代替模型回复或启动 PLC。

- `issue4-vision-v4-feed-fault-20261014-001`：真实加载偏移供料器，4 次模型调用、37.753 秒后静态通过；首个回复为 `[]`，恢复后还报告移除器问题。Ticket 允许两个对象，但实际仅修改供料器，不能算准确定位的理想样例。
- `issue4-vision-v4-feed-fault-20261014-002`：3 次调用、35.310 秒。检查只报供料器，Ticket 仅允许 emitter1，冻结 conveyor1/remover1，实际只改 emitter1；修复后重新加载/三视角截图，返回合法空异常，完成严格范围的真实静态闭环。
- `issue4-vision-v4-comparison-20261014-001`：同图 4 次调用、29.852 秒。旧单图报供料器、旧多图漏报，B 报供料器和移除器、C 只报供料器。只能描述本次结果，不能据此声称一般准确率提高。
- `issue4-vision-v4-protocol-20261014-001`：6 次对照、33.270 秒；流式 JSON object 与非流式简单 schema 返回对象，其他四次返回 `[]`。每组只有一次且其他实机 schema 也曾成功，不能确定因果或宣布协议问题已解决。
- `issue4-vision-v4-fresh-layout-20261014-001`：从原始需求提取详细英文场景说明并生成 2 m 输送布局；7 次调用、96.875 秒后停止。视觉将“覆盖下游延伸路径”误扩为“必须重叠皮带”；首次修复无变化，重试使用不支持的布局参数，未被放行为通过。失败完整保留。

随后补充通用的空间关系判断规则：不得混淆延伸、接触与重叠，图像印象和辅助测量冲突须明确不确定；不添加场景专用判断工具。后续任务复用已有 Spec/布局，重新加载并观察，避免从头重复生成。


补充重测结果：

- `issue4-vision-v4-fresh-layout-20261014-002`：复用英文 scene_intent、需求和原布局，实际重新加载/三视角拍摄；1 次调用、10.161 秒返回合法空异常。只证明这次静态审核完成，不说明此前移除器分歧已普遍解决。
- `issue4-vision-v4-columns-20261014-001`：两个立柱、无输送设备的实际重载和检查；1 次调用、10.049 秒通过，未添加供料或输送要求。
- `issue4-vision-v4-feed-fault-20261014-003`：最新提示词下再次报告供料器与移除器，实际只改变供料器；修复复查仍误解释纵/横轴，后续场景模型返回 HTTP 402。4 次调用、44.993 秒后 needs_attention，未宣称通过，也未推测 HTTP 402 的账户原因。
- `issue4-vision-v4-negative-check-20261014-001`：加入原生流向派生的纵/横轴说明，限制本次只检查不修复。供料器的偏移仍存在（原生发射点 X=31.6875 m，输送表面 X 范围 29.375–30.25 m），模型却给出空异常，框架因已编译的关系只有 visual_relation 而标记 scene_verified。这是已知注入故障的漏检，**不能作为该场景正确的验收**。严格 JSON、inspection_complete 和独立数量/注册检查无法保证纯视觉关系的感知真实性。

结论：新协议和动态信息传递已实现，严格范围成功样例存在；实机模型的误判、漏检和协议异常仍未解决，完整稳定验收未完成。没有将失败、HTTP 错误或已知故障的自动通过当作训练 gold。transport_frame 只是原生方向的辅助解释，不是新约束或模型准确率保证；记录留存每次源码哈希和完整实际回复。

- `issue4-vision-v4-positive-check-20261014-001`：最终 transport_frame 输入下，重新加载之前的 2 m 布局并拍摄三视角；1 次调用、10.642 秒返回合法空异常。与负例同时保留，不能用正例通过掩盖负例漏检。软件由框架重新载入这一布局，未启动 PLC。


## 明确的无异常回复（Vision v5）

英文 prompt、动态输出说明、JSON schema 和本地校验同时增加非空 summary。无问题时返回 `{"inspection_complete":true,"summary":"Layout correct.","issues":[]}`，终端显示“布局正确（模型判断，待程序验收）”。仅异常的 object/check 列表仍然可以为空，但整条报告有明确的文字说明。有问题或无法确定时，summary 简述问题，issues 保留对象和原因。

“Layout correct.” 是视觉模型的判断，不替代程序汇总；检查未完成、存在异常或原生几何反例时，即使 summary 写“布局正确”，也不会因此通过。裸 []/null 与空 summary 继续作为返回协议异常处理。增加文字说明用于消除正常分支的表达歧义，不能据此声称 API 问题或视觉漏检已被解决，也不能证明输入与异常完全无关。

本次实际 CLI 记录 `issue4-vision-v5-explicit-result-20261014-001`：复用正例，真实重新加载/三视角截图，1 次模型调用、10.737 秒。回复明确为 `Layout correct.`，此请求没有返回裸 []；537 项 Python 回归通过。单次成功不证明接口或感知问题已普遍解决。
