# 重量分拣控制基线

本轮由当前助手执行规划补充、编写 ST、准备设备、实际下载与联机运行。未调用外部大模型。2026-10-09 已观察到连续前方/右侧分流；完整标定与行为验收尚未完成。下面的早期产物和人工步骤保留为历史，当前联机结果见文末。

## 可审查产物

- `artifacts/weight-baseline-02/scene/scene.factoryio`：与已经请求手动加载的 `artifacts/agent-demo-02/scene-001/scene.factoryio` 字节相同，56 对象、36 对象标签，SHA 为 `2990403cc6775dc16b917c70c3951d008e16e9e85d9cd84b9164c4a44a901fa0`。
- `artifacts/weight-baseline-02/plc/PLC_PRG.implementation.st` 与声明/GVL/program.json：包含实际控制逻辑。
- `artifacts/weight-template-reopen/project/example1.project`：已经重新打开、编译和检查映射的工程副本。
- `artifacts/weight-compile-10/runner-report.json`：设备准备、30 个映射读回和真实编译的结果。
- `artifacts/weight-template-reopen/runner-report.json`：保存后重新打开检查，compiled、bindings_verified、task_program_verified 均为 true；0 错误、0 警告，started=false。
- `artifacts/weight-live-preflight/evidence.json`：真实采样只得到 8 个系统标签，目标场景要求 44 个标签，reconciliation=false。

原始 `C:/Users/tongqingwang/Documents/example1.project` 保持不变。部署与模板准备分开；当前尚未下载或启动重量程序。

## 控制与映射

状态依次为 idle(0)、feed(10)、weigh(20)、route(30)、clear(40)，错误进入 fault(99)。稳定称重要求相对锚点的变化不超过 0.05 kg、持续 500 ms；锁存后按 [0,3)、[3,6)、[6,20) kg 路由。每个活动状态有 30 s 超时，出口边沿计数，错误或同时出口触发进入故障。正常 Stop/Auto-off 中断活动周期会锁存故障，清空并复位后才能重新启动。

称重为 `WORD_TO_REAL(WeightRaw)/100 * KgPerVolt`，当前 KgPerVolt=2 是由 20 kg 模式、0–10 V 范围推断的预期系数，尚未实测。显示为整数，单位 0.01 kg。计数上限为 32767。场景中另有未映射的 Emergency stop 标签，本基线仅实现已映射的普通 Stop；Emitter 仍沿用源场景的强制开启配置，单件实验需由操作员限制发料。

| Factory I/O 槽位 | CODESYS 数据区与地址 | 内容 |
| --- | --- | --- |
| BitInput0..13 | Coils / `%IX20.0`..`%IX21.5` | 9 个位置传感器、Start/Reset/Stop/Auto、SimulationRunning |
| BitOutput0..10 | Discrete Inputs / `%QX20.0`..`%QX21.2` | 进料/秤/三路分流/三路输送/三个灯 |
| NumericInput0 | Holding Register 0 / `%IW0` | WeightRaw，Float 电压值乘 ScaleFactor=100 |
| NumericOutput0..3 | Input Registers 0..3 / `%QW0`..`%QW3` | 左/前/右计数、重量显示 |

注意：`%IW0` 等为 CODESYS IEC 地址，Register 0 为 Modbus 地址，二者不能当成相同单位相互偏移。记录到的设备数组已经扩展到两个 BYTE；实际设备参数为 Coils Size=14、Discrete Inputs Size=11。MainTask 为 20 ms，调用 PLC_PRG。

## 本机发现的模板问题

直接设置设备参数 `.value` 可以把 11/7 改为 14/11，却不会自动把离散输入的一个 BYTE 扩展为两个 BYTE。对同版本 device.update 也未产生扩展。

本机验证过的办法是在副本中导出指定 Modbus 设备，按已观察到的原生 XML 扩展输出数组，再在相同 Ethernet 父节点下替换导入。导入到工程顶层会跳过；导入时保留原节点可能创建第二个设备并改变自动 IEC 地址。原生导出文件和各次失败目录已保留，最终成功为 weight-compile-10。此能力限定于本机 Modbus 设备 115 / 0000 0002 / 4.6.0.0 和已观察到的数组格式。

## 复现准备与编译

下面的输出目录需要使用新名字：

```powershell
& .\.venv\Scripts\python.exe -m factoryio_agents weight-baseline --scene-dir artifacts\agent-demo-02\scene-001 --inspection artifacts\codesys-baseline-check-04\project-inspection.json --out artifacts\weight-baseline-new
& .\.venv\Scripts\python.exe -m factoryio_agents compile --config artifacts\weight-baseline-new\config.json --program artifacts\weight-baseline-new\plc\program.json --io-contract artifacts\weight-baseline-new\scene\io-contract.json --out artifacts\weight-compile-new
```

## 初始人工联机计划（历史）

1. 在现有 Factory I/O 打开 `artifacts/agent-demo-02/scene-001/scene.factoryio`，Console 执行 `scene.capture_screenshot('agent_weight_manual', True)`。先核对 44 个 Tag Key 和当前布局。
2. 在 CODESYS 打开工程副本，启动本机 Control Win runtime，检查 Modbus 502 端口和目标通信配置。当前最新检查为 runtime 停止、502 未监听。当前模板没有被自动下载。
3. 一次只放一个物料，记录 Factory I/O 重量电压、Modbus WORD 和已知重量。传输关系应为 WORD/100≈电压；kg/V 的系数必须由实测确认。控制面板的传感器/Stop 极性也需核对。
4. 三档各至少一件，记录称重稳定、分流输出、出口边沿、计数；随后验证普通停止、清空复位和超时。保存原始采样与图片。
5. 标定和映射实测后才更新 transport 确认与摘要，并处理 task/program 中的 unresolved。模型服务配置后再把相同流程交给四个角色执行。

手动运行采集命令：

```powershell
& .\.venv\Scripts\python.exe -m factoryio_agents collect --config artifacts\weight-baseline-02\config.json --scene-dir artifacts\weight-baseline-02\scene --plc-dir artifacts\weight-baseline-02\plc --seconds 30 --out artifacts\weight-trace-new
```

采集不等于启动或验收。没有正确物料追踪、校准和真实来源的事件，acceptance 仍不能通过。完整的流程改进见 [Agent 执行规则](agent-workflow-lessons.md)。

## 2026-10-09 自动启动与物理调试

用户保存当前场景后，Factory I/O 完整重启，auto.cfg 自动加载 bridge-build-03；代码加载 `artifacts/weight-live-start-001/scene.factoryio`，实际驱动为 ModbusTCPClient，连接 127.0.0.1:502 / Unit ID 1。场景 SHA 为 `e57f53e1190ff1d5ae11f5347065f5fe9dd8ef853f3aa2b2f3cdd7537768c5ac`，56 对象、44 标签。未修改原始高度场景和原始 CODESYS 工程。

PLC 在工程副本中使用官方 ScriptEngine 全量下载和启动，每次核验 30 个实际绑定、MainTask 20 ms 调用 PLC_PRG、模板 SHA 与传输指纹。最终代码为 `program-final.json`，工程与编译/启动回执位于 `plc-download-006`。`commissioning=True` 允许保留物理标定待办进入调试，不放宽结构、设备、任务或协议检查，也不标记完整物理验收通过。

现场出现两次可重复的控制问题，已修复到 `factoryio_agents/weight_baseline.py`：

- 出口使用 RetroreflectivePhotoelectricSensor，光束畅通时 True，遮挡时 False。先归一化为 Occupied，再用于空线判断和到达边沿计数。At scale entry 也是反射式传感器，其原始值不能当作高电平占位；该基线允许上游等待，不将它用于 PlantEmpty。秤与分支入口使用漫反射传感器。
- PopUpWheelSorter 的 SingleBinary 模式中，Send forward 同时使能滚轮升起与电机。左/右位控制转向，侧向分流也要保持 Send forward=True。旧程序在右转时关闭电机，物料在台上停留直到 30 秒超时；修正后右侧出口计数持续增加。该语义限定在已观察模式，生成器现在按实际 Tag Key 反查 prefab 与 OperatingMode；仅有同名标签无法通过检查。

操作输入通过 Web API 设置 Auto=True、Manual=False，并短暂按下/释放 Start。这些 operator 输入的强制状态单独记录；物料、称重、出口传感器及 PLC 控制的输送/分流输出未被强制。Emitter 与三个 Remover 沿用源场景的强制开启配置。运行中获取新截图和原始标签轨迹，不使用手工编造的物料事件作为验收。

`runtime-trace-start-005.json` 保存修正后的 90 秒运行，最终右侧 6、前方 1、左侧 0；故障灯未亮。随后在运行中进行官方 Keep 登录复查，观察到应用 Stop、现场计数归零和输送停止，见 `plc-running-probe-005` 与 `runtime-trace-continuous-005.json`。脚本没有调用 stop/reset/download；不将该现象认定为已确定的 SDK 根因，也不将 Keep 视作已实测不干扰运行的监控工具。已恢复最终程序；后续观察只通过 Factory I/O 标签与截图进行。

最终运行证据与当前状态保存在 `artifacts/weight-live-start-001/live-start-report.json`。标定系数、左侧样本、单件隔离、普通 Stop/复位/超时专项实验继续作为独立待办；启动成功不替代这些验收。

## 连续发料后续故障与逐件发料版

更长观察后，001 版出现故障灯亮、输送停止。`live-start-report.json` 的 current_auto_cycle=false，startup_passed=false；没有把早期 150 秒正常片段当作最终启动通过。发料器仍持续强制开启，单件控制的输入前提不能保证；后续改为受 PLC 控制的发料。

当前版为 `artifacts/weight-live-start-002`：在场景副本将 Emitter.UpTo=1，取消 Emitter 强制值，增加 `BitOutput11 → WeightIO.Emit → %QX21.3`；Modbus BitOutputCount/Discrete Inputs Size 从 11 改为 12，输出数组已有两个 BYTE，未增加数组维度。原有 30 个映射全部保留，新增第 31 个绑定先在独立模板准备步骤中读回并编译，再以新模板 SHA 与传输指纹下载启动。

Factory I/O 2.5.8 的 Emitter 在 Emit=False 时重置已发数量，因此 PLC 只在 feed(10) 开启 Emit，每个周期最多生成一件；weigh/route/clear/idle/fault 都关闭 Emit。这个语义来自本机组件代码，并用实际场景观察验证。新版基线生成器自动准备该场景副本、I/O 契约和 31 个绑定，拒绝既有槽位冲突、未知模式和非零偏移。139 项测试通过。

载入新版时，运行场景的原生未保存检查仍返回 unsaved_scene_changes。已保存当前由助手生成的场景文件、对象/标签和临时面板快照，重启该生成场景后加载新版本；未覆盖用户最初保存的高度场景。启动后旧驱动提示消失。完整结果见 `artifacts/weight-live-start-002/live-start-report.json`；完整标定和专项行为验收仍独立保留。


002 版的保存轨迹已汇总：184.704 秒、695 个采样，左/前/右计数从 0/0/0 增至 3/5/7，无故障，31 绑定，编译 0 错误 0 警告。startup_passed=true；该报告描述已保存观测，不是生成报告时的实时健康检查，physical_acceptance_passed=false。

这些调试经验已落实为 [多 Agent v2 交接与提示词](multi-agent-v2.md)，当前共有 161 项 Python 测试。
