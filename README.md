<!-- Public overview and setup instructions for SlayTheSpire2-Agent-Expert. -->
# SlayTheSpire2-Agent-Expert

在 Windows 本机运行《Slay the Spire 2》的 AI Agent，通过游戏 Mod 的结构化观测与动作接口操作游戏。

- 支持 DeepSeek 决策、Spire Mentor MCTS 战斗推演，以及上游 PPO 整局策略。
- 提供本地“尖塔观测台”仪表盘，查看流式推理、动作回执、完整历史与预算，支持暂停、继续、单步和人工指导。
- 内置中英文静态知识库；默认模型与公开推演使用公开信息，推演读取权限可在仪表盘中设置。
- 网页与命令行共用持久化会话，保留预算、公开记忆及待核对动作；支持五角色和 A0–A10 配置。

当前适配 Steam 主分支 `v0.107.1`。项目仍在开发，完整内容覆盖、纯自主通关及正式胜率评估尚未完成。

## 准备

需要正版 Steam 游戏、Windows、PowerShell 7、Git、uv，以及 MSVC C++ 构建工具和 Windows SDK。
脚本安装项目本地 Python 3.12、.NET SDK 9.0.320 和 Rust 1.99.1；编译与运行环境不进入仓库。

```powershell
git clone https://github.com/LostPatrol/SlayTheSpire2-Agent-Expert.git
Set-Location SlayTheSpire2-Agent-Expert
.\scripts\setup.ps1
```

首次准备会从模板创建未提交的 `configs/machine.json` 和 `configs/bridge.settings.json`。
修改 `machine.json` 中的游戏安装目录与 `client_id`，选用**自己的 Steam 全解锁主存档或其独立副本**。
存档位于 `%APPDATA%\SlayTheSpire2\default\<client_id>\modded\profile<编号>\saves\`。
项目不提供任何个人存档；本地全解锁备份与启动校验的准备方式见[运行手册](docs/local-runbook.md)。

关闭游戏后构建：

```powershell
.\scripts\setup-mentor.ps1
.\scripts\build-bridge.ps1
.\scripts\build-simulation.ps1 -FetchDependencies
```

首次推演构建下载锁文件指定的 Rust 依赖；以后可省略 `-FetchDependencies`，使用本地缓存离线构建。
固定上游源码、网络模型与许可证由脚本获取，不需提交 `vendor/` 或构建产物。

## 使用

使用大模型时，创建 `secrets/` 并把 DeepSeek API 密钥放入 `secrets/apikey-deepseek.txt`，也可设置当前进程的 `DEEPSEEK_API_KEY`。
密钥与本机桥设置不应提交。将战斗和整局都设为上游直接决定时，不调用大模型。

1. 双击根目录 **启动仪表盘.cmd**，打开本地网页。
2. 点击 **启动游戏**，核对当前账号、全解锁存档、角色与难度。
3. 点击 **开始自动游玩**，按需暂停、继续或单步。

默认配置为 `configs/a0-simulation.json`，累计上限为 1000 次模型请求、1000 个动作和 600 分钟。
服务默认保持暂停，网页位于 `http://127.0.0.1:18766`；服务窗口需保持运行。

```powershell
.\scripts\start-dashboard.ps1 -Config configs/a0-simulation.json -NoBrowser
.\scripts\run-agent.ps1 -Command inspect
```

日志保存在 `runs/`，运行状态与设置保存在 `local/`。预算、未完成动作和存档不会因刷新网页而重置。
完整操作见[运行手册](docs/local-runbook.md)，推演支持范围见[战斗推演](docs/combat-simulation.md)。

## 开发

```powershell
.\.venv\Scripts\python.exe -m pytest -q
node --test tests/test_dashboard_timeline.js tests/test_dashboard_usage.js
```

[文档索引](docs/README.md)提供架构、仪表盘、知识库和工具协议的修改入口。
`experiments/` 同时包含推演构建源码及离线验证工具；其中 Rust 锁文件、来源补丁和 C# 规则源码是必要构建输入。

## 来源与许可

感谢 [CharTyr/STS2-Agent](https://github.com/CharTyr/STS2-Agent)、[tfriedel/spire-mentor](https://github.com/tfriedel/spire-mentor)、
[evangambit/sts2](https://github.com/evangambit/sts2) 和 [ptrlrd/spire-codex](https://github.com/ptrlrd/spire-codex)。

各组件适用各自许可：游戏桥为 AGPL-3.0，Mentor 与 C# 规则核心为 MIT，Spire Codex 静态资料为 PolyForm Noncommercial 1.0.0。
详见[第三方来源](docs/third-party.md)及随附许可。游戏内容版权属于 Mega Crit，本项目不包含游戏程序、个人存档或 API 密钥。
