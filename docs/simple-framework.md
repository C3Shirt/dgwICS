# 简化场景框架（Issue #1，阶段 A）

新布局任务已接入 Issue #4 的独立需求、动态关系检查及局部修复，默认 `--scene-verification relations`。当前专注静态场景，使用与真实联机记录见 [布局与视觉反馈说明](scene-layout-issue4.md)。本文保留原极简流程及 PLC 路径的用法；原路径可显式选择 `legacy`。

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

默认 `scene_verified` 只表示场景阶段通过，`plc_executed` 和 `live_pipeline_passed` 为 false。工具负责格式转换、已验证通道映射、已有证据支持的极性归一化和 I/O 声明。PLC agent 根据原始需求生成控制逻辑和操作计划，工具执行计划并采集信号、物料数量、图片和操作回执，运行视觉 agent 逐项检查需求。未知极性的输入保留原始值；不会生成虚假的归一化别名。

`plc_compiled` 表示真实离线编译通过，仍未运行。`passed` 要求本次确定性运行检查与独立运行视觉检查均通过。运行提示词与布局提示词分开，均保存在 `simple_prompts.py`。

## 在哪里运行

打开 PowerShell，进入项目根目录。Factory I/O 应已启动并加载本项目桥接，配置使用现有 `config.agent.json`；源码仓库中的示例配置需要按本机路径配置。

```powershell
Set-Location 'F:\phd\project\digital-twin-factoryi-o'
& .\.venv\Scripts\python.exe run_simple_agents.py '构建简单直线输送场景：一条输送带、单件供料器、入口和出口光电传感器、末端移除器，以及启动、停止和复位按钮。传感器检测线横跨输送带。此轮只检查布局。'
```

可用 `--config <配置路径>`、`--out <新的运行目录>`、`--max-repairs <非负整数>`、`--quiet`。不提供 `--out` 时自动创建 `runs/simple-日期-时间-随机编号/`。成功退出码为 0，需要处理的问题为 2。

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

`--runtime-seconds` 为计划未指定时长时的默认观察窗口，默认 30 秒。计划可以按任务设置正数时长，具体启停、复位和重启步骤由 agent 根据需求选择。`--reset-mode warm|cold` 默认 warm。`--max-plc-repairs` 默认 2，可设置非负整数。`--start-local-services` 只在服务启动被 Windows 拒绝时请求 UAC，系统窗口仍需人确认；不会修改账户、密码或服务启动类型。

运行流程不规定输送带、供料器、移除器、按钮或传感器的数量，不通过名称猜测信号角色，也不规定单箱、持续运动或供料延时。PLC 回复的 `runtime_plan` 包含操作步骤 `steps` 和当前任务的文字检查项 `checks`；支持观察、按明确对象 ID 操作原生确认的瞬时按钮，以及两端同步启停、暖/冷复位。工具只检查场景身份、数据类型、连接与实际操作回执。模型必须为每个检查项给出证据，并独立判断原始需求是否全部覆盖；未执行的测试返回 uncertain。基础设施通过不等于工艺通过，模型视觉判断也不是连续时间物理证明。

供料器材料、UpTo 和随机化由场景 agent 根据需求配置，未指定的属性保留模板默认值。PLC 控制时仅释放模板中的输出强制覆盖，不改变供料策略。自动场景映射支持 BOOL 和 Numeric；PLC 绑定只能使用本机种子工程已验证的端点及数值线格式。缺少模拟量、通道容量或操作接口时明确报告配置/能力缺口，不能通过修改场景需求绕过。

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
| `scene-NNN/geometry-validation.json` | 实际物体注册检查；不强加供料路径或传感器用途，视觉通过不能覆盖注册失败 |
| `scene-NNN/visual/` | 三视角原始截图、桥接元数据、标签核对 |
| `scene-NNN/scene-feedback.json` | 视觉分析和对象级问题、不确定性 |
| `scene-NNN/local-patch.json`、`corrected-patch.json` | 局部修复与可能的格式纠正 |
| `training-data/candidates/`、`assets/` | 实际模型输入、输出、图片、用量、错误；待审核训练候选 |
| `scene-NNN/io-scaffold.json`、`runtime-signals.json` | 已知极性归一化、工具声明和按标签身份收集的全部绑定信号 |
| `scene-NNN/plc-NNN/` | 模型 ST、工具组装程序、工程副本、真实编译/部署报告 |
| `scene-NNN/plc-NNN/runtime/` | 原始信号、原生箱子数量、图片和两端生命周期回执 |
| `scene-NNN/plc-NNN/runtime-plan.json` | 模型生成的操作步骤和当前任务检查项 |
| `scene-NNN/plc-NNN/acceptance.json`、`runtime-feedback.json` | 基础设施检查、逐项运行视觉判断和原始需求覆盖情况 |

未审核轨迹不会自动成为 SFT/DPO 的正确答案。故障实验会记录 `experiment_fault`，恢复保留该标记。视觉误报、格式错误和工具拒绝也保留，不能只选最后一次通过掩盖失败。

场景 agent 的职责仅包括物理对象选择、配置和布局；完整需求是项目背景，不是要求它执行所有阶段。未决项用 `{owner: scene|plc|runtime, reason: ...}` 明确所属阶段：只有 `scene` 阻塞场景生成；其他项写入 `deferred-tasks.json`，并随需求交给 PLC 或运行视觉阶段。旧的无所有者字符串保守地按场景阻塞处理，不通过关键词过滤丢弃问题。原始回复仍在训练候选中保留；转交不代表任务已执行或验收通过。

## 受控故障实验

由框架在已通过场景上注入故障，随后仍使用同一视觉检查和局部修复路径。无需助手手工改场景。

```powershell
& .\.venv\Scripts\python.exe run_simple_agents.py --resume 'runs\已通过的简化任务' --inject-fault emitter_gap --out 'runs\fault-emitter-new'
```

`emitter_gap` 将供料器沿输送方向的横向移开 24 个网格；`sensor_parallel` 将一个漫反射传感器旋转 90 度。只有局部修复允许的对象能修改，其他对象保留；每版必须重新加载、重新截图。故障任务不作为普通场景成功率样本。

## 已观察到的限制

相对定位是水平直线输送的可选计算辅助；其他物体可用当前写入器支持的绝对网格布局。加载后检查物体是否实际注册，工艺连接和传感器用途由视觉模型结合需求判断。场景修复支持位置/参数调整，以及根据反馈添加、移除或替换组件；保留不相关对象。当前写入器仍使用已确认的整数网格与 90 度旋转，本机联机适配仍是 Factory I/O + CODESYS Modbus。复杂工艺和未配置端点尚未因此获得实机验收。

视觉模型可能误读传感器身份、将按钮距离当成额外要求，或输出空数组；局部修复模型也可能写错相对位置字段。小结构约束、明确的接口参数和有界格式纠正减少接口错误，但不能证明视觉判断稳定。简化路径不使用旧流程的连接与横跨检测线规则作为工艺验收条件；视觉模型结合任务和图片判断传感器朝向，遮挡或证据不足时应返回 uncertain。

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

## 2026-10-13 的任务驱动改造与验证

移除简化路径中的固定单输送带、单箱、持续运动、必须包含供料器/移除器/按钮及固定信号角色等规则。场景模型选择对象、工作模式和参数，局部修复可添加、移除或替换相关对象；PLC 模型根据原始需求提供逻辑和运行操作计划。运行视觉检查需要逐项引用实际证据，并判断原始需求是否覆盖；软件注册与连接检查通过不会自动产生工艺通过结论。九个已注册 MCP 工具的数量未增加。

本机回归验证为 495 项 Python 测试通过、12 项 Node 网页控制测试通过。2026-10-14 从发布副本再次验证：Python 共 495 项，494 项通过、1 项因未包含 Factory I/O 参考场景而跳过；Node 12 项全部通过。源码与已配置凭据扫描通过。数值 I/O 使用已验证端点和线格式的路径有离线测试覆盖，尚未取得新的数值场景联机验收。

`sc002-task-driven-compile-20261013-234043` 通过框架 CLI 复用两段交接布局，接入 14 个绑定信号，调用一次 PLC 模型（23,790 token），真实 CODESYS 离线编译为 0 错误、0 警告，总耗时 191.088 秒。设备绑定、任务引用和运输映射确认通过；布局视觉检查复用历史结果并明确标记 archived。本次状态为 `plc_compiled`，没有下载或运行 PLC。

此前 `sc002-task-driven-20261013-233128` 的运行尝试在模型调用前被 Runtime 服务启动权限拒绝（WinError 5），管理员启动器未返回服务成功回执，因此没有继续部署。这两次记录保留在本机运行目录；源仓库不包含私有工程或原始运行数据。上述编译发生在最后一次只读输入保护补丁之前，当前补丁经回归测试验证，不把两者视为同一源码快照的实际运行结果。

## 管理员服务启动的故障记录

`--plc-mode run --start-local-services` 由框架的部署工具启动已确认的 CODESYS Gateway/Runtime 服务；操作系统的 UAC 由用户确认。不需要助手单独运行服务启动脚本。

2026-10-13 发现当前管理员账户的 PowerShell 脚本执行策略会拦截该脚本，即使 UAC 已确认。框架现在给该子进程传入 `-ExecutionPolicy RemoteSigned`，仅在这个进程中生效，不修改账户、系统或组策略。组策略的优先级保持不变。

每次尝试在 `scene-NNN/` 保存 `service-uac-process.json`（启动进程 ID）、`service-uac-launch.json`（提权退出码/启动错误）、`service-uac.elevated.log`（该本地脚本的输出）和 `runtime-start-elevated.json`（实际服务回执/脚本错误）。缺少服务成功回执不会继续部署；退出码 0 也不能替代服务 `Running` 的检查。
