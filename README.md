# RetroRemakes PS3 Studio (Public Edition) v0.1.2

**RetroRemakes PS3 Studio is a RetroRemakes product. Copyright (c) 2026 RetroRemakes.**

PS3 Studio is a desktop workbench for turning PlayStation 3 games you own into native PC builds using static recompilation. You add your own game, the Studio inspects it, generates source code, compiles it, and helps you test and fix it. Every build happens on your own PC from your own files.

This public edition ships **only RetroRemakes' own program code**. It contains no games, no firmware, no decryption keys, no RPCS3 program files and no third-party source libraries. You provide all of those yourself. **Read REQUIREMENTS.md before you start** - it lists exactly what to install and where to get it.

> This is experimental software. A successful compile is not a playable game. Many titles need several rounds of fixes, and some will not work yet.

## Quick start (Windows 10/11, 64-bit)

1. Download the latest release from https://github.com/RemakerGamee/RetroRemakes-PS3-Studio/releases and extract it to a folder you can write to, for example `D:\RetroRemakes\PS3-Studio`. Keep your games in a different folder.
2. Double-click **Install-Requirements.bat**. It installs Python, Git, CMake, Ninja and the Visual Studio Build Tools (with the right C++, Windows SDK and Clang parts) from their official sources, skipping anything you already have. Then **restart your PC**.
3. Get **RPCS3** and the **PS3 firmware** yourself (the installer opens both download pages; details in REQUIREMENTS.md).
4. Double-click **START-STUDIO.bat**.
5. Open **Settings & setup**:
   - **RPCS3 executable**: browse to your `rpcs3.exe`.
   - **PS3 firmware folder**: the folder holding your `PS3UPDAT.PUP`. Install that same file in RPCS3 first (RPCS3 > File > Install Firmware) so your own `dev_flash` exists.
   - Click **Save settings**, then **Install backend + Python dependencies** (downloads the recompiler source from its public GitHub page to your PC), then **Check environment**. Fix anything it reports before continuing.
6. Optional: open **Fulton sources** and press **Sync now** / **Pull selected sources** to fetch community PS3 projects from your own Fulton library. Nothing is pulled automatically unless you tick the automatic option.
7. Add your game on **Game queue & build**: an extracted game folder that contains a **decrypted** `EBOOT.elf` (see "Decrypting your EBOOT" below), or the decrypted ELF itself.
8. **Inspect** (F5), then **Attempt native build** (F6), then **Test native build**.
9. If something fails, use the **Fix assistant** tab.

## Decrypting your EBOOT

This edition does not decrypt games. Use your own RPCS3:

1. In RPCS3 choose **Utilities > Decrypt PS3 Binaries**.
2. Select the game's `PS3_GAME\USRDIR\EBOOT.BIN` (and any `.sprx`/`.self` files the Studio asks for). If the game is a digital copy, its license file (`.rap`) must already be installed in RPCS3.
3. RPCS3 writes `EBOOT.elf` next to `EBOOT.BIN`. Add that folder to the Studio, or use **Set decrypted ELF**.

## Fixing games: your choice of AI or by hand

Open the **Fix assistant** tab, pick a game and press **Diagnose**. You will see the build status, the end of the newest build log and the result of the last test run. Then choose:

**I'll fix it myself**
- Open the attempt folder, build log or test log with one click.
- The tab lists where fixes usually go (function tables in `loader/`, `port/main.cpp`, generated sources, or RPCS3-format game patches you supply).
- Edit, save, then press **Rebuild with my changes**. Your edits are kept.

**Ask AI for a fix**
- Works with any AI service that offers a chat-completions style API, online or running locally on your PC. You supply your own account and key; RetroRemakes does not provide or pay for AI access.
- Enter the **Service address** (for example `https://<your-provider>/v1` or `http://localhost:<port>/v1` for a local server), the **Model** name, and your **API key** (leave the key blank for local servers). Press **Save AI settings**. These stay in your own workspace.
- Press **Preview request** to see exactly what will be sent: the build report, the last 120 log lines and the test summary. No game files, firmware or keys are ever sent.
- Press **Send**. If the reply contains a patch, review it, then press **Apply suggested patch**. Every touched file is backed up first; **Undo last patch** restores it.
- Press **Rebuild with this fix** and test again.

You can switch between the two modes at any time.

## Main controls

- **Inspect (F5)**: metadata, input hash, ELF check and file list.
- **Attempt native build (F6)**: generates sources, configures and compiles. Every attempt gets its own folder.
- **Rebuild existing attempt (keep fixes)**: recompiles your edited attempt without regenerating it.
- **Test native build**: runs the build and records `native-play.log` and `native-test.json`.
- **Test in RPCS3**: runs the original game in your own RPCS3 for comparison.
- **Apply fixes to selected game / games**: applies RetroRemakes' built-in runtime fixes and rebuilds.
- **Stop (Escape)**: cancels the active job and keeps its logs.
- **Export diagnostics**: a ZIP of reports and logs. Check it for personal paths before sharing.

## Updating

- **Automatic:** open **Online updates** and press **Check for updates**. The Studio finds the newest GitHub release, downloads and verifies it, and installs it when you restart.
- **By hand:** download the update ZIP from the releases page, open **Online updates**, press **Install update from ZIP…** and pick the file. It is checked the same way and installed on restart.

Updates only replace program files. Your `workspace` folder is never touched, and the old program files are backed up first.

## Problems and questions

Open an issue at https://github.com/RemakerGamee/RetroRemakes-PS3-Studio/issues. Leave out personal paths, and never attach game files.

## Where your data lives

By default everything (settings, queue, builds, logs, your AI settings) is stored in the `workspace` folder next to this README. To use another drive, set the environment variable `RETROREMAKES_PS3_HOME` to a folder outside your games folder. Keep this folder when you update the Studio.

**Never share** a compiled port, its generated source or anything from your workspace. They contain code from your copy of the game.

## Command line

```text
python -m studio --home D:\PS3-Workspace doctor
python -m studio --home D:\PS3-Workspace setup
python -m studio --home D:\PS3-Workspace inspect "D:\PS3 Games\Example"
python -m studio --home D:\PS3-Workspace build "D:\PS3 Games\Example\PS3_GAME\USRDIR\EBOOT.elf"
python -m unittest discover -s tests -v
```

## Ownership, license and credits

RetroRemakes PS3 Studio, its interface, build pipeline, fix assistant and runtime fixes are the work of **RetroRemakes**. See LICENSE.txt.

Some backend fix files build on open-source projects (ps3recomp, RPCS3 and others). Those portions keep their original authors' copyright and licenses, which the licenses themselves require to stay intact. Because a few files contain code derived from RPCS3 (GPL-2.0-only), the Studio as a whole is distributed under GPL-2.0-only; see NOTICE.md and LICENSE-GPL-2.0.txt.

RPCS3, Fulton, Cartridge, ps3recomp and all game titles belong to their respective owners. RetroRemakes is not affiliated with Sony Interactive Entertainment. "PlayStation" and "PS3" are trademarks of Sony Interactive Entertainment Inc.
