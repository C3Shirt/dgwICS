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

默认 `scene_verified` 只表示场景阶段通过，`plc_executed` 和 `live_pipeline_passed` 为 false。PLC 是明确的后续阶段：工具负责 BOOL 映射、实测按钮/传感器极性和 I/O 声明，PLC agent 只写控制逻辑；随后使用既有 CODESYS 编译/部署、两端同步生命周期工具，采集当前信号、箱子数量与图片。

`plc_compiled` 表示真实离线编译通过，仍未运行。`passed` 要求本次确定性运行检查与独立运行视觉检查均通过。运行提示词与布局提示词分开，均保存在 `simple_prompts.py`。

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

恢复要求原目录已有 `design.json`、`scene-plan.json` 和对应哈希。模板或协议变更、产物被修改时停止。尚未生成完整布局的设计失败需重新运行。Ctrl+C 会保存取消状态；仅场景模式不控制 PLC，PLC 运行模式会尝试同步停止两端。

## 从已通过场景继续 PLC

```powershell
# 先生成和离线编译，不要求 Runtime 服务已启动。
& .\.venv\Scripts\python.exe run_simple_agents.py --resume 'runs\已通过场景' --plc-mode compile

# 复用已编译逻辑，重新核对、编译部署并进行实际运行检查。
& .\.venv\Scripts\python.exe run_simple_agents.py --resume 'runs\已编译任务' --plc-mode run --keep-running --start-local-services
```

`--start-from plc` 可明确指定从 PLC 开始；恢复已有布局并请求 PLC 时默认选择该阶段。框架要求源布局哈希与检查记录匹配，再重新加载、核对当前几何和 I/O。旧布局图片检查明确标记为 archived，不声称它是新的运行证据。已保存程序和 I/O 语义匹配时复用程序，仍重新编译部署和采样。`--resume` 保留原任务的 PLC 执行范围及参数。

`--runtime-seconds` 为 10–120 秒，默认主循环观察 30 秒，随后观察同步复位后的重新启动。`--reset-mode warm|cold` 默认 warm。`--max-plc-repairs` 默认 2。`--start-local-services` 只在服务启动被 Windows 拒绝时请求 UAC，系统窗口仍需人确认；不会修改账户、密码或服务启动类型。

当前运行检查是一条可复用的单件输送检查，根据本场景对象与绑定信号识别角色，要求一条输送带、供料器、入口/出口传感器、移除器和启动/停止/复位按钮。检查连续有序循环、采样窗口内最多一箱、入口后停止供料、物体移除、输送带持续运行、停止按钮、两端同步停止和复位，以及复位后重启。持续开启 Remover 合法，不要求每箱产生一次使能边沿。其他工艺未因此自动获得运行验收能力。

运行采样或模型检查失败、运行中 Ctrl+C 时，框架尝试同步停止两端并保存部分证据；清理错误会单独记录。`--keep-running` 仅在本次运行检查通过后保留运行，不保证未来持续正确。编译成功不等于实际运行通过。

## 如何观察与收集训练材料

终端显示模型公开输出、阶段与工具结果，代码/XML 留在文件中。网页新建任务选择简化场景或简化完整流程，逐步执行工具和模型请求。已通过场景有“继续 PLC 流程”按钮，已编译任务有“部署并运行已编译 PLC”按钮。历史 CLI 轨迹显示工具和模型步骤，模型调用前的工具失败也能查看。恢复仍走简化路径。

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
| `scene-NNN/io-scaffold.json`、`runtime-signals.json` | 原生极性归一化、工具声明和动态信号角色 |
| `scene-NNN/plc-NNN/` | 模型 ST、工具组装程序、工程副本、真实编译/部署报告 |
| `scene-NNN/plc-NNN/runtime/` | 原始信号、原生箱子数量、图片和两端生命周期回执 |
| `scene-NNN/plc-NNN/acceptance.json`、`runtime-feedback.json` | 本次确定性检查及独立运行模型分析 |

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

PLC 续跑：`simple-plc-20261012-005` 经框架 CLI 调用一次 DeepSeek PLC 模型，17,069 token、173.04 秒，真实 CODESYS 离线编译为 0 错误、0 警告；设备绑定、任务引用和运输映射通过。状态 `plc_compiled`，未下载运行。前面 001 的重复布局检查误报和空数组、002 的按钮极性接线遗漏、003/004 的 Runtime 权限失败均保留；最后一个阻塞是 Windows 服务启动权限与 UAC 等待，不是 PLC 编译错误。

恢复任务 `simple-plc-20261012-006` 在 37.823 秒内复用同一程序并真实重编译通过，新增模型调用为 0；程序 SHA256 保持 `18742edbc41aabf3a5dd45ac0d3a6e64a4a993ba9d2f8efaf0dfc79e210d7b11`。它仍是离线编译结果，不是运行通过。`run_simple_agents.py --check` 可只读检查当前服务和接口。
