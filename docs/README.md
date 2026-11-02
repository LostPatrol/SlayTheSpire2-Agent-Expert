<!-- Navigation for the public operational and developer documentation. -->
# 文档索引

项目介绍与安装：[English](../README.md) · [中文](../README.zh-CN.md)

| 文档 | 内容 |
|---|---|
| [运行手册](local-runbook.md) | 本机配置、全解锁存档、启动与更新 |
| [架构与扩展入口](architecture-decisions.md) | 模块职责、决策链路与修改入口 |
| [战斗推演](combat-simulation.md) | Mentor 构建、信息范围与结果含义 |
| [仪表盘开发](dashboard.md) | 前端分工、历史与控制接口 |
| [离线知识库](knowledge.md) | 数据组织、导入与检索扩展 |
| [模型工具与计划](model-tools.md) | 工具注册、模型续接与计划作用域 |
| [SpeedX 加速](fast-mode.md) | 可选模组的安装、预设与回滚 |
| [第三方来源](third-party.md) | 组件版本、源码位置与许可 |

`src/` 是控制器和正式前端；`scripts/` 是安装与运行入口。
`experiments/` 包含推演构建源码和离线验证工具，保留其中的 Cargo 锁文件、来源补丁与 C# 规则源码。
