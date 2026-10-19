"""local_first: annotation pulls never overwrite local config."""
from campfire.deploy.local_first import KEPT_LOCAL, UNCHANGED, WRITTEN, write_if_absent


def test_write_if_absent(tmp_path):
    p = tmp_path / 'a.reg'
    assert write_if_absent(p, '# hdr 1\nimage\npolygon(1,2,3)\n') == WRITTEN
    # same content lines, different comment header: identical, file left alone
    assert write_if_absent(p, '# hdr 2\nimage\npolygon(1,2,3)\n') == UNCHANGED
    assert p.read_text().startswith('# hdr 1')
    # different polygons: the local file wins
    assert write_if_absent(p, 'image\npolygon(9,9,9)\n') == KEPT_LOCAL
    assert 'polygon(1,2,3)' in p.read_text()


def test_write_if_absent_dry_run(tmp_path):
    p = tmp_path / 'b.reg'
    assert write_if_absent(p, 'image\n', dry_run=True) == WRITTEN
    assert not p.exists()
