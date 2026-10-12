# Factory I/O 与 CODESYS 两端生命周期验证

2026-10-11，使用 SC001 的同一份 agent 生成场景与 PLC 程序完成验证。
部署来源：`runs/observer-sc001-20261011-035/scene-000/plc-000/`。
PLC 内容 SHA256：`db5ecd7d7cad92d39309770059f680ad13d1857aa56a4d0d26c2344cd7207b56`。

## 接口和操作语义

官方 ScriptOnlineApplication 提供 `stop()`、`start()`、
`reset(ResetOption.Warm, False)` 和 `reset(ResetOption.Cold, False)`。
实现只操作当前应用，不提供 Origin/设备恢复出厂操作。

热复位初始化普通变量，保留 RETAIN/PERSISTENT；冷复位另外初始化 RETAIN，仍保留
PERSISTENT。SC001 的流程状态为普通 VAR，热复位足够。
[官方复位说明](https://content.helpme-codesys.com/en/CODESYS%20Development%20System/_cds_reset_application.html)，
[官方 ScriptOnline API](https://content.helpme-codesys.com/en/ScriptingEngine/ScriptOnline.html)。

统一操作采用顺序屏障：Factory I/O 暂停 → PLC Stop 并复位 → Factory I/O 停止、复位、
确认动态物体清空 → PLC Start 并确认 Run → 显式连接 Modbus 并处理许可范围内的驱动提示
→ Factory I/O Run → 执行规划模型原有的启动按钮操作 → 重新测量过程。
两端不要求同一毫秒执行；每个步骤均有实际状态回执。

## 实测记录

| 记录 | 结果 |
| --- | --- |
| `artifacts/sc001-sync-warm-20261011-001` | PLC 已 Stop，再次 stop() 报错；改为根据实际状态处理重复操作 |
| `artifacts/sc001-sync-warm-20261011-002` | 热复位和 PLC Start 成功，Modbus 自动重连失败；加入显式重连 |
| `artifacts/sc001-sync-warm-20261011-003` | 热复位、两端恢复、30 秒过程测量成功，10/10 检查通过，保持运行 |
| `artifacts/sc001-sync-cold-20261011-001` | 冷复位、两端恢复、30 秒过程测量成功，10/10 检查通过，自动确认驱动提示；随后长时观察发生出口堆料停止 |
| `artifacts/sc001-sync-warm-20261011-004` | 堆料停止后，统一热复位清空物料并恢复运行，30 秒内 10/10 检查再次通过 |

热复位恢复窗口测到两次完整的输送/停留/出料循环，首个停留区间为约
2.035–2.141 秒，观测值约 2.089 秒。冷复位后的实时核对确认 Running=true、Paused=false、
Conveyor=true、启动灯亮、停止灯灭、Modbus socket_connected=true、has_error=false，
无阻塞弹窗。`final-running.png` 是实际软件截图，截图中已累计产生 8 个箱子。

控制入口没有调用生成模型、修改原场景/PLC 程序或重新下载代码。它验证的是已部署工程
的生命周期恢复，不是一次完整生成流水线的重新验收；不会自动把这些记录升为训练金标。
随后继续观察时，动态箱子累计达到 12 个，Exit Sensor=true、Stop Lamp=true、
Start Lamp=false、Conveyor=false，原生 Stop 按钮未按下且 Modbus 保持连接。
实际出料端箱子堆积，与 PLC 出口等待超时的控制路径吻合。原始记录保留在冷复位目录的
`later-exit-blocked-*` 和 `later-exit-blocked.png`。这限制了当前 SC001 的长期连续运行，
不能将短窗口恢复通过扩展为长期稳定通过；出料回收应交给后续 scene agent 处理。

## 框架入口

```powershell
.\.venv\Scripts\python.exe run_agents.py --control runs\observer-sc001-20261011-035 --control-action restart --reset-mode warm
```

将 action 改为 `reset` 可复位两端并保持停止，改为 `stop` 可停止两端。
`--reset-mode cold` 显式选用冷复位。完整 agent 运行的 `--keep-running` 已接入同一套操作，
项目配置默认 `synchronized_lifecycle=true`、`sync_reset_mode=warm`。

单元测试：`.venv/Scripts/python.exe -m unittest discover -s tests -q`，327 项通过。
包括重复 Start/Stop、热/冷枚举、禁止 Origin、PLC 实际状态确认、停止/复位/启动顺序，
以及正常工作流采用两端停止与持续运行交付的调用检查。
