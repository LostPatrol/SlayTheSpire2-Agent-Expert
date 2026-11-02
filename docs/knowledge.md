<!-- Offline reference data, retrieval boundaries, and extension points. -->
# 离线知识库

静态规则来自 Spire Codex，运行时不访问网络服务。模型根据当前公开实体及显式规则依赖检索资料；
仪表盘的 `/knowledge` 页面提供本地浏览。策略文章与规则数据分开，实际游戏状态优先于静态说明。

## 数据与代码

| 位置 | 内容或修改入口 |
|---|---|
| `knowledge/current.json` | 当前快照指针 |
| `knowledge/snapshots/<id>/` | 标准化实体、冲突记录、来源清单和原始资料 |
| `knowledge/strategies/` | 完整策略章节、关联资料与署名 |
| `src/sts2_agent/knowledge.py` | 实体查找、别名解析、规则依赖与模型上下文 |
| `src/sts2_agent/strategies.py` | 场景相关策略和检索 |
| `scripts/import-knowledge.py`、`import-strategies.py` | 固定版本资料导入 |

`KnowledgeBase.lookup/search` 提供实体检索，`context_for_model` 按公开观察选取相关规则，
`lookup_detailed/search_related` 用于关系展开。新增实体或规则依赖时，先修改导入与检索逻辑，再检查实际模型请求是否包含所需内容。

动态费用、计数和原生描述以实时观察为准；静态样本不替代当前数值，未知值不填零。
资料关联不表示当前持有或效果已生效。策略文章保留作者与来源，用作建议，不作强制排名或精确规则。

## 重建资料

```powershell
git clone --filter=blob:none --no-checkout https://github.com/ptrlrd/spire-codex.git vendor/spire-codex
.\.venv\Scripts\python.exe -X utf8 scripts/import-knowledge.py
.\.venv\Scripts\python.exe -X utf8 scripts/import-strategies.py
```

导入读取固定 Git 对象并生成内容寻址快照；既有快照保留，运行中的会话固定所选快照。
游戏版本变化需要重新核对资料，不能把旧快照直接标成新版本。缺失或哈希不符时显示诊断，
知识功能单独失效，基础控制器仍可运行。

资料不导入种子查询、真实对局数据库、隐藏未来状态或开发者工具。
保留原始许可、Required Notice 与来源记录；版本及许可见 [第三方来源](third-party.md)。

相关检查在 `tests/test_knowledge.py`、`tests/test_rule_delivery.py`、`tests/test_strategies.py` 和 `tests/test_knowledge_dashboard_integration.py`。
