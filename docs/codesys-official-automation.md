# CODESYS 官方自动化接口与本机适配

检查日期：2026-10-09。CODESYS 提供启动 IDE、修改工程、编译、连接、下载和启动 PLC 应用的官方接口，当前需求可以优先使用这些接口。

## 官方接口与各自作用

| 层次 | 官方接口 | 项目用途与验证边界 |
| --- | --- | --- |
| IDE 启动 | `CODESYS.exe --profile=... --runscript=... --noUI` | 独立后台进程执行脚本并退出；已实际打开工程副本和编译 |
| 工程与程序 | ScriptEngine、ScriptProject、文本对象 API | 写入完整 ST/GVL、配置已确认版本的设备、读回映射和周期任务；此前已有编译与重开证据 |
| 在线通信目标 | ScriptOnline 网关、ScriptDeviceObject 通信设置 | 读取本机网关和工程中保存的目标；已确认工程保存的网关 GUID 与当前网关不一致 |
| PLC 应用 | `online.create_online_application`、`login`、`start`、`stop`、`read_value(s)` | 本机实际 API 对象提供这些方法；尚未完成真实 Runtime 登录、下载和运行 |
| Windows Runtime 服务 | Windows Service Control Manager | 启动 CODESYS Control Win 的系统服务；当前普通账户执行返回访问拒绝 |

依据：[官方命令行脚本](https://content.helpme-codesys.com/en/CODESYS%20Scripting/_cds_starting_script_via_command_line.html)、[官方 ScriptOnline API](https://content.helpme-codesys.com/en/ScriptingEngine/ScriptOnline.html)、[官方设备通信 API](https://content.helpme-codesys.com/en/ScriptingEngine/ScriptDeviceObject.html)、[官方 Runtime 启动说明](https://content.helpme-codesys.com/en/CODESYS%20Control/_rtsl_start_runtime.html)。

`OnlineChangeOption.Keep` 表示保持已有应用，不进行下载或在线更新；`Never` 表示执行完整下载。本项目的在线探测只采用 Keep。原有部署路径采用完整下载，但仍受程序、I/O、传输、模板哈希和任务检查约束。

## 服务启动前的执行记录

- 安装版本为 CODESYS V3.5 SP22 Patch 3，ScriptEngine 插件 4.2.0.0；当前账户下后台启动和编译成功，0 错误、0 警告，源工程哈希未改变。
- 本项目 MCP 实际握手返回 9 个工具，新增 `codesys_runtime_service` 和 `codesys_online_probe`。这些工具属于项目服务器，尚未注册为 Codex 全局 MCP。
- Gateway 服务运行于本机端口 1217；CODESYS Control Win V3 - x64 服务停止，检查时 11740 和 Modbus 502 未监听。
- 原生服务启动返回 Windows 错误 5（访问被拒绝）。当前账户 `tongqingwang` 不是管理员；本地 `USER` 是管理员。
- 使用当前本地网关查找 `127.0.0.1:11740` 时，官方 SDK 返回“没有设备及时响应扫描请求”。工程编译仍成功，但 `probe_passed=false`、`online_connected=false`。未执行登录、下载或应用启动。
- 保存的目标网关 GUID 为 `fe4ca74d-3575-411e-a931-c28ccbe0cf3e`，当前 Gateway-1 为 `dbd4a22a-56a0-40bc-8ef5-cd59c6109382`。探测从当前网关选择目标；工程的持久化部署目标仍需在联机阶段核验。GUID 不一致不能单独解释本次失败，Runtime 停止是已确认的阻塞条件。
- 119 项测试通过。新增 5 项在线探测测试检查远端目标拒绝、缺少 Keep 时不退回下载、登录失败不误报连接、只读成功退出等边界；这些模拟测试不作为联机证据。

原始结果位于 `artifacts/codesys-official-api-001/`：`capabilities-report.json`、`runtime-start.json`、`mcp-inventory.json`、`mcp-runtime-status.json` 和 `mcp-online-connect.json`。

## 以管理员身份启动本机服务

当前需要管理员权限的是 Runtime 系统服务启动。已准备 `tools/start-codesys-runtime.ps1`，脚本要求提升权限，核对已观察的服务可执行路径后启动 Gateway 和 Runtime，并等待服务进入 Running。该脚本已通过语法解析，尚未在管理员账户下执行。

在开始菜单搜索 PowerShell，右键“以管理员身份运行”。当前是普通账户，凭据提示中选择或输入 `.\USER`；用户说明其密码为空，可尝试留空提交。如果提示不接受空密码，则按 `Win + L`，切换用户并本地登录 `USER`，再打开管理员 PowerShell。切换用户保留原账户会话。

在管理员 PowerShell 执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "F:\phd\project\digital-twin-factoryi-o\tools\start-codesys-runtime.ps1"
```

成功输出应包含 `"service_running": true` 和 `"service_state_after": "Running"`，证据保存到本项目 artifacts 目录中新的 `runtime-start-elevated-*.json`。随后切回 `tongqingwang` 可继续使用当前工程和软件会话。

该脚本不修改账户、服务启动类型或 Windows 安全策略，不下载 PLC 应用，也不调用应用的 Start。Runtime 启动时可能按自身配置恢复已有启动应用，因此报告保留 `plc_application_running_verified=false`，直到实际读取应用状态并验证传输。

Windows 依据：[Microsoft UAC 工作原理](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/user-account-control/how-it-works)、[Microsoft Start-Process 提权示例](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.management/start-process?view=powershell-5.1)。

## 接下来需要实际验证

服务进入 Running 后，重新使用官方 API 探测本机目标和 Keep 登录，核对当前网关与应用状态；之后确认 Factory I/O 的实际 Modbus 驱动、设备映射和读写方向，再在工程副本上下载控制程序并执行单件称重标定。服务运行、应用运行、Modbus 连接和物理分流分别记录验收结果。

## 2026-10-09 服务启动后的验证

用户在管理员 PowerShell 执行启动脚本后，Runtime 服务为 Running，PID 93672；11740 与 502 均由该进程监听。普通账户无需再次提升权限即可发送 Modbus 请求。只读 Function 2 / Unit ID 1 / offset 0 / count 1 返回合法响应与输入值 true，证明 Modbus 服务实际应答。此读数尚未对应到 Factory I/O 场景中的标签，也不能证明完整仿真通过。

第一轮 MCP 在线探测达到 120 秒主机超时，所创建的独立 CODESYS 进程已结束，用户的 IDE 保留。已将探测改为禁止交互式登录、15 秒应用操作超时，并分别保存 inspect/scan/login/logout/complete/failed 阶段回执。随后官方 API 成功发现本地设备地址 `03F4.1000.2DDC.7F00.0001`，编译 0 错误、0 警告，登录因缺少设备凭据失败。该失败属于工具/设备认证，不应驱动 PLC Agent 重写控制逻辑。

使用用户提供的设备凭据，通过 `set_specific_credentials` 注入到当前解析出的设备目标；官方 Keep 登录返回“目标设备无效用户验证”。核对凭据绑定在通信目标更新之后再次尝试，结果相同。未下载或启动 PLC 应用，源工程哈希未改变。尚需核对真实设备凭据或确认 GUI 手动登录结果，不能把服务 Running 宣布为官方在线控制已通过。

新增 `tools/probe_codesys_online.py` 与 `tools/probe-codesys-online.ps1`：后者在本机交互读取设备账户与隐藏密码，仅为子进程设置临时环境变量，执行结束恢复环境。脚本已通过语法检查。后台桥接只将环境变量名称放入请求文件，具体凭据仅在进程内提供给 SDK，异常文本会遮蔽所配置的凭据值。当前共 124 项测试通过，包括禁用交互、阶段回执、缺少凭据拒绝登录、设备范围注入及异常脱敏；最后调整凭据绑定顺序后，10 项相关测试再次通过。

直接运行本机凭据探测：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "F:\phd\project\digital-twin-factoryi-o\tools\probe-codesys-online.ps1"
```

长期运行的 Agent/MCP 可在 `codesys` 配置中指定环境变量引用，并在启动服务的进程环境中提供值：

```json
"online_credentials": {
  "username_env": "FACTORYIO_CODESYS_USERNAME",
  "password_env": "FACTORYIO_CODESYS_PASSWORD"
}
```

这些是 PLC 设备认证凭据，不是 Windows 管理员凭据。官方接口的临时认证范围和禁止交互机制见 [ScriptOnline API](https://content.helpme-codesys.com/en/ScriptingEngine/ScriptOnline.html)。

证据：`artifacts/codesys-runtime-running-001/`（服务、Modbus、Factory I/O 状态）、`artifacts/codesys-runtime-running-002/`（扫描成功、缺少凭据）、`artifacts/codesys-auth-login-001/` 和 `artifacts/codesys-auth-login-002/`（设备认证失败）。Factory I/O 当前可读 44 个标签，Running 为 false；物理分流尚未执行。

## 更新设备凭据后的在线通过记录

用户更新设备凭据后，实际 CLI Keep 登录通过，工程编译 0 错误、0 警告，源工程哈希未改变；PLC 应用状态为 `stop`，操作状态为 `4097`。随后通过项目 MCP 的 `codesys_online_probe(connect=true)` 再次登录成功，`probe_passed=true`、`online_connected=true`，读取状态后注销。两轮均未请求下载或调用应用 Start，脚本级凭据已清理。

`config.local.json` 现只保存 `online_credentials` 的环境变量引用；实际凭据通过本次进程环境提供。新增 `examples/mcp/project-online-tools.json` 指向当前本地配置，其客户端超时为 150 秒，大于内部后台进程的 120 秒预算；项目 MCP 未注册为全局服务。未来启动 MCP 的进程需要提供所引用的环境变量。

此时 Runtime PID 48232，11740 与 502 正常监听；Factory I/O 可读 44 个标签，Running 仍为 false。证据为 `artifacts/codesys-auth-login-004/acceptance-report.json` 与 `artifacts/codesys-auth-mcp-001/acceptance-summary.json`。设备认证和状态读取已实测通过，下载/应用启动与物理分流仍需各自验收。

## 实际下载启动与运行观察的边界

2026-10-09 已在工程副本中完成重量程序下载和 Start；`OnlineChangeOption.Never`、当前本机网关解析、通信目标绑定后注入临时设备凭据，与下载前设备/任务/哈希检查共同执行。程序编译为 0 错误、0 警告，官方会话读到 Run；Factory I/O 的实际 Modbus 输送和出口计数进一步验证控制在执行。最终工程和回执见 `artifacts/weight-live-start-001/plc-download-006`。

桥接读取工程副本的 IDE simulation 状态，必要时禁用并读回；在线 Application 对象在目标绑定之后创建。监控订阅使用官方 system.delay 处理 IDE 消息循环。以上措施不自动证明 Keep 探测对运行无干扰：在运行中另起后台 IDE 做 Keep 登录，观察到应用 Stop、现场计数归零。脚本未调用 stop/reset/download，原因尚未确定；这次运行中探测不计为验收通过，也不继续重复。

官方文档说明 Keep 不在线更新或下载，但本机整条探测流程的不干扰性仍需独立测试。[ScriptOnline API](https://content.helpme-codesys.com/en/ScriptingEngine/ScriptOnline.html) 当前运行观察使用 Factory I/O Web API 标签和进程内新截图；后续应在隔离实验中逐步检查构建、登录、注销、工程关闭和进程退出分别带来的影响。
