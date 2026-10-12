# 简化场景框架（Issue #1，阶段 A）

这条路径基于 [Issue #1](https://github.com/C3Shirt/dgwICS/issues/1) 实现。原 `run_agents.py` 和原网页模式保留用于对照。

## 范围与流程

```text
原始需求
  → 场景 agent：一次组件选择、功能描述与布局设计
  → 原生工具：模板选择、相对位置求解、工厂居中、XML 与 I/O
  → Factory I/O：真实加载、原生几何、标签核对、三视角截图
  → 视觉 agent：原始需求 + 物品功能 + 官方知识 + 当前截图
  → 通过 / 不确定而停止 / 场景 agent 局部修复后重新检查
```

正常目标是一次场景设计与一次视觉检查。没有 Planner 多轮拆解、提案审批、信号 DSL、Supervisor 或交接契约。模型输出不合格式时，最多补一次格式纠正；局部修复默认最多两轮。

`scene_verified` 只表示当前场景的原生几何和静态图片检查通过。此阶段不生成、编译、下载或运行 PLC；`plc_executed` 和 `live_pipeline_passed` 始终为 false。阶段 B 的 PLC 最小闭环仍待实现。

## 在哪里运行

打开 PowerShell，进入项目根目录。Factory I/O 应已启动并加载本项目桥接，配置使用现有 `config.agent.json`；源码仓库中的示例配置需要按本机路径配置。

```powershell
Set-Location 'F:\phd\project\digital-twin-factoryi-o'
& .\.venv\Scripts\python.exe run_simple_agents.py '构建简单直线输送场景：一条输送带、单件供料器、入口和出口光电传感器、末端移除器，以及启动、停止和复位按钮。传感器检测线横跨输送带。此轮只检查布局。'
```

可用 `--config <配置路径>`、`--out <新的运行目录>`、`--max-repairs 0..5`、`--quiet`。不提供 `--out` 时自动创建 `runs/simple-日期-时间-随机编号/`。成功退出码为 0，需要处理的问题为 2。

恢复已有简化任务时复用原始设计和已保存布局，重新读取原生几何、加载与拍摄，不重新调用场景设计 agent：

```powershell
& .\.venv\Scripts\python.exe run_simple_agents.py --resume 'runs\simple-原运行目录' --out 'runs\simple-resumed-new'
```

恢复要求原目录已有 `design.json`、`scene-plan.json` 和对应哈希。模板或协议变更、产物被修改时停止。尚未生成完整布局的设计失败需重新运行。Ctrl+C 会保存取消状态；不会自动同步停止 PLC，此路径本身不控制 PLC。

## 如何观察与收集训练材料

终端显示模型公开输出、阶段与工具结果，代码/XML 留在文件中。网页新建任务选择“简化流程”，逐步执行工具和模型请求，查看发送的 System/User、实际图片和原始回复。历史简化 CLI 任务的恢复仍走简化路径。

| 文件 | 内容 |
| --- | --- |
| `run.json`、`result.json` | 状态、模型调用数、实际 token 用量、耗时、恢复来源 |
| `execution.json` | 框架来源哈希，不复制整份源代码 |
| `trace.jsonl`、`model-events.jsonl` | 工具和模型事件 |
| `design.json`、`scene-plan.json` | 模型设计与工具求解后的布局 |
| `scene-NNN/scene.factoryio` | 每版实际生成的场景 |
| `scene-NNN/load-receipt.json` | 本次加载 ID、场景哈希与软件确认 |
| `scene-NNN/geometry-validation.json` | 原生几何检查，视觉通过不能覆盖失败 |
| `scene-NNN/visual/` | 三视角原始截图、桥接元数据、标签核对 |
| `scene-NNN/scene-feedback.json` | 视觉分析和对象级问题、不确定性 |
| `scene-NNN/local-patch.json`、`corrected-patch.json` | 局部修复与可能的格式纠正 |
| `training-data/candidates/`、`assets/` | 实际模型输入、输出、图片、用量、错误；待审核训练候选 |

未审核轨迹不会自动成为 SFT/DPO 的正确答案。故障实验会记录 `experiment_fault`，恢复保留该标记。视觉误报、格式错误和工具拒绝也保留，不能只选最后一次通过掩盖失败。

## 受控故障实验

由框架在已通过场景上注入故障，随后仍使用同一视觉检查和局部修复路径。无需助手手工改场景。

```powershell
& .\.venv\Scripts\python.exe run_simple_agents.py --resume 'runs\已通过的简化任务' --inject-fault emitter_gap --out 'runs\fault-emitter-new'
```

`emitter_gap` 将供料器沿输送方向的横向移开 24 个网格；`sensor_parallel` 将一个漫反射传感器旋转 90 度。只有局部修复允许的对象能修改，其他对象保留；每版必须重新加载、重新截图。故障任务不作为普通场景成功率样本。

## 已观察到的限制

当前相对布局工具优先支持一条直线输送带和配套供料器、传感器、移除器及按钮。其他对象可用绝对网格坐标，但缺少原生能力时仍停止；复杂工业场景尚未进行系统评估。

视觉模型可能误读传感器身份、将按钮距离当成额外要求，或输出空数组；局部修复模型也可能写错相对位置字段。小结构约束、明确的接口参数和有界格式纠正减少接口错误，但不能证明视觉判断稳定。原生检查已补充横跨检测线的检查，避免沿带射线仅因相交而通过。

## 2026-10-12 的真实 CLI 结果

全部实验通过 `run_simple_agents.py` 调用现有 DeepSeek 文本服务与 Qwen 图片服务。助手只检查记录和在失败时修改框架，没有替模型编写场景。

| 任务 | 当轮调用 / token / 秒 | 结果 |
| --- | --- | --- |
| `simple-issue1-baseline-20261012-006`，全新正常任务 | 6 / 35,141 / 41.675 | 场景通过；原生工具发现按钮冲突，经过两轮局部修复 |
| `simple-issue1-fault-20261012-005`，恢复受控供料器偏移实验 | 5 / 30,731 / 43.831 | 图片发现错位并修复，最后几何与视觉通过 |

修复期间未选中的对象保持原样，所有对象及 I/O 的 Key 保持稳定。故障轮次还有视觉误报引起的额外调整，不能视作理想的单对象修复示范。

前面的 baseline 001/002/004/005 与 fault 001–004 包含格式、HTTP、设置路径或修复参数失败，全部保留在本机运行目录；baseline 003 是一次恢复后的场景通过。上表只记录最后两轮的用量，**不包含之前失败轮次，不是成功率或累计成本统计**。旧 13 步轨迹的 9 次模型调用、103,841 token 是不同执行范围的历史参照；两者均没有本次 PLC 运行验收，不能据此声称完整系统已加速。稳定两次调用的目标尚未达到。
