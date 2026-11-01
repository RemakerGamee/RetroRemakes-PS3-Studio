# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
"""HTTPS update checks and verified staging. Applying happens in bootstrap.py."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import urllib.parse
import urllib.request
import zipfile

from . import VERSION

APP_ID = 'retroremakes-ps3-studio'
REPO_URL = 'https://github.com/RemakerGamee/RetroRemakes-PS3-Studio'
CHANNEL_URL = REPO_URL + '/releases/latest/download/ps3-studio.json'
MAX_PACKAGE = 20 * 1024 * 1024
TOP_FILES = {'README.md','RESEARCH.md','REQUIREMENTS.md','LICENSE.txt','LICENSE-GPL-2.0.txt','NOTICE.md','START-STUDIO.bat',
             'bootstrap.py','update_engine.py','tool-catalog.json','ONLINE-UPDATES.md','Install-Requirements.bat','PATCH-PACKS.md'}


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d+\.\d+\.\d+', value):
        raise ValueError('Invalid release version')
    return tuple(map(int, value.split('.')))


def https_url(url):
    p = urllib.parse.urlsplit(url)
    if p.scheme != 'https' or not p.hostname or p.username or p.password:
        raise ValueError('Updates require an HTTPS URL without credentials')
    return url


def fetch(url, maximum):
    req = urllib.request.Request(https_url(url), headers={'User-Agent':'RetroRemakes-PS3-Studio/'+VERSION,
                                                        'Cache-Control':'no-cache'})
    with urllib.request.urlopen(req, timeout=30) as response:
        https_url(response.geturl())
        content = response.read(maximum + 1)
    if len(content) > maximum:
        raise ValueError('Update response exceeds the size limit')
    return content


def validate_manifest(data):
    if not isinstance(data, dict) or data.get('app_id') != APP_ID or data.get('schema') != 1:
        raise ValueError('This update channel does not describe PS3 Studio')
    version(data.get('version'))
    if not re.fullmatch(r'[a-fA-F0-9]{64}', data.get('sha256', '')):
        raise ValueError('Update manifest has no valid SHA-256')
    if not isinstance(data.get('size'), int) or not 1 <= data['size'] <= MAX_PACKAGE:
        raise ValueError('Invalid package size')
    https_url(data.get('url',''))
    if not isinstance(data.get('root'), str) or not re.fullmatch(r'RetroRemakes-PS3-Studio-v\d+\.\d+\.\d+', data['root']):
        raise ValueError('Invalid package root')
    if data['root'] != 'RetroRemakes-PS3-Studio-v'+data['version']:
        raise ValueError('Package root and version disagree')
    return data


def check():
    if not CHANNEL_URL:
        raise ValueError('No update channel is set. Download new versions from ' + REPO_URL + '/releases')
    data = validate_manifest(json.loads(fetch(CHANNEL_URL, 128 * 1024)))
    return data, version(data['version']) > version(VERSION)


def validate_package(path, manifest):
    validate_manifest(manifest)
    path = Path(path)
    if path.stat().st_size != manifest['size'] or hashlib.sha256(path.read_bytes()).hexdigest() != manifest['sha256'].lower():
        raise ValueError('Downloaded update failed its size/SHA-256 check')
    files, seen, expanded = [], set(), 0
    with zipfile.ZipFile(path) as z:
        if len(z.infolist()) > 1000:
            raise ValueError('Too many files in update')
        for item in z.infolist():
            name = item.filename
            parts = PurePosixPath(name).parts
            if '\\' in name or ':' in name or name.startswith('/') or '..' in parts or not parts or parts[0] != manifest['root']:
                raise ValueError('Unsafe update archive path')
            if stat.S_ISLNK(item.external_attr >> 16):
                raise ValueError('Update contains a symbolic link')
            if item.is_dir():
                continue
            rel = '/'.join(parts[1:])
            if not rel or not (parts[1] in ('studio','tests') or rel in TOP_FILES):
                raise ValueError('Update attempts to write outside application files')
            if rel.casefold() in seen:
                raise ValueError('Duplicate update path')
            if any(p in ('__pycache__','.git','workspace','.studio-updates') for p in parts):
                raise ValueError('Update contains runtime or repository data')
            seen.add(rel.casefold());expanded += item.file_size
            if expanded > MAX_PACKAGE * 4:
                raise ValueError('Expanded update exceeds the size limit')
            files.append(rel)
        required = {'bootstrap.py','update_engine.py','studio/__init__.py','studio/__main__.py','studio/gui.py','studio/updater.py'}
        if not required.issubset(files):
            raise ValueError('Update is missing required application files')
        init = z.read(manifest['root']+'/studio/__init__.py').decode('utf-8')
        if not re.search(r'VERSION\s*=\s*[\"\']'+re.escape(manifest['version'])+r'[\"\']', init):
            raise ValueError('Package VERSION differs from the manifest')
        if z.testzip() is not None:
            raise ValueError('Update archive CRC failed')
    return files


def stage(app_root, manifest):
    if version(manifest['version']) <= version(VERSION):
        raise ValueError('This release is already installed or older')
    root = Path(app_root).resolve()
    folder = root / '.studio-updates'
    folder.mkdir(exist_ok=True)
    data = fetch(manifest['url'], MAX_PACKAGE)
    temp = folder/'download.tmp'
    temp.write_bytes(data)
    try:
        files = validate_package(temp, manifest)
        package = folder/'pending.zip'
        temp.replace(package)
        pending = {'manifest':manifest, 'files':files}
        tmp = folder/'pending.tmp'
        tmp.write_text(json.dumps(pending, indent=2),encoding='utf-8')
        tmp.replace(folder/'pending.json')
        return pending
    finally:
        temp.unlink(missing_ok=True)


def stage_local(app_root, zip_path):
    """Check an update ZIP the player downloaded and queue it for the next restart."""
    path = Path(zip_path)
    size = path.stat().st_size
    if not 1 <= size <= MAX_PACKAGE:
        raise ValueError('This file is not a PS3 Studio update (wrong size)')
    try:
        with zipfile.ZipFile(path) as z:
            roots = {PurePosixPath(i.filename).parts[0] for i in z.infolist() if PurePosixPath(i.filename).parts}
            if len(roots) != 1:
                raise ValueError('This ZIP is not a PS3 Studio update')
            root = roots.pop()
            init = z.read(root + '/studio/__init__.py').decode('utf-8')
    except (zipfile.BadZipFile, KeyError):
        raise ValueError('This ZIP is not a PS3 Studio update')
    found = re.search(r'VERSION\s*=\s*["\'](\d+\.\d+\.\d+)["\']', init)
    if not found:
        raise ValueError('This update has no version number')
    data = path.read_bytes()
    manifest = {'schema': 1, 'app_id': APP_ID, 'version': found.group(1), 'root': root, 'size': size,
                'sha256': hashlib.sha256(data).hexdigest(), 'url': REPO_URL + '/releases'}
    if version(manifest['version']) <= version(VERSION):
        raise ValueError(f"This ZIP is version {manifest['version']}, and version {VERSION} or newer is already installed")
    files = validate_package(path, manifest)
    folder = Path(app_root).resolve() / '.studio-updates'
    folder.mkdir(exist_ok=True)
    temp = folder / 'local.tmp'
    temp.write_bytes(data)
    temp.replace(folder / 'pending.zip')
    pending = {'manifest': manifest, 'files': files}
    tmp = folder / 'pending.tmp'
    tmp.write_text(json.dumps(pending, indent=2), encoding='utf-8')
    tmp.replace(folder / 'pending.json')
    return pending
