RetroRemakes PS3 Studio - Research & limits

RetroRemakes PS3 Studio is a RetroRemakes product.

How it works
The Studio uses the open-source ps3recomp recompiler, downloaded to your PC during setup, plus RetroRemakes' own runtime fixes. It translates a game's PPU code to C++, finds and translates SPU programs it can identify, bridges PS3 system library calls, and compiles a native Windows program. Your copy of RPCS3 is used to decrypt your EBOOT, to provide your own installed firmware files and as a reference to compare against.

What to expect
- Any decrypted PS3 executable can be attempted. There is no list of supported games.
- A successful compile does not mean the game is playable. Graphics (RSX), SPU workloads and system behaviour still need testing and often fixes for each game.
- Failed attempts are kept so you can inspect, fix and rebuild them. Use the Fix assistant tab to fix by hand or with an AI service of your choice.
- Community projects from your Fulton library are references and starting points. Downloading one does not mean it builds with this Studio.

Tools & sources
The Tools & sources list (tool-catalog.json) describes related open-source tools and projects, their roles and limits. Listing a project is not an endorsement or a compatibility claim. Each project keeps its own license.

Never shared
The Studio never ships or uploads games, firmware, keys or RPCS3 files. Compiled ports and generated source contain code from your own game and must not be shared.
