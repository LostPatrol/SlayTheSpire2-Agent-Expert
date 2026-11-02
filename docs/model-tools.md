<!-- Read-only model tool architecture, extension points, and protocol boundaries. -->
# 模型工具

`src/sts2_agent/model_tools.py` 是只读工具注册入口。`Provider.configure_tools()` 注入推演器、同局公开历史、攻略库和请求预算检查；`Provider.decide()` 将调用绑定到当前公开观察。工具提供依据，最终动作仍由会话验证与执行。

## 工具与修改入口

| 工具 | 数据来源与用途 |
|---|---|
| `simulation_evaluate` | `CombatSimulator.evaluate`：按声明的候选动作前缀比较假想后继，使用现有 episode 推演额度 |
| `history_search` | 本局已发生的完整公开事件，倒序检索并分页 |
| `wiki_lookup` | 冻结 `KnowledgeBase.lookup_detailed`，含显式关联规则与奖励 |
| `wiki_search` | 冻结 Wiki 检索，不兼容版本不可用 |
| `strategy_search` | `StrategyLibrary` 中有来源署名的完整攻略章节 |

新增工具时，在 `TOOL_SPECS` 添加描述与参数 schema，在 `ModelTools._dispatch` 接入实现，并验证公开信息边界。请求与续接逻辑在 `provider.py`，知识与攻略实现分别在 `knowledge.py`、`strategies.py`，以上均位于 `src/sts2_agent/`。

## 调用合同

- 工具不能执行真实游戏动作，也不能在失败后替模型改选动作。推演使用假想世界，失败、截断和未完成结果应可见，不能将搜索价值解释为实战胜率。
- Provider 串行处理工具调用，最后要求动作 JSON。工具续接、重试及格式修复均受会话累计请求与时间预算约束；推演额度另由推演器管理。
- 会话记录 `tool_call_started`、`tool_call_completed`、`tool_call_failed`，以 `tool_call_id` 关联参数、结果、错误及决策。工具错误返回给模型，并保留在仪表盘和回放中。
- Wiki/攻略结果仅在同一决策、同一公开根内复用，并标记 `cache_hit`。推演不能通过缓存绕过预算，历史检索保持当前结果。
- thinking 启用且使用工具时，按 [DeepSeek 协议](https://api-docs.deepseek.com/guides/thinking_mode/) 保留续接所需完整 `reasoning_content`；`thinking=disabled` 不回传该字段，也不发送 `reasoning_effort`。未完成或已取消的流不派发工具，取消后的部分回答不进入上下文。

工具结果可共享重复内容，但必须能从当前请求完整还原，完整原始结果仍进入日志。新增结果字段时核对公开摘要与共享编码，不通过删除事实压缩输入。

计划生命周期由会话管理：`build_plan` 跨房间，`scene_plan` 绑定场景；对话与共享定义在新战斗或新幕重建。自动单步流程在 `automatic.py` 中复用正常准入与回执链。

## 开发验证

在项目根目录运行相关离线检查：

```powershell
.venv/Scripts/python.exe -m pytest tests/test_model_tools.py tests/test_provider.py tests/test_provider_cancellation.py tests/test_strategy_scenes.py
```

推演语义见[推演与验证](combat-simulation.md)，输入与动作边界见[架构与扩展入口](architecture-decisions.md)。
