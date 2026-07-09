"""FitsGL producer integration for CAMPFIRE (epic #337).

Builds (and, in later phases, deploys) FitsGL FITS tile-pyramid datasets for the
map/cutout service. Requires the ``campfire[fitsgl]`` extra for the actual build;
the pure config-generation layer in :mod:`campfire.fitsgl.build` imports no FitsGL.
"""
