# Measured diffraction-spike arm profile

How `campfire_pipeline/data/nircam_spike_profile_v2.fits` was built (2026-11-14/15).
These are the scripts as run, with the paths of the machine they ran on; they are
kept for provenance, not as a maintained tool.

1. `find_candidates.py`, `rank_candidates.py` — public level-3 NIRCam imaging at
   |b| > 25 with a Gaia star brighter than G = 12.5 within 150" of the pointing.
2. `footprints.py`, `select_files.py` — per-exposure footprints around the chosen
   stars; `download_list.ecsv` is the resulting list (exposures with the star on
   the detector or within 150" of its centre).
3. `download_uncal.py`, `add_fields.py` — raw files into the pipeline's raw
   layout and one scratch field per star; then `cfpipe nircam run --field
   spikecal_<star> --process` (Gaia reference catalog from `cfpipe nircam refcat
   query`), so the arms are measured after the standard per-exposure background
   step.
4. `measure_arms_pipe.py` — for each exposure and each of the eight arm
   directions, the median brightness in bins of distance from the star and
   offset across the arm. MACS0647's G = 8 star is measured the same way from
   the production field's exposures.
5. `build_profile2.py` — divides each cross-section by the star's flux
   (`gaia_sed.py`: Gaia + 2MASS + AllWISE), puts it on wavelength-scaled axes
   and builds the two tables: axis brightness against distance / wavelength,
   and the shape across the arm against distance and offset in arcsec.
   `wing_scaling.py` is the test that the shape is fixed in arcsec;
   `strut_by_filter.py` compares the strut arms with the segment arms.
6. `flux_test.py` — aperture photometry of ~1400 unsaturated Gaia stars against
   the brightness predictions.

Stars used: four in COSMOS (G = 8.4, 9.5, 10.1, 10.2), one in a program-5398
field (G = 8.4) and MACS0647's (G = 8.0). A sixth candidate (program-5398 field
at RA 11.25, Dec -73.23) is in the Small Magellanic Cloud; the background step
fails there and it is excluded.

Known limits: all six stars are saturated and span two magnitudes; beyond about
100 arcsec/um the segment arms rest on the MACS0647 star alone; exposures that
failed to align to Gaia are included without a test of whether they broaden the
profile.
