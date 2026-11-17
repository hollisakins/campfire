"""Which exposures a visit is compared with in the outlier pass.

The outlier pass of every imaging instrument goes visit by visit: a visit's
exposures are flagged against themselves and the exposures of other visits
that overlap them. Which other exposures count is one rule for all
instruments, and it is here; each instrument supplies how it tells two
footprints overlap and how it reads a program from a file.
"""


def overlap_inputs(visit_files, filter_files, overlaps, program_of,
                   cross_program):
    """Exposures of other visits that overlap a visit's exposures.

    By default only exposures of the visit's own program are taken: the
    comparison then stays within data taken with one strategy and detector
    state, and an exposure is not drizzled once more for every other
    program's visit that happens to cover it. ``cross_program`` (the
    instrument's ``[<instrument>.outlier].cross_program_overlap``) takes
    overlapping exposures of every program.

    Parameters
    ----------
    visit_files : list of str
        The visit's exposures.
    filter_files : list of str
        Every exposure of the filter, the visit's included.
    overlaps : callable
        ``overlaps(a, b)`` is true when exposures ``a`` and ``b`` overlap on
        the sky.
    program_of : callable
        ``program_of(path)`` identifies the program of an exposure.
    cross_program : bool

    Returns the other exposures, each once, in the order they are met going
    through the visit's exposures and, for each, through ``filter_files``.
    """
    visit_set = set(visit_files)
    program = program_of(visit_files[0])
    found, seen = [], set()
    for visit_file in visit_files:
        for other in filter_files:
            if other in visit_set or other in seen:
                continue
            if not cross_program and program_of(other) != program:
                continue
            if overlaps(visit_file, other):
                found.append(other)
                seen.add(other)
    return found
