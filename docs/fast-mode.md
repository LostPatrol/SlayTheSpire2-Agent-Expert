# SpeedX animation acceleration

The Steam installation uses the author's **SpeedX 0.11.12** and **ModConfig 0.2.3** packages. SpeedX declares support for game v0.107.1 and marks itself as not affecting gameplay. The official distribution is [xhyrzldf/sts2-pp-mod-release](https://github.com/xhyrzldf/sts2-pp-mod-release); the requested [catalogue entry](https://catalogue.smods.ru/archives/453152) identifies the same SpeedX mod.

With the game closed, run `scripts/install-fast-mode.ps1`. The installer checks pinned official archive hashes, backs up existing mod folders under `local/mod-backups/`, and copies only each mod's DLL, PCK and root manifest into the Steam game's `mods/` folder. It does not alter save progress. `start-game.ps1` preserves the user's existing mod list and enables these helpers when installed.

`configs/speedx-agent.json` is the reproducible Agent preset. The installer copies it to `%APPDATA%/SlayTheSpire2/ModConfig/sts2.piyixiajiuhenfen.speedx.json`. Use `-ConfigureOnly` to apply just that preset while the game is closed.

The preset enables 20x global speed, banner skipping, hit-stop removal and card/character/effect acceleration. SpeedX's automatic end turn, loot, proceed, target selection, quick play and damage previews are disabled. Gameplay actions therefore remain under the controller's observation/receipt loop; SpeedX supplies animation acceleration only. The multiplayer-specific acceleration is disabled for the current single-player setup.

In-game settings are available under **Settings → Mod Configuration → SpeedX**. Restart after applying the preset outside the game. The pinned version loaded and its settings were inspected in the native v0.107.1 main menu; whole-run compatibility is covered by the separate live acceptance run, not by this installation check.

To remove the helpers, close the game and move `mods/SpeedX` and `mods/ModConfig` outside the game's `mods/` directory. Restore any backed-up prior folders/configuration if present. Keep backup manifests outside `mods/`, because the game scans JSON files there as mod manifests.
