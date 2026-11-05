# Background estimation and subtraction

Every NIRSpec spectrum sits on top of background light: zodiacal light and thermal emission from the telescope, passed by the same shutter as the source's light. In the near infrared this background is often brighter than the galaxies being observed, so how it is removed decides the quality of a faint spectrum.

CAMPFIRE removes the background in two independent ways and keeps both results:

- **Nod subtraction**, the standard method: each exposure minus exposures in which the source sat in a different shutter. This gives the main spectrum, `fnu`.
- **The master background**: the background spectrum measured from every empty shutter in the exposure and laid back onto the detector. This gives a second spectrum, `SPEC1D_MBKG`, in the same file.

Both depend on knowing which shutters actually let light through, so this page also covers shutters that misbehave.

## The master background

For every exposure and detector, CAMPFIRE measures the background spectrum from the shutters that hold no source: the empty shutters of each slitlet and any shutters the observation planned for background. It then predicts the background on every pixel of the detector, shutter by shutter, through each shutter's position, its bar shadow and the path its light takes onto the detector.

![The master background of one PRISM exposure. Top: a region of the detector, the master background predicted there, and the difference. What is left is mostly the light of the sources (one is labeled). The red ends of several traces, past the calibrated wavelength range, keep a few percent of background. The outlined traces belong to failed-open shutters, included at the light each passes. Bottom left: the background spectrum measured from this exposure's empty shutters, with the zodiacal light predicted for the pointing and date. Bottom right: each empty shutter's brightness relative to that spectrum. Shutters that disagree, for example because a faint source sits in them, are left out.](/docs/nirspec/master_background.png)

How it is built:

1. **Which pixels to measure.** Only the central part of each empty shutter, and only pixels inside one slitlet's footprint. On the gratings the spectra of many slitlets overlap on the detector (see [Extraction](/docs/reduction/nirspec/extraction)), and a pixel lit by two slitlets reads twice the background. The source's own shutter and the shutters of other cataloged sources are left out. Each footprint runs past the nominal wavelength range, because light continues beyond it: on G395M, about 200 pixels past the red end.
2. **Which shutters to trust.** Each empty shutter is compared with the spectrum of all of them, and left out when it is:

   | Rule | Why |
   |---|---|
   | brighter than the others by more than 5 % (and 3σ) | a faint source in it |
   | brighter in three neighboring wavelength bins (4σ each) | an emission line in it |
   | fainter than the others by more than 30 % | a shutter partly or fully closed |

3. **One spectrum per exposure, or the group's.** Each exposure's spectrum is measured on its own, then compared with the spectrum pooled over the other exposures of its group (every nod, both detectors) in eight wavelength bands. When no band differs by more than 2 % (and 3σ), the pooled spectrum is used, because it has several times more shutters and less noise. An exposure that differs keeps its own. An exposure with fewer than five clean shutters takes the pooled spectrum.
4. **Laying it onto the detector.** The spectrum is scaled by a small factor for each quarter of the detector, multiplied by the bar shadow of each shutter (next section), and placed along every open shutter's trace, including light that falls past the calibrated wavelength range at either end.

The master background is written per exposure and detector as `<exposure>_mbkg.fits`, next to the count-rate file. It is never used to correct the nod-subtracted spectrum. It is a few percent noisier than the nods, because every shutter differs slightly from the average in a way that nodding cancels and an average cannot; its error includes a 3 % floor for that.

## The bar shadow

The MSA's bars and the shutter edges block part of the light of a uniformly lit shutter, more toward its edges. This profile, the bar shadow, shapes the background in every shutter. CAMPFIRE measures it from the background itself: the empty shutters of many observations are uniformly lit, so stacking millions of their pixels gives the profile directly, as a function of position along the slitlet and wavelength.

![The bar shadow of a three-shutter PRISM slitlet, at three wavelengths. Solid lines are CAMPFIRE's measured profile; dashed lines are the CRDS reference. The two differ in where the shutter edges fall and in the light under the bars; the measured profile also follows the light a little past the outer shutters, and narrows toward longer wavelengths.](/docs/nirspec/bar_shadow_prism.png)

How the tables were made:

1. The background spectrum of each disperser is measured from the centers of the empty shutters alone, where the bar shadow is one by definition. No bar-shadow model enters, so the result does not depend on the reference it replaces.
2. Every pixel of a clean empty shutter is divided by that spectrum at its wavelength.
3. The ratios are collected by position along the slitlet and wavelength, and the median in each cell is the profile: the outer edges of a slitlet, and the bar between two open shutters.
4. The profile is written in the layout of the CRDS reference, so `jwst` can read it.

| Disperser | Replaces | Measured from |
|---|---|---|
| PRISM | `jwst_nirspec_barshadow_0006.fits` | 8,459 empty shutters in seven EMBER pointings |
| G140M | `jwst_nirspec_barshadow_0003.fits` | 6,925 empty shutters in the four SPURS fields |
| G235M | `jwst_nirspec_barshadow_0005.fits` | 1,589 empty shutters in the four SPURS fields |
| G395M | `jwst_nirspec_barshadow_0008.fits` | 1,780 empty shutters in the four SPURS fields |

The high-resolution gratings use the CRDS reference. Each table was also derived with one field left out and compared with that field; the left-out field follows the table more closely than the CRDS reference everywhere inside the open shutter.

In the master background, the measured table is used for every disperser that has one. In the spectra themselves the bar shadow is part of the [optics model](/docs/reduction/nirspec/slit-losses), which reproduces these profiles. Science frames are never divided by a bar shadow.

## Shutters that stayed closed

A shutter can be commanded open and stay closed. If the pipeline believed it open, it would subtract that shutter as background in the nod subtraction, lay background onto pixels where no light arrives, and extract a source from nothing. Closed shutters change from one MSA configuration to the next, so CAMPFIRE finds them in each exposure's own data.

![Two shutters that stayed closed. Each row: the data around one slitlet, the background model with every shutter open, the model with each shutter's brightness fitted, and the brightness across the slitlet for all three. Top, PRISM: the closed shutter (orange) is dark in the data and fits to zero, while its neighbors (blue) fit to one. Bottom, G395M: other slitlets' spectra cover the closed shutter's pixels, so the data alone barely show it. The fit still finds it, because it models those spectra too.](/docs/nirspec/stuck_shutters.png)

Before Stage 2a calibrates the exposures, every exposure is fitted as a sum of one background model per commanded-open shutter, each with its own brightness: about one for an open shutter, about zero for a closed one. Overlapping spectra are part of the fit, so the method works on the gratings as well as on the PRISM. A shutter is called closed when the median of its brightness over the exposures and both detectors, from at least two measurements, is below 0.3, and the two detectors agree within 0.5. Values between 0.3 and 0.8, or detectors that disagree, are noted for review by eye.

The calls are written to the observation's list of closed shutters (`reference/nirspec/<observation>/stuck_closed_shutters.toml`), keyed by the shutter's position in the MSA and marked as automatic. Entries made by a person, by hand in that file or in the portal's review of the nod plots, always win over the automatic ones, including an entry that says a shutter is open.

## Shutters that stayed open

The opposite failure is a shutter stuck open outside any slitlet. It passes background, and sometimes an unrelated object, onto the detector where no spectrum was planned, often across a planned spectrum. jwst's list of these shutters (from MSA operations) says where they are. How much light each one passes varies a lot, so CAMPFIRE measures it from the shutter's own pixels.

![Three failed-open shutters in one exposure, outlined in orange. Each row: the data, the master background, and the difference. The first passes all of an empty shutter's background, the second about half, the third almost none. The master background includes each at its measured level; what is left inside the outlines is mostly the light of other spectra crossing them.](/docs/nirspec/failed_open.png)

The master background includes each failed-open shutter at its measured level, combined over the exposures that agree. Light above that level, an object behind the shutter, is fitted together with the source in the master-background spectrum and reported there as other sources' light. In the nod-subtracted spectrum the shutter's background cancels between exposures; an object behind it is not modeled.

## Nod subtraction

A NIRSpec observation usually nods the telescope between exposures, so the source moves from one shutter of its slitlet to another. Subtracting one exposure from another then removes the background, which is the same in every shutter, and leaves the source as a positive image with negative copies where it sat in the other exposures.

![Nod subtraction for one PRISM source in a three-shutter slitlet. Left: the three calibrated exposures, with the source in a different shutter each time. Middle: each exposure minus the mean of the other two. The source is positive (red) in its own shutter and negative (blue) in the other two. Right: the profile along the slitlet, averaged over wavelength.](/docs/nirspec/nod_subtraction.png)

Each exposure has subtracted from it the mean of the other exposures of the same exposure group. The noise of the subtracted exposures is carried into the error.

### Leaving out a nod

A slitlet often holds more than one cataloged source. If a neighbor sits, in another exposure, where the source sits in this one, nod subtraction removes part of the neighbor's light from the source. With three exposures, a neighbor left in costs the source half of the neighbor's light.

![The same subtraction for a source whose neighbor (orange) sits, in nod 2, where the source sits in nod 3. For nod 3, nod 2 is left out and only nod 1 is subtracted. Right, bottom: the profile as subtracted (solid) and as it would have been with nod 2 included (dashed), where the neighbor's light pulls the source down.](/docs/nirspec/nod_left_out.png)

CAMPFIRE leaves out an exposure when a cataloged neighbor sits within three quarters of a shutter height of the source's position in it and is bright enough to matter: its light, measured on that exposure before subtraction, is at least 2 % of the master background in its shutter (`slitlet_neighbor_min_background`). It never leaves out the last background exposure. When every background exposure has such a neighbor, all are kept and the spectrum is flagged (`BKGWHY` in its exposure table).

Once Stage 3 has fitted every source together with its neighbors, the choices are made again with each neighbor's fitted spectrum. Sources whose choice changes are subtracted and fitted again. The exposure table of each spectrum records which exposures were subtracted (`BKGNODS`) and why any were left out (`BKGWHY`).

## Two spectra in every file

Every spectrum file holds the nod-subtracted spectrum (`fnu`) and the master-background spectrum (`SPEC1D_MBKG`, with its 2-D in `SCI_MBKG`). The second is made from the same exposures with the same extraction; only the background differs. In it, neighbors in the same slitlet are modeled rather than left out, because no exposure is subtracted.

![One extended galaxy in both versions. Left: the 2-D spectrum after nod subtraction (top) and after master-background subtraction (bottom), on one color scale. Middle: both profiles along the slit. The nods subtract part of the galaxy itself, because its light reaches the neighboring shutters. Right: both spectra. The extraction models the subtracted light, so they agree, but the master-background spectrum does not need that model.](/docs/nirspec/mbkg_spectrum.png)

The nod-subtracted spectrum is usually the better one: its background is measured in the source's own shutters. The header card `MBKGPREF` is true when the master-background spectrum is likely the better one, and `MBKGWHY` gives the first reason that applies:

| `MBKGWHY` | When |
|---|---|
| `no_clear_background` | Every background exposure had a bright neighbor at the source's position |
| `single_exposure` | An exposure had no other exposure to subtract |
| `extended` | The nods subtract more than a tenth of the source's own light, because the source is larger than its shutter |
