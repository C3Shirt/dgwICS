# 在仿真闭环中收集 SFT 与 DPO 数据

实现目标是边完成场景，边积累可追溯的训练材料。保存失败和修复过程，人工审核后按角色导出；成功运行不会自动把所有模型回复标为正确。现在先支持 LoRA-SFT 所需的数据，保留未来制作 DPO 偏好对的条件，不在本轮启动训练。

## 已实现的采集范围

`Workflow` 默认用 `RecordingClient` 包装模型客户端，覆盖 `prepare` / `run` 中 Agent1、Agent2、Agent3 和 PLC Agent 的实际调用。独立 CLI `review --out feedback.json` 写入旁边的 `feedback-training-data/`。配置可以关闭采集，但研究运行建议保持启用：

```json
"training": {
  "enabled": true,
  "scenario_id": "SC001",
  "lineage_group": "single-conveyor-original"
}
```

上面是配置片段，加入现有配置对象；不要照此替换完整配置。`scenario_id` 标识项目；`lineage_group` 标识共同来源/基础拓扑/派生链，全部尺寸、颜色、提示词改写、PLC 修复与运行变体继承同一个组。未配置组时照常采集，导出前必须人工补全。

原始记录位于 `<run>/training-data/`：

- **输入**：角色、阶段、实际 system/user 文本、结构化 payload、图片快照、交接包 ID、任务/场景/I/O/PLC/配置和提示词策略版本。
- **输出**：provider 最终 content 原文、解析后对象、回复哈希；JSON 格式错误也保存原文。HTTP 失败没有实际答案时只保存错误，不伪造 completion。
- **调用信息**：请求/返回模型名、端点、响应 ID、HTTP 状态、usage、耗时。授权头和服务返回的隐藏 `reasoning_content` 不采集。
- **闭环**：状态事件和修复反馈，关联运行 ID、最近候选及版本。编译、场景加载、I/O、运行验收等原始文件仍保存于工程目录，通过哈希引用；保留整个运行目录，不能只拷贝 candidates 后删除证据。
- **资格**：初始 `review_status=pending`，`sft_eligible=false`、`dpo_eligible=false`。原始记录保持原样，审核资格在独立审核文件中维护，导出时重新验证。

视觉输入先快照，再把快照送给模型，确保保存的图像就是模型看到的内容。密钥、已知设备密码、授权串和凭据字段会在文本记录中脱敏；有脱敏的候选不直接导出，避免训练输入与实际请求不一致。应先清理输入，再发起新的模型调用。截图中的文字和第三方资料需要人工隐私/使用许可审核，自动文本脱敏不代替图片审核。

采集写入失败会使本次调用/流程报错，以免丢掉轨迹仍继续声称完整采集。进程异常中断可能留下 `started` 候选；它不能作为正样本。注入客户端标记为 `injected_client`，固定测试标记为 `fixture`，导出器拒绝把两者转成训练 gold。

已有人工调试文件可以保留为证据材料；本聊天历史、手工操作及历史 probe 不会被自动伪装成四角色模型轨迹。当前双服务的 API 通讯已经单独验证，按角色路由到 DeepSeek/Aliyun 的完整集成仍需继续；采集机制本身不代表端到端仿真已验收。

## SFT：按角色和证据确定正样本

每个样本是一次明确任务输入和对应最终答案。修复阶段的合格样本必须保留当时的反馈、旧方案和版本，教模型如何依据反馈修改；不要仅保存最终代码而丢失输入。

| 角色 | `gates` 中要求通过的证据门槛 | 正样本的含义 |
| --- | --- | --- |
| planner | `task_contract`, `design_review` | 需求拆解、部件可用性、流程及约束经审核；不声称运行已通过 |
| scene | `scene_load`, `geometry_check`, `process_check`, `visual_layout` | 这版场景已加载，布局与工艺关系有确定检查和画面依据 |
| plc | `plc_compile`, `io_binding`, `protocol_mapping`, `runtime_acceptance`, `visual_runtime` | 完整程序编译、绑定、协议和指定运行验收均通过；编译通过单独不足 |
| vision | `current_evidence`, `human_alignment` | 判断使用当前版本证据，问题定位及修复目标经人工对照确认 |

门槛结果由人工审阅原始报告后填写，导出器校验门槛、文件存在及哈希，不重新执行仿真，也不证明审核人身份。模型的 `verdict=pass` 不能单独代替独立验收或人工复核。

`candidate_reviews` 的单条字段如下；示例是格式说明，所有占位值必须换成实际值，不能直接用于导出：

```json
{
  "candidate_id": "候选记录中的ID",
  "prompt_sha256": "候选记录中的prompt_sha256",
  "response_sha256": "候选记录中的response_sha256",
  "status": "accepted",
  "reviewer_type": "human",
  "reviewer_id": "研究人员标识",
  "reviewed_at": "2026-10-10T00:00:00Z",
  "rationale": "根据哪些证据确认该答案符合本角色任务",
  "privacy_review_passed": true,
  "source_use_permitted": true,
  "lineage_group": "single-conveyor-original",
  "split": "train",
  "gates": {
    "task_contract": {
      "path": "../../实际运行目录/task-contract-check.json",
      "sha256": "实际文件SHA256",
      "result": "passed"
    },
    "design_review": {
      "path": "../../实际运行目录/human-design-review.json",
      "sha256": "实际文件SHA256",
      "result": "passed"
    }
  }
}
```

文件路径相对审核文件所在目录解析，也支持绝对路径。场景/PLC/视觉候选换成各自的 gates。`rejected` 候选也需要完整的人工、权限、划分和哈希字段，并至少一条 `failed` 证据；它不进入 SFT 正样本，但可能成为 DPO 的较差答案。报告文件应明确候选 ID/工程版本/验收条件和实际结果，不能拿其他场景的一份通过报告代替。

## DPO：保存可比较的偏好，不能自动拼接修复轨迹

严格配对要求：同角色、相同 system/user、相同图像内容与顺序、相同输入版本和反馈；两个答案不同；共同来源与 split 相同；比较条件相同。`prompt_sha256` 按实际 user JSON 文本计算，包含字段顺序，不通过删掉交接 ID、修复反馈或图片来凑成相同输入。

“错误答案 → 加入反馈 → 修复成功”是两个不同输入，保留为修复轨迹，并把合格修复作为 SFT。它本身不是 DPO 对。需要偏好数据时，在修改反馈或场景版本之前，对同一个冻结输入分别生成候选 A/B，隔离运行验证各自的输出，再由人工依据相同验收条件比较；也可以在较优候选通过后，对原输入重采样较差候选。当前不自动额外调用模型或执行候选。

较优答案必须是 `accepted` 候选；较差答案可以是有失败证据的 `rejected`，或也已通过但被人工判为较弱的答案。含原始回复的格式错误可以成为较差候选；HTTP 401/超时等无回复错误不能充当 rejected 答案。

在 `preference_reviews` 中填写：

```json
{
  "pair_id": "PREF-0001",
  "chosen_id": "已审核的较优候选ID",
  "rejected_id": "已审核的较差候选ID",
  "reviewer_type": "human",
  "reviewer_id": "研究人员标识",
  "reviewed_at": "2026-10-10T00:00:00Z",
  "rationale": "共同条件下，较优候选满足了哪些具体标准",
  "privacy_review_passed": true,
  "source_use_permitted": true,
  "same_evaluation_conditions": true,
  "evaluation_context": {
    "path": "../../实际运行目录/preference-evaluation-context.json",
    "sha256": "实际文件SHA256"
  }
}
```

比较上下文文件记录任务版本、测试物料/种子、运行时长、初始状态、故障/强制值、指标和两个候选各自的验收文件。导出器验证上下文文件哈希并要求人工确认比较条件，不能从哈希本身判断两次实验物理条件相同。

## 数据划分与发布

100 个项目的数量不是训练样本数量。一项工程会积累规划、场景、修复、视觉和 PLC 多种样本；先收集真实闭环，再统计每角色/工艺/难度的合格数量。避免靠简单尺寸和名称替换虚增覆盖面。

先按基础场景来源、拓扑和变体派生链定义 `lineage_group`，再分 train/validation/test。同一组全部样本继承同一个 split，不能随机拆分单次调用。既有 36 个拟定模板组是划分参考，最终要依据实际来源合并共用基础场景的组。可将约 80/10/10 作为组层面的目标比例，实际数量随组规模调整；不要先承诺每一档都有固定项目数。

一个发布版本要同时纳入全部相关运行的审核记录。导出器检查本次发布内部的组一致性；跨多个发布版本的 split 延续仍须由项目的统一审核索引维护。迁移版本时保留相同 group 分配，禁止把旧训练组重新放进新测试集。

```powershell
# 示例：模板是空审核文件，导出数量将为零。
# 正式发布前先复制模板，在副本中填写真实审核记录；raw 使用实际运行目录。
& .\.venv\Scripts\python.exe tools/export_training_data.py `
  --raw artifacts/run-new/training-data `
  --reviews training_data/reviews/review-v001.json `
  --out training_data/exports/v001
```

多次运行可重复 `--raw`。输出目录必须是新目录；先验证全部请求的样本，再创建发布目录。有无效审核、旧哈希、不同 DPO 输入或划分冲突时拒绝发布，不静默忽略。

SFT 输出为 `messages`；DPO 为 `prompt`、`chosen`、`rejected`，采用对话消息列表，与 [TRL 数据格式](https://huggingface.co/docs/trl/dataset_formats)对齐。偏好学习的任务背景见 [DPO 原论文](https://arxiv.org/abs/2305.18290)。视觉样本带图像占位和内容寻址的 `images` 路径，训练时还需所选小模型的图片加载器/processor，不能保证原样 JSONL 对所有训练后端均可直接使用。审核、来源和版本信息放在 `audit.jsonl`，不混入 assistant 的答案正文。

暂不做提示词压缩或上下文裁剪。将来为小模型构造精简视图时，必须保留原始输入映射，并重新核验压缩后是否还能支撑答案；精简视图不能继续冒用原始 prompt 哈希或原偏好对身份。

## 本轮验证

新增测试覆盖原始回复留存、格式错误与 HTTP 失败、凭据脱敏、图片副本、真实 Agent prepare 调用路径、人工审核、角色证据门槛、DPO 输入一致性、证据变更和同源划分冲突。全套 187 项测试通过。

[离线验证报告](../artifacts/training-collection-smoke-001/probe-report.json)保存了 planner/scene 的两条注入客户端候选，空审核导出为 0 条 SFT、0 对 DPO。这次验证没有请求外部模型、加载 Factory I/O 或下载 PLC，不能用于物理仿真验收或正式训练。
