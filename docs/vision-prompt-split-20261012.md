# 2026-10-12 视觉提示词按阶段拆分

布局检查与运行检查使用独立的英文提示词，`VisionAgent.review()` 在发送请求前根据 `phase` 选择。

| phase | prompt_id | 文件 |
| --- | --- | --- |
| scene | vision_layout | factoryio_agents/prompts/vision_layout.md |
| runtime | vision_runtime | factoryio_agents/prompts/vision_runtime.md |

布局提示词聚焦原始场景需求、对象类型、位置、方向、高度与功能连接。运行提示词聚焦流程执行、物料轨迹、标签变化、时间相关的控制器证据、原生操作计划和确定性验收结果。单个模型请求只包含所选阶段的业务提示词。

`vision_output.md` 共享回复字段与证据约束，`common.md` 共享工作流约束；它们不组合两个阶段的业务检查清单。原 `vision.md` 改为迁移说明，不参与模型请求。其它角色的提示词不变。

实际 payload 增加 `review_task`，记录当前 phase、prompt_id 和英文 objective。原始需求仍在 `task.requirement`，流程仍在 `task.process`；布局和运行数据沿用现有按阶段投影的规则。这次没有改动响应 Schema 或模型服务配置，也没有执行现场场景运行。

策略版本更新为 4，分别记录 `vision_layout`、`vision_runtime` 的完整提示词哈希。保留 `vision` 的聚合哈希供现有角色级缓存/来源记录使用；聚合的是哈希身份，不是发送给模型的混合提示词。

## 验证

先新增实际角色调用测试，原代码因相同提示词、缺少阶段身份、未知阶段仍调用模型和缺少独立策略哈希而失败；完成拆分后测试通过。四角色的工作流测试同时核对布局与运行两次视觉调用选择了各自的提示词。

新增测试使用真实 VisionAgent 和现有 payload 投影/验证代码，外部模型服务采用 mock，避免把模型答复当作测试基准。检查范围是提示词选择、英文内容、任务保留、阶段身份、非法阶段拦截及来源策略；不代表识别准确率或现场物理验收。

```powershell
& .\.venv\Scripts\python.exe -m unittest discover -s tests -q
```

提示词在进程导入时读取，修改后重新启动 CLI。历史候选与原始证据保留；使用新策略进行视觉审核时会得到新的策略身份。

本次全量回归通过 372 项测试。独立构建的 wheel 包含当前两份英文业务提示词和共享输出文件，并通过隔离导入与阶段选择检查。验证记录位于 `artifacts/vision-prompt-split-20261012-001/verification.json`。运行中的软件与模型配置未修改，视觉准确率需另行评测。
