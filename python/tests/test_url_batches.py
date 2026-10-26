"""url_batches sizes PostgREST IN-filter batches by encoded URL length (#657)."""
from urllib.parse import quote

from campfire.deploy.url_batches import MAX_FILTER_BYTES, encoded_len, url_batches

KEY = 'data/products/nirspec/go1671/intermediates/jw01671001001_02101_00001_nrs1/jw01671001001_02101_00001_nrs1_cal.fits'


def test_encoded_len_counts_the_wire_bytes():
    # every slash becomes %2F; plus the quotes and comma around the value
    assert encoded_len(KEY) == len(quote(KEY, safe='')) + 9
    assert encoded_len('abc') == 3 + 9


def test_batches_preserve_order_and_cover_every_key_once():
    keys = [f'{KEY[:-5]}{i:05d}.fits' for i in range(1000)]
    batches = list(url_batches(keys))
    assert [k for b in batches for k in b] == keys
    assert all(batches)


def test_every_batch_fits_the_budget():
    keys = [f'{KEY[:-5]}{i:05d}.fits' for i in range(1000)]
    for b in url_batches(keys):
        assert sum(encoded_len(k) for k in b) <= MAX_FILTER_BYTES
    # the old fixed count of 200 keys of this shape did not
    assert sum(encoded_len(k) for k in keys[:200]) > 8192


def test_an_oversize_key_goes_out_alone():
    huge = 'x/' * 5000
    batches = list(url_batches(['a', huge, 'b'], max_bytes=100))
    assert batches == [['a'], [huge], ['b']]


def test_empty_input_yields_nothing():
    assert list(url_batches([])) == []
