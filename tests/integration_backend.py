# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
"""Optional real upstream integration check; no retail game files required.
Run: python tests/integration_backend.py /path/to/ps3recomp
Requires the backend's Python dependencies in the executing interpreter.
"""
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from studio.core import Studio
from test_core import make_elf


def main():
    backend = Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory(prefix='ps3-studio-integration-') as tmp:
        base = Path(tmp)
        for with_opd in (True, False):
            game = base / ('with-opd' if with_opd else 'heuristic-fallback')
            game.mkdir()
            elf = make_elf(game / 'EBOOT.ELF', opd=with_opd)
            studio = Studio(base / ('workspace-opd' if with_opd else 'workspace-heuristic'))
            studio.state['settings']['backend'] = str(backend)
            p = studio.add(elf)
            report = studio.build(p, stop_after='lift')
            assert report['status'] == 'sources generated'
            assert len(list((Path(p['attempt']) / 'port/recompiled').glob('*.cpp'))) > 0
            if not with_opd:
                assert report['function_boundaries'] == 'heuristic; requires verification'
            print('PASS', game.name, report['backend']['revision'])
    print('No gameplay compatibility is established by this synthetic fixture.')

if __name__ == '__main__':
    main()
