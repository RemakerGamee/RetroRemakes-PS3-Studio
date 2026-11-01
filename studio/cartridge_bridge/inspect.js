// RetroRemakes PS3 Studio
// Copyright (c) 2026 RetroRemakes. All rights reserved.
// Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
'use strict';
const fs = require('fs');
const path = require('path');
const rom = require(process.env.RR_CARTRIDGE_ROM || './rom.js');
(async () => {
  let file = process.argv[2];
  if (fs.statSync(file).isDirectory()) {
    const marker = rom.dumpMarker(file, fs.readdirSync(file));
    if (!marker) throw Error('No recognized extracted-game marker found in this folder.');
    file = marker;
  }
  const info = await rom.inspectRom(file);
  if (!info || !info.system) throw Error('Unrecognized game input.');
  delete info.data;
  delete info.alt;
  process.stdout.write(JSON.stringify(info));
})().catch(e => { process.stderr.write(e.message); process.exitCode = 1; });
