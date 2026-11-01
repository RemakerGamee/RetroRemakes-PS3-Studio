# Port help

- **Launch.bat** starts the game. It checks for an update first; turn that off with
  `"check_updates_on_start": false` in port.json.
- **Check-Updates.bat** checks and installs updates. Updates contain fixes only; the
  port is rebuilt on this PC from your own game files when an update needs it.
  The previous version is kept in `.port-backups`; `PortTools.ps1 -Action Rollback`
  restores it.
- **Report-Problem.bat** saves a support file (logs, settings and PC specs, no game
  files, user names removed) in `support\` and opens the support page. Attach the
  zip there.
