# Isolated combat simulation experiment

公开观测驱动的 STS2 当前玩家回合模拟与搜索对照，以铁甲战士公开轨迹为验收范围。可独立编译运行，也作为正式控制器的可选推演host，由 `src/sts2_agent/simulation.py`提供建议与单步预测；真实游戏动作仍由模型选择和原执行器提交。

## 来源与选择

- **规则核心：evangambit/sts2**，固定提交 `37e4264115546b6ac640d54aab3d7e2ac6983d2d`，目标游戏 `v0.107.1`，MIT。`third_party/evangambit` 保留其 Core、Generated 和 LICENSE；Core的目标身份、多段伤害、自损和格挡修正见 `ORIGIN.json`，Generated未改。只编译托管 C#，不需要游戏 DLL、NativeAOT、Python RL 包或网络服务。
- **架构参考：ing-gom/sts2-combat-ai**，审阅提交 `44f7d681e25185cb97b1e48b2a068b81c5e9def1`。借鉴模拟与规划分层，未移植其规划器。上游部分路径直接读取顶牌，且有按卡牌 ID 去重等策略过滤，不适合作为本项目公开信息下的完整穷举动作集。
- 未采用 sts2-cli：其 setup 涉及游戏 DLL 复制和 IL patch，本次不运行。未采用 yizhang-dream 版本：公开规则快照较旧，未找到明确许可证。Bottled AI 面向 STS1，只参考规则与策略分离思想。

## 运行

在项目根目录的 PowerShell 执行：

```powershell
.venv\Scripts\python.exe experiments\combat_simulation\run.py build
.venv\Scripts\python.exe -m pytest -q experiments\combat_simulation\tests
.venv\Scripts\python.exe experiments\combat_simulation\run.py benchmark --repeats 5
```

有本机历史 `runs/cdd55ffd-7149-4d2f-944d-02136cd18ca0` 时，可再执行：

```powershell
.venv\Scripts\python.exe experiments\combat_simulation\run.py all --repeats 5
.venv\Scripts\python.exe experiments\combat_simulation\run.py corpus-benchmark --repeats 5
```

`all` 依次运行诊断场景、历史回合搜索、单步回放。历史日志不随本实验复制；没有日志时仍可执行诊断 benchmark。使用现有 `.tools/dotnet/dotnet.exe` 和项目 `.venv`，没有新增 Python/NuGet 依赖。首次运行缺少 DLL 时会自动编译本目录 host。

输出 JSON 保留每例支持级别、预测、差异、最佳序列、评分明细、计数、耗时及源文件哈希；CSV 用于比较策略。每次同时比较 greedy、beam 16、beam 64、exhaustive；共用一个 host，按重复轮次轮换执行顺序。仅对该子进程设置 `DOTNET_TieredCompilation=0`，减少跨场景 JIT 升级导致的计时偏移，不修改系统设置。warm search 时间包含穷举的预计数成本；roundtrip 另含 JSON、公开状态导入和进程通信。冷启动单独记录；复用进程的第二套测试不会重新冒充冷启动。

## 代码入口

| 文件 | 责任 |
| --- | --- |
| `host/SimulationPosition.cs` | public → core 状态适配、稳定实例身份、规则支持与随机边界检查 |
| `host/Search.cs` | 统一合法动作、评分、greedy／beam／exhaustive |
| `host/Program.cs` | 常驻 JSONL host，`inspect`／`simulate`／`search` |
| `scenarios.py` | 14 个诊断场景，不充当真实游戏 oracle |
| `replay.py` | 从已接受动作和稳定公开后继提取真值、比较资源及 Power |
| `run.py` | 可复现实验 CLI、warm/cold 计时与结果保存 |

host 的一行请求格式：

```json
{"op":"search","public_state":{},"strategy":"beam","max_depth":6,"beam_width":16,"exhaustive_limit":20000}
```

`public_state` 填原公开 `state` 对象。`simulate` 请求添加 `actions` 数组，例如 `{"type":"play_card","card_handle":"h0","target_index":0}`。手牌 handle 按初始行生成，出牌后不重新编号；敌人目标始终是该分支初始公开槽位。自体／全体目标卡不传 `target_index`。`end_turn` 是**停止投影**，不会执行敌人回合、抽牌或下一招 AI。

## 搜索和验证口径

三策略仅搜索出牌和停止，不包含药水等其他操作。默认最多 6 次出牌，beam 宽度 16。允许每一步停止；保留不同卡牌实例、升级、费用及全部可打目标，不按卡牌 ID 合并。零费／生成循环也受显式深度限制。

exhaustive 先计数到 `exhaustive_limit+1`；默认不超过 20,000 条终结策略才再遍历求优。超限返回 `skipped=true`、`best=null`，不把截断搜索称为最优。`optimal_within_supported_horizon` 仅表示支持动作子图内、有限深度下、给定启发评分的最优；`exact_within_horizon` 还要求完整的该出牌动作域覆盖，均不表示整局最优。

默认评分为敌 HP 削减 + 击杀数×12 + 自身 HP 变化×4 − 当前宣告攻击预计掉血×4 + 剩余能量×0.05 + 多余格挡×0.02，另有胜利／致死权重。权重可用 `score_weights` 修改，所有对照组必须相同。破甲眩晕和击杀会取消对应宣告攻击。**该评分不是完整结束回合模拟，不是胜率；非攻击结束回合机制和未来成长可能改变真实最优选择。** `support` 单独表示即时动作覆盖，`score_support` 始终注明投影不完整，并按当前局面列出沙坑、束缚、中毒、留手状态牌等具体缺口。

输入不读取游戏 seed、原生 RNG、内部 move ID 或真实牌序；只保留公开牌堆构成。规则以 core 的基础值和公开 Power 计算，不把动态牌面数字再次当基础伤害加力量。未知抽牌、随机、选牌、生成／自动出牌等不能可靠导入的转换明确拒绝；相关卡仍作为 coverage 缺口报告，不假装无效果。单帧缺少的卡牌实例成长或遗物历史也会标明。

真实回放仅使用 before 的原公开状态。先核对 action_id 有接受回执，再找 revision+1、同楼层同回合、三项 readiness 全 true 的公开帧；不能用 receipt 内尚在 stabilizing 的帧。比较玩家及存活敌人的 HP、格挡、能量、星数和 Power；不比较未知抽牌身份，也不从 after 反向恢复牌序。敌人移除后按存活顺序比较，不把帧内 `i` 当跨帧身份。

结果与局限见 `RESULTS.md`。实验不改变正式 NoSL/calculation_only 运行模式；后续如接入正式候选预推演，还需单独设计随机分支、公开历史恢复、真实结束回合验证和决策预算。
