"""
MIRI-specific orchestrator: step ordering, runner dispatch table.

v1 stub — empty step lists and runner dict. Phase entry points
(``run_process`` / ``run_combine`` / ``run_step``) are wired up so that
``cfpipe miri run --field <name>`` exercises the full plumbing
(workspace setup, filter resolution, CFP status scan, prefetch hook)
even though no steps actually execute yet.

As MIRI reduction steps land (see ``docs/design-miri-reduction.md``),
add entries to ``PROCESS_STEPS`` / ``COMBINE_STEPS`` and ``_RUNNERS``
following NIRCam's pattern. CRDS-touching steps (detector1, image2)
go in ``_CRDS_STEPS`` so ``run_step`` triggers ref-file prefetch.
"""

from campfire_pipeline.common.imaging import orchestrate as _orch
from campfire_pipeline.common.imaging.prefetch import prefetch_process_references


# (step_name, cfp_key) tuples in execution order. CFP key is ``None`` for
# steps that produce mosaic-level outputs stamped via CMPFRVER rather than
# CFP_* keys.
PROCESS_STEPS: list = []

COMBINE_STEPS: list = []

ALL_STEPS = PROCESS_STEPS + COMBINE_STEPS
STEP_NAMES: list = [name for name, _ in ALL_STEPS]

# Steps that hit CRDS — used by run_step() to decide when to pre-fetch
# reference files before parallel dispatch.
_CRDS_STEPS: set = set()


# Dispatch table: step name → callable that takes (field, config, filtname,
# n_processes, overwrite, status). Empty in v1.
_RUNNERS: dict = {}


# ---------------------------------------------------------------------------
# Phase entry points — thin delegates to the common skeleton
# ---------------------------------------------------------------------------

def run_process(field, config, filters=None, n_processes=1, overwrite=False):
    """Run all process-phase steps in order across each filter."""
    return _orch.run_process(
        field, config, PROCESS_STEPS, _RUNNERS, prefetch_process_references,
        filters=filters, n_processes=n_processes, overwrite=overwrite,
    )


def run_combine(field, config, filters=None, n_processes=1, overwrite=False):
    """Run all combine-phase steps in order across each filter."""
    return _orch.run_combine(
        field, config, COMBINE_STEPS, _RUNNERS,
        filters=filters, n_processes=n_processes, overwrite=overwrite,
    )


def run_step(step_name, field, config, filters=None, n_processes=1,
             overwrite=False):
    """Run a single named step across the field's filters."""
    return _orch.run_step(
        step_name, field, config, STEP_NAMES, _RUNNERS, _CRDS_STEPS,
        prefetch_process_references,
        filters=filters, n_processes=n_processes, overwrite=overwrite,
    )
