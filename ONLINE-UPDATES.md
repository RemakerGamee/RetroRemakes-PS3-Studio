# Updating RetroRemakes PS3 Studio

RetroRemakes PS3 Studio is a RetroRemakes product. New versions are published at https://github.com/RemakerGamee/RetroRemakes-PS3-Studio/releases

## Automatic

Open **Online updates** and press **Check for updates** (or tick "Check for updates when Studio opens"). The Studio reads `ps3-studio.json` from the newest release, downloads the update ZIP, checks its size and SHA-256, and installs it on the next restart.

## By hand

1. Download `RetroRemakes-PS3-Studio-vX.Y.Z.zip` from the releases page.
2. Open **Online updates**, press **Install update from ZIP…** and choose the file.
3. Press **Restart to apply**.

The ZIP is checked before anything changes: it must be a PS3 Studio package, newer than what you have, and contain only program files.

## What is kept

Updates replace program files only. Previous program files are backed up first. Your `workspace` folder (settings, games, builds, logs, AI settings) is never touched.
