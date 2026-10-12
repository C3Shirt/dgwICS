# 从本机操作提炼的 Agent 执行规则

这些规则来自实际文件解析、Factory I/O 标签采样、CODESYS 副本编译和失败反例。当前助手执行了手动基线工作；没有把固定回复或手动代码标成外部模型输出。物理重量分拣尚未验收。

## 工作流需要补充的阶段

原来的“选对象 → 摆放 → 看画面 → 写 PLC”需要加入以下可验证阶段：

1. **能力检查**：软件版本、运行授权、可用 API、模型服务、目标 runtime 与协议端口。
2. **工艺契约**：设备、物料、流向、正常状态、停止与复位方式、超时、重量区间及验收样本。
3. **场景构建**：真实 prefab 模板、XZ 布局、Y 高度、旋转、安装点、工作模式、Emitter/Remover、标签与驱动槽位。
4. **加载确认**：当前文件哈希、当次加载回执、软件中的标签 Key、当前画面。稳定 Key 不能证明修复后的几何已经加载。
5. **传输契约**：逻辑数据类型、线上位宽、Modbus 表区/offset、缩放和物理单位分别记录。
6. **PLC 模板准备**：设备参数、实际通道、声明与映射、20 ms 任务调用；保存后重新打开检查。
7. **控制程序编译**：真实编译消息、错误计数、当次 request ID 和工程哈希；只有实际代码错误进入 PLC 修复循环。
8. **标定与运行**：先单件实验，核对电压/寄存器/重量和传感器极性，再进行三类物料的分流。
9. **证据验收**：真实采样时间、未强制输入、称重到出口的物料关联、计数一致性、超时与停止测试。

## 已转化为代码的经验

| 实际问题 | 对 Agent/工具的约束 | 实现入口 |
| --- | --- | --- |
| 初始对坐标轴的理解与文件不符 | XZ 为水平面、Y 为高度；沿用工作模板的部件安装关系 | scene.py、Agent2 提示词 |
| 一个 Weight 名称同时指称重输入和显示输出 | 使用 Tag Key、方向和类型定位，不以显示名称作为唯一标识 | scene.py、runtime.py、plc.py |
| Float 标签经 Modbus 变成 16 位数值 | 声明 wire_encodings，绑定 WORD，再在 ST 中转换；缺失编码不能部署 | plc.py、PLC 提示词 |
| ScaleFactor=100 与 kg/V 是两个系数 | WORD/100 恢复电压，电压到 kg 需要单独标定 | weight_baseline.py |
| 工程原有数字量数量不足 | 检查设备参数与实际数组，不能仅生成更多 BOOL | codesys_bridge.py |
| 写入 Coils/Discrete Inputs 数量没有更新实际数组 | 读取修改后的结构；本机限定格式支持导出/修改/导入设备，之后重新核验 | codesys_bridge.py |
| 原生设备导入到错误层级可能跳过或生成另一个设备 | 在工程副本保留导出后替换指定节点，检查唯一设备路径、地址、位宽和全部变量 | codesys_bridge.py |
| CODESYS 的原始 Windows 参数与普通 argv 引号不同 | 本机版本要求 --profile="名称"；实际命令和诊断保存在报告中 | plc.py |
| ScriptEngine API 与假设不同 | 使用 active_application 属性、System.Guid 和原生接口类；真实编译验证签名 | codesys_bridge.py |
| 工具失败可能被误认为 PLC 逻辑问题 | 未完成 build、配置缺失或工具异常转到 tools；真实编译错误才转 PLC | workflow.py |
| 多出口同时触发可能被误丢弃 | 保留全部冲突事件，跟踪验证失败 | validation.py |
| 强制值、无时间或无来源的事件可能造成假通过 | 明确未强制，要求时间和原始记录引用；离线验收永远不标 live passed | validation.py |
| 旧图片/旧配置混入新的运行证据 | 版本元组、文件哈希、新建采集路径、配置一致性检查 | contracts.py、runtime.py、workflow.py |
| 软件内部类有路由声明，但服务没有注册 | 查服务器注册链，再进行只读实测；不能凭方法或 URL 字符串宣布接口可用 | software-analysis-001、factoryio-internal-automation.md |
| 场景加载请求返回时尚未执行实际加载 | 等待 IsLoading、CurrentSceneInfo 和帧完成；之后核对对象/标签并生成新截图 | factoryio_bridge/AgentBridge.cs、workflow_hook.py（已联机验证） |
| 外部程序加载软件 DLL 无法获得 Unity 活动对象 | 首次在软件进程内初始化，通过主线程请求队列调用组件；编译和隔离测试不等于进程内通过 | factoryio_bridge/（原型） |
| 删除旧缩略图后首次加载成功，第二次加载报 NullReferenceException | 保留空 Screenshot 节点；Camera、Description、Screenshot 组成原生修改检查所需的元数据。加载验收必须包含再次加载 | scene.py、bridge-live-validation-003 |
| 44 个标签都可读，但画面当前驱动仍为 None | 标签核对不能证明 Modbus 已接通；后续 PLC 联机必须单独检查实际驱动、端点和信号传输 | bridge-live-validation-003/views/overview.png |

## 工具能力的当前边界

2026-10-09 补充，来自用户提供的 johannes CODESYS MCP 实测：

- tools/call 的 isError=false 只表示操作回执成功。向 PLC_PRG 写入未定义变量后，上游 compile_project 仍成功，独立编译器确认 2 个错误；Agent 必须按真实错误计数判断编译结果。新 codesys_compile_program 已用正负两组真实调用验证这个边界。
- 部分代码更新的接口说明也要验证。上游省略 declarationCode 时会清空声明；使用它时必须同时发送完整声明和实现，不能让修复轮次丢掉上一版变量。
- 创建 Program 不会自动加入周期任务。MCPProbe 已创建且保存，但真实 MainTask 仍只调用 PLC_PRG；必须读取任务调用关系。
- MCP 负责工程操作，运行控制仍经 Modbus。上游每次调用启动新 headless IDE，实测约 22–23 秒，不适合充当 PLC 扫描循环。

完整证据见 [用户提供的 MCP 本机验证](johannes-codesys-mcp-validation.md)。

2026-10-09 官方接口适配补充：IDE 后台启动、Windows Runtime 服务启动和 PLC 应用 Start 分属不同层次。普通账户已能执行 ScriptEngine 和编译，但启动 Runtime 服务返回 Windows 错误 5；Agent 应将它归入环境权限问题。工程保留的网关 GUID 与当前本机网关不同，需要按当前环境选择并核验通信目标。只读在线探测明确使用 Keep，不以完整下载作为连接失败的回退。服务 Running 仍需单独核验应用状态、Modbus 连接和物理分流，见 [官方自动化适配](codesys-official-automation.md)。

服务启动后补充：管理员身份启动 IDE 不会提升已有 Agent 进程的权限；Runtime 服务需单独验证。服务监听 502 后，实际只读 Modbus 响应成功，但 CODESYS 在线接口仍需独立的 PLC 设备认证。Windows USER 账户不能替代设备账户。后台探测禁止交互式凭据弹窗，记录阶段并限定应用操作超时；用户名和密码从配置指定的环境变量传入，通信目标更新后再绑定到该设备，错误回执不包含实际凭据。扫描成功、认证失败与程序编译失败必须分别归因。保持原工程与当前 IDE，不能因为认证失败而重写控制程序或回退到完整下载。

- 场景工具可组合现有模板并做受限编辑，还不能把任意新 prefab 当成已知可用部件。
- CODESYS 设备扩展限定在本机已观察的 Modbus TCP Server 115 / 0000 0002 / 4.6.0.0，以及一个输出 BYTE 扩展为两个 BYTE 的格式。不能泛化为任意设备或版本。
- 官方脚本导出/导入 API 是公开接口；导出 XML 内部字段是本机观察到的结构，需要版本限制、读回和编译共同验证。[CODESYS 原生导出/导入 API](https://content.helpme-codesys.com/en/ScriptingEngine/ScriptProject.html)
- 数值实验限定非负且缩放后不超过 32767，避免未经验证的负数解释。Factory I/O 文档说明 Modbus 转为两字节整数，并对 Float 使用 ScaleFactor；这些说明不替代物理标定。[标签类型](https://docs.factoryio.com/manual/tags/)、[Modbus 驱动](https://docs.factoryio.com/manual/drivers/modbus-client/)
- MCP 握手和工具列表只证明可调用接口存在。工程编译、设备准备、下载启动、场景运行分别需要自己的执行证据。

## 下一次联机运行的验收顺序

场景自动加载、对象/标签核对、截图和相机切换已通过；本机工作流 hook 已配置。下一步检查实际 Modbus 驱动和 CODESYS 通信，在准备好的工程副本中完成单件重量标定，再做三档路由、错误出口、停止/复位和超时实验。每次反馈记录“观测到的步骤和信号、期望变化、证据 ID、负责修改的角色、修复后的实测结果”。启动引导配置已写入，软件完整重启后的自动初始化仍需另行验证。

尚未执行的运行实验必须保留未执行状态。不能因编译无错误、图片看起来整齐，或语言模型返回 pass 而宣布流程成功。

## 2026-10-09 下载启动与场景保存边界

用户明确要求启动 Factory I/O 和 PLC 后，在 `artifacts/weight-live-start-001/plc-download-001` 完成了重量程序的实际全量下载与启动。30 个设备绑定、MainTask 调用和当前模板 SHA 在下载前重新检查；官方在线接口解析当前本机 Gateway 后注入进程环境凭据，使用 `OnlineChangeOption.Never` 下载、`start()` 启动并读取 `application_state=run`。编译 0 错误、0 警告，原工程保持不变。

物理标定需要运行中的仿真，因此新增显式 `commissioning=True` 调试模式：保留程序内的物理观测待办，仍检查声明、完整绑定、Modbus 编码、传输指纹、模板 SHA、实际设备映射和任务调用；不将调试启动标记为物理验收通过。默认工作流继续要求所有待办关闭。在线只读探测仍使用 Keep，不下载、不启动。

本次 Factory I/O 拒绝加载驱动副本，原因是 `unsaved_scene_changes`。退出准备弹出了 Unsaved Changes；现有五动作桥接没有原生保存接口，不能自动丢弃这些改动。已备份加载文件、对象/标签快照和 auto.cfg，准备新版桥接的本机 Modbus 连接与状态读取功能；等待当前场景保存后再启动 Factory I/O。后续应增加“保存到新路径并核对原生文件”的桥接能力，并在重启前将未保存状态作为明确前置检查。此处未声称 Factory I/O 已运行或物料分流通过。126 项 Python 检查通过。

## 保存后完成启动：实际工艺语义与工具干扰

用户回复已保存后完整重启 Factory I/O，auto.cfg 自动初始化新版桥接。当前场景由代码加载，驱动连接与相机/截图均可通过桥接执行。运行中调试发现：反射式出口的 True 表示光束畅通；分拣轮 SingleBinary 的 Send forward 是升起/电机使能，侧向分流也需要它。旧程序分别因此无法起动和侧向输送超时。修正后通过真实出口变化、计数递增与新截图观察到连续分流，修正已进入 PLC 基线生成器。

Agent2/PLC Agent 的部件契约应包含 prefab、OperatingMode、输入极性和输出作用，按 Tag Key 追溯实际对象。不能从“Send forward”这样的名称推断三个输出互斥，也不能从 True 推断所有位置传感器都被占用。当前基线拒绝同名标签上的错误 prefab、错误工作模式和重复 Key；135 项测试通过。

在成功运行之后调用 Keep 在线探测，现场出现 Stop 与计数归零。脚本显式使用 Keep，且不调用 stop/reset/download；仍没有证明整个工具链对运行无干扰。因而 API 文档语义、脚本调用检查与现场不干扰性属于三项证据。该探测暂用于停机调试，运行观察采用 Factory I/O 标签与新截图。恢复最终 PLC 后另存新的连续轨迹，不复用受到探测中断的轨迹宣布通过。

更长观察发现持续发料版后续故障，001 最终报告保留 startup_passed=false。控制器按单件状态机工作，源场景 Emitter 却强制持续开启；Agent1 的“单件”前提必须落实到 Agent2 发料配置与 PLC 输出。002 版将 Emitter.UpTo=1、取消强制并映射给 PLC，只在 feed 开启；Emitter=False 重置已发数量。新增槽位需要同步更新 Factory I/O 通道数量、CODESYS 参数、实际绑定、模板 SHA 和传输指纹，不能只在 ST 增加变量。31 绑定准备/下载分开执行，139 项测试通过。

API 控制 Run 的请求回执和真实 Running 状态也应分别检查。驱动提示窗口在场景切换请求后仍可能可见，不能将请求成功当作通知已关闭或编辑模式已生效。只对本轮生成、所有临时修改已记录的场景执行重启；原生未保存检查继续保留。后续桥接应提供保存到新路径与关闭已确认恢复连接的通知两项受限能力。


## 已落实到多 Agent v2

工艺步骤/单件发料契约、实际模板配置上下文、版本限定部件语义、SHA 交接包、结构化因果诊断、无进展修复检测、驱动连接回执、Running 读回、事件优先证据选择与验收结果回传均已进入代码。161 项测试包含四个真实角色包装器的模拟交接，未调用外部模型或改变本轮正在运行的软件。详细协议、提示词位置与剩余 HMI/自动调查能力见 [架构 v2](multi-agent-v2.md)。
