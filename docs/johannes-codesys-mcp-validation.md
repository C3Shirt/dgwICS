# 用户提供的 CODESYS MCP 本机验证

验证日期：2026-10-09。[johannesPettersson80/codesys-mcp-toolkit](https://github.com/johannesPettersson80/codesys-mcp-toolkit) 与之前列出 82 个工具的 zbest1000 项目不同。

**工程浏览与 POU 编辑可以复用，不能原样完成下载、启动和 Factory I/O 分拣。** 本轮未调用外部模型、未下载/启动 PLC，也未进行物理分流。

## 固定版本

源码位于 `artifacts/mcp-research/johannes-codesys-mcp-toolkit/`，提交 `1195a6724017b831b4f2e24d15541dbdc57429b7`，日期 2025-05-07，MIT LICENSE 保留。包版本为 1.1.16，实际握手返回服务器版本 1.7.1，需按提交识别代码。本机 Node v20.20.0，CODESYS V3.5 SP22 Patch 3 / 3.5.22.30。

`npm ci --ignore-scripts --no-audit --no-fund` 和 `npm run build` 均通过。第三方源码未修改，未全局注册 MCP。

启动配置：[examples/mcp/johannes-codesys.json](../examples/mcp/johannes-codesys.json)。它是本项目客户端接受的服务器参数对象，不是 Codex 全局配置文件。

## 实际证据

全部修改针对输出目录的工程副本，源工程和已有 GUI 进程保留。以下路径均位于 `artifacts/johannes-mcp-validation-001/`。

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| MCP initialize / tools/list | 成功，8 个工具 | `probe/initialize.json`、`probe/tools.json` |
| 打开工程、创建 ST Program MCPProbe | 成功并保存副本 | `probe/open-project.json`、`valid/00-create-probe.json` |
| 写入完整重量分拣 PLC_PRG 并重开读取 | 成功 | `valid/02-write-weight-program.json`、`valid/03-read-weight-program.json` |
| 独立真实编译 | 0 错误、0 警告 | `native-valid-report.json` |
| 设备绑定与任务 | 30 个绑定匹配，MainTask 调用 PLC_PRG | `native-valid-mappings.json`、`native-valid-task.json` |
| 只传 implementationCode | 原声明被清空，缺陷已确认 | `invalid/01-read-partial-probe.json` |
| 写入未定义变量再编译 | 上游 isError=false；真实编译有 2 个错误 | `invalid/03-compile-invalid.json`、`native-invalid-report.json` |
| 项目完整程序 MCP 工具 | 正例 0 错误、0 警告；负例 compiled=false、2 错误 | `project-mcp-valid.json`、`project-mcp-invalid.json` |

`acceptance-summary.json` 汇总结果和证据 SHA。故意错误的 invalid-mcp.project 与有效副本分开保存。114 项现有单元测试通过；单元测试和真实 MCP 调用分别记录。

## 为什么不能直接驱动完整闭环

[固定版本 server.ts](https://github.com/johannesPettersson80/codesys-mcp-toolkit/blob/1195a6724017b831b4f2e24d15541dbdc57429b7/src/server.ts) 提供 open_project、create_project、save_project、create_pou、set_pou_code、create_property、create_method、compile_project；结构与代码读取通过 resources 提供。

- 没有设备/通道配置、GVL 创建、任务配置、网关选择、登录/下载或 PLC start/stop 工具。创建 Program 不会自动加入循环任务，本轮 MCPProbe 未被 MainTask 调用。
- compile_project 调用 build() 后打印 SCRIPT_SUCCESS，没有读取真实错误计数。本机中文错误输出到 stderr，执行器成功分支又只返回 stdout。错误反例证实 isError=false 不能表示编译通过。
- set_pou_code 将省略的声明或实现替换成空字符串，并执行两个 replace。使用上游时必须同时传完整声明和实现。
- [执行器](https://github.com/johannesPettersson80/codesys-mcp-toolkit/blob/1195a6724017b831b4f2e24d15541dbdc57429b7/src/codesys_interop.ts) 每次启动新的 CODESYS --noUI，超时固定 60 秒。本机四次反例调用各约 22–23 秒，适合编辑工程，不适合扫描控制。脚本采用 latin1；中文代码/路径需要修改编码后另行实测，本轮未确认中文支持。

## 本项目接入

上游保留为可选工程浏览/POU 编辑服务。新增项目 MCP 工具 `codesys_compile_program(program, io_contract, output_name)`，接收 PLC Agent 的完整 program.json 和场景 I/O 契约，在新工程副本中写入 PLC_PRG/GVL，返回真实编译消息、错误/警告计数、设备绑定与任务调用检查。

新工具复用已验证的 ScriptEngine 桥接，不使用上游编译回执，不下载或启动 PLC。正例和未定义变量负例均完成真实 tools/call，项目 MCP 工具从 6 个增为 7 个。配置：[examples/mcp/project-plc-tools.json](../examples/mcp/project-plc-tools.json)，使用准备好的重量模板。原 config.local.json 未改为自动部署；Python 编排器仍直接调用项目适配器。

工具调用不要求模型服务，模型只负责生成 program 输入。工程链路为：场景 I/O 契约 → 完整程序 → 副本写入/真实编译 → 映射、任务、协议和标定检查 → 单独下载启动 → Modbus TCP 运行 → Agent3 画面/标签轨迹验收。MCP 不能替代 Modbus 通信。

当前在线阶段未通过：SoftPLC 服务 Stopped，127.0.0.1:502 无监听；最后已确认的 Factory I/O 画面驱动为 None。项目报告 transport_verified=false，传输/模板摘要尚未与本轮工程重新绑定，重量标定和信号极性待确认。Gateway Running 不表示 SoftPLC Running，编译通过不表示分拣已运行。

## 重放

在项目根目录执行，使用新输出目录。第一条仅握手列工具。

```powershell
& .\.venv\Scripts\python.exe tools/probe_johannes_mcp.py --server examples/mcp/johannes-codesys.json --out artifacts/johannes-probe-new
& .\.venv\Scripts\python.exe -m factoryio_agents mcp-probe --server examples/mcp/project-plc-tools.json --out artifacts/project-plc-inventory-new.json
```

probe 脚本支持 --operations 重放请求，valid-operations.json 与 invalid-operations.json 保留本轮 payload。重放编辑前先复制模板并将工程路径改为新副本，避免修改源工程。
