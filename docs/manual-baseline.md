# 手动闭环基线：From A to B

状态：操作方案；尚未在本项目完成编译、连接和仿真。场景暂定，实际标签、极性与通道需要逐项实测。

## 目标与连接方式

使用 `2 - From A to B (Set and Reset).factoryio`，实现“到 A 启动输送带，到 B 停止”。[官方场景说明](https://docs.factoryio.com/manual/scenes/from-a-to-b-sr/)

拟采用同机连接：Factory I/O 作为 Modbus TCP Client，CODESYS Control Win 作为 Modbus TCP Server。CODESYS IDE 通过 Gateway 连接 SoftPLC；Factory I/O 通过 Modbus 端口交换 I/O。两个端口承担不同职责。[官方接入教程](https://docs.factoryio.com/tutorials/codesys/setting-up/codesys-mb-sp16/)

## 1. 准备场景与工程

1. 在 Factory I/O 中打开内置场景；原始文件位于 `E:\Program Files (x86)\factory\Scenes\2 - From A to B (Set and Reset).factoryio`。
2. 将场景另存为项目工作副本，记录保存路径和场景名。后续调整使用工作副本。
3. 确认能选择 Modbus TCP/IP Client 驱动。该驱动要求 Modbus & OPC 或 Ultimate Edition；安装版本号不能证明授权版本。[驱动说明](https://docs.factoryio.com/manual/drivers/modbus-client/)
4. 在 CODESYS 创建 Standard Project，选择与本机 `Control Win V3 - x64` 匹配的目标设备，以 Structured Text 创建 `PLC_PRG`。
5. 保存工程工作副本。记录设备版本、编译器版本、profile 与任务周期。将 `PLC_PRG` 配置到循环任务中。

## 2. 先确定信号语义

分别记录 Sensor A 和 Sensor B 在无遮挡、被箱子遮挡时的 BOOL 值。官方入门资料中这个场景的两个传感器采用常闭信号：无遮挡为 TRUE，检测到箱子为 FALSE；本机工作副本仍要实测。[入门资料](https://docs.factoryio.com/controlio/getting-started.html)

为 PLC 中的变量区分“原始电平”与“检测到物体”的逻辑含义。不要直接把原始 TRUE 当成有箱子。记录场景里既有的强制值；官方入门案例的入口输送带处于强制开启状态，验证主输送带控制时要区分两者。

## 3. 建立 I/O 表

以下是拟定分配，不是已确认的实际映射。地址以各 Modbus 数据区内的零起始偏移记录；不要把 Coil 0 与 Discrete Input 0 当成同一个地址。

| Factory I/O 标签 | PLC 变量（建议） | 类型 | Modbus 区与偏移（拟定） | 数据流 |
| --- | --- | --- | --- | --- |
| Sensor A | `FIO.iSensorARaw` | BOOL | Coil 0 | 场景写入，PLC读取 |
| Sensor B | `FIO.iSensorBRaw` | BOOL | Coil 1 | 场景写入，PLC读取 |
| Conveyor | `FIO.oConveyor` | BOOL | Discrete Input 0 | PLC更新，场景读取 |

这里的 Coil 存放场景传感器值，Discrete Input 存放 PLC 的执行器命令。数据区名称与工艺信号的输入/输出方向要分别记录。Factory I/O Client 将数字传感器写入 Coils，执行器读取区可配置为 Inputs 或 Coils。[驱动配置依据](https://docs.factoryio.com/manual/drivers/modbus-client/)

在 CODESYS 添加 Ethernet 和 Modbus TCP Server/Slave 设备，开启独立位区，按拟定分配准备 2 个 Coils 与 1 个 Discrete Input，并把通道映射到 GVL 变量。按官方教程建立 Symbol Configuration。检查设备 I/O 更新配置与变量刷新情况。

在 Factory I/O 驱动配置中选择本机目标地址，端口暂定 502；数字输入数量暂定 2，数字输出数量暂定 1，偏移均为 0，Read Digital 选择 Inputs，再逐个映射场景标签。Unit ID 初始可按官方教程使用 0 或 255；本机 SP22 Patch 3 的行为以实际连接结果为准。

## 4. 编写与编译 PLC 逻辑

先实现能够解释和观测的最小逻辑：

- 根据实测极性得到 `atA`、`atB`。
- `atB` 为 TRUE 时清除输送带运行状态。
- `atA` 为 TRUE 且 `atB` 为 FALSE 时置位运行状态。
- A 与 B 之间保持运行状态，输出到 `FIO.oConveyor`。
- 为人工验证提供运行许可和复位变量；停机、复位优先于启动，复位时清除内部保持状态。

初次输出应为 FALSE。人工控制变量可先通过 CODESYS 在线监视操作；它们不属于上面三个场景 I/O 通道。首次验证以单件运输为范围。

编译工程，保存诊断并确认 0 个编译错误。编译通过后再进行运行验证。当前没有提供已验证的 ST 源文件。

## 5. 启动并验证通信

1. 启动 CODESYS Control Win SoftPLC，在 IDE 中选择对应网关与目标设备。
2. 确认目标是本机仿真运行时，并记录已有应用状态；如果已有应用，先保存其工程/归档，避免丢失现有工作。
3. 登录并下载基线工程，启动 PLC 应用；同时检查服务运行状态和配置的 Modbus 端口。
4. 在 Factory I/O 连接驱动，保持场景暂停，观测连接状态。
5. 改变两个传感器的物理遮挡状态，确认 PLC 原始变量逐项对应变化。
6. 在受控条件下检查主输送带命令能反馈到场景，检查是否有强制值覆盖，然后恢复基线初态。

传感器变化没有传到 PLC 时，先查数据区、地址、I/O 映射与刷新配置。PLC 输出变化而场景不动时，先查读取区、标签映射、强制值和场景运行状态。

## 6. 验证工艺行为并保存证据

每轮从相同初始状态开始：暂停场景、复位 PLC 内部运行状态和场景物料状态、确认输出为 FALSE，然后允许程序运行。场景复位与 PLC 内部状态复位都要执行。

| 检查项 | 通过条件 | 证据 |
| --- | --- | --- |
| 工程有效 | 0 个编译错误，应用进入 RUN | 编译诊断、在线状态 |
| 输入正确 | A/B 物理遮挡与 PLC 原始值对应，极性已确认 | I/O 表、变量记录 |
| 到 A 启动 | A 检测事件后，PLC 命令为 TRUE，主输送带运转 | 标签轨迹、场景观测 |
| 到 B 停止 | B 检测事件后，PLC 命令为 FALSE，箱子停在目标区 | 标签轨迹、场景观测 |
| 停止与复位 | 运行许可撤销或复位后输出关闭，内部状态清除 | 在线变量记录 |
| 重复性 | 完整复位后至少三轮独立通过 | 每轮结果与证据路径 |

第一轮测量实际响应时间和正常运输时长，据此确定后续自动验收的时间容差与超时阈值。分别记录 PLC 输出变化和物理停止时刻，避免混淆通信响应与物理惯性。

可从 Factory I/O Console 导出标签和映射，并用定期标签导出采集轨迹；命令入口包括 `scene.export_tags_csv`、`drivers.export_mapping`、`scene.start_tag_export`、`scene.stop_tag_export`。先核对本机导出字段和时间信息，再确定解析格式。[Console 文档](https://docs.factoryio.com/manual/console/)

每次运行保存：场景副本、工程与 ST 源码、实际 I/O 表、版本配置、编译结果、初始条件、标签轨迹、截图/录像（如有）、验收结果和失败原因。结果使用“通过 / 失败 / 未执行”，没有证据的步骤保持“未执行”。

## 7. 手动通过后的工具化验证

| 人工步骤 | 候选工具入口 | 首次需要验证的内容 |
| --- | --- | --- |
| 加载场景、导出标签和映射 | Factory I/O Console；启动配置 | 指定副本是否加载成功，导出内容是否一致 |
| 读状态、管理仿真控制标签 | Factory I/O Web API 或 SDK | 本机版本与授权、标签身份和实际效果 |
| 修改工程并编译 | CODESYS Scripting | 脚本能否执行、能否捕获真实编译诊断 |
| 登录、下载并启动应用 | CODESYS 在线脚本 API | 本机目标识别、下载结果与运行状态 |
| 验收与复位 | 轨迹解析、规则验证、执行工具 | 能识别失败，并能恢复下一轮所需初态 |

[Web API](https://docs.factoryio.com/manual/web-api/) 与 [SDK](https://docs.factoryio.com/manual/sdk/) 提供 I/O 访问，要求 Ultimate Edition。先用它们读取状态；常规验证中执行器命令仍由 PLC 输出，避免两个写入方竞争。故障注入需要明确安排，并在测试结束恢复。

[CODESYS 脚本入口](https://content.helpme-codesys.com/en/CODESYS%20Scripting/_cds_starting_script_via_command_line.html)、[编译 API](https://content.helpme-codesys.com/en/ScriptingEngine/ScriptApplication.html)、[在线 API](https://content.helpme-codesys.com/en/ScriptingEngine/ScriptOnline.html) 均需在当前 profile 上完成最小验证。任意三维场景自动搭建仍属于待研究能力。
