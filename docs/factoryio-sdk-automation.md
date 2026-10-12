# Factory I/O SDK、Web API 与 Console 自动化核查

核查日期：2026-10-09。已阅读官网、下载官方 SDK 示例，并通过 .NET 反射检查公开 DLL 接口。这里记录的是接口与示例核查；没有启动 SDK 控制器，也没有把启动脚本执行视为已经完成的场景加载或截图。

## 官方 SDK 实现

[官网 SDK 页面](https://docs.factoryio.com/manual/sdk/) 指向 [realgamessoftware/factoryio-sdk](https://github.com/realgamessoftware/factoryio-sdk)。SDK 要求 Ultimate Edition。核心 `Engine IO.dll` 为 .NET Standard 2.0 程序集，通过内存映射文件访问输入、输出和通用内存；`MemoryMap.Instance.Update()` 同步缓存与共享内存，事件通知也依赖调用 Update。它可与已有驱动同时访问 I/O；只有完全用 SDK 接管 I/O 时，官方才建议把驱动选为 None。[官方说明](https://github.com/realgamessoftware/factoryio-sdk#engine-io)

本次下载固定到提交 `067ef528583fb96d419085b83c5d82ff6527cd7b`（GitHub 返回的提交时间为 2021-05-13）。保留官方许可证与原文件，目录为：

`artifacts/sdk-research/official-sdk/factoryio-sdk-067ef528583fb96d419085b83c5d82ff6527cd7b/`

| 官方文件 | 用途 |
| --- | --- |
| `samples/Read Sample/Program.cs` | 获取输入并读取最新值 |
| `samples/Write Sample/Program.cs` | 修改输出并同步至仿真 |
| `samples/Event Sample/Program.cs` | 轮询 Update，接收输入/输出名称和值变化事件 |
| `samples/Controllers/Scenes/SortingWeight.cs` | 重量分拣 C# 状态机参考 |
| `RESERVED ADDRESSES.md` | 运行、暂停、复位和相机位置的保留地址 |

反射结果保存于 `artifacts/sdk-research/engineio-public-api.json`。该 DLL 的公开类型集中在 `MemoryMap`、各种类型的 `Memory` 和变化事件；在这些公开方法、属性和事件中没有找到场景加载、截图、Console 执行或对象几何编辑入口。这个结论限定于本次检查的公开 SDK 版本，不能扩展为软件内部绝无相关能力。

## 对用户所列命令的对应方式

| 需求 | 官方可用入口 | 对项目的作用 |
| --- | --- | --- |
| 枚举当前场景标签 | Web API `GET /api/tags` | 导出标签并核对 UUID、类型、方向、强制状态 |
| 周期记录标签 | 外部程序轮询 Web API 或 SDK Update/事件 | 可自行按约 0.1 秒采样，并自行停止；实际间隔必须记录，不能当作硬实时 |
| 切换已保存相机位置 | 系统输出标签 Camera Position；SDK Int Output 240 | 外部调用无需输入 Console 命令，但场景必须已保存对应视角 |
| 运行、暂停、复位 | 系统输出标签；SDK Bit Output 496、497、498 | 工具操作后还要读取系统输入确认状态 |
| 加载场景、截图、原生 CSV 导出 | Console；支持通过 `auto.cfg` 在启动时执行 | 可免除逐条输入；启动顺序、文件落盘和截图内容需要本机验证 |
| 运行中远程执行任意 Console 命令 | 本次公开 SDK/API 文档与 DLL 中未找到 | 不能把 Console 方法直接包装成未经验证的 HTTP 调用 |
| 增删对象、修改摆放并保存场景 | 本次公开 SDK/API 中未找到 | 继续使用项目 XML 写入器，并通过软件加载和当前画面验收 |

依据：[Web API](https://docs.factoryio.com/manual/web-api/)、[系统标签](https://docs.factoryio.com/manual/tags/#simulation-tags)、[SDK 保留地址](https://github.com/realgamessoftware/factoryio-sdk/blob/master/RESERVED%20ADDRESSES.md)、[Console](https://docs.factoryio.com/manual/console/)。SDK 地址与数据类型一起使用，不能把这里的地址直接当作 Modbus 通道号或 XML `PointIOKey`。

## `auto.cfg` 可以避免手工输入，但只解决启动阶段

官方明确支持将 Console 命令放入 `auto.cfg`，位置为安装目录、`ProgramData\Real Games\Factory IO` 或 `Documents\Factory IO`。安装目录/ProgramData 中的文件先执行，Documents 中的文件后执行。[官方启动规则](https://docs.factoryio.com/manual/console/)

项目已准备 `examples/factoryio/auto.cfg.example`。它是待验证的启动候选，没有安装到软件配置目录，也没有重启当前软件。要启用，需要在保留既有配置的前提下合并为 `auto.cfg`，并从正常许可的安装启动 Factory I/O；修改文件本身不会给已经运行的进程发送命令。截图只在该命令执行时产生一次，`start_tag_export` 记录的是标签而不是连续画面。

后续软件内部分析确认 `Scene.Load` 延迟到下一帧加载，因此已经移除样例中紧随加载的相机、导出和截图命令。当前样例只启用 Web API 并请求加载场景。必须等加载完成后再截图，核对当前标签、文件的新鲜度、画面内容和本次场景版本。出现文件也不能直接签发 `load-receipt.json`。内部方法和待实测桥接见 [软件内部调用分析](factoryio-internal-automation.md)。

官网 `scene.load_from_path` 表格旁的示例拼成了 `scene.load_from.path`；候选文件使用表格定义的 `scene.load_from_path`。本机安装附带 Console 文档也包含这些命令。官方配置目录写作 `Factory IO`，截图说明写作 `FactoryIO/Screenshots`，输出目录应以本机实际新文件为准。

## 对现有 Agent 工具的改进

保留 CODESYS ↔ Factory I/O 的 Modbus 控制链。项目 `FactoryIOClient` 已实现标签读取和 run/pause/reset/camera 系统输出写入；`collect_evidence` 已能配置采样间隔并保存原始时间戳。Agent3 的标签观测先复用 Web API，无需为 Python 编排器额外引入 .NET，也无需启动官方 C# 控制器接管执行器。

SDK 的事件和共享内存适合以后增加本机观测后端。SDK 公开 Memory 接口以地址、类型、方向和名称标识数据，未提供 Web API 的标签 UUID 字段；接入时需要与当前场景契约核对，名称重复时不能只按名称绑定。

官方重量示例可用于核对状态机思路与传感器极性，并给 `weight.Value * 2f` 提供代码参考；它不是本机 kg 标定证据。其整型分拣条件没有覆盖恰好 8、10 的情况，不宜直接原样作为项目验收逻辑。[官方重量代码](https://github.com/realgamessoftware/factoryio-sdk/blob/master/samples/Controllers/Scenes/SortingWeight.cs)

完整重启后的启动配置执行仍需单独验收。后续已经补齐运行中的进程内桥接，实测按需加载与截图；本机 `load_command` / `capture_command` 已配置并验证，详见下方补充。这条路径使用本机内部程序集，不属于官方公开 SDK 的稳定接口。

## 本轮证据范围

- 已完成：官方资料核查；固定提交的 SDK 文件下载；DLL 公开接口反射；本机附带 Console 文档交叉检查。
- 未完成：官方 C# 项目编译、官方 SDK 控制器与运行中的 Factory I/O 数据交换、完整重启后的启动配置执行。相机切换与自动加载/截图已通过另外的进程内桥接验证。
- 本机 `dotnet --info` 显示已有运行时但没有 .NET SDK；官方 C# 样例目标为 net5.0。尚未安装编译环境或改变其项目文件。
- `artifacts/sdk-research/research.json` 保存本次来源、哈希与验证标记。

## 2026-10-09 进程内桥接的实际结果

用户在已有场景执行一次 Console 引导，此后外部 Python 通过文件请求队列，调用 Unity 主线程中的 Scene/CameraManager/EditContext。实际软件为 2.5.8，加载生成场景后 56 个对象、44 个标签一致，97 个 prefab 名称可枚举，相机切换和新 PNG/SHA 均通过。加载和截图工作流 hook 也已通过；不再依赖桌面窗口截图。

连续加载发现原生未保存检查要求 Screenshot 元数据节点存在；生成器已改为保留空节点，并验证再次加载成功。当前驱动 None，PLC 通信与实际分流未验收；自动启动引导配置已写入，但未重启软件验证。证据：`artifacts/bridge-live-validation-003`；实现与版本限制见 [内部调用分析](factoryio-internal-automation.md)。
