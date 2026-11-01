# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
"""Application-only install transaction, run before importing Studio GUI modules."""
import json
from pathlib import Path
import shutil
import uuid


def apply_pending(app_root):
    root = Path(app_root).resolve()
    folder = root/'.studio-updates'
    pending = folder/'pending.json'
    if not pending.exists():
        return None
    # The staged package is revalidated immediately before applying it.
    from studio.updater import validate_package
    import zipfile
    data = json.loads(pending.read_text(encoding='utf-8'))
    package = folder/'pending.zip'
    files = validate_package(package, data['manifest'])
    backup = folder/('backup-'+uuid.uuid4().hex[:12]);backup.mkdir()
    changed = []
    try:
        with zipfile.ZipFile(package) as z:
            # Preflight every path before modifying any application file.
            for rel in files:
                target = root/rel
                if not target.resolve().is_relative_to(root) or target.is_symlink() or any(p.is_symlink() for p in target.parents if p != root):
                    raise ValueError('Update destination escapes the application directory')
                if target.exists() and not target.is_file():
                    raise ValueError('Update target is not a regular file')
            for rel in files:
                target = root/rel;old = backup/rel
                existed = target.is_file()
                if existed:
                    old.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(target,old)
                changed.append((rel,existed))
                target.parent.mkdir(parents=True,exist_ok=True)
                temp=target.with_name(target.name+'.update-tmp')
                temp.write_bytes(z.read(data['manifest']['root']+'/'+rel));temp.replace(target)
        result={'version':data['manifest']['version'],'status':'installed','backup':str(backup),'files':files}
        (folder/'last-update.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
        pending.unlink();package.unlink()
        return result
    except Exception:
        for rel,existed in reversed(changed):
            target=root/rel
            if existed:shutil.copy2(backup/rel,target)
            else:target.unlink(missing_ok=True)
            target.with_name(target.name+'.update-tmp').unlink(missing_ok=True)
        raise
