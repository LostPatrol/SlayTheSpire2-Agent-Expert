<!-- Simulation architecture, information boundaries, and extension points. -->
# 战斗推演

推演使用 Spire Mentor 的规则、MCTS 和 PPO 策略。战斗与非战斗可分别选择模型决策、推演辅助或推演直接决策；
辅助模式保留模型最终选择权，直接模式不调用模型。每次仍只提交一个真实游戏动作。

## 架构与修改入口

游戏桥导出状态 → `CombatMirror` 持续同步 → 原生扩展执行搜索 → 控制器校验并提交动作 → 回执更新镜像。

| 位置 | 修改内容 |
|---|---|
| `src/sts2_agent/simulation.py` | 推演配置、进程通信、预算与结果摘要 |
| `src/sts2_agent/mentor_runtime.py` | 镜像生命周期、动作续接与策略调用 |
| `src/sts2_agent/public_history.py` | 公开历史与选择归属 |
| `experiments/mentor_simulation/native/` | Python 原生扩展入口 |
| `experiments/mentor_simulation/host/src/` | MCTS、PPO、网络与公开结果投影 |
| `experiments/mentor_simulation/patches/` | Mentor 规则与适配修正 |

搜索克隆完整内存状态，保留未完成选择的续体。规则问题直接修正对应组件，并维护有序补丁；
状态适配与结果投影问题在 runtime/host 中修正。

## 信息范围与结果

公开信息包括当前可见事实，以及完美记忆和已知机制允许的推断。公开搜索对未知牌序和未来随机结果采样；
内部读取模式可使用真实未来信息。镜像可使用实际状态校准已发生动作，但真实随机状态不进入模型或公开结果。

同一根的状态与公开动作必须对应，已完成动作通过回执续接一次。
镜像校准后的相等不证明先前预测正确；无法恢复的选择续体应明确报告不可用。

搜索强度为 1–5，默认 3：普通战斗 1000 次模拟，Boss 4000 次。rollout、先验与截断估值可独立配置。
固定前缀评估按规则执行候选动作，再继续搜索；候选使用同一组假想世界，失败与超时保留。
搜索胜败统计和 PPO 概率不是实战胜率，采样后继不是确定的下一手。

## 构建与验证

```powershell
.\scripts\setup-mentor.ps1
.\scripts\build-simulation.ps1 -FetchDependencies
.\scripts\start-dashboard.ps1 -Config configs/a0-simulation.json
```

产物在 `local/simulation/mentor-native/`，包含扩展、Python 模块、映射、模型与来源记录。
固定组件版本见 [第三方来源](third-party.md)。保持 Cargo 锁文件；首次下载依赖后可省略 `-FetchDependencies`。
候选构建可通过 `experiments/mentor_simulation/build-native.ps1 -DeploymentDirectory <目录>` 输出到独立目录。

宿主回归在 `tests/test_simulation*.py`、`tests/test_mentor_runtime.py`；
规则与搜索检查在 `experiments/mentor_simulation/test_*.py`，整局验证入口在 `experiments/solver_validation/`。
规则测试、原生游戏场景和实机整局分别验证，不互相代替。
