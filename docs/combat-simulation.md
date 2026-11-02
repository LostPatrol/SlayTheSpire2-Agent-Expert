<!-- Current simulation architecture, information boundary, and validation entry points. -->
# 战斗搜索与验证

生产复用 Spire Mentor 的原生状态导出、Python CombatMirror、Rust core/content/MCTS/runai 和原网络，固定提交 `28429066d91ed05c10b7b57597fc624196d197a4`，目标游戏 v0.107.1。控制器每次只提交一个真实动作；模型辅助模式保留模型最终选择权。

## 信息边界

“公开信息”以人类理论可知为准：当前可观察事实，以及完美记忆和游戏机制允许的推断。字段位于程序内部，并不自动使其不可使用。敌人当前招式、已发生的动作和机制计数可用于固定循环、重复限制及阶段预测；仍不可知的未来随机结果需要按概率处理。

桥在同一游戏线程快照内提供 `simulation_internal.mentor_state`。真值镜像可以接收实际牌序、命名 RNG 和内部机械状态，用于校准已经发生的事情。它们不会进入模型观测、公共 token 或公开回放。

`public` 与 `internal` 控制未来搜索的知识范围。公共搜索先克隆完整镜像，在网络先验、根过滤、搜索和前缀预览之前运行原 BeliefDraw，替换真实 RNG 并延迟未知牌序到抽牌时采样。内部模式允许克隆使用真实未来信息。两种模式都使用同一规则引擎。不能用某个字段的名称判定是否作弊，也不能用一次 RNG 不变性测试宣称全部内部状态均无信息泄漏。

## 运行链路

1. 游戏桥直接编译上游的 `SimStateExtractor` 和 `SimCombatStateBuilder`。
2. `mentor_runtime.py` 调用原 `realgame_adapter` 和 `sts2_hint.adapter`，由原 `CombatMirror` 校准持续状态。
3. 已完成动作通过回执续接一次，包括模型选择的动作；UI 勾选在真实确认前不结算卡牌效果。
4. `mentor_native.pyd` 在同一动态库注册原 `sts2_rs` 类型，直接克隆内存中的 `PyCombatState` 交给完整 `MctsCombatPolicy::search_combat` 包装层。
5. JSONL 返回公开坐标、样本后继和搜索统计。近期完整根保留给已准入动作的后台审计，避免把新根用于旧动作。

搜索不会把 `state_to_dict` 再导入作为主路径；这种往返无法保留 pending_play 等续体。旧的公开牌费重建、历史随机结果重放和宿主历史后验搜索已删除。原生规则修复保留在有序补丁中。

桥的公开抽牌观察器同时把已见证的 bands、inserted、discard_bands 绑定到同帧 `mentor_state.draw_knowledge` 的牌堆索引。runtime 将它送入 `deck.draw_knowledge`，每次普通 reconcile 都从当前证书重建，保留 Perfect Fit 的公开优先级；组内无序，不从真值牌序推断优先次序。观察器声明不可用时，公共推演明确返回 `public_draw_knowledge_unavailable`；未见证的根仍遵守原 belief cutoff。

镜像校准使用上游 OBSERVE 策略：差异显式记录，由上游校准或重建；不能把校准后的相等冒充原预测正确。严格验证使用 STRICT。进程在未完成选择中重启时，单个静态快照可能无法恢复原续体，推演会明确报告不可用；不得以无效果选择继续给建议。

## 构建与代码入口

```powershell
.\scripts\setup-mentor.ps1
.\scripts\build-simulation.ps1
.\scripts\start-dashboard.ps1 -Config configs/a0-simulation.json
```

生产目录为 `local/simulation/mentor-native/`，包含扩展、必要的原 Python 模块、本地化映射、模型和来源记录。构建使用项目 Rust 1.99.1、MSVC 和锁文件，复用增量缓存及逻辑核心数减 2 的并行度；有序补丁通过临时 Git index 核对，不重置 vendor。PPO 显式使用包内 `models/ppo`，不以 Python 可执行文件位置猜测模型路径。

| 位置 | 职责 |
|---|---|
| `src/sts2_agent/mentor_runtime.py` | 原镜像生命周期、回执续接、原选牌准备、JSONL |
| `src/sts2_agent/simulation.py` | IPC、配置、持久预算、公开摘要 |
| `src/sts2_agent/public_history.py` | 公开历史摘要与选择界面归属 |
| `experiments/mentor_simulation/native/` | 共享原类型的 PyO3 入口 |
| `host/src/public.rs` | 当前 UI 坐标与公开后继投影 |
| `host/src/mcts.rs`、`networks.rs` | 完整上游搜索、固定前缀续局、网络配置与统计 |
| `host/src/ppo.rs` | 原 PPO 编码/推理/分组选择、非战斗公开接口 |
| `host/src/alignment.rs`、`validation.rs` | 原 RunManager 整局及连续战斗验证 |
| `vendor/spire-mentor/` | 原导出、镜像、规则、网络和搜索实现 |

`build-native.ps1 -DeploymentDirectory <目录>` 可构建候选。旧独立 Rust CLI 仅供离线验证；其输入须为完整 wire，不能再调用已删除的公开状态恢复器。运行中仪表盘需要正常重启，游戏关闭后才能安装新桥。

## 设置与结果

搜索强度为 1–5，在普通战斗 250/500/1000/2500/5000 次之间连续插值，Boss 四倍。默认强度 3、深度 200，与原部署一致。卡牌、药水、选择、结束回合共用搜索；原先验、根过滤、药水后处理和持久决策计数仍有效。Mentor 不再接受 beam/greedy/exhaustive 策略。

战斗与非战斗分别可选模型、辅助模型或上游直接决策。上游直接模式不调用付费模型。网络组件分别控制 rollout、先验、截断估值和搜索配置；默认 TexelWide。h16 是原上游旧 88 维资产，当前特征 216 维，已有兼容限制仍需标注，不额外重训或扩展适配。非战斗 PPO 的随机奖励继续使用独立假想世界及原编码/网络/分组选择。

`evaluate` 对模型声明的前缀进行真实规则推演，再用上游搜索继续战斗；各候选使用同一组假想世界。失败、超时和未解决选择保留，不丢掉后重算平均。采样后继明确为假想；即使某次抽牌样本恰好与实机一致，也不是预知能力。

visits/wins/losses/cutoffs 和终局 HP 是自适应搜索轨迹统计，不是校准后的实战胜率。截断不算死亡或胜利，即时后继不算最终战斗结果。确定性胜负证明与采样机会节点分开。预算、暂停、重试和模型计费仍由原控制器负责，重规划和升级不退款。

## 验证范围

分别记录：原规则与搜索的必要回归、完整内存状态的连续动作、真实游戏 DLL 导出与镜像校准、公共 RNG/牌序扰动、跨回合战斗、原 RunManager 整局和配对搜索预算。模拟器世界整局、原生 DLL 场景和实机整局是三种不同证据，不能相互替代。测试结果与未解决项写入每次任务报告。
