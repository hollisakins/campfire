# Data-release page scripts

Figures and tables for a release's page document (`campfire dr page push <tag> <dir>`,
design D-P6-3) are produced by ad-hoc scripts kept here and run by hand before
publication; there is no generator in the CLI. Each script writes into a local
directory (`README.md` plus images) that `dr page push` uploads under
`dr/<tag>/page/<sha16>/<name>` (content-addressed, never overwritten) and
registers on the release. A published release's page is frozen with it.
