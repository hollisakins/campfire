# design-sync notes — Campfire UI

Repo-specific gotchas for syncing `web/components/ui` to the "Campfire UI" Claude
Design project (`projectId` in config.json). This is a **Next.js app, not a
published component library**, so the converter runs in **synth-entry mode**
(no `dist/` — it synthesizes an entry from source).

## Re-sync setup (do these before `package-build`, every fresh clone)

1. **Self-symlink so PKG_DIR resolves.** The converter needs
   `node_modules/<pkg>/package.json` to exist even in synth mode
   (`exportedNames` reads it unguarded). Create:
   ```sh
   ln -sfn .. web/node_modules/campfire-web
   ```
   `web/package.json` has no `main`/`module`/`exports`, so the dist resolver
   still returns null → synth path triggers. The symlink is gitignored
   (node_modules), so recreate it per clone.
2. **Regenerate the cssEntry** (`web/.ds-sync-styles.css`, gitignored):
   ```sh
   bash .design-sync/build-css.sh
   ```
   It compiles Tailwind utilities + token vars (`:root`/`.dark` from
   `app/globals.css`) and prepends the brand-font block (`css-header.css`).
   `cfg.cssEntry` is bounded to the **package root (web/)**, which is why the
   output lives under `web/`, not under `.design-sync/`.
3. Then the standard staged-script copy + build:
   ```sh
   node .ds-sync/package-build.mjs --config .design-sync/config.json \
     --node-modules web/node_modules --out ./ds-bundle
   ```

## Config gotchas

- **Paths are PKG_DIR-relative** (`resolve(PKG_DIR, rel)`), NOT cwd-relative.
  PKG_DIR = `web/node_modules/campfire-web`. So `cfg.tsconfig` =
  `../../../.design-sync/tsconfig.sync.json` and `cfg.cssEntry` =
  `../../.ds-sync-styles.css`. `cfg.srcDir` = `components/ui` (resolves through
  the symlink to `web/components/ui`).
- **`next/link` is shimmed.** Two components (`Breadcrumbs`, `FilterChip`) import
  `next/link`. Bundling real `next` is wrong (no router) and bloated (244KB→110KB
  after shimming). The shim (`.design-sync/shims/next-link.tsx`, renders a plain
  `<a>`) is wired via a `paths` alias in `.design-sync/tsconfig.sync.json`, which
  the bundle's tsconfig-paths plugin resolves.
- **Do NOT put a `"//"` comment key in `tsconfig.sync.json`.** The plugin's
  comment-stripper regex mangles `"//":` (the `"` before `//` matches `[^:]`),
  corrupting the JSON so the whole paths plugin silently no-ops → `next/link`
  falls back to node_modules and `next` gets bundled. Use `_note` instead.
- `@/*` resolves two ways: the sync tsconfig's `paths`, AND esbuild's own
  auto-discovery of `web/tsconfig.json` for each source file. Both point to
  `web/`, so they agree.

## Rendering / fonts

- **Fonts load remotely** (`[FONT_REMOTE]`): `css-header.css` `@import`s Inter +
  JetBrains Mono from Google Fonts and defines `--font-sans`/`--font-mono`
  (the app sets these via `next/font` at runtime, which previews can't do).
  Acceptable; not self-hosted. To self-host later, download the woff2 and wire
  `cfg.extraFonts`.
- Render check was **skipped** (`--no-render-check`) — Playwright/Chromium not
  installed. Verification is by human browser review of `.review.html`. If a
  future sync installs Playwright, drop `--no-render-check`.

## Component-specific

- **ThemeToggle** reads `useTheme()` from `@/lib/contexts/ThemeContext`. (See
  whether it needs `cfg.provider` = ThemeProvider, or renders fine standalone —
  resolve during preview authoring.)
- Dropdown/chip components (`RangeFilterChip`, `CoordinateSearchChip`,
  `ColumnVisibilityDropdown`, `MultiSelect`, `FilterChip`, `QueryBuilder`,
  `InlineMultiFilter`) are interaction-driven; `isOpen` defaults to closed with
  no prop to force-open, so previews render the closed trigger/chip state.
- `Tabs` is compound: `TabsList`/`TabsTrigger`/`TabsContent` throw outside a
  `Tabs` parent — author their previews as the full `Tabs` composition.
- 23 exports from 20 files (Tabs file exports 4). `Badge` is a **stat** badge
  (`value` + `label`), not a tag/label chip.

## Decisions (user-confirmed)

- **Dropdown/chip components render closed** with a small "▾ Click to open…"
  caption in each preview card (user asked for the open-on-click behavior to be
  noted prominently). The open panel is interaction-only and can't render
  statically. Captions live in the authored `previews/*.tsx`.
- **Fonts stay remote** (Google Fonts `@import`); user OK'd not self-hosting.
- **Props come from `cfg.dtsPropsFor`** (hand-written, all 23). Synth mode has no
  `.d.ts`, and `loadDts` only ingests `.d.ts`, so auto-extraction yields
  `[key: string]: unknown`. If a component's real props change, update its
  `dtsPropsFor` entry. (A future improvement: fork `dts.mjs` to add `.tsx`
  sources to the ts-morph project — would auto-extract instead.)

## Re-sync risks (watch-list)

- `dtsPropsFor` is hand-maintained — it can silently drift from the real
  component props. Re-check against source on each sync.

- The compiled cssEntry is regenerated, not committed — if `build-css.sh` isn't
  run, the build copies a stale/missing stylesheet. Always run step 2.
- Remote Google-Fonts dependency: if the font host changes URLs, previews fall
  back to system fonts silently.
- The self-symlink and `web/.ds-sync-styles.css` are gitignored; both must be
  recreated on a fresh clone (steps 1–2).
