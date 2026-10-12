# 模型服务通信与图片输入验证

验证日期：2026-10-10。配置来自仓库根目录`LLM_Config.yaml`，密钥只在本机内存中读取，不复制进报告。原文件已加入`.gitignore`。

## 模型分工与本轮范围

- DeepSeek `deepseek-flash`：拟用于需求规划、场景构建代码和PLC代码的基础推理服务。
- 阿里云 `qwen3.8-flash`：拟用于Agent3多模态观察、视觉缺陷定位与运行证据理解。

本轮验证远程API、JSON输出及图片理解。没有启动或改动Factory I/O/CODESYS，没有运行完整多Agent生成与验收链，也没有训练模型。当前`ModelClient`配置只提供单一base_url；双服务的角色路由和YAML配置接入仍需后续实现。通信通过不等于视觉诊断、工艺验收或LoRA训练能力通过。

## 实测结果

| 测试 | 模型 | HTTP | 校验 | 请求耗时 |
| --- | --- | --- | --- | --- |
| 文本与JSON | deepseek-flash | 200 | 随机token及算术结果完全匹配 | 0.796秒 |
| 文本与JSON | qwen3.8-flash | 200 | 随机token及算术结果完全匹配 | 2.079秒 |
| 受控图片理解 | qwen3.8-flash | 200 | 随机排列的三个颜色/形状逐项匹配独立答案 | 5.978秒 |
| Factory I/O历史截图 | qwen3.8-flash | 200 | 可见输送带、蓝色围栏、右侧物体面板校验通过 | 5.939秒 |

耗时为这一次请求实测，不是性能基准或服务稳定性保证。实际场景图片为已有截图，约5.45MB；未重新截图，不证明当前仿真已运行或工艺已通过。

## 修正的配置问题

初始Aliyun `Model_Name: Qwen3.8-Flash`请求返回HTTP 404，错误码`model_not_found`。改用[官方文档](https://help.aliyun.com/zh/model-studio/qwen3-8-flash)的调用ID`qwen3.8-flash`后，保持地址、密钥和请求格式相同即可成功。

已仅修正YAML中的这一模型名，其他配置字节保留。第一次失败记录保存在[model-connectivity-001/report.json](../artifacts/model-connectivity-001/report.json)；成功记录保存在[model-connectivity-002/report.json](../artifacts/model-connectivity-002/report.json)。成功记录保留测试时使用的显式模型ID覆盖参数，该ID现已写回原配置。

## 复现

标准库脚本：[probe_model_connectivity.py](../tools/probe_model_connectivity.py)。接受本项目使用的两级字符串映射YAML；不执行YAML标签，也不依赖第三方SDK。

```powershell
.venv\Scripts\python.exe tools/probe_model_connectivity.py `
  --config LLM_Config.yaml `
  --out artifacts/model-connectivity-next `
  --scene artifacts/bridge-live-validation-003/views/overview.png `
  --timeout 90
```

输出目录必须新建，避免覆盖旧证据。默认直连配置中的HTTPS端点，使用`/chat/completions`、`response_format=json_object`与Base64 `image_url`。密钥按各provider字段显式选择，不使用全局`OPENAI_API_KEY`覆盖它们；当前环境存在代理变量，本轮通过的是直连路径。需要时可显式指定`--use-proxy`另行验证。

报告包含状态码、返回模型、耗时、usage、结果与图片SHA；不包含Authorization、API Key或Base64图片正文。随机图片的答案和实测结果分别归档，避免把“接受image_url请求”直接当作“理解了图片”。
