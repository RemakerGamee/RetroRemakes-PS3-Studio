# What you need - RetroRemakes PS3 Studio (Public Edition)

RetroRemakes PS3 Studio is a RetroRemakes product. This edition ships only RetroRemakes' program code.

> **Easiest way:** double-click **Install-Requirements.bat** (or press **Install everything needed** in Settings & setup). It installs every program in section 2, plus Node.js if you want it, from their official sources using Windows' built-in installer (winget), then opens the RPCS3 and firmware download pages. Restart your PC afterwards. The tables below are for checking, or for installing by hand.

You still provide RPCS3, the firmware and your games yourself (section 3).

## 1. Computer

| Item | Requirement |
|---|---|
| Operating system | Windows 10 or 11, 64-bit (Linux works for advanced users; see README) |
| Memory | 16 GB RAM or more recommended. Large games generate very large source files. |
| Disk | Plenty of free space on a fast drive. A single game's build can take tens of GB. |
| Internet | Needed for first-time setup and for pulling sources. |

## 2. Required programs

| Program | Version | Where to get it | Notes |
|---|---|---|---|
| **Python** | 3.11 or newer, 64-bit | https://www.python.org/downloads/windows/ | In the installer tick **Add python.exe to PATH**, and keep **tcl/tk and IDLE** and **py launcher** selected. |
| **Git** | Current | https://git-scm.com/downloads/win | Default options are fine. Used to download the recompiler and to apply fixes. |
| **CMake** | Current | https://cmake.org/download/ | Choose "Add CMake to the system PATH". |
| **Ninja** | Current | https://github.com/ninja-build/ninja/releases | Put `ninja.exe` in a folder on your PATH. |
| **Visual Studio 2022 Build Tools** | 2022 | https://visualstudio.microsoft.com/downloads/ | Select the **Desktop development with C++** workload, plus **Windows 11 (or 10) SDK** and **C++ Clang tools for Windows**. Clang (`clang-cl`) is required; MSVC alone is not enough. |

After installing, restart your PC so the new PATH entries take effect.

## 3. Things you provide yourself (not included)

| What | Required? | How to get it |
|---|---|---|
| **Your own PS3 games** | Required | Dump discs or digital games you own (for example with a PS3 and a dumping tool, or a compatible Blu-ray drive). Keep them as extracted folders (`PS3_GAME\USRDIR\...`). |
| **RPCS3** (the PS3 emulator) | Required | Download the latest Windows build from https://rpcs3.net/download and extract it to its own folder. The Studio uses it to decrypt your EBOOT, provide system files and run reference tests. |
| **PS3 system firmware** (`PS3UPDAT.PUP`) | Required | Download the official file from PlayStation's system-update page (https://www.playstation.com/en-us/support/hardware/ps3/system-software/). Install it in RPCS3 with **File > Install Firmware**, and keep the `.pup` file in a folder you point the Studio at. |
| **Decrypted game EBOOT** (`EBOOT.elf`) | Required | Made with your RPCS3: **Utilities > Decrypt PS3 Binaries** on `PS3_GAME\USRDIR\EBOOT.BIN`. Digital games need their `.rap` license installed in RPCS3 first. |
| **Game updates** (`.pkg`) | Sometimes | Some built-in fixes target a specific game version. Install the matching official update in RPCS3; the Studio tells you when a version is needed. |
| **Fulton** (community PS3 source library) | Optional, recommended | Install from https://github.com/TriwindBryan/fulton. The **Fulton sources** tab reads your installed Fulton library and pulls the projects you choose. Nothing is pulled automatically unless you turn that on. |
| **Cartridge** (multi-console game identifier) | Optional | Download https://github.com/TriwindBryan/cartridge (Code > Download ZIP) and extract it as a folder named `cartridge` next to the Studio folder, or set the environment variable `RR_CARTRIDGE_DIR` to where you put it. Also install **Node.js LTS** from https://nodejs.org/. |
| **An AI service** | Optional | Only for "Ask AI" in the Fix assistant. Any service with a chat-completions style API, online (your own account and API key) or a local server on your PC. You can always fix games by hand instead. |

## 4. Downloaded by the Studio during setup

When you press **Install backend + Python dependencies**, the Studio downloads the open-source **ps3recomp** recompiler (https://github.com/sp00nznet/ps3recomp) at a pinned version, applies RetroRemakes' fixes on top, and creates a private Python environment with its requirements. This needs internet access once.

## 5. Check that it all works

1. Run **START-STUDIO.bat**.
2. **Settings & setup**: set the RPCS3 executable and PS3 firmware folder, **Save settings**.
3. Press **Check environment**. Every line should be OK. If Clang or CMake is missing, install it as above, restart your PC and reopen the Studio from START-STUDIO.bat.

## Never shipped, never uploaded

The Studio never includes or uploads games, firmware, keys, `.rap` licenses or RPCS3 files. The Fix assistant sends only build reports and log text, and only when you press Send.
