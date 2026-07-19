"""FitsGL producer integration for CAMPFIRE (epic #337).

Builds and deploys FitsGL FITS tile-pyramid datasets for the map/cutout service:
:mod:`campfire.fitsgl.build` (Phase 2) generates a ``fitsgl.toml`` and calls the
producer's ``build_dataset``; :mod:`campfire.fitsgl.deploy` (Phase 3) pushes a built
dataset to the campfire-tiles bucket and upserts a ``fitsgl_datasets`` row. Both
require the ``campfire[fitsgl]`` extra for the FitsGL calls; their pure layers
(config generation, prefix/row/source-hash assembly) import no FitsGL.
"""
