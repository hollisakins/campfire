#!/usr/bin/env node
// Copy the campfire-etc noise models (etc/campfire_etc/models/*.json) into
// web/public/etc/models/ so the hidden /nirspec/etc calculator can fetch
// them as static assets. Vercel builds from web/ alone, so the copies are
// committed; the model files are versioned data that never change once
// published, and `--check` fails when the copies drift from the source.
//
//   node scripts/sync-etc-models.mjs          # copy
//   node scripts/sync-etc-models.mjs --check  # exit 1 if out of date

import { readdirSync, readFileSync, writeFileSync, mkdirSync, existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const SRC = path.resolve(here, '..', '..', 'etc', 'campfire_etc', 'models');
const DST = path.resolve(here, '..', 'public', 'etc', 'models');
const check = process.argv.includes('--check');

if (!existsSync(SRC)) {
  console.error(`sync-etc-models: source directory not found: ${SRC}`);
  process.exit(1);
}
mkdirSync(DST, { recursive: true });

const files = readdirSync(SRC).filter((f) => f.endsWith('.json')).sort();
let stale = 0;
for (const f of files) {
  const src = readFileSync(path.join(SRC, f), 'utf8');
  const dstPath = path.join(DST, f);
  const cur = existsSync(dstPath) ? readFileSync(dstPath, 'utf8') : null;
  if (cur === src) continue;
  if (check) {
    console.error(`sync-etc-models: ${f} is ${cur === null ? 'missing' : 'out of date'} in web/public/etc/models`);
    stale++;
  } else {
    writeFileSync(dstPath, src);
    console.log(`sync-etc-models: wrote ${path.relative(process.cwd(), dstPath)}`);
  }
}
if (check) {
  if (stale) {
    console.error('run `npm run etc-models` in web/ and commit the result');
    process.exit(1);
  }
  console.log(`sync-etc-models: ${files.length} model file(s) up to date`);
}
