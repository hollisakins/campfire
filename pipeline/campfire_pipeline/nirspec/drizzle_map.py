"""
One product's 2-D grid and every exposure's pixel mapping onto it, computed once and
reused for every 2-D image stage 3 puts on that grid (extraction v2).

Spec3 (``stage3.run_stage3_single_source``) writes a product's s2d and its
outlier-flagged per-source file, the crf (a MultiExposureModel). Every other 2-D image
of the product is drizzled here with the drizzle library directly, exactly as jwst's
``ResampleSpecStep`` drizzles the crf (jwst 1.20: ``resample_spec.ResampleSpec`` on
stcal's ``Resample``), but with each exposure's pixel map computed once:

1. ``DrizzleMap.from_crf``: the crf's exposures, with jwst's NaN / DO_NOT_USE matching
   (``match_nans_and_flags``); the output grid jwst builds from them
   (``ResampleSpec.build_nirspec_output_wcs``) with the step settings Spec3 used (the
   product's s2d, ``meta.resample``); per exposure its pixel map (``calc_pixmap``), its
   intensity scale and its input range. This is the costly part, done once per product.
2. ``DrizzleMap.drizzle``: one image per exposure through those maps. Arrays not given
   (DQ, ERR, VAR_*) are the crf's, so an image takes the data's masks and weights (an
   image on the crf's exposures: ``PROF2D``, ``CONTAM2D``, ``CONTERR2D``); a full set
   (the master-background frames, and the model images on their masks,
   ``mbkg_extraction``) is drizzled as given. Weights,
   variance propagation and ``ERR`` follow stcal: each variance drizzled as its square
   root with the data's weight map, combined with the exposure-time (or read-noise)
   weights, ``ERR`` the square root of their sum.

Re-drizzling the crf's own arrays reproduces Spec3's s2d (SCI, ERR, VAR_*, WHT) to the
bit (``tests/test_nirspec_drizzle_map.py``; p1 check in the design record, section 4).
"""

import numpy as np

# jwst's ResampleSpecStep: every DQ flag is good except DO_NOT_USE and NON_SCIENCE
GOOD_BITS = '~DO_NOT_USE+NON_SCIENCE'
NAN_EXTENSIONS = ('data', 'err', 'var_rnoise', 'var_poisson', 'var_flat')   # jwst match_nans_and_flags
VARIANCES = ('var_rnoise', 'var_flat', 'var_poisson')                        # stcal's order
DO_NOT_USE = 1


def crf_path(product_name, source_id, workspace_dir=''):
    """A product's Spec3 crf, whose pixel maps everything is drizzled through: MSA
    products are ``{product}_s{id:09d}_crf.fits``, fixed-slit ones insert the slit name
    (``{product}_{slit}_s{id:09d}_crf.fits``), as ``stage3.run_stage3_single_source``
    renames their s2d and x1d. The MSA name when neither exists."""
    import glob
    import os
    msa = os.path.join(workspace_dir, f'{product_name}_s{int(source_id):09d}_crf.fits')
    if os.path.exists(msa):
        return msa
    fs = sorted(glob.glob(os.path.join(workspace_dir, f'{product_name}_*_s{int(source_id):09d}_crf.fits')))
    return fs[0] if fs else msa


def crf_dq(crf_path):
    """{exposure file name: DQ} of a product's Spec3 crf, each on its canonical's cutout: what
    Spec3's outlier detection flagged (``profile_fit.mask_crf_outliers``)."""
    from jwst import datamodels
    from jwst.datamodels import SourceModelContainer
    with datamodels.open(crf_path) as m:
        return {str(x.meta.filename): np.array(x.dq) for x in SourceModelContainer(m)}


def step_settings(s2d_path):
    """The resample settings Spec3 used for a product (its s2d's ``meta.resample``): pixfrac,
    kernel, fillval, weight_type, pixel_scale_ratio."""
    from jwst import datamodels
    with datamodels.open(s2d_path) as m:
        r = m.meta.resample
        return dict(pixfrac=float(r.pixfrac), kernel=str(r.kernel), fillval=str(r.fillval),
                    weight_type=str(r.weight_type), pixel_scale_ratio=float(r.pixel_scale_ratio or 1.0))


def match_nans_and_flags(arrays):
    """jwst's ``match_nans_and_flags`` on a dict of an exposure's arrays (in place): a pixel
    NaN in any of ``NAN_EXTENSIONS`` or DO_NOT_USE in ``dq`` becomes NaN in all of them and
    DO_NOT_USE in ``dq``. Arrays of another shape than the first are left alone."""
    present = [k for k in NAN_EXTENSIONS if arrays.get(k) is not None]
    if not present:
        return arrays
    shape = arrays[present[0]].shape
    bad = None
    for k in present:
        if arrays[k].shape != shape:
            continue
        bad = np.isnan(arrays[k]) if bad is None else bad | np.isnan(arrays[k])
    dq = arrays.get('dq')
    if dq is not None and dq.shape == shape:
        bad |= (dq & DO_NOT_USE).astype(bool)
    for k in present:
        if arrays[k].shape == shape:
            arrays[k][bad] = np.nan
    if dq is not None and dq.shape == shape:
        dq[bad] |= DO_NOT_USE
    return arrays


class MappedExposure:
    """One crf exposure: its arrays as the crf holds them (``data``, ``dq``, ``err``,
    ``var_*``), the metadata the weights read (``exposure_time``, ``measurement_time``,
    ``level``, ``subtracted``), and its mapping onto the grid: the pixel map, the
    intensity scale and the input range (xmin, xmax, ymin, ymax)."""

    def __init__(self, name, arrays, meta, pixmap, iscale=1.0, range_=None):
        self.name = name
        self.arrays = arrays
        self.meta = meta
        self.pixmap = pixmap
        self.iscale = iscale
        self.range = range_

    @classmethod
    def from_model(cls, model, arrays, resamp):
        """From a crf exposure (a SlitModel) and jwst's ``ResampleSpec`` of the product."""
        from drizzle.utils import calc_pixmap
        from jwst.resample.resample import input_jwst_model_to_dict
        from stcal.resample.utils import resample_range

        meta = input_jwst_model_to_dict(model, weight_type=resamp.weight_type, enable_var=True,
                                        compute_err='from_var')
        shape = model.data.shape
        return cls(str(model.meta.filename), arrays,
                   {k: meta[k] for k in ('exposure_time', 'measurement_time', 'level', 'subtracted')},
                   calc_pixmap(model.meta.wcs, resamp.output_wcs, shape), resamp._get_intensity_scale(meta),
                   resample_range(shape, model.meta.wcs.bounding_box))


class DrizzleMap:
    """A product's 2-D grid and its exposures' pixel maps (module doc, 1)."""

    def __init__(self, exposures, out_shape, settings, pixel_scale_ratio, wcs=None):
        self.exposures = exposures
        self.out_shape = tuple(out_shape)
        self.settings = settings
        self.pixel_scale_ratio = pixel_scale_ratio
        self.wcs = wcs

    @property
    def names(self):
        return [x.name for x in self.exposures]

    @classmethod
    def from_crf(cls, crf_path, settings):
        """The map of a product from its crf, with Spec3's resample ``settings``
        (``step_settings``)."""
        from jwst import datamodels
        from jwst.datamodels import SourceModelContainer
        from jwst.lib.pipe_utils import match_nans_and_flags as jwst_match
        from jwst.resample.resample_spec import ResampleSpec

        s = dict(settings)
        with datamodels.open(crf_path) as m:
            container = SourceModelContainer(m)
            raw = [{k: np.array(getattr(x, k)) for k in NAN_EXTENSIONS + ('dq',) if hasattr(x, k)}
                   for x in container]
            for x in container:                  # as ResampleSpecStep, before the grid is built
                jwst_match(x)
            resamp = ResampleSpec(container, good_bits=GOOD_BITS, output_wcs=None,
                                  wcs_pars=dict(output_shape=None, pixel_scale=None,
                                                pixel_scale_ratio=s['pixel_scale_ratio']),
                                  pixfrac=s['pixfrac'], kernel=s['kernel'], fillval=s['fillval'],
                                  weight_type=s['weight_type'], blendheaders=False, enable_var=True,
                                  compute_err='from_var', output=None)
            # each exposure keeps the crf's own arrays, from before the matching
            exposures = [MappedExposure.from_model(x, a, resamp) for x, a in zip(container, raw)]
            return cls(exposures, resamp.output_wcs.array_shape, s, resamp.pixel_scale_ratio, wcs=resamp.output_wcs)

    def drizzle(self, inputs, variances=False):
        """Drizzle one image per exposure onto the grid (module doc, 2).

        inputs : {exposure file name: image or dict of arrays} on the crf's cutouts. An
            image (an array; None or a name not given: zeros) replaces the crf's data with
            its NaNs zeroed, as a model drawn on the data's masks; a dict ('data' and any
            of 'dq', 'err', 'var_rnoise', 'var_poisson', 'var_flat') replaces those arrays
            as they are, NaNs included (a frame drizzled as data). Arrays not given are
            the crf's; each is cast to the crf's dtype.
        variances : drizzle the variances too and return ``var_*`` and ``err``.

        Returns dict(data, wht[, var_rnoise, var_flat, var_poisson, err]), float32."""
        from drizzle.resample import Drizzle
        from stcal.resample.utils import build_driz_weight, get_tmeasure
        from jwst.datamodels.dqflags import pixel

        s = self.settings
        driz = Drizzle(kernel=s['kernel'], fillval=s['fillval'], out_shape=self.out_shape, exptime=0.0,
                       begin_ctx_id=0, max_ctx_id=len(self.exposures) - 1, disable_ctx=True)
        acc = {v: dict(wsum=np.full(self.out_shape, np.nan, np.float32), wt=np.zeros(self.out_shape, np.float32))
               for v in VARIANCES} if variances else None
        for x in self.exposures:
            given = inputs.get(x.name)
            if not isinstance(given, dict):
                image = np.zeros(x.arrays['data'].shape) if given is None else given
                given = {'data': np.nan_to_num(image)}
            a = {k: np.array(v) for k, v in x.arrays.items()}
            for k, v in given.items():
                if np.shape(v) != a['data'].shape:
                    raise ValueError(f'drizzle: {k} {np.shape(v)} vs crf {a["data"].shape} for {x.name}')
                a[k] = np.asarray(v).astype(a[k].dtype if k in a else np.float32)
            match_nans_and_flags(a)
            model = dict(x.meta, data=a['data'], dq=a['dq'], **{v: a.get(v) for v in VARIANCES})
            weight = build_driz_weight(model, weight_type=s['weight_type'], good_bits=GOOD_BITS,
                                       flag_name_map=pixel)
            data = a['data']
            if not model['subtracted'] and model['level'] is not None:
                data = data - model['level']
            xmin, xmax, ymin, ymax = x.range
            kw = dict(exptime=model['exposure_time'], pixmap=x.pixmap, scale=x.iscale, weight_map=weight,
                      wht_scale=1.0, pixfrac=s['pixfrac'], in_units='cps', xmin=xmin, xmax=xmax, ymin=ymin,
                      ymax=ymax)
            driz.add_image(data, **kw)
            if variances:
                self._add_variances(acc, model, kw, get_tmeasure)
        out = dict(data=driz.out_img, wht=driz.out_wht)
        if variances:
            out.update(self._finalize_variances(acc))
        return out

    def _add_variances(self, acc, model, kw, get_tmeasure):
        """stcal ``Resample.resample_variance_arrays``."""
        from drizzle.resample import Drizzle
        s = self.settings
        var = {}
        for name in VARIANCES:
            v = model.get(name)
            if v is None or v.size == 0 or v.shape != model['data'].shape:
                continue
            d = Drizzle(out_shape=self.out_shape, kernel=s['kernel'], fillval=np.nan, disable_ctx=True)
            d.add_image(data=np.sqrt(v), **kw)
            var[name] = d.out_img ** 2
        if s['weight_type'].startswith('ivm'):
            rn = var.get('var_rnoise')
            weight = np.zeros(self.out_shape)
            if rn is not None:
                ok = (rn > 0) & np.isfinite(rn)
                weight[ok] = np.reciprocal(rn[ok])
        elif s['weight_type'] == 'exptime':
            t, _ = get_tmeasure(model)
            weight = np.full(self.out_shape, t)
        else:
            raise ValueError(f"unrecognized weight type {s['weight_type']!r}")
        for name, v in var.items():
            wsum = acc[name]['wsum']
            ok = (v >= 0) & np.isfinite(v) & (weight > 0)
            with np.errstate(over='ignore'):
                wsum[ok] = np.nansum([wsum[ok], v[ok] * weight[ok] * weight[ok]], axis=0)
            acc[name]['wt'][ok] += weight[ok]

    def _finalize_variances(self, acc):
        """stcal ``Resample.finalize_resample_variance`` and ``ERR`` from the variances
        (spectral grid: the variances scale by 1 / pixel_scale_ratio)."""
        scaling = 1.0 if self.pixel_scale_ratio is None else 1.0 / self.pixel_scale_ratio
        out = {}
        with np.errstate(invalid='ignore', divide='ignore'):
            for name in VARIANCES:
                w = acc[name]['wt']
                out[name] = (acc[name]['wsum'] / (w * w * scaling)).astype(np.float32)
        comps = [out[name] for name in VARIANCES]
        err = np.sqrt(np.nansum(comps, axis=0))
        err[np.all(np.isnan(comps), axis=0)] = np.nan
        out['err'] = err
        return out
