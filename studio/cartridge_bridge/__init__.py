# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
"""Cartridge catalog and optional upstream inspector bridge for RetroRemakes."""
from pathlib import Path
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
UPSTREAM = 'https://github.com/TriwindBryan/cartridge'
COMMIT = '9045086aaaa9ed9d09f757fb0fdbce707d6af870'
SYSTEMS = {'xbox360': 'x360'}
APP_ROOT = ROOT.parents[1]


def cartridge_dir():
    """The player's own Cartridge download (not shipped with the Studio)."""
    for base in (os.environ.get('RR_CARTRIDGE_DIR'), APP_ROOT / 'cartridge', APP_ROOT.parent / 'cartridge'):
        if base and (Path(base) / 'src' / 'core' / 'rom.js').is_file():
            return Path(base)
    return None


def cartridge_file(rel):
    base = cartridge_dir()
    return base / rel if base and (base / rel).is_file() else None


def state_path(console):
    base = Path(os.environ.get('LOCALAPPDATA', str(Path.home() / '.local/share')))
    return base / 'RetroRemakes' / 'CartridgeBridge' / console / 'settings.json'


def load_state(path):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='settings-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, indent=2)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def catalog(console, query=''):
    path = cartridge_file('catalog/games.json')
    if not path:
        return []
    data = json.loads(path.read_text(encoding='utf-8'))
    system = SYSTEMS.get(console, console)
    return [g for g in data['games'] if g.get('system') == system
            and query.casefold() in json.dumps(g, ensure_ascii=False).casefold()]


def repo_url(game):
    repo = game.get('repo', '')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*/[A-Za-z0-9][A-Za-z0-9_.-]*', repo):
        raise ValueError('This catalog entry has no official GitHub repository.')
    return 'https://github.com/' + repo


def normalize_n64(data):
    if len(data) < 64 or len(data) % 4:
        raise ValueError('N64 input must contain a complete header and whole 32-bit words.')
    magic = data[:4]
    if magic == bytes.fromhex('80371240'):
        return bytes(data), 'z64'
    if magic == bytes.fromhex('37804012'):
        result = bytearray(data)
        result[0::2], result[1::2] = data[1::2], data[0::2]
        return bytes(result), 'v64'
    if magic == bytes.fromhex('40123780'):
        result = bytearray(data)
        for i in range(4):
            result[i::4] = data[3-i::4]
        return bytes(result), 'n64'
    raise ValueError('Unrecognized N64 byte-order signature.')


def inspect_n64(path):
    p = Path(path)
    if p.stat().st_size > 256 * 1024 * 1024:
        raise ValueError('Input is too large for the N64 header inspector (256 MiB).')
    data, order = normalize_n64(p.read_bytes())
    return {'system': 'n64', 'format': order, 'size': len(data),
            'internalName': data[32:52].decode('ascii', errors='replace').strip(' \0'),
            'regionByte': data[62], 'revision': data[63],
            'sha1': hashlib.sha1(data).hexdigest().upper(),
            'sha256': hashlib.sha256(data).hexdigest(),
            'engine': 'RetroRemakes N64 inspector', 'gameplay_verified': False}


def inspect(path, console, node=None):
    p = Path(path).resolve(strict=True)
    if p.is_file() and p.suffix.lower() in ('.z64', '.v64', '.n64'):
        result = inspect_n64(p)
    else:
        node = node or shutil.which('node')
        if not node:
            raise ValueError('Install Node.js LTS to use Cartridge’s multi-console inspector. N64 inspection works without Node.js.')
        if p.is_file() and p.stat().st_size > 256 * 1024 * 1024 and p.suffix.lower() not in ('.iso', '.pkg', '.chd', '.xex', '.xbe', '.pbp', '.cso', '.rvz', '.gcm'):
            raise ValueError('Choose a recognized disc/package file or extracted game folder.')
        rom = cartridge_file('src/core/rom.js')
        if not rom:
            raise ValueError('Cartridge was not found. Download it (see REQUIREMENTS.md) into a "cartridge" folder next to the Studio, or set RR_CARTRIDGE_DIR.')
        completed = subprocess.run([str(node), str(ROOT / 'inspect.js'), str(p)],
                                   capture_output=True, text=True, timeout=120, check=False,
                                   env=dict(os.environ, RR_CARTRIDGE_ROM=str(rom)))
        if completed.returncode:
            raise ValueError(completed.stderr.strip() or 'Cartridge could not identify this input.')
        result = json.loads(completed.stdout)
        result['engine'] = 'Cartridge rom.js (your copy)'
    result['path'] = str(p)
    result['expected_system'] = SYSTEMS.get(console, console)
    result['system_matches_studio'] = result.get('system') == result['expected_system']
    sha1 = result.get('sha1', '').upper() if result.get('sha1') else ''
    result['exact_catalog_hash_matches'] = [g['title'] for g in catalog(console)
        if sha1 and sha1 in [s.upper() for s in g.get('rom', {}).get('sha1', [])]]
    result['version_verified_for_port'] = bool(result['exact_catalog_hash_matches'])
    result['gameplay_verified'] = False
    result['note'] = 'File recognition is not a native build or gameplay result. Only explicit catalog SHA-1 matches verify a port input revision.'
    return result


def convert_n64(source, destination):
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve():
        raise ValueError('Choose a separate output file; the original is preserved.')
    if destination.suffix.lower() != '.z64':
        raise ValueError('Choose an output ending in .z64.')
    if source.stat().st_size > 256 * 1024 * 1024:
        raise ValueError('N64 input exceeds 256 MiB.')
    data, _ = normalize_n64(source.read_bytes())
    # Exclusive creation prevents accidental replacement of an existing ROM.
    with destination.open('xb') as f:
        f.write(data)
    return {'path': str(destination), 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def launch_command(path):
    p = Path(path).expanduser().resolve(strict=True)
    if sys.platform == 'darwin' and p.is_dir() and p.suffix == '.app':
        return ['open', '-W', '-n', str(p)], p.parent
    if not p.is_file():
        raise ValueError('Select a local native executable.')
    if os.name == 'nt':
        if p.suffix.lower() != '.exe':
            raise ValueError('Select a Windows .exe. Command scripts are not launched here.')
    elif not os.access(p, os.X_OK):
        raise ValueError('Selected file is not executable on this system.')
    return [str(p)], p.parent
