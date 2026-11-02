<!-- Optional animation acceleration setup and rollback. -->
# SpeedX 加速

可选使用 [SpeedX 0.11.12 / ModConfig 0.2.3](https://github.com/xhyrzldf/sts2-pp-mod-release) 加速动画，目标游戏版本为 v0.107.1。

关闭游戏后，指定自己的安装目录：

```powershell
.\scripts\install-fast-mode.ps1 -GameRoot 'C:\Program Files (x86)\Steam\steamapps\common\Slay the Spire 2'
```

脚本校验固定安装包，备份已有文件到 `local/mod-backups/`，并应用 `configs/speedx-agent.json`。
预设启用 20x 动画加速，关闭自动出牌、结束回合、领奖、目标选择和伤害预览，游戏动作仍由 Agent 控制。
只更新预设可加 `-ConfigureOnly`；配置界面位于游戏的“设置 → Mod Configuration → SpeedX”。

修改预设从 `configs/speedx-agent.json` 入手，安装与备份流程在 `scripts/install-fast-mode.ps1`。
应用配置后重启游戏。移除时先关闭游戏，将 `mods/SpeedX` 和 `mods/ModConfig` 移出游戏的 mods 目录，
按需恢复原备份；备份目录不要放在 mods 内。
