<!-- Runtime entry points and frontend integration boundaries. -->
# 仪表盘开发

正式页面为 `src/sts2_agent/web/`，由 `dashboard.py` 提供本机 HTTP 服务。默认 `?layout=stream`，`?layout=split` 可切换分栏，无前端构建步骤。公开仓库仅保留正式前端，设计草稿不属于运行入口。

| 修改内容 | 入口 |
|---|---|
| 页面结构、角色配色和响应式样式 | `web/index.html`、`web/styles.css` |
| 主题、模块折叠、抽屉、知识库浏览 | `web/observatory.js` |
| 快照、SSE、控制、推演设置、当前决策和遥测 | `web/app.js` |
| 历史分页、展开、关联与显示 | `web/timeline.js` |
| 公共日志索引和只读历史接口 | `history.py`、`dashboard.py` |

`session.py` 是会话与动作的唯一权威。页面加载、刷新、重连和历史查询只读；启动、继续、暂停、单步、重决策和指导均由用户操作后提交既有接口。暂停或停止会取消本轮未完成的模型生成、断开已连接的请求，并禁止后续重试、工具续接和格式修复；恢复后重新生成被取消的决策，已完成的缓存决策仍可复用。已提交游戏动作继续按原回执结算；停止游玩不等于停止 HTTP 服务。

取消令牌属于单次生成，恢复使用新令牌，不能清除旧令牌。请求终止事件 `model_request_cancelled` 保留已收到的 usage，缺失用量仍为未知，不退款。HTTPX 暴露 socket 后主动中断连接；尚未取得 socket 的 DNS/建连仍受原连接超时约束。取消后的部分回答不进入后续模型上下文。

前端按明确的 `decision_id`、`model_request_id`、`tool_call_id` 和 `action_id` 关联记录，不按时间相近推断归属。历史列表分页，完整响应、工具参数/结果、推演及回执在用户展开时读取。新条目默认折叠，实时动作字段与底色同步呈现；旧历史摘要不得覆盖新动作状态。无归属事件仅保留在分栏事件列表及独立接口；日志缺失或损坏会明确提示，不生成演示记录。

大记录详情由 `timeline.js` 的 `rawInspector()` 分页排版，所有文字均可翻页读取或导出为 JSON；页码随本局保留。详情开合与翻页只更新本行，重绘签名只包含可见元数据，分栏工具全文延迟到点击后生成。不要把完整工具结果放进刷新签名或单个自动换行的 `<pre>`；设置高度上限仍会触发全文排版。

历史索引从现有公共日志在后台重建，使用内存中的文件偏移读取原文，后续只处理追加内容，不改写原始日志。首次加载显示索引进度状态，服务重启后重建。新增历史类型时同时维护索引与前端呈现；不要把模拟器内部状态或密钥加入网页数据。

stream 按用户要求不显示会话事件组及常态历史说明，统计与时间轴标题同行；遥测不显示用量明细及速率/累计标题，状态紧邻速率数字。候选为空时区分等待、不适用和后端失败。分栏资产读取公开遗物、药水和牌组；聚合牌组行数不能当作总张数。分栏原“决策证据”示意模块没有对应后端合同，已移除。

Windows 下 `simulation.py` 启动 Mentor Python 子进程时显式传入 `-X utf8`，使 JSON 双向编码一致；主服务的 UTF-8 参数不会被子进程继承。

推演强度显示两位小数，提交保留原始精度。预设只填写草稿，点击应用后生效；网络辅助同时选择 MCTS/PPO 辅助并保留奖励池。MCTS 搜索价值、PPO 策略概率和模拟胜败均不是实战胜率。累计 token 只取 API usage，未知用量保持未知。

相关检查：`node tests/test_dashboard_usage.js`、`node tests/test_dashboard_timeline.js`，以及项目 Python 的 `tests/test_dashboard_api.py`、`tests/test_dashboard_history.py`、`tests/test_knowledge_dashboard_integration.py`。浏览器重点验收 stream 的桌面/窄屏布局、历史展开及加载失败、断线恢复；不要为界面验收启动新游戏。
