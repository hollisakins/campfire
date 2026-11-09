# Background estimation and subtraction

The NIRSpec background (zodiacal light and telescope thermal emission) is comparable to or brighter than most high-redshift targets, so its subtraction sets the noise floor of faint spectra. CAMPFIRE estimates it in two independent ways and propagates both through to the final products:

- **Nod subtraction**, which provides the primary spectrum (`fnu`).
- **A master background**, measured from the empty shutters of each exposure and modeled onto the detector, which provides a secondary spectrum (`SPEC1D_MBKG`).

Both depend on which shutters transmit light; stuck-closed and failed-open shutters are identified from the same exposures.

## Master background

For each exposure and detector, the background spectrum is measured from shutters containing no cataloged source: the empty shutters of science slitlets and any dedicated background slitlets in the MSA configuration. It is then forward-modeled onto every detector pixel through the geometry of each open shutter, including its bar shadow.

![Master background of one PRISM exposure. Top: a detector region, the master-background model, and the residual, which is dominated by source flux (one source labeled). The red ends of several traces, beyond the calibrated wavelength range, retain a few percent of background. Outlined traces are failed-open shutters, included at their measured transmission. Bottom left: the background spectrum measured from the empty shutters, with the zodiacal prediction for the pointing and epoch. Bottom right: the flux of each empty shutter relative to that spectrum; outliers are rejected.](/docs/nirspec/master_background.png)

1. **Sampling.** The spectrum is measured from the central region of each empty shutter, using only pixels covered by a single slitlet footprint. On the gratings, overlapping traces would otherwise double-count the background (see [Extraction](/docs/reduction/nirspec/extraction)). Shutters containing the target or other cataloged sources are excluded. Footprints extend beyond the nominal wavelength range to capture the light that continues past it (about 200 pixels beyond the red limit on G395M).
2. **Shutter rejection.** Each empty shutter is compared with the ensemble spectrum and rejected if:

   | Criterion | Typical cause |
   |---|---|
   | brighter than the ensemble by > 5 % and > 3σ | faint source |
   | three consecutive 2-pixel bins above 4σ | emission line |
   | fainter than the ensemble by > 30 % | partially or fully closed shutter |

3. **Combination.** Each exposure's spectrum is compared with the spectrum combined over the other exposures of its group (all nods, both detectors) in eight wavelength bands. If no band deviates by more than 2 % and 3σ, the combined spectrum is adopted for its higher signal-to-noise; otherwise the exposure retains its own. Exposures with fewer than five empty shutters always adopt the combined spectrum.
4. **Forward model.** The spectrum is scaled per detector quadrant, multiplied by the bar shadow of each shutter, and placed along every open shutter's trace, including the light beyond the calibrated wavelength range at either end.

The master background is written per exposure and detector as `<exposure>_mbkg.fits`. It is not used to correct the nod-subtracted spectra: shutter-to-shutter throughput variations, which cancel in nod subtraction but not in an ensemble average, make it a few percent noisier. Its uncertainty includes a 3 % systematic floor.

## Bar-shadow calibration

The bars and shutter edges of the MSA vignette uniformly illuminated shutters, increasingly toward their edges. CAMPFIRE replaces the CRDS bar-shadow references with profiles measured from the background in empty shutters across many observations, as a function of position along the slitlet and of wavelength.

![Bar shadow of a three-shutter PRISM slitlet at three wavelengths: CAMPFIRE's measured profile (solid) and the CRDS reference (dashed). The two differ in the position of the shutter edges and in the transmission under the interior bars. The measured profile also follows the light just beyond the outer shutters, and narrows with increasing wavelength.](/docs/nirspec/bar_shadow_prism.png)

1. The background spectrum of each disperser is measured from the shutter centers alone, where the bar shadow is unity by definition. No bar-shadow model enters, so the result is independent of the reference it replaces.
2. Each pixel of an empty shutter is normalized by that spectrum at its wavelength.
3. The normalized values are binned in position along the slitlet and wavelength; the median in each bin defines the profile for the outer edges of a slitlet and for an interior bar.
4. The profiles are written in the format of the CRDS reference.

| Disperser | Replaces | Derived from |
|---|---|---|
| PRISM | `jwst_nirspec_barshadow_0006.fits` | Empty shutters in seven EMBER pointings |
| G140M | `jwst_nirspec_barshadow_0003.fits` | Empty shutters in the four SPURS fields |
| G235M | `jwst_nirspec_barshadow_0005.fits` | Empty shutters in the four SPURS fields |
| G395M | `jwst_nirspec_barshadow_0008.fits` | Empty shutters in the four SPURS fields |

The high-resolution gratings retain the CRDS reference. The shipped tables, their inputs and the reference files they were derived against are listed in `pipeline/campfire_pipeline/data/nirspec_calibrations.toml`.

The measured tables are used in the master background. In the extraction, the bar shadow is part of the [optics model](/docs/reduction/nirspec/slit-losses), which reproduces these profiles. Science frames are never divided by a bar-shadow correction.

## Stuck-closed shutters

A shutter commanded open may fail to open. Treated as open, it would be subtracted as background in the nod subtraction, receive a modeled background where no light arrives, and yield a spurious extraction. Because stuck-closed shutters vary between MSA configurations, CAMPFIRE identifies them from each exposure's own data.

![Two stuck-closed shutters. Each row: the data around one slitlet; the background model with all shutters open; the model with fitted per-shutter amplitudes; and the profile across the slitlet for all three. Top, PRISM: the stuck-closed shutter (orange) is dark and fits to zero, its neighbors (blue) to unity. Bottom, G395M: the shutter is covered by other slitlets' traces and is barely visible in the data alone, but is recovered because the fit models the overlapping traces.](/docs/nirspec/stuck_shutters.png)

Before Stage 2a calibration, each exposure is fitted as a sum of background templates, one per commanded-open shutter, each with a free amplitude (unity for an open shutter, zero for a closed one). Overlapping traces are included, so the method applies equally to the gratings and the PRISM. A shutter is flagged as closed when its median amplitude over at least two measurements (exposures × detectors) is below 0.3 and the two detectors agree within 0.5. Amplitudes between 0.3 and 0.8, or discrepant detectors, are flagged for visual review.

Flags are written to the observation's stuck-shutter list (`reference/nirspec/<observation>/stuck_closed_shutters.toml`), keyed by MSA shutter coordinates and marked as automatic. Manual entries, made in that file or through the portal's nod review, take precedence over automatic ones, including entries that mark a shutter as open.

## Failed-open shutters

Shutters stuck open outside any commanded slitlet admit background, and occasionally unrelated sources, onto the detector, frequently across science traces. Their locations come from the MSA operations list used by `jwst`; their transmission varies widely and is measured from each shutter's own pixels.

![Three failed-open shutters in one exposure (outlined): data, master background, and residual. Their transmissions are about 1, 0.5 and nearly 0 relative to an open shutter. The master background includes each at its measured transmission; the remaining residual inside the outlines is mostly from other traces crossing them.](/docs/nirspec/failed_open.png)

Each failed-open shutter enters the master background at its measured transmission, combined over consistent exposures. Excess flux from a source behind the shutter is fitted jointly with the target in the master-background spectrum and reported there as contamination. In the nod-subtracted spectrum the shutter's background cancels; a source behind it is not modeled.

## Nod subtraction

Exposures are nodded so that the target moves between the shutters of its slitlet. Each exposure is background-subtracted using the mean of the other exposures in its group, which leaves a positive image of the target and negative images at its other nod positions.

![Nod subtraction for a PRISM source in a three-shutter slitlet. Left: the three calibrated exposures. Middle: each exposure minus the mean of the other two; the source is positive in its own shutter and negative in the other two. Right: the spatial profile, averaged over wavelength.](/docs/nirspec/nod_subtraction.png)

The noise of the subtracted exposures is propagated into the error array.

### Exclusion of contaminated background exposures

When a cataloged neighbor in the same slitlet falls, in a background exposure, at the target's position in the science exposure, nod subtraction removes part of the neighbor's flux from the target; with three nods, half of it.

![A source whose neighbor (orange) falls, in nod 2, at the source's position in nod 3. For nod 3, nod 2 is excluded and only nod 1 is subtracted. Bottom right: the profile as subtracted (solid) and with nod 2 included (dashed), where the neighbor's flux suppresses the source.](/docs/nirspec/nod_left_out.png)

A background exposure is excluded when a cataloged neighbor lies within 0.75 shutter heights of the target's position and its flux, measured in that exposure before subtraction, exceeds 2 % of the master background in its shutter (`slitlet_neighbor_min_background`). The last remaining background exposure is never excluded; if all are contaminated, all are kept and the spectrum is flagged (`BKGWHY`).

After the joint fit in Stage 3, the exclusions are re-evaluated using each neighbor's fitted spectrum, and affected sources are re-subtracted and refitted. The `EXPOSURES` table records the background exposures used (`bkg_nods`) and the reason for any exclusion (`bkg_why`).

## Master-background spectra

Each spectrum file also contains a master-background-subtracted spectrum (`SPEC1D_MBKG`, with the 2-D spectrum in `SCI_MBKG`), extracted from the same exposures with the same model; only the background treatment differs. Same-slitlet neighbors are modeled rather than excluded, since no exposures are subtracted.

![An extended galaxy reduced both ways. Left: 2-D spectra after nod subtraction (top) and master-background subtraction (bottom), on a common scale. Middle: both spatial profiles; nod subtraction removes part of the galaxy's own extended light. Right: both 1-D spectra. The extraction models the self-subtraction, so the two agree, but the master-background spectrum does not depend on that model.](/docs/nirspec/mbkg_spectrum.png)

The nod-subtracted spectrum is generally preferred, since its background is measured in the target's own shutters. The header keyword `MBKGPREF` flags cases where the master-background spectrum is likely more reliable, and `MBKGWHY` gives the first applicable reason:

| `MBKGWHY` | Condition |
|---|---|
| `no_clear_background` | every background exposure is contaminated by a neighbor at the target's position |
| `extended` | nod subtraction removes more than 10 % of the source's own flux |

The master background models light only through the shutters in the pipeline's trace maps. Other light that is the same in every nod, such as light through an open shutter outside the trace maps or light from the instrument itself, cancels in nod subtraction but remains in the master-background spectrum of every source whose trace it crosses, whether or not `MBKGPREF` is set. A master-background spectrum well above both the nod-subtracted spectrum and the other dispersers can indicate such light.
