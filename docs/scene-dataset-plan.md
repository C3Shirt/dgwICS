# Factory I/O 场景数据集：来源调查与 100 场景构建规划

调查日期：2026-10-10。目标是为场景构建 Agent 和 PLC Agent 的 LoRA-SFT 建立数据基础。

规划材料已落地到 [scenario_projects/README.md](../scenario_projects/README.md)：100个项目子目录，每项包含需求、工艺拓扑、对象/模式、布局约束、IO语义草案、PLC思路、验收与修复计划、来源及材料位置。按10工艺族、36拟定模板组和L1/L2/L3/L4的20/30/30/20分配；[curriculum.md](../scenario_projects/curriculum.md)列出首批20项试点。当前均为planned，未生成或运行这些工程，未通过训练资格。

来源范围包括真实工业流程、设备厂商案例、PLC/机电实训任务、竞赛任务、其他仿真平台案例，以及 Factory I/O 教程与工程。先收集工艺需求，再评估其能否映射到 Factory I/O；无需来源本身提供 `.factoryio` 文件。工艺覆盖配额与来源平台分别管理。

本文件是资源调查与构建规划。网上工程的存在、作者的运行描述、在本机加载成功、CODESYS 编译成功和工艺验收成功分别记录。100 是目标配额，不是已经收集或验证的数量；本轮没有下载场景文件、观看完整视频、生成场景或执行 PLC。

## 1. 数据目标与计数口径

目标包含三层：约 30–40 种基础工艺/拓扑模板、100 个具有有效结构差异的可运行场景实例，以及这些实例产生的按角色划分的 SFT 样本。基础模板数量是初步目标，需根据来源调查和去重调整。

- 场景实例：有明确工艺目标、对象/配置、物流或检测关系、I/O 契约、PLC 与验收条件。
- 结构差异：分支/工位/缓存数量、物流连接、执行机构、传感器配置、部件工作模式等发生有效变化。
- 任务变体：同一场景改订单、调度策略、配方、阈值，通常只增加控制任务，不增加独立场景数。
- 表述变体：需求改写、标签改名、相机切换、全局平移或旋转，增加覆盖或观测，不增加独立场景数。
- 故障实例：同一正常场景注入故障形成诊断/修复样本，不增加正常场景数。

官方 21 个场景与已有自建场景先登记为种子。官方编号可能共享机械布局，自建场景也可能与官方场景近似，因此不能直接用 100 减 22 得出剩余独立场景数量。

## 2. 已调查的来源

### 官方与教学视频

| 来源 | 可提供什么 | 当前证据与下一步 |
| --- | --- | --- |
| [官方 Opening a Scene](https://docs.factoryio.com/getting-started/opening-a-scene/) / [Scenes](https://docs.factoryio.com/manual/scenes/) | 官方 21 场景，包含输送、分拣、加工、装配、码垛、升降、仓储、液位控制 | 官方文档确认数量与用途；本机实际目录后续清点，不把同布局编号直接计为独立拓扑 |
| [官方 Creating a Scene](https://docs.factoryio.com/getting-started/creating-a-scene/) | 输送带、传感器与转台的摆放和组织 | 可作重建与布局约束的参考 |
| [工控圈：西门子博途与 FactoryIO 联合仿真教程](https://www.bilibili.com/video/BV12B9EYMEKt/) | 摆轮拐弯输送、三路汇流、称重分拣及改造、堆垛等 | 搜索索引显示 101 集章节；网页直接读取返回 412，仅确认索引中的章节，不声称已观看或确认附件。优先选 p=14、18–26、27–47 与堆垛相关章节 |
| [Instrumentation Tools 视频课程目录](https://instrumentationtools.com/factory-io-plc-automation/) | 输送调速、按高度/重量/颜色分拣、液位/流量、升降、缓存、码垛、抓放 | 已读取作者课程目录与视频链接；课程集数不作为独立场景数 |
| [高度分拣场景设计视频](https://www.youtube.com/watch?v=8jQbBHKuCrM) | 通过传感器与输送带设计高度分拣 | 标题和作者描述确认设计主题，完整视频与附件待核对 |
| [我要自学网 FactoryIO 教程](https://www.51zxw.net/List.aspx?cid=1015) | 多段输送、正反转、双通道、装配、升降、仓库上下料 | 已读取课程目录；素材页与访问要求尚未核对，不声称存在可直接下载的完整场景集 |
| [Code and Compile Tasks and Solutions](https://wiki.codeandcompile.com/factory-automation/digital-twin/factory-io/tasks-and-solutions) | 预置场景控制方案、定制输送案例、PID 示例 | 作者页提供解决方案与输送案例附件入口；附件内容尚未检查，主要用于控制逻辑与任务说明参考 |
| [ACC Automation 码垛教程](https://accautomation.ca/plc-programming-example-palletizer-drum-instructions/) | 码垛操作、Modbus TCP、手动/自动测试、传感器异常案例 | 页面包含场景/程序下载入口及视频；附件可用性、是否官方场景改版、软件兼容性待核对 |

### 已核对文件树的公开仓库

通过 GitHub 仓库元数据与默认分支递归树 API 核对，四个树结果均未截断。

| 仓库 | `.factoryio` 路径数 | 内容价值 | 许可元数据与限制 |
| --- | ---: | --- | --- |
| [Faaizz/tuk_project](https://github.com/Faaizz/tuk_project) | 18 | 模块化生产、分拣、缓存、装配、堆叠/仓储，并有 ST、工艺要求与验证材料 | GitHub license 元数据为空；Old 路径与完整/局部工程较多，必须按谱系与结构去重。ST 存在不代表可直接在 CODESYS 编译 |
| [Ahmedhh1218/TIA-FACTORY-IO-Production-Line-Automation](https://github.com/Ahmedhh1218/TIA-FACTORY-IO-Production-Line-Automation) | 1 | 加工—装配—分拣的组合生产线与 TIA 工程 | GitHub license 元数据为 MIT；原控制程序需要按工艺语义适配 CODESYS |
| [Far2Deluxe/Smart-Factory-Simulation-FactoryIO-PLC-](https://github.com/Far2Deluxe/Smart-Factory-Simulation-FactoryIO-PLC-) | 1 | 分拣—装配—矩阵仓储与 TIA 控制工程 | GitHub license 元数据为空；运行与兼容性待验收 |
| [rheradio/FactoryIO](https://github.com/rheradio/FactoryIO) | 2 | 输送与液位控制教学案例 | GitHub license 元数据为空；可能与官方布局重叠 |
| 合计 | 22 | 22 条候选文件路径 | 不是 22 个独立且已验收的场景 |

路径记录：

```text
Faaizz/tuk_project (master)
  src/virtual_factories/AL1.factoryio
  src/virtual_factories/Old/Assembly.factoryio
  src/virtual_factories/Old/Section.factoryio
  src/virtual_factories/Old/Storage.factoryio
  src/virtual_factories/Old/buffer.factoryio
  src/virtual_factories/Old/complete_production_sorting_buffer_assembly.factoryio
  src/virtual_factories/Old/demo_factory.factoryio
  src/virtual_factories/Old/production.factoryio
  src/virtual_factories/Old/production_sorting_buffer_assembly.factoryio
  src/virtual_factories/Old/sec_A.factoryio
  src/virtual_factories/Old/sorting.factoryio
  src/virtual_factories/Old/sorting_buffer.factoryio
  src/virtual_factories/Old/sorting_buffer_assembly.factoryio
  src/virtual_factories/Old/stacking.factoryio
  src/virtual_factories/Stacking_Storage.factoryio
  src/virtual_factories/complete_production_sorting_buffer_assembly.factoryio
  src/virtual_factories/production.factoryio
  src/virtual_factories/sorting_buffer_assembly.factoryio

Ahmedhh1218/TIA-FACTORY-IO-Production-Line-Automation (main)
  Production-Line-Scene.factoryio

Far2Deluxe/Smart-Factory-Simulation-FactoryIO-PLC- (main)
  Factory Io Scenes/SmartFactory_v1.6.factoryio

rheradio/FactoryIO (main)
  FactoryIO_resources/FactoryIO/ConveyorBelt.factoryio
  FactoryIO_resources/FactoryIO/LevelControl_PID.factoryio
```

这些是调查当日默认分支的路径快照。正式获取时记录 commit、原始文件 SHA、作者、来源链接和许可/访问情况；许可未确定的文件保留为参考候选，进入训练或发布前明确可用范围。教程用于提取工艺和自行重建，原视频、字幕和源码的复用分别记录。

## 3. 100 个场景的覆盖配额

以下是我们的设计配额，非网上已找到数量。官方、社区与自建场景共同填充同一张覆盖表，去重后补齐空缺。以主要工艺归类，组合场景额外记录多个标签。

| 工艺家族 | 配额 | 有效变化与候选实例 |
| --- | ---: | --- |
| 基础输送与供料 | 12 | 单段/多段、直线/转弯/坡道、独立供料段、不同输送设备与检测配置 |
| 转台、移载与分流 | 10 | 转台/链条移载/推杆、两路/三路、串联分流、不同出口组织 |
| 缓存与汇流 | 12 | 单/多区缓存、双/三路汇合、入口缓冲、合流后检测、不同物料交接结构 |
| 检测与分拣 | 14 | 高度、重量、颜色、材质、二级联合检测、合格/不合格通道；以可用部件和物料属性为准 |
| 抓放与上下料 | 10 | 两轴/三轴、数字/模拟模式、单/双供料、输送带间交接、托盘装载 |
| 加工单元 | 8 | 单机/双机、盖/底座生产、前后缓冲、上下料交接、并行加工后汇流 |
| 装配单元 | 8 | 双路盖/底座供料、定位后装配、加工接装配、装配后检测或分流 |
| 码垛与托盘物流 | 8 | 高位码垛器/XYZ 抓放码垛、预缓存、空满托盘交接、码垛后输送/入库 |
| 升降与立体仓储 | 10 | 两层/多层物流、单/双升降、堆垛机和货架、单/双侧存取、前置分流 |
| 液位、流量与模拟量控制 | 8 | 数字/模拟阀、不同液位开关配置、液位/流量任务、双独立罐控制单元 |
| 总计 | 100 | 以实际加载和工艺验收通过后的结构实例计数 |

当前研究范围以 Factory I/O 已有部件与行为为边界。[官方 Stations](https://docs.factoryio.com/manual/parts/stations/) 提供加工、抓放、仓储、码垛、罐体接口。罐体案例不假定支持未验证的罐间液体输送、混料化学或温度物理模型；加工中心的外观重命名不等于新增真实加工能力。数字与模拟工作模式差异可形成不同接口配置，但共享基础模板和数据谱系。

## 4. 难度递进与建设批次

难度同时记录布局、控制和观测三个维度，不只按 Object 数量评定。对象数与 I/O 数是连续元数据，不用未经校准的阈值定义难度。

| 层级 | 配额 | 学习重点 | 代表场景 |
| --- | ---: | --- | --- |
| L1 基础 | 20 | 单一目标、顺序动作、数字量检测 | 输送到位、定量供料、两路分拣、定时注排水 |
| L2 单元协同 | 30 | 多工位握手、模拟量或有限缓存 | 三路汇流、称重分流、抓放交接、升降上下料 |
| L3 多单元生产 | 30 | 并行支路、物料跟踪、共享资源、阻塞传播 | 双加工供料、分拣+缓存+装配、码垛+入库 |
| L4 综合与恢复 | 20 | 组合工艺、受控并发、订单和异常恢复 | 加工—装配—分拣—码垛—仓储、具有缓冲/旁路的可恢复生产线 |

家族配额与难度配额是交叉维度，不相加；具体场景登记时共同满足。恢复故障试验依附正常场景，不另凑 100 场景。

建设里程碑按累计“已验收”数量划分为 20 → 40 → 70 → 100。首批优先扩展类型覆盖，不先生成大量复杂组合：

1. 单段输送到位。
2. 多段输送交接。
3. 转弯/转台移载。
4. 两路高度分拣。
5. 现有称重案例的完整验收。
6. 颜色分流。
7. 双路汇合。
8. 三路汇合与入口缓存。
9. 分区缓存与分批放行。
10. 两轴抓放交接。
11. XYZ 托盘放置。
12. 单加工中心上下料。
13. 双加工中心分别生产盖与底座。
14. 双路供料装配。
15. 码垛与满托盘输出。
16. 两层升降输送。
17. 堆垛机入库/出库。
18. 数字液位开关控制。
19. 模拟液位/流量控制。
20. 分拣—缓存—装配组合线。

以上也是候选建设清单，实际重复模板应合并；不宣称已完成这 20 项。复杂场景先按单物料/串行模式验收，再逐步引入并发和队列。并发案例需要新的物料身份、时序和资源契约，不能直接沿用单物料验收。

## 5. 从资源到训练样本的流程

```text
资源链接与章节登记
  → 按工艺与文件谱系去重
  → 提取物料流程、对象、关系、I/O、状态机与验收规则
  → 获取现成文件或在本机重建
  → 场景结构与真实加载检查
  → 按语义适配 CODESYS ST 与 Modbus 映射
  → 运行、回读、逐件/过程验收和多视角检查
  → 保留成功方案与有证据的修复过程
  → 按角色导出 SFT 样本
```

视频提取先看场景总览、标签配置、状态转移和实际运行片段，再补充建模步骤。保留 URL、章节/时间点及“明确展示/作者描述/我们推断”的标注。视频看不到的高度、传感器极性、阈值或工艺条件不能当作已知真值，需通过官方部件语义和本机实验确定。

外部 TIA、Allen-Bradley 或 Control I/O 程序作为工艺与控制策略参考。重新确定接口方向、模拟量尺度、上升沿、扫描周期、块调用顺序和停止/复位语义，再在 CODESYS 编译运行；不把文本语法转换视为联机成功。

每个正式场景包至少包含：

- 需求、工艺步骤与可执行验收条件；有歧义时保留显式假设。
- 对象清单、工作模式、物流/安装/检测关系和布局方案。
- `.factoryio` 文件、软件版本、结构与文件 hash。
- 以稳定对象/I/O Key 关联的标签表、极性、尺度、故障/强制属性，以及 Modbus 映射。
- ST/GVL、CODESYS 工程或可导入源、构建与运行配置。
- 场景截图、关键时刻截图、完整 I/O/状态时序、验收结果。
- 来源、父模板、派生方式、构建工具/提示词版本及修复轨迹。

验收状态独立记录：reference_only、spec_ready、scene_loaded、plc_compiled、connected、functional_passed。可作为某个角色样本不代表可作为全流程成功样本；场景构建样本要求场景质量通过，PLC 样本要求对应编译与功能验证，端到端样本要求完整工艺通过。完整功能结果不足的样本不标记成功。

## 6. 多样性、修复数据与划分

先以原始 SHA 去除完全相同的文件，再忽略截图、保存时间、GUID 重编号、显示名称和整体刚体变换，比较组件模式与关系图。机械布局相同的不同控制策略保持相同 scene/template 谱系，作为不同 task 登记。

有效扩展围绕新增支路、替换分流机构、加入缓存、改变传感器类型/布置与工作模式、增加单元握手和组合工艺展开。改变尺寸与位置时必须满足接合面、运动范围和检测覆盖约束；随机坐标生成不作为扩展方法。

在通过验收的基础实例上分别构造检测高度偏移、方向错误、映射错位、信号极性错误、丢失状态转移或错误放料条件等故障。保存注入来源和受控复现实验；模型输入只包含当时可取得的观测，故障标签、正确修改和修复后观测作为独立监督信息，避免答案泄漏。

100 个场景不等于 100 条 SFT 样本。可按角色从规划、局部构建、PLC 实现和修复产生多个样本，但数量以通过角色验收的记录为准，不先承诺有效样本总数。相机视角、描述改写和修复样本不能代替工艺多样性。

划分先于改写、故障注入和派生扩展，按母模板/原工程谱系分组，避免同源版本跨训练、验证、测试。另保留未见模块组合测试；若模块单体已用于训练，明确称为组合泛化，而不是所有子模块均未见。

暂定约 70%/15%/15% 的场景级比例，按 group 分配并尽量分层；group 大小时不强求恰好 70/15/15。测试仍包含基础与中等任务，不能把困难程度和训练/测试划分完全重合。测试工程与参考答案不用于合成训练轨迹。

## 7. 下一批可交付物

1. 清点本机官方/已保存种子，建立 source/template/scene/task 四级索引。
2. 优先获取许可明确、资料完整的生产线工程，并审核 Faaizz 项目的文件谱系和工艺规格；保留未核实来源为参考。
3. 对中文汇流与称重教程提取可重建规格，对视频视角与未知配置逐项标注。
4. 选择 5 个不同家族做适配试点，测量重建、CODESYS 适配与自动验收成本，然后确定首批 20 个的具体工程安排。
5. 用可重复验收的场景补齐覆盖表，逐批达到 40、70、100；出现不可实现的机制时换为同家族的可实现案例并记录理由。

当前已有 Agent 的版本化交接与证据保存架构可复用。大规模教程提取、模板去重、通用场景生成、按角色导出 SFT 数据与批量验收仍需逐项实现；本规划不将这些能力标为已完成。

## 8. 来源扩展：把外部工业工艺映射为 Factory I/O 任务

本节根据用户的补充扩展收集范围。下列外部案例已进行网页、文档或检索索引核对；映射方案是我们的设计判断，尚未构建、联机或进行物理验收。来源工艺、我们保留的子流程和自行补充的条件分别登记。

### 可优先使用的外部来源

| 来源 | 来源实际提供的工艺 | 可提取的候选任务与证据状态 |
| --- | --- | --- |
| [Festo MPS 模块化生产系统手册](https://www.festo.com/net/supportportal/files/10142/mps.pdf) | 供料、检测、抓放、分拣、缓存、加工、存取；站点可组合并通过握手协调 | 已读取官方 PDF。适合从单站逐步组合成生产线；具体工作件、气动机构与加工机理需单独映射 |
| [Siemens SCE 020-100 分拣过程说明](https://www.automation.siemens.com/sce-static/learning-training-documents/tia-portal/example-processes/sce-020-100-process-description-sorting-station-r1904-de.pdf) | 金属/非金属识别，输送与气缸分流，以及手动/自动、初态、停止等控制要求 | 已读取官方德文 PDF。可先重建材料检测与推杆分流子流程，接口语义按 Factory I/O 重订；不直接复用原地址和尺度 |
| [Dematic Patagonia 案例](https://www.dematic.com/en-us/insights/case-studies/how-big-brands-deliver-roi-with-green-initiatives/) | 称重前控制包裹间距，称重后按发运方式分流 | 已读取厂商案例。适合“供料—分区缓存—间距控制—称重—分流”；订单类型在我们的任务中另行定义，不从重量自动推断运输方式 |
| [Dematic 模块化分拣方案](https://www.dematic.com/content/dam/dematic/downloads/brochures/NA_BR_1112_ModularSortSystemSolution.pdf) | 缓存、间距控制、扫描识别与多个分流出口 | 已核对官方 PDF 检索内容。可提取按目的地分流的物流拓扑；识别装置及物料种类按现有接口选择 |
| [Dematic 订单拣选与配套流程](https://www.dematic.com/en-us/insights/articles/conveyor-systems-for-order-picking-and-kitting/) | 主输送干线与多个区域支路，订单容器按需求进入各区并回到干线 | 已核对厂商说明。可重建区域路由、等待工位完成与再汇流；人工拣选动作及订单系统需抽象或外部逻辑 |
| [FlexLink 托盘输送系统](https://www.flexlink.com/en/portfolio/conveyor-systems/pallet-conveyors) | RFID 识别、工艺路线、缓存、定位与产品变型管理 | 已核对厂商检索内容，直接网页读取失败。候选是带 ID 的托盘/箱体路由，仍需检查物料与 RFID 支持及读取范围 |
| [FlexLink 多生产线末端自动化案例](https://www.flexlink.com/en/about-us/success-stories/integrated-end-of-line-automation-streamlines-palletizing-across-11-food-production-lines) | 多线供料、FIFO 缓冲、多个码垛单元与成品托盘物流 | 已核对厂商案例检索内容。缩小支路规模重建输送和码垛协调，原案例的缠膜机构不作为 Factory I/O 原生能力 |
| [Visual Components 码垛课程](https://academy.visualcomponents.com/lessons/palletizing-robot-programming/) / [应用案例](https://www.visualcomponents.com/case-studies/) | 码垛、制造与仓储应用，以及机器人信号协调 | 已读取官方课程/案例入口。提取物流、放置目标与握手协议；更换为 XYZ 抓放或高位码垛器时明确机构差异，不声称复现六轴运动学 |

进一步来源包括职业院校任务书、机电竞赛、公开毕业设计、工程作者视频和论文实验流程。优先寻找有工艺图、动作顺序、I/O、初态和验收要求的材料。营销视频只展示整体效果时先登记参考线索，不直接形成完整规格。

### 实现范围分类

- A：原生部件可覆盖目标子流程。比如输送、缓存、金属识别、称重分流、升降和托盘存取。A 是候选可实现性判断，实际成功仍需要加载和运行验收。
- B：保留物流与控制语义的抽象。比如用 XYZ 抓放替代六轴搬运，或将包装工位表示为到位、占用、完成的逻辑服务工位。仅验证抽象后的目标，不将等待计时或完成信号描述为真实包装/焊接过程。
- C：需要外部物理模型或当前不适合。比如化学反应、真实温度过程、柔性物料、精密接触装配、AGV 导航、焊接或喷涂轨迹。作为后续扩展，不纳入本阶段原生完整工艺通过的统计。

按 [Factory I/O Sensors](https://docs.factoryio.com/manual/parts/sensors/) 核对身份机制：RFID 适用于带 RFID 的物料；Raw Materials、Product Lids 和 Product Bases 不带该标签。这些物料可考虑 Vision Sensor 的 All ID 模式，但 ID 在 reset/load 后不持久。外部条码/配方需求必须明确采用原生 ID、PLC/外部订单表，还是自建观测接口，不把任意箱体直接当作已支持原生视觉条码读取。

### 增加到场景规格的字段

每个外部改编案例增加：`source_process_spec`、`target_process_spec`、`source_evidence`、`fidelity_class`、`preserved_invariants`、`adaptations`、`unsupported_physics`、`identity_strategy`、`acceptance_scope`。来源明确展示的行为、我们作出的假设和自建验收条件分开保存。

保留的不变量通常包括物流先后关系、检测与物料身份关联、出口判定、工位资源互斥、握手、满料反馈、停止/恢复条件。例如将原扫描器换成 RFID，不意味着原货物、协议与传感器时序不需重新定义。

这也形成独立的规划训练任务：外部工艺描述 → 支持范围与简化说明 → 组件/物流/I/O/验收契约。SFT 的监督输出由人工核对和工具验收支撑，不能仅靠同一模型自评“映射成立”。

### 五个优先改编试点

1. Siemens 金属/非金属分拣：供料—检测—定位—推杆—两出口，先解决物料种类和推杆反馈语义。
2. Dematic 称重前间距控制：分区缓存—间距调整—称重—分流，检查单件称重与身份关联。
3. Festo 模块化供料/检测/抓放：先分站验收，再加入工位握手与组合流程；不要求复制原工作件或机械外观。
4. FlexLink 托盘配方路由：选择支持 RFID 的物料，建立 ID—路线表，经过支路工位后回流；接口与轨迹先行验证。
5. Visual Components 码垛目标改编：在 Factory I/O 抓放范围内选择托盘与放置目标，验证放置顺序、空间接合与满托盘输出。

这些试点优先从单件或受控串行模式运行，后续再加入并发。主干—支路、回流等新布局需要扩展生成器与部件语义目录；当前知识文件只覆盖部分已验证部件，不能据此宣称已支持所有候选。

100 场景的工艺/难度配额保持，来源不按软件设限制。母模板或同一真实案例的派生版本共享谱系，继续按组划分训练/验证/测试。行业名称变化但物流、部件和控制完全相同的案例不增加独立结构计数。
