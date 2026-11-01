# Notices

RetroRemakes PS3 Studio - Copyright (c) 2026 RetroRemakes. All rights reserved.
The Studio's own code is RetroRemakes' work. The projects below are credited as their licenses require.

# Third-party notices

## Licensing

This build of PS3 Studio includes code derived from RPCS3, which is licensed
under the GNU General Public License version 2 only. As a combined work, PS3
Studio and its recompiler backend overlay are distributed under GPL-2.0-only
(see LICENSE-GPL-2.0.txt). Files that contain no RPCS3-derived code remain
available under the MIT License (LICENSE.txt) when used on their own.

If you distribute this software, you must make its complete source code
available under GPL-2.0-only.

## RPCS3

Copyright (C) RPCS3 contributors. https://github.com/RPCS3/rpcs3
Reference revision: 6c44fff7549e941389a4ce072a95c5ff9db7237c

Derived code (marked in each file with "Derived from RPCS3 ... GPL-2.0-only"):

- studio/game_versions.py (PKG reader)
- backend overlay: libs/spurs/cellSpurs.c
- backend overlay: libs/system/sysPrxForUser.c
- backend overlay: libs/system/cellGame.c
- backend overlay: runtime/syscalls/sys_memory.c (sys_memory_allocate_from_container)
- backend overlay: libs/system/cellSaveData.c (funcStat result handling)
- backend overlay: libs/system/cellSysutilNpEula.c (sceNpEulaCheckEulaStatus, sceNpEulaShowCurrentEula)
- backend overlay: libs/network/sceNp.c (offline NP manager/basic functions)
- backend overlay: libs/spurs/cellSpurs.c (SPURS 2 taskset functions)
- backend overlay: runtime/syscalls/lv2_register.c (SPU thread queue binding and stop service)

PS3 system function names in studio/ps3_function_names.txt were collected from
RPCS3's module registration tables.

## ps3recomp

MIT License, see studio/backend_overlay/LICENSE-ps3recomp.txt.

## Upstream recompiler projects (MIT)

Backend fixes may include code from:

- ps3recomp v0.18.0, Copyright (c) 2026 Ned Heller / sp00nznet and contributors,
  MIT License. https://github.com/sp00nznet/ps3recomp
- benjidotsh/ps3recomp, branch lbp (LittleBigPlanet runtime fixes: SPU
  stall-and-notify tracing, SPURS kernel trace, filesystem open flags,
  truncate/rename on the cellFs descriptor table, loopback networking),
  Copyright (c) 2026 Ned Heller / sp00nznet, MIT License.
  https://github.com/benjidotsh/ps3recomp
- Project Pyrrhus by undecuple (port/patches, the Killzone 2 runtime patch set
  for ps3recomp v0.18.0), Copyright (c) 2026 undecuple, MIT License.
  https://github.com/undecuple/project-pyrrhus — used only in the
  ps3recomp-v018-pyrrhus-rr candidate. Its patch headers name further MIT
  sources (Project Helghast by undecuple; ps3recomp pull requests by their
  authors), which are credited in that project's NOTICE.md.

MotorStorm: Monument Valley research notes by robbe1912
(https://github.com/robbe1912/motorstorm-mv-recomp, MIT) were read as
reference only; no code was copied from it.

## inFAMOUS 2 recompilation (erikomis/infamous2-recomp)

MIT License, Copyright (c) 2026 Erik Martins. https://github.com/erikomis/infamous2-recomp
Used in: backend overlay runtime/spu/spu_channels.c (resident-overlay lookup when
the interpreter returns into a lifted WWS job module).
Also: studio/wws_jobmod_recipes.py (scan for WWS job modules linked into the
EBOOT, after tools/extract_eboot_jobmods.py).

## ps3recomp fork (NoahBPeterson/ps3recomp, branch infamous/rebase-v0.13.0)

MIT License, Copyright (c) 2026 Ned Heller / sp00nznet and contributors.
https://github.com/NoahBPeterson/ps3recomp
Used in: backend overlay libs/spurs/cellSpurs.c (cellSpursEventFlagWait returns
the bits that satisfied the wait).
