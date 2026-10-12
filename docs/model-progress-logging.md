# 模型输出与运行进展日志

2026-10-11：`run_agents.py` 的默认终端输出已接入实际模型请求和响应。启动命令保持不变。

## 现在能看到什么

- **开始工作**：角色、具体子任务、本次目标、重试原因、实际服务及模型名、使用的图片数。
- **等待期间**：每 15 秒报告真实等待时长，区分等待响应、服务处理和接收正文，显示已收到的正文字符数。
- **生成期间**：流式 JSON 中完整返回的工艺用途、控制规则、观察、预期、修复建议、判断依据等业务字符串。
- **回复结束**：对象与布局摘要、测量规则、PLC 策略说明、视觉结论、监督建议、实际 token 用量和完整候选文件位置。
- **检查与修复**：子任务合同通过或被拒绝的原因、已有模型产物复用、布局工具调整，以及没有请求模型的规则路由。

公开输出均来自模型实际返回的业务字段或工具实际事件；日志不补造模型正在考虑的内容。生成期间及回复结束都标注“尚待验证”，模型的 pass、supported 等自报结论仍需后续合同与证据检查。

不向终端写入程序声明、ST 正文、GVL、XML、完整原始 JSON、图片 Base64、凭据或模型内部推理文本。代码和完整回复仍保存在原来的候选及工程产物文件中。说明文字里的代码块和赋值片段也会被过滤。长业务文字及过多条目有显示上限，保留对应完整文件的位置。

日志保留模型原文，因此模型用英文填写的业务说明会按原文显示；角色、阶段和运行状态提示使用中文。

## 文件与查看方法

| 文件 | 内容 |
| --- | --- |
| `<run>/progress.log` | 人可读的工具和模型日志，时间显示为北京时间；`--quiet` 也会保存 |
| `<run>/model-events.jsonl` | 结构化的公开模型活动，包括调用 ID、阶段、候选引用、等待时长和摘要 |
| `<run>/trace.jsonl` | 原来的工作流事件，保留 UTC 时间戳 |
| `<run>/training-data/candidates/*.json` | 原始回复、真实请求、用量、流式接收元信息及审核状态 |

在另一个 PowerShell 窗口执行，替换为实际目录：

```powershell
Get-Content -LiteralPath '.\runs\你的运行目录\progress.log' -Encoding UTF8 -Tail 60 -Wait
```

已有旧任务没有新格式的 `progress.log` 时，可以只读回放其模型输出：

```powershell
& .\.venv\Scripts\python.exe run_agents.py --show-models runs\observer-simple-transfer-20261011-005
```

回放不加载模型凭据、不调用服务、不操作软件、不写回原运行目录。依据 `run.json.created_at` 筛选本轮候选，恢复复制的历史候选不计入；它展示归档记录，不表示软件此刻仍在运行。尚未保存完整回复的候选会单独说明。

## 实现与验证范围

`ModelClient` 在安装观测回调后默认请求 SSE 流式响应，使用 `stream_options.include_usage=true` 获取实际用量。接口依据 [DeepSeek Chat Completions 文档](https://api-docs.deepseek.com/api/create-chat-completion/) 和 [阿里云流式输出文档](https://help.aliyun.com/en/model-studio/stream)。

流式接收只是公开活动观测，不改变模型任务或响应合同。完整内容仍通过原来的 JSON 解析、版本检查和验收；中断或长度截断不会变成成功。服务返回普通 JSON 时记录实际 `response_mode=json`，不自动再请求一次。可在现有配置的 `llm` 对象中设置 `stream:false` 使用完整响应模式，仍保留等待提示与回复摘要。

主要代码为 [progress.py](../factoryio_agents/progress.py)、[llm.py](../factoryio_agents/llm.py)，已接入工作流和监督角色。公开日志使用白名单、文本过滤和既有凭据脱敏；观察线程只报告等待状态，不调用仿真软件。

实测记录：

- [真实服务通讯报告](../artifacts/model-progress-live-20261011-001/probe-result.json)：DeepSeek 和 AliyunLLM 均返回 SSE，收到终止标志与实际用量；视觉请求带一张已归档场景图片。
- [真实服务可读日志](../artifacts/model-progress-live-20261011-001/progress.log)：验证了业务字符串在完整 JSON 返回前显示。
- [既有运行记录回放](../artifacts/model-progress-preview-20261011.log)：展示视觉反馈与监督诊断。
- 单元测试覆盖分片 JSON、转义、代码/凭据/推理过滤、等待提示停止、断流、长度截断、普通 JSON 响应、训练来源保留和只读回放。

通讯探针位于 `tools/verify_model_progress.py`，使用 `source_kind=connectivity_probe`；未操作 Factory I/O/PLC，没有创建训练 gold，也不构成新的完整场景验收。
