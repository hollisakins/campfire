# etc/ — `campfire-etc`

The empirical NIRSpec/MSA exposure-time calculator and spectrum simulator,
fitted to the archive, plus its MCP server. `README.md` covers usage, the model,
the rebuild procedure and hosting.

```bash
pytest etc -q        # numpy + mcp only, ~5 s
```

## Rules

- **Keep the core numpy-only.** `model.py`, `sed.py` and `simulate.py` depend on
  numpy alone, so `uvx` can install the package straight from GitHub. Anything
  needing astropy or scipy goes behind the `[fits]` or `[build]` extra with a lazy
  import. The package is deliberately independent of `campfire-layout`.
- **Models are versioned data.** `campfire_etc/models/nirspec-<version>.json`
  files are never edited once published. A rebuild adds a new version
  (`campfire-etc build assemble --version <YYYY.MM>`, after re-running the harvest
  and fit steps; run order and pitfalls are in `README.md`) and updates
  `manifest.json`'s `latest`. Every tool result carries `model_version`.
- **Regression tests pin a model explicitly.** `tests/test_model.py` pins the
  2026.09 numbers. If a rebuild changes the default model, keep those tests on
  `load_model("2026.09")` rather than loosening them.
- **The web page mirrors the models.** After adding a model version, run
  `cd web && npm run etc-models` (CI's `web-etc-models.yml` fails on drift). The
  TypeScript port of the arithmetic is `web/lib/etc/model.ts`, pinned to the same numbers.
- The server (`server.py`, `mcp>=2`, `MCPServer`) serves stdio locally and
  streamable HTTP when hosted (Fly.io: `Dockerfile`, `fly.toml`; deployed by
  `.github/workflows/etc-server.yml` on merge). The Claude Code plugin that uses
  it lives in the separate `claude-astro-tools` marketplace as `nirspec-etc`.
