"""
Shared I/O utilities: logging and filename helpers.
"""

import os
from datetime import datetime


def log(*args, **kwargs):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}]", *args, **kwargs)


def files_to_glob(filenames):
    """
    Compress a list of filenames into a minimal glob-style string by collapsing
    varying tokens into {opt1,opt2,...} syntax.

    Example:
        ['jw_00002_nrs1_cal.fits', 'jw_00003_nrs2_cal.fits']
        -> 'jw_0000{2,3}_nrs{1,2}_cal.fits'
    """
    split = [f.split('_') for f in filenames]

    # Sanity check: all filenames should have the same number of tokens
    n_tokens = len(split[0])
    if not all(len(s) == n_tokens for s in split):
        raise ValueError("Filenames have inconsistent structure (different number of '_'-separated tokens)")

    result_tokens = []
    for i in range(n_tokens):
        # Unique values at this token position, preserving order
        seen = {}
        values = [seen.setdefault(s[i], s[i]) for s in split if s[i] not in seen]

        if len(values) == 1:
            result_tokens.append(values[0])
        else:
            prefix = os.path.commonprefix(values)
            suffixes = [v[len(prefix):] for v in values]
            result_tokens.append(f"{prefix}{{{','.join(suffixes)}}}")

    return '_'.join(result_tokens)
