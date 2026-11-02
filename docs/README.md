<!-- Navigation for the public operational and developer documentation. -->
# 文档索引

首次安装与日常使用从根目录 [README](../README.md) 开始。

| 文档 | 用途 |
|---|---|
| [运行手册](local-runbook.md) | 本机配置、全解锁存档、安装、启动、预算与恢复 |
| [架构与扩展入口](architecture-decisions.md) | 模块分工、公开信息边界与动作执行约束 |
| [战斗推演与回放](combat-simulation.md) | Mentor 构建、搜索配置、支持边界与轨迹导出 |
| [仪表盘开发](dashboard.md) | 前端文件、快照、流式事件与历史接口 |
| [离线知识库](knowledge.md) | 静态资料来源、导入、检索与适用限制 |
| [模型工具与计划](model-tools.md) | 工具注册、协议续接、预算与计划作用域 |
| [SpeedX 加速](fast-mode.md) | 可选加速模组的安装、配置与回滚 |
| [第三方来源](third-party.md) | 固定上游版本、来源补丁与许可 |

构建与验证源码在 `experiments/`，其中 Mentor 的 host/native 源码、补丁和 Cargo 锁文件由正式构建脚本直接使用。
其他目录的 README 说明对应离线验证工具；验证结果受具体版本、场景与输入限制，不能等同于实机胜率。

个人配置、密钥、存档、运行日志、开发指令、过程报告和环境缓存均留在本地。
公开仓库提供源代码、配置模板与操作说明，不包含历史实机日志或私人开发记录。
