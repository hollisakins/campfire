#!/usr/bin/env node
// First-Load-JS budget per app route (perf T1-7 / #503).
//
// Reads .next/app-build-manifest.json after `next build`, sums the gzipped
// size of every client chunk each page route loads (the same quantity Next
// prints as "First Load JS"), and fails when a route exceeds its budget.
// Run `npm run build && npm run budget`; CI does the same on pull requests.

import { readFileSync, statSync } from 'node:fs';
import { gzipSync } from 'node:zlib';
import path from 'node:path';

const NEXT_DIR = path.resolve(process.cwd(), '.next');
const DEFAULT_BUDGET_KB = 250;
// Routes allowed above the default, with the reason. Shrink, don't grow.
const OVERRIDES = {
  // Client-rendered program editorial (markdown parser in the bundle); the
  // /docs route moved to a server renderer, this one is the follow-up.
  '/nirspec/metadata/programs/[slug]': 285,
};

const manifest = JSON.parse(readFileSync(path.join(NEXT_DIR, 'app-build-manifest.json'), 'utf8'));
const gzCache = new Map();
function gzKb(file) {
  if (!gzCache.has(file)) {
    const abs = path.join(NEXT_DIR, file);
    try {
      statSync(abs);
      gzCache.set(file, gzipSync(readFileSync(abs), { level: 9 }).length / 1024);
    } catch {
      gzCache.set(file, 0);
    }
  }
  return gzCache.get(file);
}

const rows = [];
for (const [entry, files] of Object.entries(manifest.pages)) {
  if (!entry.endsWith('/page')) continue;
  const route = entry.slice(0, -'/page'.length) || '/';
  const js = [...new Set(files)].filter((f) => f.endsWith('.js'));
  const kb = js.reduce((sum, f) => sum + gzKb(f), 0);
  const budget = OVERRIDES[route] ?? DEFAULT_BUDGET_KB;
  rows.push({ route, kb, budget, over: kb > budget });
}
rows.sort((a, b) => b.kb - a.kb);

const pad = (s, n) => String(s).padEnd(n);
console.log(`${pad('route', 44)} ${'first-load gz kB'.padStart(16)} ${'budget'.padStart(7)}`);
for (const r of rows) {
  console.log(`${r.over ? '✗' : ' '} ${pad(r.route, 42)} ${r.kb.toFixed(1).padStart(16)} ${String(r.budget).padStart(7)}`);
}
// The Plotly preload hints (components/plot/PlotlyPreload.tsx) name loadable
// ids that the build assigns to LazyPlot's import() calls. A stale id
// preloads nothing, silently — so a build whose manifest no longer carries
// them fails here.
const loadable = JSON.parse(readFileSync(path.join(NEXT_DIR, 'react-loadable-manifest.json'), 'utf8'));
const plotlyIds = JSON.parse(readFileSync(path.resolve(process.cwd(), 'components/plot/plotly-loadable-ids.json'), 'utf8'));
const missingIds = plotlyIds.filter((id) => !(loadable[id]?.files?.length > 0));
if (missingIds.length > 0) {
  console.error(`\nPlotly preload ids missing from react-loadable-manifest.json: ${missingIds.join(', ')}\nUpdate components/plot/plotly-loadable-ids.json to the ids the manifest carries for LazyPlot.tsx.`);
  process.exit(1);
}
console.log(`Plotly preload ids present in the loadable manifest (${plotlyIds.length}).`);

const failures = rows.filter((r) => r.over);
if (failures.length > 0) {
  console.error(`\n${failures.length} route(s) over budget: ${failures.map((r) => `${r.route} (${r.kb.toFixed(0)} kB > ${r.budget})`).join(', ')}`);
  process.exit(1);
}
console.log(`\nAll ${rows.length} routes within budget.`);
