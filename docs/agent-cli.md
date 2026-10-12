# 单指令运行与能力归因

日常操作和项目材料导航见 [中文使用说明书](agent-user-guide.zh-CN.md)，本页补充命令参数及适配能力细节。

默认终端日志现已展示模型具体子任务、公开输出、等待心跳、合同错误与修复建议，并写入 `progress.log`。代码/XML 不打印。旧任务可用 `--show-models <运行目录>` 只读回放，详见 [模型进展日志](model-progress-logging.md)。

在项目目录执行：

```powershell
python run_agents.py "构建单段输送到位停止场景：中型箱单件供料，入口、末端和出口检测；到位停2秒后放行，确认出口清空再供下一箱；带启动、停止和复位。"
```

也可使用项目虚拟环境：`& .\.venv\Scripts\python.exe run_agents.py "场景描述"`。
安装包后入口为 `factoryio-agent "场景描述"`。默认运行完整工作流；程序不会使用固定 demo
回复或助手手写的场景、ST作为模型生成的回退。

默认联机验收会执行停止、复位测试，并在取证结束时停止 Factory I/O。需要交付持续运行
的场景时，加 `--keep-running`：完整验收和视觉检查通过后，再执行规划模型的启动动作，
重新采集一个运行窗口，验证原来的供料、输送、停留、出口等可观测规则。重新启动失败
会停止仿真并进入正常模型诊断和 PLC 修复流程；仅 Running 为真不能通过。

```powershell
.\.venv\Scripts\python.exe run_agents.py --resume runs\已有运行 --revalidate --keep-running --out runs\新的运行
```

`plc-*/live-handoff/receipt.json` 是结束时仍在运行的独立回执。停止/复位测试的证据保留在
完整 `acceptance.json`，继续运行检查的投影及省略项记录在 `live-handoff/measurement-scope.json`。
该回执描述交付时的观察，不代表以后无限期保持运行。

## 两端统一停止、复位和恢复

Factory I/O 的 Run/Pause/Reset 与 CODESYS Application 的 Stop/Reset 是独立操作。
仿真停止并清空箱子后，PLC 中的普通变量也可能还保留着上一次流程的状态。
项目配置已启用 `factoryio.synchronized_lifecycle`：完整验收结束时两端一起停止；
`--keep-running` 则在验收后复位两端、显式重连 Modbus，再实测流程并保持运行。

无需重新生成或下载代码，也可以对已有部署直接操作：

```powershell
.\.venv\Scripts\python.exe run_agents.py --control runs\observer-sc001-20261011-035 --control-action stop
.\.venv\Scripts\python.exe run_agents.py --control runs\observer-sc001-20261011-035 --control-action reset --reset-mode warm
.\.venv\Scripts\python.exe run_agents.py --control runs\observer-sc001-20261011-035 --control-action restart --reset-mode warm
.\.venv\Scripts\python.exe run_agents.py --control runs\observer-sc001-20261011-035 --control-action restart --reset-mode cold
```

`stop` 先冻结仿真再停止 PLC；Factory I/O 退出运行会清空动态物体，继续实验时应使用
`restart` 从一致的初始状态重新开始。`reset` 复位两端后保持停止；`restart` 在此基础上
启动 PLC、重连 Modbus、启动 Factory I/O，并执行规划模型原有的启动按钮操作和运行检查。
这里的“一起”是有顺序且逐步确认的统一操作，不保证两个软件在同一毫秒执行。

默认 `warm` 初始化普通变量并保留 RETAIN/PERSISTENT；`cold` 还初始化 RETAIN，但仍保留
PERSISTENT。SC001 的流程状态为普通 VAR，因此热复位即可；不提供会清除应用的 Origin
复位。参考 [CODESYS 复位说明](https://content.helpme-codesys.com/en/CODESYS%20Development%20System/_cds_reset_application.html)
和 [ScriptOnline 接口](https://content.helpme-codesys.com/en/ScriptingEngine/ScriptOnline.html)。

输出中的 `lifecycle/steps.json` 保存实际 Stop/Reset/Start、连接及原生场景回执；
`operation-evidence/` 保存恢复后的 Tags、按钮回执、截图与过程检查。`--control` 没有模型
生成调用，单独通过它不能声称整个生成流水线重新验收通过。场景 HMI 的 Stop/Reset 按钮
仍由 PLC 程序定义，其行为与实验管理器的两端复位命令分别记录。

## 配置与首次检查

```powershell
python run_agents.py --check
python run_agents.py "场景描述" --scenario-id SC001 --lineage-group T01
python run_agents.py "场景描述" --mode prepare
```

`--check`只读检查本地软件接口、模型配置及设备凭据是否可用，不调用模型或启动仿真。
`--mode prepare`只调用规划与场景角色生成方案，结果为等待场景复核，不代表四角色闭环完成。
每次创建独立的 `runs/时间-随机ID/`；显式 `--out` 必须是全新目录。

失败后使用 `--resume <旧运行目录>` 复用校验过的产物，原目录保留。
`--resume-inspect` 只检查可恢复范围，`--supervise <目录>` 让全局监督模型分析进度与下一步。
`--investigate <目录>` 用真实监督模型调查该运行最后一轮实测失败，保持软件和历史产物不变。
`--resume <目录> --revalidate` 允许复用已通过任务的设计，重新加载、编译下载和联机验收。
详见 [任务记忆与全局监督](task-memory-and-supervision.md)。

本机默认配置为 `config.agent.json`。模型从已有 `LLM_Config.yaml`读取：
DeepSeek负责规划、场景与PLC；Aliyun负责视觉。配置引用不包含API Key副本。
当前文本角色使用 `deepseek-v4-pro`，每次候选保留实际请求与服务返回的模型名。
DeepSeek规划与场景请求关闭思考模式；PLC与监督通过 `llm.request_options` 分角色开启思考，
输出上限设为24000。保留实际请求选项、模型名和用量，截断回复直接归为输出预算问题；
实现依据[官方思考模式参数](https://api-docs.deepseek.com/guides/thinking_mode/)。
回复不是合法合同、接口失败或缺少验收证据时，运行会失败或要求处理，不以模型自报成功通过。

Factory I/O须打开，CODESYS Runtime须可用。当前已安装的本机桥接支持原生快照保存和连续加载；
发生未保存修改时先保存至新快照，再加载新场景，不跳过软件的修改检查。
原生保存会切回编辑模式，因此只能在当前运行证据采集完成后作为场景修订边界。
旧的加载失败弹窗可在下次加载时由桥接确认关闭，须匹配原生错误标题且没有回调；
关闭内容和时间写入回执。其他弹窗会阻止截图，不自动确认。三视图按原生对象边界
计算相机取景并记录目标及边界；超过已检查的相机范围时要求分区取景。

此前提供的PLC设备凭据保存在 `secrets/codesys-device.dpapi`，使用Windows DPAPI
CurrentUser加密；只在当前Windows用户的CLI进程中解密并传给CODESYS子进程。
也可通过配置指定的用户名/密码环境变量覆盖；不会记录密码明文。
管理员USER账户与当前用户的DPAPI身份不同，不能混用保护文件。

## 执行边界

模型输出职责：Agent1的任务合同、Agent2的对象/布局/配置、PLC Agent的ST、
Agent3的视觉反馈。工具职责：XML组合、GUID生成、合同检查、设备映射、
软件保存/加载/截图、工程副本、编译与下载、轨迹采集与独立验收。

`staged_task_design=true` 时，Agent1 分别生成需求/对象/流程、执行合同、验收规则与验证待办。
工具检查三个回复的独立 schema 后组合任务，不补写控制语义。原始候选、组合哈希及来源 ID
写入 `training-data/derived/task-assembly.json`；归因检查会从三个原始模型候选重新组合并比较。
同时启用 `staged_measurement_design=true` 时，验收进一步拆为信号、操作计划、检查规则三个
子任务，总共五个规划回复；组合审计按对应五个原始候选重新计算。
组合文件不能单凭一个回执自报为模型生成，且注入测试客户端仍不能计为真实服务运行。
下游模型输入省略上游 `handoff_id`，只提供本次交接 ID；原始产物版本与文件哈希先行检查。

`staged_scene_design=true` 时，Agent2 分两次调用：布局选择模板、整数 XYZ 和 90 度方向；
配置阶段只处理选中模板的参数及标签，须完整保留已经检查的布局。每个子任务最多三次
合同纠错，外层场景修订仍受原有次数限制。每次回复单独保留，最终方案对应模型原文。
`auto_bool_mappings=true` 时，模型不输出地址，工具按稳定对象 ID 和标签类型生成连续低地址，
将原生 Running 信号放入输入零通道，清除遗留高地址系统映射。GUID、原生内存地址、
Modbus 通道和 IEC 地址分别处理，设备容量及实际绑定继续单独验证。数字类型尚不支持
这条自动分配路径，须先实现并验证对应工具。

`apply_process_constraints=true` 时，单件供料工具从已验证的执行合同设置 Emitter.UpTo=1，
并在实际部件输出标签上解除模板强制值，交由 PLC 控制；不会强制物理输入。
原值、修改位置、GUID、合同哈希和权限归属保留在 manifest。配置属性先在具体模板上检查，
失败只重试配置子任务；配置类的场景修订复用布局，几何问题仍须重做布局和下游验证。

视觉请求保留当前几何、信号和部件参数，省略重复 XML 和未选中的工作模式；完整原件
仍在交接文件中按哈希验证。格式不合格时原样记录，并要求模型重新审核相同证据，最多
三次，不会把空数组解释成通过。Aliyun 请求使用 `enable_thinking=false`；参数依据
[官方结构化输出说明](https://www.alibabacloud.com/help/zh/model-studio/qwen-structured-output)。

当前自动设备准备只支持从已验真的本机重量工程继承BOOL通道布局，
在工程副本清理并重新绑定当前场景GUID和模型声明的变量；不生成PLC控制逻辑。
参数、源码SHA、模板SHA、实际通道/变量和周期任务仍经CODESYS检查。
数值通道、额外设备容量和物理标定超出此适配范围时应明确停止处理，
不能通过移除对象、强制传感器或偷偷换成手工基线来通过。

默认完整运行要求规划中的需求缺项、不可用的关键能力等 `unresolved` 关闭。
布局、单件发射配置、编译映射、通信和运行验证写入 `verification_tasks`，由对应角色/工具
在 `scene`、`scene_review`、`plc`、`connection`、`runtime` 阶段执行。每项须使用框架支持的
验证器，并记录当前版本及证据文件哈希。场景修订使旧验证失效，PLC修订使其下游验证失效；
待办未完成仍不能最终通过。未知的控制语义和未完成的物理标定不会因列为待办而自动确认。
确有需求缺项时，失败报告仍标为需求未决；不保证任意场景描述都能一次通过。
原有重量事件解码器仍是专用实现，其他工艺需要对应的可观察事件解码/验收合同；
仅有图片或PLC Run不会自动产生成功标签。

## 记录与失败分析

| 文件 | 含义 |
|---|---|
| `execution.json`、`framework-source/` | 本次入口、模式、配置路径以及工具/提示词源码快照与哈希 |
| `config.snapshot.json` | 脱敏后的本次配置 |
| `trace.jsonl`、`run.json` | 阶段、角色交接、版本、修复和最终状态 |
| `handoffs/` | 各次角色接收的文件、哈希、版本及输出要求 |
| `verification.json` | 分阶段验证清单、状态、所属版本和证据哈希 |
| `training-data/candidates/` | 真实模型请求、原始回复、实际模型、耗时、用量与错误 |
| `training-data/assets/` | 视觉输入图片的独立副本 |
| `scene-*/` | 模型方案、生成场景、标签、软件加载回执、截图与视觉反馈 |
| `scene-*/plc-*/` | 模型ST、工程副本、编译/下载诊断与运行证据 |
| `result.json`、`failure-analysis.json` | 各角色实际调用数、产物与模型候选的对应关系、失败阶段和证据分类 |

退出码：0代表相应模式完成，2代表需要配置/处理或未通过；Ctrl+C记录取消。
prepare模式完成与完整运行通过的意义不同，均在输出中明确标注。
完整通过额外要求四角色都有实际服务回复，产物能对应记录，场景文件哈希未被修改，
且工艺与视觉验收均通过。

失败分类区分模型服务、模型合同、需求未决、PLC编译、工具/环境、
软件/适配器和工艺/证据不足。没有独立证据时根因保持未确认，供后续分析。
模型网络超时不再交给Agent2改位置，环境或认证失败不交给PLC Agent改控制逻辑。

训练候选始终等待审核；`training_data/run-index.jsonl`仅索引运行位置。
失败、修复和取消均保留。SFT/DPO资格仍按既有审核规范决定，不因CLI成功自动入库。

### 原生几何与 PLC I/O 工具

当前配置的 geometry_command 在模型调用前读取运行软件的原生部件尺寸与变换。布局阶段执行有限的单输送机空间检查；加载后复核实际实例。多输送机拓扑、碰撞和过程验收仍需要相应检查。

`tool_plc_io: true` 让工具从 verified_device_bindings 生成全局 I/O 声明。PLC 模型仅生成内部变量及 ST 控制逻辑，按 io_scaffold.signals 的精确引用读写。工具组合回执位于 training-data/derived；原始候选不会被改写为完整程序或自动标为训练正样本。

`tool_scene_layout: true` 将场景阶段进一步分为“模板和相对关系 → 工具坐标解算 → 部件配置”。当前相对放置求解器仅支持一个已测 roller conveyor；不能把该试验外推为任意工业场景已支持。`codesys.normalize_bool_inputs: true` 使用实际测量的极性生成只读 SceneInputs 副本。模型的 unresolved 会接受有限复查；真实缺失需求仍阻止部署。

`factoryio.operator_startup: true` 保留兼容的固定 Reset/Start 启动方式。当前默认关闭它，启用
`staged_measurement_design: true` 与 `factoryio.execute_model_probes: true`：Planner 分别生成测量信号、
原生操作计划和检查规则；工具解释 `signal_contract`，执行有真实输入与输出检查的按钮操作。
新场景组合通用测量原语，不按场景名称新增验收函数。详见 [通用验收设计](generic-acceptance-design.md)。
第034轮正式CLI再次验收已完整通过SC001：真实编译下载、Modbus连接、14项实测检查、
场景/运行视觉审核及来源审计均通过，退出码为0。它复用了第031轮的模型产物并重新采集
联机证据，详见 [任务记忆与全局监督](task-memory-and-supervision.md)。
