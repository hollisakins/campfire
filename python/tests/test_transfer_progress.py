"""run_transfers with a caller-owned bar: one bar across passes, advanced by weight."""

import io

from tqdm import tqdm

from campfire.storage.transfer import run_transfers


def test_shared_bar_advances_by_weight_and_stays_open():
    bar = tqdm(total=60, file=io.StringIO())
    sizes = {'a': 10, 'b': 20, 'c': 30}

    def worker(item):
        if item == 'c':
            raise OSError('boom')
        return item

    first = run_transfers(['a', 'b'], worker, max_workers=2, progress=bar,
                          weight=sizes.__getitem__)
    second = run_transfers(['c'], worker, max_workers=1, progress=bar,
                           weight=sizes.__getitem__)

    assert (first.succeeded, second.failed) == (2, 1)
    assert bar.n == 30            # the failed item does not advance it
    bar.update(0)                 # still open: the caller closes it
    bar.close()


def test_own_bar_counts_items_by_default():
    result = run_transfers([1, 2, 3], lambda x: x, max_workers=2, show_progress=False)
    assert result.succeeded == 3
