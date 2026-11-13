# Extended sources

An extended source raises two problems that a compact one does not. Nod subtraction removes part of its outer light, because the background exposures see the galaxy in the neighboring shutters. And stage 3 describes every source as one Gaussian along the slit (see [Extraction](/docs/reduction/nirspec/extraction#source-model)), which a galaxy with a bulge and a disk, clumps, or light that moves along the slit with wavelength does not follow. Each spectrum file also holds the master-background spectrum, in which nothing of the source is subtracted from itself, and the transmission of the MSA along the slit, which shows the source without the bar shadows. Together with the fitted model they show where a galaxy departs from it, and the code that fits the model can fit other source models to the same exposures.

## Reading the 2-D spectrum

The nod-subtracted 2-D spectrum (`SCI`) shows each source together with its negative images from the subtracted background exposures. For an extended source these overlap the source's own outer light, so part of the galaxy is subtracted from itself (the fraction in its own shutter is `SELFSUB`). The 2-D spectrum alone does not show the galaxy's extent.

![zenith_p15 547856, an extended galaxy in a two-shutter slitlet. The nod differences (SCI), stage 3's model and the residual on one color scale, with the profile along the slit summed over wavelength. The negative images above and below the source are its light in the subtracted background exposures; they overlap the galaxy's outer light, and the residual along them and beside the core shows a galaxy wider than the model. For such a source the master-background spectrum, with none of the source subtracted from itself, is the better measure of its light.](/docs/nirspec/extended_2d_nod.png)

Three extensions describe the fitted model on the grid of `SCI`:

| Extension | Content |
|---|---|
| `PROF2D` | The fitted source, per µJy of reported flux, negative images included |
| `CONTAM2D` | Other sources' predicted light |
| `TRANS2D` | The share of the source's light at each pixel that the MSA passes along the slit |

The full model is `PROF2D × (fnu_prof - fnu_contam) + CONTAM2D`, and `SCI` minus the model shows where the source departs from a Gaussian. A residual that changes sign between the source's position and its negative images, or along the dispersion, points to a profile the model does not describe.

The master-background spectrum offers a second view. `SCI_MBKG` is made from the same exposures with the master background subtracted instead of a nod partner, so nothing of the source is subtracted from itself, and its fit (`PROF2D_MBKG`, `TRANS2D_MBKG`, `CONTAM2D_MBKG`) measures the source's profile again on these frames. For an extended source the two fits differ: for 547856 the width along the slit is 0.70 shutter heights on the master-background frames and 0.50 on the nod differences, and the master-background flux is 8–16 % higher.

`SCI_MBKG / TRANS2D_MBKG` shows the source along the slit as it would look with no MSA in the way: the bar shadows, the slitlet's ends and the diffraction along the slit are divided out. The loss across the shutter's width is not, so the divided 2-D, like `fnu`, holds the light that entered the shutter across the dispersion. `TRANS2D` comes from the fitted profile, so in the faint wings it depends on that profile and can exceed 1, where diffraction at the shutters spreads light from the core outward; where it is small the division amplifies the noise. The master background models only the light through known shutters, so other light that is the same in every nod remains in `SCI_MBKG` (see [Master-background spectra](/docs/reduction/nirspec/backgrounds#master-background-spectra)).

![The same galaxy on its master-background frames: the master-background 2-D spectrum (SCI_MBKG), the share of the source's light the MSA passes along the slit (TRANS2D_MBKG), and their ratio, in which the bar shadows are divided out. On the right, the profiles along the slit of SCI_MBKG and of the ratio, each scaled to its peak.](/docs/nirspec/extended_2d_mbkg.png)

> **Placeholder.** A stored version of the divided 2-D spectrum is planned; its form and name are not decided.

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

A fit reads the target's calibrated 2-D cutouts, one per exposure and detector, the background exposures included. Stage 3 records in them what its fits used (outlier flags, other sources' predicted light, the master background and its variance, the bar offset), so `forward.load` reproduces the pixels stage 3 fitted, for the nod differences and for the master-background frames alike. No other file is needed.

> **Placeholder.** Downloading one target's files. These are deployed intermediate products: `campfire pull --obs <obs> --intermediate` fetches those of a whole observation. A download for a single target is planned.

> **Placeholder.** Installation for readers outside the team (the package is part of `campfire-pipeline`).

### A first fit

```python
from campfire_pipeline.nirspec import forward as fw
from campfire_pipeline.nirspec.forward import plots

files = fw.files_for('products/nirspec/<obs>', source_id, 'PRISM/CLEAR')
exps = fw.load(files, frames=True)               # each exposure minus its master background
model = fw.ForwardModel(exps, mbkg_sigma=0.01)   # with stage 3's correction to the background level

dx, dy = fw.slit_frame_offset(exps, along=fw.Param(0.0, bounds=(-0.3, 0.3), name='along'))
bulge = fw.Sersic(dx=dx, dy=dy, n=4.0, r_e=fw.Param(0.05, bounds=(0.01, 0.5), name='bulge.r_e'), q=1.0)
disk = fw.Sersic(dx=dx, dy=dy, n=1.0, r_e=fw.Param(0.2, bounds=(0.05, 1.5), name='disk.r_e'), q=1.0)

fit = fw.Fit(model, [bulge, disk], names=['bulge', 'disk'])
result = fit.run()
print(result.params, result.errors, result.chi2_red)
plots.panels(fit, result, path='fit.png')        # data, model and residual for each exposure
```

`result.spectra` holds one spectrum per component on the wavelength grid `result.lam` (µm), in MJy (× 10¹² for µJy), with errors in `result.spectra_err`. A fit to six PRISM exposures takes a few tens of seconds.

Each component's spectrum is its total light on the sky, not limited to the shutter. The data contain only what the shutter passes across the dispersion, so the light outside it is extrapolated from the assumed shape; this is why the pipeline reports the light within the shutter's 0.22″ width (`FLUXWID`). To compare a component with `fnu_prof`, multiply its spectrum by its share of light within ±0.11″ of the planned position across the shutter. Stage 3 does this for its Gaussian; for other components the share is not computed for you.

### Components

| Component | Parameters |
|---|---|
| `Point` | `dx`, `dy`: offset east and north of the catalog position, arcsec |
| `Gaussian` | `dx`, `dy`; `sigma`, arcsec (round) |
| `Sersic` | `dx`, `dy`; index `n`, half-light radius `r_e` (arcsec, major axis), axis ratio `q`, position angle `pa` (degrees east of north) |
| `SlitGaussian` | Stage 3's source: `centre` and `width` along the slit (shutter heights), and their change with wavelength (`drift`, `width_change`) |
| `Sum` | Several components sharing one spectrum, for example the clumps of one galaxy |

Offsets on the sky are east and north. With one position angle the data measure a position along the slit far better than across it, so a component is often easier to place in the slit's frame: `fw.slit_frame_offset(exps, along, across)` returns the `dx`, `dy` of an offset in arcsec along the slit (toward `PA_SLIT_Y`) and across it (toward `PA_SLIT_X`, the dispersion direction), each a number or a parameter. A north offset on a slit at position angle 64°, for example, moves a component 0.9″ across the shutter for every 0.4″ along it. Exposures at more than one position angle have no common slit frame, and the function raises for them.

Any other surface brightness works through a subclass that gives `brightness(east, north)` and an `extent()`; a NIRCam image of the source would be one. Such an image is collapsed onto the slit, weighted at each position across the shutter by the share of light the shutter passes there.

### Parameters

Each component parameter is a number (fixed), a `Param` (free unless `fixed=True`, with optional bounds and a prior), or `Tied` (a function of other parameters). The same `Param` object given to two components is one parameter, so a bulge and a disk share a center by sharing `dx` and `dy`:

```python
east, north = fw.Param(0.0, bounds=(-0.3, 0.3)), fw.Param(0.0, bounds=(-0.3, 0.3))
r_bulge = fw.Param(0.15, bounds=(0.02, 1.0), prior=fw.LogNormal(0.15, 0.3))    # e.g. a size from imaging
disk = fw.Sersic(dx=east, dy=north, n=1.0, r_e=fw.Tied(lambda r: 3.0 * r, r_bulge),
                 q=fw.Param(0.5, bounds=(0.1, 1.0)), pa=30.0)
```

A parameter's `name` labels it in `result.params` and `result.errors`; without one, the label is the first component field it reaches. Names must be distinct.

Priors on one parameter are `Normal`, `LogNormal` and `Uniform`, or any object with a `logpdf`. A prior on several parameters at once goes to the fit as a `JointPrior`; `JointPrior.gaussian(samples, params)` builds one from posterior samples, for example of a fit to NIRCam imaging, with an optional `inflate` for a size measured in another band.

### The fit

`Fit(model, components, names, priors, nonnegative)` exposes its pieces, so a script can use its own optimizer or sampler:

| | |
|---|---|
| `fit.labels`, `fit.start`, `fit.bounds` | The free parameters, their start values and bounds |
| `fit.log_posterior(theta)` | −χ²/2 at the best spectra for shape `theta`, plus the log-priors |
| `fit.residuals(theta)` | The data residuals over their noise, followed by the priors' residuals |
| `fit.run()` | Least squares from the start values; returns the parameters, errors, covariance, spectra and χ² |

`run()` searches from one start, so a fit can stop at a local minimum. When adding freedom to a model, starting the larger model from the smaller one's best values (`fit.run(start=...)`, or the parameters' own values) ensures it can only improve on it.

`nonnegative=True` (or a list of component names) holds those spectra at or above zero. It removes the downward noise of a faint component, so that component's spectrum is biased high where it has no light; it is an option, not the default.

### Master-background frames or nod differences

For an extended source the master-background frames are usually the better data to fit: each exposure is the frame minus its master background, so the source appears once and none of its outer light is subtracted. `fw.load(files)` without `frames=True` gives the nod differences stage 3 fits for `fnu`, where each exposure also holds the source's negative images from the subtracted background exposures, and the model reproduces those too.

On the frames, `mbkg_sigma=0.01` is the master-background scale term stage 3 uses: a common correction to the master background's level, with an allowance of 1 %. Stage 3 also drops the term and refits when the fitted correction reaches 3 %, since a correction that large is light from a source rather than an error in the master background's level; `Fit` does not, and the fitted value is in `result.solution.scale`. A bright source that fills much of the slitlet is the usual case: its outer light looks like a change in the background level. When stage 3 dropped the term for a source, `MBKGSCL` is 0 and `MBKGSCLC` holds the value it fitted; fitting such a source without the term (`fw.ForwardModel(exps)`) matches what stage 3 did.

### An example

zenith_p15 547856 is a dusty galaxy at z = 1.64 whose Hα is weak at the continuum peak while Paα is strongest there. On its six master-background frames, one Sérsic profile fits at χ²/dof 4.0, and a compact core inside a disk on the same center reaches 1.69 only with a negative core spectrum. Three round Gaussians placed along the slit, each with its own spectrum, fit at 1.53 with every spectrum positive, free or held non-negative:

```python
exps = fw.load(fw.files_for('products/nirspec/zenith_p15', 547856, 'PRISM/CLEAR'), frames=True)
model = fw.ForwardModel(exps)        # MBKGSCL is 0: stage 3 fitted this source without the scale term

across = fw.Param(0.0, bounds=(-0.15, 0.15), name='across')     # one offset across the shutter, shared
comps = []
for name, along, sigma in (('core', 0.0, 0.1), ('southwest', -0.5, 0.2), ('northeast', 0.5, 0.2)):
    dx, dy = fw.slit_frame_offset(exps, along=fw.Param(along, bounds=(-1.2, 1.2), name=f'{name}.along'),
                                  across=across)
    comps.append(fw.Gaussian(dx=dx, dy=dy, sigma=fw.Param(sigma, bounds=(0.02, 0.8), name=f'{name}.sigma')))

result = fw.Fit(model, comps, names=['core', 'southwest', 'northeast']).run()
```

The core lies 0.04″ from the catalog position along the slit with σ = 0.14″; the other two lie 0.44″ to the southwest and 0.33″ to the northeast, with σ = 0.16″ and 0.14″. The fit takes about 20 seconds.

![zenith_p15 547856 on its master-background 2-D spectrum. Top two rows: the 2-D spectrum, the model and the residual on one color scale, with the profile along the slit summed over wavelength, for stage 3's one-component fit and for the three components, whose model is drizzled onto the same grid. Bottom: stage 3's master-background spectrum and the three component spectra, each a component's total light. The core is the reddest and has the strongest Paα but little Hα; the two outer components are bluer, with most of the Hα and weaker Paα.](/docs/nirspec/extended_components.png)

A fitted model can be drawn on the product's combined 2-D grid, as in the figure, and compared with the data there:

```python
files = fw.files_for('products/nirspec/zenith_p15', 547856, 'PRISM/CLEAR')
grid = fw.product_grid(files)                 # the product's 2-D grid, rebuilt from the cutouts
data = fw.data_2d(grid, frames=True)          # data, err: SCI_MBKG and ERR_MBKG
model = fw.model_2d(fit, result, grid)        # the fitted model on the same grid, in MJy
```

`frames=False` gives the nod-differences 2-D (`SCI`) instead. The grid uses stage 3's resample settings unless an `_s2d.fits` is given (`s2d=`). `model_2d` includes the master-background scale term when the fit has one, so it is compared with `data_2d` rather than with `SCI_MBKG` itself. On the combined 2-D spectrum of 547856 the squared residual over the noise averages 6.8 per pixel for stage 3's one component and 1.4 for the three. Each component spectrum is the component's total light, extrapolated across the shutter from its assumed shape, while `fnu_prof` holds the light within the shutter, so the two are not on the same footing. Some blue light far along the slit, in the exposures with the source in the lower shutter, remains in the residual of the individual exposures (`plots.panels`).