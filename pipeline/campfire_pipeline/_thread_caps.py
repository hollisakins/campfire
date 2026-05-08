"""
Pin BLAS / OpenMP thread counts to 1 by default.

Imported as the *first* thing by every ``cfpipe`` entry point, before
matplotlib/numpy/astropy. The pipeline parallelizes via fork-pool
processes; without this, each worker spawns one BLAS thread per visible
core. On high-core HPC nodes (e.g. candide, 64 cores) the collective
thread count exhausts ``RLIMIT_NPROC`` and surfaces as cascading
``OpenBLAS blas_thread_init: pthread_create failed`` errors plus
spurious ``KeyboardInterrupt`` tracebacks from workers that lost a
thread-spawn race inside an astropy.modeling call.

Setting these via ``os.environ.setdefault`` means anything the user
already exported wins. ``setup_environment`` also applies the same
defaults for the programmatic-import path; this module is the
defense-in-depth that runs before any module that might trigger a BLAS
call at import time.
"""

import os

_BLAS_THREAD_VARS = (
    'OPENBLAS_NUM_THREADS',
    'MKL_NUM_THREADS',
    'OMP_NUM_THREADS',
    'NUMEXPR_NUM_THREADS',
    'VECLIB_MAXIMUM_THREADS',
    'BLIS_NUM_THREADS',
)

for _v in _BLAS_THREAD_VARS:
    os.environ.setdefault(_v, '1')
