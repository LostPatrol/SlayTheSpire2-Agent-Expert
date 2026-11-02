<!-- Machine setup, save selection, and persistent controller operation. -->
# 运行手册

首次安装见 [中文 README](../README.zh-CN.md)。以下命令在项目根目录使用 PowerShell 执行。

## 配置与存档

`setup.ps1` 从模板创建私人配置，已有文件不会被覆盖：

| 文件或字段 | 用途 |
|---|---|
| `configs/machine.json: game_root` | 本机 Steam 游戏安装目录 |
| `configs/machine.json: client_id` | 自己的 Steam 全解锁主档或独立副本所在账号目录 |
| `configs/machine.json: api_port` | 游戏桥端口，默认 18765，与 Agent 的 `bridge.base_url` 对应 |
| `configs/bridge.settings.json` | 本机 Mod 设置 |
| `configs/a0-simulation.json` | 日常 Agent、推演与预算设置 |
| `secrets/apikey-deepseek.txt` / `DEEPSEEK_API_KEY` | 模型 API 密钥 |

存档位于 `%APPDATA%\SlayTheSpire2\default\<client_id>\modded\profile<编号>\saves`。
编号由账号目录的 `profile.save` 选择，默认 profile1。核对实际 modded profile 已全解锁，不使用新档或部分解锁档。

```powershell
.\.venv\Scripts\python.exe -m sts2_agent.cli --config configs/a0-simulation.json ensure-save --check
```

检查不修改存档。若需自动恢复，自行准备已核验的全解锁 `progress.save`，放入 `local/saves/full-unlock/`。
仅在游戏关闭后恢复，替换前备份到 `local/game-saves-backup/`；项目不提供或上传个人存档。

## 构建与启动

`build-bridge.ps1` 构建并安装游戏桥，安装前关闭游戏；旧 Mod 备份在 `local/mod-backups/`。
游戏运行时可用 `-SkipInstall` 仅构建，关闭后运行 `install-built-bridge.ps1` 安装准备好的包。
推演产物位于 `local/simulation/mentor-native/`，首次构建使用 `-FetchDependencies`，之后可离线构建。

双击 **启动仪表盘.cmd**，依次点击“启动游戏”和“开始自动游玩”。也可运行：

```powershell
.\scripts\start-dashboard.ps1 -Config configs/a0-simulation.json -Port 18766
```

服务位于 `http://127.0.0.1:18766`，启动后保持暂停；`-NoBrowser` 禁止自动打开浏览器。
暂停或停止会取消未完成的模型生成，已提交动作继续核对回执；单步只放行一个真实动作。
人工指导可按下一次决策、本场战斗或整局生效，使用后的对局标记为 `human_assisted`。

## 会话与更新

默认推演配置累计上限为 1000 次模型请求、1000 个动作、600 分钟。重试也计请求，暂停和重启不退还已用预算。
运行日志在 `runs/`，会话与设置在 `local/`；网页刷新和服务重启保留账本，未知动作先查回执。

更新服务：暂停 → 等模型取消和动作结算 → 调用本机 `POST /api/service/stop` → 等服务退出 → 替换文件并重启。
关闭浏览器不会退出后台服务。终局账本在下一次启动游戏时归档，不删除锁或待定动作强行恢复。

CLI 策略循环与网页共用会话，不能同时控制游戏。无界面运行前先关闭仪表盘服务：

```powershell
.\scripts\run-agent.ps1 -Command run -MaxActions 20
```
