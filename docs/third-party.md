<!-- Component origins, pinned versions, and redistribution licenses. -->
# 第三方来源

| 组件 | 固定版本 | 用途与许可 |
|---|---|---|
| [CharTyr/STS2-Agent](https://github.com/CharTyr/STS2-Agent) | `7b3cb83ff97e05477db7688a37a3e622b12ef074` | 游戏桥；[AGPL-3.0](../licenses/STS2-Agent-AGPL-3.0.txt) |
| [tfriedel/spire-mentor](https://github.com/tfriedel/spire-mentor) | `28429066d91ed05c10b7b57597fc624196d197a4` | 规则、MCTS、PPO 与模型；[MIT](../licenses/Spire-Mentor-MIT.txt)，Thomas Friedel |
| [evangambit/sts2](https://github.com/evangambit/sts2) | `37e4264115546b6ac640d54aab3d7e2ac6983d2d` | C# 对照规则核心；[MIT](../experiments/combat_simulation/third_party/evangambit/LICENSE) |
| [ptrlrd/spire-codex](https://github.com/ptrlrd/spire-codex) | `2af6543ca7a6c76acfde6be582651c7c05e88e50` | 静态知识与策略；[PolyForm Noncommercial 1.0.0](../knowledge/strategies/LICENSE.md) |
| [wuhao21/sts2-cli](https://github.com/wuhao21/sts2-cli) | `d11aa883b582dd68bd39b331f3370746b30d447e` | 可选原生规则验证；MIT，Hao Wu，来源见 `experiments/native_oracle/UPSTREAM.json` |

## 源码与维护

游戏桥由 `scripts/setup.ps1` 恢复固定源码，并应用 `patches/bridge-main-public-v1.patch`。
Mentor 由 `experiments/mentor_simulation/prepare-source.ps1` 恢复并应用有序修正，构建包保留许可与来源记录。
C# 核心及来源清单保存在 `experiments/combat_simulation/third_party/evangambit/`。

知识快照保留原始资料、许可和 `sources.lock.json`；策略目录保留许可与 manifest 中的 Required Notice。
Python 依赖由 `pyproject.toml`、`uv.lock` 记录，各自适用原许可。

[ing-gom/sts2-combat-ai](https://github.com/ing-gom/sts2-combat-ai) 与 [Torch1230/CombatSolver](https://github.com/Torch1230/CombatSolver)
仅作设计参考，没有接入其隐藏状态捕获或搜索实现。

本项目没有为全部文件统一指定一种许可，第三方内容按各自许可与署名要求使用。
游戏内容版权属于 Mega Crit；游戏程序、个人存档及运行环境不随仓库分发。
