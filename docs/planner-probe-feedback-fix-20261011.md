# 规划阶段按钮测量时间重复失败：诊断与修复

2026-10-11 的 `runs/20261011-222758-ab15c9bc` 停在规划阶段的 `measurement_probes` 子任务。需求拆解、执行流程和观测信号三个子任务已经通过。规划模型两次返回相同的测试操作：按下按钮保持 0.8 秒，释放后保持 0.2 秒。

测量解释器要求每个动作状态保持 0.4～3 秒，按下和释放均适用。这是采集按钮状态与 PLC 输出的测量窗口，不是修改用户要求的物料停留时间。原校验反馈只有 `probe hold outside supported bounds`，没有指出动作、实际值或范围，模型重试没有修正，随后重复失败保护阻止第三次同样的请求。因此 `execution_limit` 是终止原因，具体错误是释放测量窗口不合格；本轮尚未生成场景或运行 PLC。

## 已改动

- 测量工具从同一组常量导出 `native_probe_constraints`，在测试操作子任务开始前提供范围，并明确适用于 press 和 release。
- 校验错误现在列出具体 probe/action、实际保持时间和允许范围，要求修正所有不合格动作。
- 重复失败保护继续生效，最终报错保留具体子任务和原始校验错误。
- 可读进展日志显示每个模型生成的测试动作，包括按钮、按下/释放和保持时间。

没有修改原候选或自动把 0.2 秒改成合格值。前三个子任务的输入与提示策略未改变，原有缓存仍可验证后复用。

## 验证与恢复

[规划恢复验证报告](../artifacts/planner-probe-fix-20261011-001/planner-verification/planning-recovery-result.json)记录真实服务结果：复用 `spec`、`execution`、`measurement_signals`，仅调用规划模型两次，生成测试操作和验收规则。模型生成的按下、释放窗口均为 0.5 秒，两部分通过合同检查，并组装出完整任务。

[实际模型日志](../artifacts/planner-probe-fix-20261011-001/planner-verification/progress.log)与训练候选均保留。探针使用 `source_kind=planning_recovery_probe`，不修改源运行，不操作 Factory I/O 或 PLC，不创建训练 gold，也不代表完整场景验收通过。

通用隔离验证工具 `tools/verify_planner_recovery.py` 接受任意规划阶段中断的来源目录，并通过相同的 PlannerAgent、录制、观测与调用预算执行规划恢复。它不是场景专用验收器。

在项目根目录继续完整流程：

```powershell
$taskResumeRun = Join-Path 'runs' ('resume-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
& .\.venv\Scripts\python.exe run_agents.py --resume runs\20261011-222758-ab15c9bc --out $taskResumeRun --keep-running
```

完整续跑仍需重新确认当前软件状态、场景加载、PLC 编译部署以及运行和视觉验收。
