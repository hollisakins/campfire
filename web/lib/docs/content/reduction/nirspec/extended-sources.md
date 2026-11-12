# Extended sources

Stage 3 describes every source as one Gaussian along the slit (see [Extraction](/docs/reduction/nirspec/extraction#source-model)). That is enough to measure the flux of most sources, but a galaxy with a bulge and a disk, clumps, or light that moves along the slit with wavelength does not follow it. Each spectrum file carries what is needed to see where the model departs from the data, and the code that fits the model can fit other source models to the same exposures.

## Reading the 2-D spectrum

The nod-subtracted 2-D spectrum (`SCI`) shows each source together with its negative images from the subtracted background exposures. For an extended source these overlap the source's own outer light, so part of the galaxy is subtracted from itself (the fraction in its own shutter is `SELFSUB`). The 2-D spectrum alone does not show the galaxy's extent.

Three extensions describe the fitted model on the grid of `SCI`:

| Extension | Content |
|---|---|
| `PROF2D` | The fitted source, per µJy of reported flux, negative images included |
| `CONTAM2D` | Other sources' predicted light |
| `TRANS2D` | The share of the source's light at each pixel that the MSA passes along the slit |

The full model is `PROF2D × (fnu_prof - fnu_contam) + CONTAM2D`, and `SCI` minus the model shows where the source departs from a Gaussian. A residual that changes sign between the source's position and its negative images, or along the dispersion, points to a profile the model does not describe.

The master-background spectrum offers a second view. `SCI_MBKG` is made from the same exposures with the master background subtracted instead of a nod partner, so nothing of the source is subtracted from itself, and its fit (`PROF2D_MBKG`, `TRANS2D_MBKG`, `CONTAM2D_MBKG`) measures the source's profile again on these frames. For an extended source the two fits differ: on one zenith_p15 galaxy the width along the slit is 0.70 shutter heights on the master-background frames and 0.50 on the nod differences, and the master-background flux is 8–16 % higher.

`SCI_MBKG / TRANS2D_MBKG` shows the source along the slit as it would look with no MSA in the way: the bar shadows, the slitlet's ends and the diffraction along the slit are divided out. The loss across the shutter's width is not, so the divided 2-D, like `fnu`, holds the light that entered the shutter across the dispersion. `TRANS2D` comes from the fitted profile, so in the faint wings it depends on that profile and can exceed 1, where diffraction at the shutters spreads light from the core outward; where it is small the division amplifies the noise. The master background models only the light through known shutters, so other light that is the same in every nod remains in `SCI_MBKG` (see [Master-background spectra](/docs/reduction/nirspec/backgrounds#master-background-spectra)).

> **Placeholder.** A stored version of the divided 2-D spectrum is planned; its form and name are not decided.

> **Placeholder figure.** One extended source: `SCI`, the model and the residual; `SCI_MBKG`, its model and residual; `SCI_MBKG / TRANS2D_MBKG`. To be drawn from the ember_cosmos_p1 products at the current fit version.

## Fitting your own source model

The forward model stage 3 uses for its extraction is available as a Python package, `campfire_pipeline.nirspec.forward`. Stage 3's own profile fit is one application of it: a fit of one Gaussian along the slit with priors on its shape. The same code fits any combination of components, each with its own spectrum, to one target's exposures.

### The model

In each exposure, the value of a pixel is modeled as

```
sum over components i of  throughput(λ) × f_i(λ) × L_i(pixel)
    − the same for each background exposure subtracted from it
    + other sources' predicted light (held fixed)
```

where `L_i` is component *i*'s light at that pixel per unit of its total light, seen through the [MSA optics](/docs/reduction/nirspec/slit-losses) and that exposure's shutters, and `f_i` is the component's spectrum, on the wavelength grid of the reported spectrum. Components placed on the sky are oriented in each exposure by its WCS, so exposures at different position angles see the same source.

With the shapes fixed, the model is linear in every spectrum. At each trial shape all spectra are solved together, with their full covariance, and only the shape parameters are searched. The spectra take their best values at each shape (they are profiled, not marginalized); the shape parameters' errors come from the curvature at the minimum, and the spectra's errors are those at the best shape.

### The files a fit needs

A fit reads the target's calibrated 2-D cutouts, one per exposure and detector, the background exposures included. Stage 3 records in them what its fit used (outlier flags, other sources' predicted light, the bar offset), so `forward.load` reproduces the pixels stage 3 fitted. Fits to the master-background frames also read each exposure's `_mbkg.fits`.

> **Placeholder.** Downloading one target's files. These are deployed intermediate products: `campfire pull --obs <obs> --intermediate` fetches those of a whole observation. A download for a single target is planned.

> **Placeholder.** Installation for readers outside the team (the package is part of `campfire-pipeline`).

### A first fit

```python
from campfire_pipeline.nirspec import forward as fw
from campfire_pipeline.nirspec.forward import plots

files = fw.files_for('products/nirspec/<obs>', 547856, 'PRISM/CLEAR')
exps = fw.load(files, frames=True)               # each exposure minus its master background
model = fw.ForwardModel(exps, mbkg_sigma=0.01)   # with stage 3's correction to the background level

north = fw.Param(0.0, bounds=(-0.3, 0.3))        # one center, shared by both components
bulge = fw.Sersic(dx=0.0, dy=north, n=4.0, r_e=fw.Param(0.05, bounds=(0.01, 0.5)), q=1.0)
disk = fw.Sersic(dx=0.0, dy=north, n=1.0, r_e=fw.Param(0.2, bounds=(0.05, 1.5)), q=1.0)

fit = fw.Fit(model, [bulge, disk], names=['bulge', 'disk'])
result = fit.run()
print(result.params, result.errors, result.chi2_red)
plots.panels(fit, result, path='fit.png')        # data, model and residual for each exposure
```

`result.spectra` holds one spectrum per component on the wavelength grid `result.lam` (µm), in MJy (× 10¹² for µJy), with errors in `result.spectra_err`. A bulge-plus-disk fit of six PRISM exposures takes about a minute.

Each component's spectrum is its total light on the sky, not limited to the shutter. The data contain only what the shutter passes across the dispersion, so the light outside it is extrapolated from the assumed shape; this is why the pipeline reports the light within the shutter's 0.22″ width (`FLUXWID`). To compare a component with `fnu_prof`, multiply its spectrum by its share of light within ±0.11″ of the planned position across the shutter. Stage 3 does this for its Gaussian; for other components the share is not computed for you.

### Components

| Component | Parameters |
|---|---|
| `Point` | `dx`, `dy`: offset east and north of the catalog position, arcsec |
| `Gaussian` | `dx`, `dy`; `sigma`, arcsec (round) |
| `Sersic` | `dx`, `dy`; index `n`, half-light radius `r_e` (arcsec, major axis), axis ratio `q`, position angle `pa` (degrees east of north) |
| `SlitGaussian` | Stage 3's source: `centre` and `width` along the slit (shutter heights), and their change with wavelength (`drift`, `width_change`) |
| `Sum` | Several components sharing one spectrum, for example the clumps of one galaxy |

Any other surface brightness works through a subclass that gives `brightness(east, north)` and an `extent()`; a NIRCam image of the source would be one. Such an image is collapsed onto the slit, weighted at each position across the shutter by the share of light the shutter passes there.

### Parameters

Each component parameter is a number (fixed), a `Param` (free unless `fixed=True`, with optional bounds and a prior), or `Tied` (a function of other parameters). The same `Param` object given to two components is one parameter, so a bulge and a disk share a center by sharing `dx` and `dy`:

```python
east, north = fw.Param(0.0, bounds=(-0.3, 0.3)), fw.Param(0.0, bounds=(-0.3, 0.3))
r_bulge = fw.Param(0.15, bounds=(0.02, 1.0), prior=fw.LogNormal(0.15, 0.3))    # e.g. a size from imaging
disk = fw.Sersic(dx=east, dy=north, n=1.0, r_e=fw.Tied(lambda r: 3.0 * r, r_bulge),
                 q=fw.Param(0.5, bounds=(0.1, 1.0)), pa=30.0)
```

Priors on one parameter are `Normal`, `LogNormal` and `Uniform`, or any object with a `logpdf`. A prior on several parameters at once goes to the fit as a `JointPrior`; `JointPrior.gaussian(samples, params)` builds one from posterior samples, for example of a fit to NIRCam imaging, with an optional `inflate` for a size measured in another band.

### The fit

`Fit(model, components, names, priors, nonnegative)` exposes its pieces, so a script can use its own optimizer or sampler:

| | |
|---|---|
| `fit.labels`, `fit.start`, `fit.bounds` | The free parameters, their start values and bounds |
| `fit.log_posterior(theta)` | −χ²/2 at the best spectra for shape `theta`, plus the log-priors |
| `fit.residuals(theta)` | The data residuals over their noise, followed by the priors' residuals |
| `fit.run()` | Least squares from the start values; returns the parameters, errors, covariance, spectra and χ² |

`nonnegative=True` (or a list of component names) holds those spectra at or above zero. It removes the downward noise of a faint component, so that component's spectrum is biased high where it has no light; it is an option, not the default.

### Master-background frames or nod differences

For an extended source the master-background frames are usually the better data to fit: each exposure is the frame minus its master background, so the source appears once and none of its outer light is subtracted. `fw.load(files)` without `frames=True` gives the nod differences stage 3 fits for `fnu`, where each exposure also holds the source's negative images from the subtracted background exposures, and the model reproduces those too.

On the frames, `mbkg_sigma=0.01` is the master-background scale term stage 3 uses: a common correction to the master background's level, with an allowance of 1 %. Stage 3 also drops the term and refits when the fitted correction reaches 3 %, since a correction that large is light from a source rather than an error in the master background's level; `Fit` does not, and the fitted value is in `result.solution.scale`.

### An example

> **Placeholder.** A bulge-plus-disk fit to the master-background frames of one extended galaxy: data, model and residual for each exposure, the component spectra, and what the residuals show. To be fitted and drawn from the ember_cosmos_p1 products at the current fit version.

### Current limitations

- Other sources' light is held at stage 3's prediction. Fitting a neighbor as a component of its own uses the same machinery but is not yet connected to `forward.load`.
- `log_posterior` works with any sampler, but at about a minute per least-squares fit, sampling is slow.
- No NIRCam-image component is shipped yet.
