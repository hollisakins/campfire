"""
WCS and bounding box helpers for 2D spectral data.
"""

import numpy as np


def boundingbox_to_indices(data_shape, bounding_box):
    """
    Translate a bounding box to image indices.

    Takes a bounding_box (tuple of tuples: ((x1, x2), (y1, y2)) and
    a datamodel and calculates the range of indices in the X and Y dimensions
    of the overlap between the bounding box and the datamodel's data array.

    Parameters
    ----------
    data_shape : tuple
        The data shape for the input science datamodel.
    bounding_box : tuple of tuple
        Bounding box returned from wcs object.

    Returns
    -------
    xmin, xmax, ymin, ymax : int
        Range of indices of overlap between science data array and bounding box.
    """
    nrows, ncols = data_shape[-2:]
    x1, x2 = bounding_box[0]
    y1, y2 = bounding_box[1]
    xmin = int(min(x1, x2))
    xmin = max(xmin, 0)
    xmax = int(max(x1, x2)) + 1
    xmax = min(xmax, ncols)
    ymin = int(min(y1, y2))
    ymin = max(ymin, 0)
    ymax = int(max(y1, y2)) + 1
    ymax = min(ymax, nrows)
    return xmin, xmax, ymin, ymax


def wcs_to_dq(wcs_array, flag):
    """
    Create a DQ subarray corresponding to a failed open slitlet.

    The created array has the value `flag` wherever the WCS coordinates
    are valid (non-NaN) and 0 otherwise.

    Parameters
    ----------
    wcs_array : tuple of ndarray
        Image coordinates for the failed open region.
    flag : int
        DQ flag to set.

    Returns
    -------
    dq : ndarray of int
        Output DQ array.
    """
    dq = np.zeros((wcs_array[0].shape), dtype=np.uint32)
    non_nan = ~np.isnan(wcs_array[0])
    dq[non_nan] = flag
    return dq
