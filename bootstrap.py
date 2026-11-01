# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
"""Restart entry point: apply a staged update before loading the application."""
import os
from pathlib import Path
import sys
import subprocess

root=Path(__file__).resolve().parent
os.chdir(root)
if str(root) not in sys.path:sys.path.insert(0,str(root))
try:
    from update_engine import apply_pending
    result=apply_pending(root)
except Exception as exc:
    print('Update could not be installed; application files were restored:',exc)
    try:
        import tkinter.messagebox
        tkinter.messagebox.showerror('PS3 Studio update',str(exc)+'\nYour workspace was preserved. The update remains staged for retry.')
    except Exception:pass
# Run a fresh interpreter so no module imported for update validation stays stale.
if __name__=='__main__':
    raise SystemExit(subprocess.call([sys.executable,'-m','studio',*sys.argv[1:]]))
