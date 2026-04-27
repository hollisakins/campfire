# Programmatic Access

CAMPFIRE ships a Python package with three entry points that share the same authentication and local data layout. Pick the one that matches how you work.

| | What it gives you | When to use it |
|---|---|---|
| **CLI** (`campfire`) | Catalog sync + bulk FITS download to a local directory | Pull data once, then work with your own scripts / pandas / astropy |
| **Python client** (`Campfire`) | Interactive querying, lazy spectrum loading, plotting, calibration & stacking | Notebook analysis, custom selections, multi-step workflows |
| **REST API** | HTTP endpoints with signed URL downloads | Non-Python clients, lightweight integrations |

The CLI and Python client are siblings — same install, same credentials, same on-disk layout. Anything you `campfire sync` is immediately queryable from `Campfire()`, and anything you `cf.download()` is immediately readable by the pipeline tools.

## Install

```bash
pip install "git+https://github.com/hollisakins/campfire.git#subdirectory=python/"
```

Optional extras:

```bash
pip install "campfire[plotting] @ git+..."   # interactive Plotly figures
pip install "campfire[deploy]   @ git+..."   # NIRCam cutouts, calibration, stacking
pip install "campfire[all]      @ git+..."   # everything
```

## First five minutes

```bash
campfire login                    # browser-based OAuth
campfire sync                     # pulls full catalog (metadata only, ~seconds)
```

```python
from campfire import Campfire

cf = Campfire()
obj = cf.get_object('J141934.14+525238.7')   # public RUBIES-EGS LRD at z=6.69
print(obj)
# Object(J141934.14+525238.7, z=6.6900, egs)
#   12 spectra (G140H, G140M, G235H, G395H, G395M, PRISM)
#   tags: blagn, hae, lrd, o3e
#   Photometry(11 bands, UNICORN EGS v0.9)

obj.spectra[0].plot()              # quick-look matplotlib
cf.plot_cutout(obj.object_id)      # NIRCam RGB + shutter overlay
```

Walk through it step-by-step in [Getting Started](/docs/api/getting-started), or jump to the [Recipes](/docs/api/recipes) for end-to-end task examples.

## Where things live

The CLI and Python client share a single data directory. `$CAMPFIRE_ROOT` (or `~/campfire/` if unset) holds:

```
$CAMPFIRE_ROOT/
├── meta/
│   ├── campfire.db        # local catalog (queried by Campfire client)
│   ├── objects.csv        # exported on every sync — open with pandas/astropy
│   ├── spectra.csv
│   └── photometry.csv
└── products/
    └── <observation>/     # downloaded FITS files
        └── *_spec.fits
```

Credentials are stored separately at `~/.campfire/credentials` and cover all three entry points.

## Reference

- [Getting Started](/docs/api/getting-started) — install → first query → first plot, with figures
- [Recipes](/docs/api/recipes) — six end-to-end task examples
- [CLI Reference](/docs/api/cli) — `campfire` command-line tool
- [Python Client](/docs/api/python-client) — full `Campfire` class reference
- [REST API](/docs/api/rest) — direct HTTP endpoints
