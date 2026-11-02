<!-- Local setup, profile selection, and persistent controller operation. -->
# 本机运行手册

以下命令在项目根目录用 PowerShell 7 执行。首次克隆与依赖要求见 [README](../README.md)。

## 本机配置与存档

`scripts/setup.ps1` 从公开模板创建 `configs/machine.json` 和 `configs/bridge.settings.json`，已有文件不会被覆盖。
修改 `machine.json`：

- `game_root`：本机 Steam 游戏安装目录，需包含 `SlayTheSpire2.exe`。
- `client_id`：自己的 Steam 全解锁主存档所在的账号目录，或其独立全解锁副本的目录 ID；不要使用模板占位符。
- `api_port`：游戏桥端口，默认 18765，应与 Agent 配置中的 `bridge.base_url` 对应。
- `upstream_commit`：已审核的游戏桥版本，普通安装保持模板中的固定值。

账号目录位于 `%APPDATA%\SlayTheSpire2\default\<client_id>\`。
启动器使用其 `modded/profile<编号>/saves/`，具体编号由账号目录的 `profile.save` 选择，未设置时为 profile1。
启动路径本身不能证明解锁状态，必须核对实际使用的 modded profile。禁止用新档、部分解锁档替代全解锁主档测试。

启动校验要求至少 57 个已解锁 epoch、578 卡牌、297 遗物、65 药水、62 事件、4 幕和 5 角色。
当前存档已满足条件时不需要恢复文件。需要自动恢复时，先自行准备并核验自己的全解锁 `progress.save`，
复制到 `local/saves/full-unlock/progress.save`；该目录及所有存档都不提交。
只有游戏完全关闭后才允许恢复，替换前会备份当前进度与游戏侧备份到 `local/game-saves-backup/`。

```powershell
.\.venv\Scripts\python.exe -m sts2_agent.cli --config configs/a0-simulation.json ensure-save --check
```

`--check` 仅检查，不恢复进度。模板配置与存档路径不会自动解锁游戏内容。
不同账号的存档不自动迁移或合并，运行日志与原生游戏存档各自保存。

## 构建与安装

```powershell
.\scripts\setup.ps1
.\scripts\setup-mentor.ps1
.\scripts\build-bridge.ps1
.\scripts\build-simulation.ps1 -FetchDependencies
```

游戏桥从固定上游恢复，再应用 `patches/bridge-main-public-v1.patch`。
Mentor 从固定上游恢复，并按 `experiments/mentor_simulation/prepare-source.ps1` 的顺序应用来源补丁。
`-FetchDependencies` 下载锁定的 Rust 依赖；缓存齐备后构建默认离线。MSVC C++ 工具链与 Windows SDK 需另行安装。

`build-bridge.ps1` 构建并安装游戏桥，安装前须完全关闭游戏，旧文件自动备份至 `local/mod-backups/`。
游戏运行时可用 `-SkipInstall` 仅构建；关闭游戏后运行 `scripts/install-built-bridge.ps1` 安装准备好的包。
推演构建不替换游戏桥，产物位于 `local/simulation/mentor-native/`。
现有 vendor checkout 的无关改动会保留；固定源码不一致时需自行核对，不应直接删除目录或重置改动。

## 启动与控制

DeepSeek 密钥放在 `secrets/apikey-deepseek.txt`，或当前进程的 `DEEPSEEK_API_KEY`。
不要把密钥填入公开配置模板。正式 Agent 配置在 `configs/a0-simulation.json` 等文件，桥设置只用于本机 Mod。

双击 **启动仪表盘.cmd**，然后在网页点击“启动游戏”和“开始自动游玩”。
也可运行：

```powershell
.\scripts\start-dashboard.ps1 -Config configs/a0-simulation.json -Port 18766
.\scripts\run-agent.ps1 -Command inspect
```

服务只监听本机，启动后保持暂停；`-NoBrowser` 禁止自动打开浏览器。
角色和 A0–A10 在启动按钮旁选择，进行中的对局不会被选择器重开。
正式页面默认时间轴，也支持分栏；可查看公开状态、完整决策历史、流式输出及实际 API 用量。
开始、继续或单步才会运行策略。上游直接执行模式不调用模型，辅助模式由模型作最终决定。
同一游戏不能同时由网页和另一个 CLI 策略循环控制。

暂停或停止会取消未完成的模型生成，已提交游戏动作仍须通过回执结算。
单步只放行一个原生动作；一次结束回合可包括敌人行动。
人工指导按下一次决策、本场战斗或整局生效，实际投递后的对局标记为 `human_assisted`。

## 预算、恢复与更新

默认推演配置累计最多 1000 次模型请求、1000 个动作、600 分钟；重试和格式修复同样计 API 请求。
已用预算不会因暂停、重启或重决策退还。smoke 配置用于短程检查，已有账本继续计入所选配置的限制。
游戏记录保存在 `runs/<UUID>/`，会话与设置保存在 `local/`，网页刷新及多标签共用持久会话。
服务重启后保持暂停；未知动作先查同一游戏实例的回执，不盲目重复提交。

更新服务前先暂停，等模型请求取消及在途动作结算，再调用本机 `POST /api/service/stop`。
没有暂停或仍有工作时接口返回 409。旧服务退出后才替换文件、重启；关闭浏览器不会退出后台服务。
终局记录冻结预算，下一次“启动游戏”在无在途工作时归档旧账本；不要删除锁、账本或待定动作强行恢复。

无界面运行仍共用相同会话与锁，先关闭仪表盘服务：

```powershell
.\scripts\run-agent.ps1 -Command run -MaxActions 20
```

遇到版本不符、存档不全、非法动作或推演不可用时，先看网页错误与本地日志，再核对配置和上游构建。
不要用旧日志或历史测试数量判断当前版本已完成实机验证。
