<!-- Third-party origins and redistribution notices for the current implementation. -->
# 第三方来源

游戏桥来自 [CharTyr/STS2-Agent](https://github.com/CharTyr/STS2-Agent)，固定 commit `7b3cb83ff97e05477db7688a37a3e622b12ef074`，许可 AGPL-3.0。
完整上游源码由 `scripts/setup.ps1` 从固定版本恢复到本机 `vendor/STS2-Agent/`，本地修改在 `patches/bridge-main-public-v1.patch`。公开仓库保留[完整 AGPL-3.0 许可](../licenses/STS2-Agent-AGPL-3.0.txt)，修改后的桥源码可用同一脚本恢复。

外部 Python 控制器在本项目实现，未整段复制 AgenticSTS 主循环、数据或技能库。
Python 第三方依赖和版本由 `pyproject.toml`、`uv.lock` 记录，各依赖保留其原有许可。

旧版对照后端使用 [evangambit/sts2](https://github.com/evangambit/sts2) 的 C# 规则核心，固定 commit `37e4264115546b6ac640d54aab3d7e2ac6983d2d`，采用 **MIT** 许可。
规则源码、来源清单与许可证保存在 `experiments/combat_simulation/third_party/evangambit/`，`ORIGIN.json`记录目标身份、多段全体伤害、自损防护和狂怒格挡等本地核心修正；Generated未改。本项目的公开状态适配与搜索在独立 host 中实现。
[ing-gom/sts2-combat-ai](https://github.com/ing-gom/sts2-combat-ai) 仅作为模拟与搜索分层的设计参考，未复制其隐藏牌序读取路径或规划器代码。

生产默认规则层使用[tfriedel/spire-mentor](https://github.com/tfriedel/spire-mentor)，固定`28429066d91ed05c10b7b57597fc624196d197a4`，MIT、Thomas Friedel。直接复用core/content/mcts/runai及原PPO、rollout和动作先验资产，使用本地Tract推理。源码由构建脚本恢复到本机`vendor/spire-mentor/`；有序直接修正位于`experiments/mentor_simulation/patches/`，由`prepare-source.ps1`验证。公开仓库保留[原 MIT 许可](../licenses/Spire-Mentor-MIT.txt)。构建记录实际源码、补丁、模型及二进制hash，部署目录`local/simulation/mentor-native/`保留上游MIT许可证及署名。

[Torch1230/CombatSolver](https://github.com/Torch1230/CombatSolver)固定`5c773caaec742bc55588bc9ee21c361326a732f0`，MIT，包含Torch1230与Random Foreseer作者hotwords123署名。本轮用于续接、实例Fork和原生差分设计核查，未把其原生状态/RNG捕获或搜索代码接入生产。

原生规则参照使用[wuhao21/sts2-cli](https://github.com/wuhao21/sts2-cli)兼容历史`d11aa883b582dd68bd39b331f3370746b30d447e`，MIT、Hao Wu。适配来源与改动记录于`experiments/native_oracle/UPSTREAM.json`；Godot替身及调度/本地化适配仍需按范围核验。游戏DLL由本机合法安装复制，仅在忽略的过程目录使用，不进入本项目可分发文件。

离线静态资料来自 [ptrlrd/spire-codex](https://github.com/ptrlrd/spire-codex)，固定 commit `2af6543ca7a6c76acfde6be582651c7c05e88e50`，采用 **PolyForm Noncommercial 1.0.0**。
快照中的 `sources.lock.json` 与 `raw/LICENSE.md` 保存来源、哈希和许可；策略资料同样保留 `knowledge/strategies/LICENSE.md` 与 manifest 中的 Required Notice。游戏内容版权属于 Mega Crit。
本地浏览和模型检索仅使用筛选后的静态资料，不包含上游站点的实时对局、种子查询等服务。导入方式见 [离线知识库](knowledge.md)。

本项目没有为全部文件统一指定一种许可。第三方源码、补丁与资料应按其原许可及署名要求使用；各组件许可不自动扩展为对其他原创文件的授权。
