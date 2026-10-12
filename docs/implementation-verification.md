# 第一版实现与验证记录

日期：2026-10-08。当前已完成四角色接口、工具、版本管理和有限修复编排，未完成大模型驱动的联机重量分拣。

## 实际验证结果

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| Python 单元与集成边界测试 | 99 项通过；模型与运行适配器在 workflow 测试中明确 mock | `python -m unittest discover -s tests -q` |
| 模板目录 | 两个真实源场景、102 个对象模板 | `artifacts/agent-catalog.json` |
| 重量场景生成 | 56 个对象、36 个对象标签；结构/引用检查通过，修复前后 Key 稳定 | `artifacts/agent-demo/demo-report.json` |
| 角色修复示例 | 固定回复示例中完成一次传感器 Y 高度修正；未调用模型 | `artifacts/agent-demo/scene-000`、`scene-001` |
| Factory I/O 标签采样 | 本机 7410 API 实际采到 8 个系统标签，当前没有匹配生成场景的对象标签 | `artifacts/live-probe/evidence.json` |
| 项目 MCP | 握手列出 6 个工具；实际工具调用读出 8 个当前标签 | `artifacts/mcp-research/project-tools-inventory.json`、`project-tags-call.json` |
| 第三方 CODESYS MCP | 握手列出 82 个工具；未调用其工程/在线操作 | `artifacts/mcp-research/codesys-candidate-inventory.json` |
| 基线工程真实编译 | 独立 noUI 进程打开 example1.project 副本，0 错误、0 警告 | `artifacts/codesys-baseline-check-04/result.json` |
| ST 写入真实编译 | 在新副本替换 PLC_PRG 的声明/实现，0 错误、0 警告 | `artifacts/codesys-st-write-check/runner-report.json` |
| 编译错误反例 | 故意加入 `State := ;`，捕获 1 个实际错误，compiled=false；同时有 18 个警告 | `artifacts/codesys-diagnostic-check/runner-report.json` |
| 原工程保护 | 上述编译均未改变 Documents 中的原工程 | 各 runner-report 的 source_unchanged；`artifacts/implementation-verification.json` |
| 重量控制真实编译 | 手写稳定称重/分流/计数/停止/超时 ST；30 个设备映射；0 错误、0 警告 | `artifacts/weight-compile-10/runner-report.json` |
| 重量模板保存后重开 | 实际映射与任务调用核验通过，再编译 0 错误、0 警告；started=false | `artifacts/weight-template-reopen/runner-report.json` |
| 当前场景联机前检查 | 8 个实际标签 / 44 个预期标签，reconciliation=false | `artifacts/weight-live-preflight/evidence.json` |

早期 ST 写入验证使用 State 固定为 0 的离线 fixture。新增重量基线由本轮助手手写真实控制逻辑，已通过工程副本编译；并未调用外部模型。所有编译均 `started=false`，没有下载或启动重量 PLC。

初次编译定位并修复了 Windows 原始命令行参数引号、ScriptProject.active_application 属性，以及 System.Guid 类型要求。失败目录保留在 `artifacts/codesys-baseline-check*`，最终成功结果为 `-04`。

## 实际模板检查

原始 inspection 包含 14 个工程节点、453 个设备参数、18 个具名数字量映射，MainTask 为 20 ms 并调用 PLC_PRG。原工程使用 FIO GVL 和转台分流逻辑。重量副本已增加 WeightIO，绑定 14 个输入位、11 个输出位、1 个输入 WORD 和 4 个输出 WORD。

真实实验发现修改参数数量不会扩展通道数组。限定的本机 Modbus 4.6 模板通过设备原生导出/修改/导入解决，再检查实际数组、映射和编译。导入操作只发生在副本，原生导出保留；准备与部署分开。详见 [重量运行记录](weight-baseline-runbook.md)。

模板部署要求 `verified_template_sha256` 与原工程一致，`verified_device_bindings` 完整覆盖生成程序的每个绑定。每行包含 tag_key、variable、device_path、connector_id、manual_iec_address、channel_type、bit_size，信息来自实际 inspection。桥接核对实际映射和任务，在下载前再读取核验；读取失败或映射不匹配会阻断部署。`verified_transport_sha256` 还把证据绑定到当前 Factory I/O 协议端点、槽位、offset 和缩放配置。加载器需提供当次 nonce、文件路径和 scene hash 的实际加载回执，不能用稳定标签 Key 代替新场景加载确认。重量的单位、Modbus 缩放和寄存器编码还需实测；仅配置布尔开关不能替代这些实验。

## 尚未通过的环节

- 当前没有四个角色的大模型服务配置；本轮助手完成了控制基线，但没有独立四模型服务的执行测试。
- 早期桌面窗口截图失败：FrameArrived/window capture 超时；后续进程内截图已通过，见下方 2026-10-09 联机补充。
- 使用注册表中的 `load:<path>` 文件打开参数尝试启动独立 Factory I/O 进程；15 秒内原 7410 服务仍只有系统标签，生成场景的 Key 没有匹配，不能确认场景已加载。只关闭了本次新建的测试进程，原进程 PID 40048 保留。详见 `artifacts/factoryio-load-check.json`。
- 早期加载和截图 hook 未配置；后续本机 hook 已配置并实测，Agent3 的独立模型服务仍缺少配置。
- 重量模板设备映射已通过实际读回、保存后重开和编译检查。传输、信号标定与物料追踪未完成联机验收。最新 runtime 服务停止，502 未监听。所有报告仍明确 live_simulation_passed=false。
- 第三方 CODESYS MCP 尚未成为运行后端；官方 MCP 的安装与许可未验证。

后续实验先完成“软件加载 → 标签 Key 对照 → 当前画面采集”，再在已准备的重量模板中标定并进行真实物料路由。模型配置到位后再交给 prepare/run。根据本轮过程已经改进了失败分流、证据来源、版本校验、数值编码、设备准备和桥接脚本快照；详见 [Agent 执行规则](agent-workflow-lessons.md)。


## 2026-10-09 软件内部分析补充

完成 Factory I/O 核心程序集的选择性分析、项目桥接 DLL 的真实编译，以及隔离 IronPython 的 execfile/程序集加载/协议方法验证。104 项 Python 测试通过，包含 5 项新的桥接 IPC 失败条件测试。全部 159 个原安装托管 DLL 哈希未改变。桥接未在 Unity 进程中初始化，加载和截图尚未联机验收。详见 [内部调用分析](factoryio-internal-automation.md)；机器记录为 `artifacts/software-analysis-001/analysis-report.json`。

## 2026-10-09 实际进程与工作流验证

用户在已打开的高度分拣场景执行一次 Console 引导。桥接在原 PID 40048 中成功初始化，软件版本 2.5.8，核心 DLL SHA 与分析版本一致。此后场景加载、截图和相机请求均由代码发起，没有手动导入生成场景。

首次验证通过后，连续加载暴露生成器删除 `Screenshot` 节点的问题：原生 `Scene.GetSceneData` 无空值检查地移除 Screenshot/Camera/Description，第二次加载的未保存检查因此异常。现在生成器清空旧缩略图并保留空节点；已在实际进程中恢复并再次加载。恢复时只短暂补充旧生成文件的元数据，之后将旧文件逐字节恢复，原 SHA 保持不变。

| 本次检查 | 结果 | 证据 |
| --- | --- | --- |
| 修复后的自动加载、再次加载 | 已通过；文件 SHA、实际路径和加载完成帧核对 | `artifacts/bridge-live-validation-003/verification/verification-report.json` |
| 对象与标签 | 56/56 对象 Key/prefab/网格/旋转/精细偏移匹配；44/44 标签匹配 | 同目录 `object-reconciliation.json`、`tag-reconciliation.json` |
| 当前截图与相机切换 | 新 PNG/SHA 通过；相机 0、4 分别有新截图 | `bridge-live-validation-002/capture-extra.json`、`bridge-live-validation-003/views/overview.png` |
| 实际可用 prefab | 97 个名称；未据此推断新部件尺寸或配置 | `artifacts/bridge-live-validation-003/prefabs-response.json` |
| 工作流 hook | 加载 nonce/SHA/对象回执、collect_evidence 标签与截图通过 | `artifacts/bridge-live-validation-003/workflow-hooks/hook-verification.json` |
| Python 测试 | 114 项通过，含重复加载元数据回归与失败回执条件 | `python -m unittest discover -s tests -q` |
| 完整重启后的启动引导 | 已写 auto.cfg，尚未重启测试 | `artifacts/bridge-live-validation-002/workflow-hooks/configuration-install.json` |

当前画面是处于编辑状态的重量场景，驱动显示 None。没有启动 PLC、物料实验或四个独立模型服务；重量标定与实际分流仍为未通过。已有工程编译证据不能替代这些实验。
