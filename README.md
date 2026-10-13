# 工业自动化仿真 Agent

按 [Issue #1](https://github.com/C3Shirt/dgwICS/issues/1) 新增简化框架：`run_simple_agents.py "场景需求"`。场景设计 → 原生生成/真实加载/三视角截图 → 视觉检查 → 必要的局部修复。`--with-plc` 接入 PLC 生成、编译部署和按任务规划的运行检查；也可从已验证布局直接继续，或只离线编译。对象数量、供料策略和检查项由实际需求决定，工具不预设单输送带或单箱工艺。使用、恢复、验证范围与记录见 [简化框架说明](docs/simple-framework.md)。SC002 两段交接已由框架生成 PLC 并真实离线编译通过；新路径的实际运行验收仍待 Runtime 启动权限解决。原流程保留用于对照。

日常使用先看 [中文使用说明书](docs/agent-user-guide.zh-CN.md)：100 项设计材料的位置、单指令运行、实时日志、结果判定、失败续跑及两端同步操作。

终端现在默认显示模型子任务、公开业务输出、等待提示和修复原因，代码/XML 仅保存到产物文件；查看方法见 [模型进展日志说明](docs/model-progress-logging.md)。

现在可从统一命令行入口启动外部模型框架：

```powershell
python run_agents.py "构建单段输送到位停止场景：单件供料，到位停2秒后放行，出口清空后供下一箱，带启动停止复位。"
python run_agents.py --check
```

入口、模型原始回复、工具回执与运行证据独立留存；不会用助手手写场景/PLC代替模型产物。
参数、能力边界及失败分析见[命令行使用说明](docs/agent-cli.md)。

基于 Factory I/O 与 CODESYS，实现“需求 → 场景规划与生成 → 视觉检查 → PLC 工程 → 运行验收”的四角色工作流。Factory I/O 2.5.8 的进程内桥接已实测自动加载、原生保存后连续加载、截图、相机切换和对象/标签核对，并接入工作流。2026-10-11 核对：SC001 第 034 轮的正式 CLI 记录已通过当次完整联机验收；新实验 EXP002 第 005 轮已启动并采集运行证据，但工艺验收未通过。具体结果、目录及能力范围见使用说明书；历史通过不代表当前软件持续运行。

用户已手动跑通 `Sorting by Height (Learn).factoryio` 和 CODESYS。工程基线为 `C:/Users/tongqingwang/Documents/example1.project`；工具只在输出目录内操作其副本。

[100个工业场景项目设计库](scenario_projects/README.md)已建立独立项目目录，覆盖10工艺族、36拟定模板组和四级难度。每项包含工艺与布局设计、IO草案、PLC方案、验收/修复计划、来源与待填材料目录；均为规划状态。[实施顺序](scenario_projects/curriculum.md)从20项试点开始，工程实现和运行验收分别推进。

[训练数据采集](training_data/README.md)已接入 `prepare` / `run` 和独立视觉 `review`：保存角色请求、最终回复、图片副本、错误和修复事件，默认等待审核。按角色验收后可导出 SFT；DPO 另需相同实际输入及人工偏好，修复前后不同输入不会自动配对。固定 demo、注入客户端和连通性探针不作为训练 gold。字段与导出步骤见 [采集与审核规范](docs/training-data-collection.md)。

## 角色和闭环

| 角色 | 工作 | 共享产物 |
| --- | --- | --- |
| Agent1 | 拆解需求，选择部件，明确工艺、控制约束、单位与验收条件 | task.json |
| Agent2 | 选择真实模板，安排 XZ 平面布局、Y 高度、旋转和配置 | scene-plan.json、scene.factoryio、manifest.json、io-contract.json |
| Agent3 | 使用当前画面、真实标签轨迹和 PLC 诊断定位场景或运行问题 | 带版本与证据引用的反馈 |
| PLC Agent | 根据任务与已验证的 I/O 生成完整 ST，处理编译和运行反馈 | program.json、ST、工程副本、编译诊断 |

编排器串行执行修改，管理场景和 PLC 版本。几何/配置问题回到 Agent2，逻辑问题回到 PLC Agent；通信、需求和原因不明的问题分别返回工具、规划和调查阶段。修复次数有上限。运行通过必须同时满足确定的行为验收和 Agent3 的判断。

文件中 **XZ 是水平平面，Y 是高度**。本机内部代码确认对象网格坐标到引擎世界坐标的基础换算为 `(grid + 0.5) * 0.125`，物理米制仍需校准；部件原点不同，不能把所有输送设备的 Y 简单设为相等。对象通过稳定语义 ID 和 UUID5 Key 关联，标签名允许重复。

## 安装和运行

已根据联机调试实现 [v2 交互流程与提示词](docs/multi-agent-v2.md)：工艺执行契约、版本化交接包、部件语义、因果诊断、无效修复检测和事件优先的视觉上下文。提示词位于 `factoryio_agents/prompts/`，由代码直接加载。

核心工具只使用 Python 标准库，要求 Python 3.11+。MCP 是可选依赖。本机已准备项目 `.venv`。

```powershell
& .\.venv\Scripts\python.exe -m pip install -e '.[mcp]'
& .\.venv\Scripts\python.exe -m unittest discover -s tests -v
& .\.venv\Scripts\python.exe -m factoryio_agents catalog --out artifacts/catalog-new.json
& .\.venv\Scripts\python.exe -m factoryio_agents demo --out artifacts/demo-new
& .\.venv\Scripts\python.exe -m factoryio_agents collect --out artifacts/tags-new --seconds 1
& .\.venv\Scripts\python.exe -m factoryio_agents compile --out artifacts/compile-new
```

输出目录需使用新名称。`demo` 使用固定角色回复和事件输入，生成重量分拣场景并演示一次传感器高度修复；它没有调用大模型、导入场景或运行重量控制。demo 的 ST 用于验证导出边界。

新增 `weight-baseline` 使用本轮助手写好的真实重量分拣 ST，并基于实际 inspection 准备设备参数与 31 个绑定。它没有调用外部模型。运行步骤和当前通过范围见 [重量基线运行记录](docs/weight-baseline-runbook.md)。

`compile` 没有 `--program` 时只编译现有基线副本；指定 `--program program.json --io-contract io-contract.json` 时写入生成 ST 后编译。CLI 的 compile 不下载或启动 PLC。必须收到当次 request ID、保存后工程哈希和真实编译消息，才能标记 compiled。

## 配置模型和联机工作流

复制 `config.example.json` 为 `config.local.json`，填写 `llm.base_url` 和四个 `llm.models`。Agent3 的模型需要支持图片输入。API Key 通过配置中的环境变量名读取，默认 `OPENAI_API_KEY`；不用写入文件。也可使用 `FACTORYIO_LLM_BASE_URL`、`FACTORYIO_LLM_MODEL` 或各角色 `FACTORYIO_PLANNER_MODEL` / `SCENE_MODEL` / `VISION_MODEL` / `PLC_MODEL`。

当前单指令入口使用 `config.agent.json`，从用户的 `LLM_Config.yaml` 路由到DeepSeek文本角色和Aliyun视觉角色，已配置本机桥接与工程副本部署。加载器先原生保存未保存的场景，再载入模型生成的文件；本机新版IPC为 `artifacts/sc001-agent-validation-001/ipc`。设备凭据经当前Windows用户的DPAPI保护。旧 `config.local.json` 是早期联调配置，仍保留空模型及禁用部署的设置；下面的底层CLI示例使用该旧配置时，需要自行补齐。

```powershell
& .\.venv\Scripts\python.exe -m factoryio_agents prepare --config config.local.json --requirement '构建一个重量分拣 ICS 场景' --out artifacts/prepare-new
& .\.venv\Scripts\python.exe -m factoryio_agents run --config config.local.json --requirement '构建一个重量分拣 ICS 场景' --out artifacts/run-new
```

模型接口采用 `/chat/completions` 和 JSON 输出；可通过 `json_mode` 配置兼容性。没有模型服务时，prepare/run 返回 needs_configuration，不会用离线回复冒充真实模型。

`prepare` 只运行 Agent1/2 并生成可审查场景。`run` 还依赖：

- `factoryio.load_command`：可信 argv 数组，支持 `{scene}` 和 `{output}`，支持 `{request}` 和 `{confirmation}`。加载器必须读取 load-request.json，并在实际确认软件加载这版文件后创建 load-receipt.json；仅 exit=0 或标签一致不足以确认修复后的几何已加载。之后仍核对当前标签。
- `factoryio.connect_command`：可信连接 hook，支持 `{request}` 与 `{confirmation}`；必须用当次回执确认实际 Modbus 驱动、连接和配置端点。运行命令还需读回未强制的 Running 输入。
- `factoryio.capture_command`：可信 argv 数组，`{output}` 为必须新生成的 PNG 路径。本机进程内截图已通过真实 PNG/SHA 与工作流采集验证，输出另有桥接响应元数据。
- 已核实的 CODESYS 模板与设备映射、任务调用、协议编码。`verified_template_sha256` 绑定模板，`verified_transport_sha256` 绑定两端协议槽位、offset、缩放和端点；几何调整不改变运输摘要，映射调整需要重新验证。`verified_device_bindings` 来自真实工程检查。
- `codesys.deploy=true`、`factoryio.runtime_control=true`，以及经校准的事件解码配置。通用示例配置禁用部署；本机单指令配置启用联机步骤，但尚无适用于所有新工艺的事件解码器。

常规部署保留和核验已有设备映射。另有独立的模板准备步骤：只操作工程副本，通过 source hash、参数旧值和实际通道进行检查；本机限定的 Modbus 4.6 格式已完成位数组扩展、数值寄存器映射和真实编译。准备与部署分开执行，尚未支持任意 CODESYS 设备/协议配置。

`wire_encodings` 区分逻辑 Float/Int 与 Modbus 16 位 WORD。当前支持非负、缩放后不超过 32767 的数值范围。桥接脚本也保存为每次运行的文件副本及 SHA，避免正在运行时被代码编辑改变。

称重原始量、Modbus 缩放和 kg 是不同层次。`event_decoder` 只适用于已经确认一次一件、重量标定和传感器极性的实验；配置需指定称重/到位/出口标签 Key、kg_per_raw_unit、stable_samples 等。测量/跟踪输入必须明确未被强制；多个出口同时触发会保留为冲突。实测事件需包含时间和原始采样引用。未建立可信物料事件时，图片和普通标签采集不会让运行验收通过。

## 单步操作与产物

CLI 还支持 `inspect <scene>`、`build --plan <plan.json>`、`review --scene-dir <dir> --task <task.json> --evidence <evidence.json> --image <png> --out <feedback.json>`。runtime review 另需 `--phase runtime --plc-dir <plc-dir>`。手动运行的采集同样支持 `collect --scene-dir <dir> --plc-dir <plc-dir>`，核对程序版本后把证据绑定到对应 PLC。手工图片标记为 operator_supplied，不声称其拍摄时刻已被工具确认。

一次执行保存 task、catalog、每次 scene revision、PLC revision、versions、原始证据、验收结果和 run.json。改变场景会使旧 PLC 运行证据失效。配置中的真实坐标、重量阈值、模型地址和协议编码应根据具体实验修改。

## MCP 与证据

[CODESYS/Factory I/O MCP 评估](docs/mcp-evaluation.md) 记录官方/开源候选、用途、许可和本机实测范围。本项目提供九个 MCP 工具，`codesys_compile_program` 可接收完整 ST/GVL 和场景 I/O 契约，在工程副本中生成程序并返回真实编译诊断。正例与故意错误的负例已通过真实 MCP 调用。新增 Runtime 服务控制与只读在线探测，官方接口及权限验证见 [CODESYS 自动化适配](docs/codesys-official-automation.md)。用户提供的 [johannes CODESYS MCP 本机验证](docs/johannes-codesys-mcp-validation.md) 完成 8 工具握手、工程/POU 编辑与重量代码写入，发现部分更新清空声明和编译误报成功；另一个候选的 82 工具仍只完成列表握手。第三方服务尚未接管完整工作流。

已验证：161 项测试；真实重量控制 ST、31 个设备映射与任务调用，实际下载编译为 0 错误、0 警告；两个源场景的 102 个对象模板；实际 Factory I/O 进程中加载生成场景，56 个对象与 44 个标签匹配；新截图、相机切换、97 个 prefab 名称，以及工作流加载/截图 hook。当前调试版为 `artifacts/weight-live-start-002`，发料器 UpTo=1、取消强制开启并映射到 PLC 的 BitOutput11，只在进料阶段使能。先前持续发料版本出现后续故障，已保留失败报告。出口传感器极性、分拣轮电机使能和受 PLC 控制的逐件发料已写回生成器，按部件 Key/prefab/工作模式限定适用范围。Keep 在线探测期间观察到现场停止与计数归零，其运行不干扰性尚未通过，运行观察使用 Factory I/O 标签采集。当前记录见 [重量基线运行记录](docs/weight-baseline-runbook.md)。完整标定、隔离物料追踪及停止/复位/超时专项验收仍待执行。

## 已有分析资料

- [Factory I/O 文件格式](docs/factoryio-file-format.md)：只读解析结果、对象/标签/驱动关联。
- [场景构建步骤](docs/factoryio-scene-construction.md)：设备、物流、安装、布局、物料、I/O 和协议配置。
- [四角色设计](docs/superpowers/specs/2026-10-08-four-agent-simulation-design.md) 与 [实施计划](docs/superpowers/plans/2026-10-08-four-agent-simulation.md)。
- `tools/inspect_factoryio.py` 和 `tools/inspect-environment.ps1`；初始环境快照不代表当前运行状态。
- [手动练习指南](docs/manual-baseline.md)：早期 From A to B 候选练习，不是本轮运行记录。
- [本轮操作提炼的 Agent 规则](docs/agent-workflow-lessons.md)：失败原因、角色分流、证据和工具能力边界。
- [Factory I/O SDK 与 Console 自动化核查](docs/factoryio-sdk-automation.md)：官方 C# 示例、共享内存能力、启动配置与截图入口的边界。
- [Factory I/O 软件内部调用分析](docs/factoryio-internal-automation.md)：实际程序集、延迟加载、对象放置路径，以及已完成加载/截图联机验证的版本限定桥接。

官方参考：[CODESYS Modbus 接入教程](https://docs.factoryio.com/tutorials/codesys/setting-up/codesys-mb-sp16/)、[Factory I/O Web API](https://docs.factoryio.com/manual/web-api/)、[Console](https://docs.factoryio.com/manual/console/)、[CODESYS 脚本命令行](https://content.helpme-codesys.com/en/CODESYS%20Scripting/_cds_starting_script_via_command_line.html)。
