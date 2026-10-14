# Issue #4：布局检查、视觉反馈与修复的运行轨迹

本目录保留 2026-10-14 的 13 轮实际命令行运行，包括失败与格式校正；没有用助手手写场景替代框架结果。

**当前边界：三类静态布局与一次供料器偏移修复有成功记录；最新严格冻结范围重测失败，Qwen 两次原始回复均为 `[]`。没有完成 PLC/动态工艺验收。A/B/C 的准确率在独立标注前保持 null。**

## 快速查看

- [最近两次 Qwen 完整交互（含原图）](trajectories/issue4-feed-fault-20261014-003/interaction-review/qwen-interaction-review.md)
- [成功修复的 Ticket](trajectories/issue4-feed-fault-20261014-002/scene-000/repair-ticket.json)、[Patch](trajectories/issue4-feed-fault-20261014-002/scene-000/local-patch.json)、[修复后验收](trajectories/issue4-feed-fault-20261014-002/scene-001/verification.json)
- [同图 A/B/C 对照](trajectories/issue4-visual-comparison-20261014-001/comparison.json)
- [汇总](validation/issue4-validation-summary.json)、[Python 回归](validation/issue4-all-tests-final.log)、[网页回归](validation/issue4-dashboard-tests-final.log)
- [文件与脱敏清单](publication-manifest.json)、[实现说明](../../docs/scene-layout-issue4.md)

## 全部运行

| 轨迹 | 状态 | 模型调用 | Tokens | 耗时（秒） |
| --- | --- | ---: | ---: | ---: |
| [issue4-line-layout-20261014-001](trajectories/issue4-line-layout-20261014-001/result.json) | needs_attention | 2 | 11666 | 39.917 |
| [issue4-line-layout-20261014-002](trajectories/issue4-line-layout-20261014-002/result.json) | needs_attention | 2 | 6226 | 12.371 |
| [issue4-line-layout-20261014-003](trajectories/issue4-line-layout-20261014-003/result.json) | needs_attention | 4 | 22444 | 24.778 |
| [issue4-line-layout-20261014-004](trajectories/issue4-line-layout-20261014-004/result.json) | scene_verified | 3 | 27910 | 26.761 |
| [issue4-feed-fault-20261014-001](trajectories/issue4-feed-fault-20261014-001/result.json) | needs_attention | 1 | 11650 | 11.674 |
| [issue4-feed-fault-20261014-002](trajectories/issue4-feed-fault-20261014-002/result.json) | scene_verified | 4 | 45197 | 45.381 |
| [issue4-independent-stations-20261014-001](trajectories/issue4-independent-stations-20261014-001/result.json) | needs_attention | 1 | 2913 | 8.425 |
| [issue4-visual-comparison-20261014-001](trajectories/issue4-visual-comparison-20261014-001/result.json) | visual_comparison_recorded | 3 | 27289 | 33.724 |
| [issue4-independent-stations-20261014-002](trajectories/issue4-independent-stations-20261014-002/result.json) | needs_attention | 2 | 6423 | 12.725 |
| [issue4-independent-stations-20261014-003](trajectories/issue4-independent-stations-20261014-003/result.json) | needs_attention | 3 | 19448 | 15.337 |
| [issue4-independent-stations-20261014-004](trajectories/issue4-independent-stations-20261014-004/result.json) | scene_verified | 2 | 20177 | 17.621 |
| [issue4-column-layout-20261014-001](trajectories/issue4-column-layout-20261014-001/result.json) | scene_verified | 5 | 25807 | 43.74 |
| [issue4-feed-fault-20261014-003](trajectories/issue4-feed-fault-20261014-003/result.json) | needs_attention | 2 | 23298 | 15.849 |

包含全部失败轮次，共 34 次模型调用、250,448 tokens。场景类真实视觉图像共 21 张，均保存原字节，不做缩放或重新编码。

## 如何读轨迹

- `training-data/candidates/*.json`：最终 system/user、原始 response_text、图片顺序、模型/响应格式/Token、错误。`structured_output: null` 不代表原始回复是 null。
- `trace.jsonl` / `model-events.jsonl` / `progress.log`：阶段、公开输出、请求/工具事件、拒绝与退出。
- 每个 `scene-NNN`：需求、设计、布局、检查计划、原生测量/加载回执、视觉检查、Ticket/Patch、重新验收及原始 `.factoryio`。
- `execution.json`：该轮实际执行源码的哈希。失败中的 partial resume 保留 requirement/spec 的复用与拒绝证据。

## 图片和脱敏

相同 PNG 只保存于 assets/ 一份；候选记录中的路径以该运行的 training-data/ 为基准解析。记录里其他文件引用以仓库根目录为基准。完整交互 Markdown 的图片/文件链接已改为相对路径。

配置快照不上传；实际凭据不上传；私有服务主机和机器路径已改写。原始本机文件未改动，publication-manifest.json 同时记录原文件和发布文件哈希。图像 SHA 保持不变，便于核对。

本目录不是可直接重启 PLC 或 Factory I/O 的配置包。复现实机运行需本机安装、桥接和模型配置；历史截图只证明对应采集时刻。所有模型候选保留待审核训练状态，对照数据不自动作为 gold。

发布目录复验：[523 项 Python 测试（参考场景未发布，1 项跳过）](validation/issue4-publication-tests.log)、[12 项网页测试](validation/issue4-publication-dashboard-tests.log)。
