"""The NIRSpec MSA forward model: a target's exposures modelled as a sum of source components, each
with its own spectrum, seen through the optics model of the MSA (``optics``) and each exposure's
own geometry.

    exposure k's value at pixel p =
        sum over components i of  tau_k(lam) f_i(lam) L_ik(y, lam)
        (minus the same for each frame subtracted from it, for a nod difference)
        + the other slitlets' predicted light (held)  [+ s x the master background, single frames]

L_ik is component i's light at the pixel per unit of its total light (``components``), tau_k the
throughput no source changes (``ForwardModel.throughput_fixed``), f_i the component's total light
per wavelength. With the shapes fixed the model is linear in every f_i, so all spectra are solved
together (``ForwardModel.solve``); shapes are fitted around that solve.

Stage 3's profile fit is one application: a ``Fit`` of a single ``components.SlitGaussian`` on the
nod differences, with its priors on the shape (``profile_fit.ProfileModel.shape_fit``).

Modules: ``model`` (the data, placement, design matrix and solve), ``components`` (point, round
Gaussian, Sersic, the pipeline's Gaussian along the slit, and the general projection), ``products``
(``load``: a target's exposures from its deployed canonicals, prepared as stage 3 prepared them),
``params`` (free, fixed, tied parameters and priors), ``fit`` (the shape fit), ``plots`` (data, model
and residual per exposure).
"""
from campfire_pipeline.nirspec.forward.products import files_for, load  # noqa: F401
from campfire_pipeline.nirspec.forward.params import (JointPrior, LogNormal, Normal, Param, Tied,  # noqa: F401
                                                      Uniform)
from campfire_pipeline.nirspec.forward.fit import Fit, FitResult  # noqa: F401
from campfire_pipeline.nirspec.forward.components import (Component, Gaussian, Point, Sersic, SlitGaussian,  # noqa: F401
                                                          Sum, project, slit_offsets)
from campfire_pipeline.nirspec.forward.model import ForwardModel, Solution  # noqa: F401
