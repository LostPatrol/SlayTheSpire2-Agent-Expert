<!-- Dashboard modules, public API contracts, and focused verification. -->
# 仪表盘开发

正式前端位于 `src/sts2_agent/web/`，由 `dashboard.py` 提供本机 HTTP 服务，无前端构建步骤。默认使用 `?layout=stream`，`?layout=split` 切换分栏。

## 模块与接口

表内路径相对于 `src/sts2_agent/`。

| 修改内容 | 入口 |
|---|---|
| 页面结构、配色与响应式样式 | `web/index.html`、`web/styles.css` |
| 主题、折叠、抽屉与知识库浏览 | `web/observatory.js` |
| 快照、SSE、控制、推演设置与遥测 | `web/app.js` |
| 历史分页、详情与关联显示 | `web/timeline.js` |
| 公共日志索引与历史接口 | `history.py`、`dashboard.py` |
| 会话及动作状态 | `session.py` |

| 接口 | 用途 |
|---|---|
| `GET /api/snapshot`、`GET /api/events` | 当前公开快照与可续接 SSE |
| `GET /api/history`、`GET /api/history/{decision_id}` | 历史分页与完整详情，均需 `run_id` |
| `GET /api/history-events` | 无决策归属的事件分页，需 `run_id` |
| `POST /api/commands`、`POST /api/instructions` | 用户显式控制与指导 |
| `POST /api/simulation/settings` | 应用推演设置 |
| `POST /api/game/start`、`POST /api/service/stop` | 启动游戏与停止 HTTP 服务 |

## 交互与数据合同

`session.py` 是唯一控制权威。页面加载、刷新、重连和历史查询保持只读；控制操作由用户显式提交。暂停或停止会取消未完成的模型生成，阻止重试与工具续接；恢复重新生成被取消的决策，已完成缓存可经校验复用。已提交游戏动作按原回执结算，停止游玩与停止 HTTP 服务是不同操作。

记录按 `decision_id`、`model_request_id`、`tool_call_id`、`action_id` 关联，不按时间相近推断。历史默认折叠，展开时读取完整详情；实时动作状态及时更新，旧历史不能覆盖当前状态。无归属事件单独展示，缺失或损坏明确提示。

历史索引只读现有公共日志，后台重建后增量扫描。完整详情按需分页读取并提供 JSON 导出；刷新只处理摘要，避免重复处理大记录拖慢页面。

页面只展示公开数据，不加入模拟器私域状态或密钥。历史卡牌与目标索引必须使用当时的记录，不能用当前手牌或敌人解释；聚合牌组以总张数计数，不能用行数替代。

推演预设只填写草稿，应用后才生效；强度显示可四舍五入，提交保留原始精度。候选为空须区分等待、不适用和失败。MCTS 价值、PPO 概率与模拟胜败不能标成实战胜率；累计 token 只取 API usage，未知用量保持未知。

## 开发验证

在项目根目录按修改范围运行：

```powershell
node tests/test_dashboard_usage.js
node tests/test_dashboard_timeline.js
.venv/Scripts/python.exe -m pytest tests/test_dashboard_api.py tests/test_dashboard_history.py tests/test_knowledge_dashboard_integration.py
```

浏览器检查桌面与窄屏布局、历史展开和分页、加载失败、SSE 断线恢复。界面验收使用现有会话，不为验证启动新游戏。运行步骤见[本机手册](local-runbook.md)。
