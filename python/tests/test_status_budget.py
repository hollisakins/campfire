"""`campfire status` cloud-budget lines (get_storage_budget payload, P2 classes)."""

from campfire.cli import format_budget_lines

def test_budget_lines_without_classes_is_one_line():
    out = format_budget_lines({"total_bytes": 2048, "cap_bytes": 4096, "pct_used": 50.0})
    assert len(out) == 1 and "2.0 KB of 4.0 KB (50.0%)" in out[0]

def test_budget_lines_with_classes():
    out = format_budget_lines({"total_bytes": 3300, "cap_bytes": 10000, "pct_used": 33.0,
                               "by_class": {"live": {"count": 2, "bytes": 3000},
                                            "archive": {"count": 1, "bytes": 300},
                                            "collected": {"count": 1, "bytes": 0, "logical_bytes": 7000}}})
    assert len(out) == 2
    assert "live 2.9 KB" in out[1] and "archive 300" in out[1] and "1 collected (no bytes)" in out[1]

def test_budget_lines_no_collected_omits_it():
    out = format_budget_lines({"total_bytes": 0, "cap_bytes": 1, "pct_used": 0,
                               "by_class": {"live": {"count": 0, "bytes": 0}, "archive": {"count": 0, "bytes": 0},
                                            "collected": {"count": 0, "bytes": 0, "logical_bytes": 0}}})
    assert "collected" not in out[1]

def test_budget_lines_gc_pending_shown_only_when_present():
    base = {"total_bytes": 0, "cap_bytes": 1, "pct_used": 0}
    classes = {"live": {"count": 1, "bytes": 1000}, "archive": {"count": 0, "bytes": 0},
               "collected": {"count": 0, "bytes": 0, "logical_bytes": 0}}
    out = format_budget_lines({**base, "by_class": {**classes, "gc_pending": {"count": 0, "bytes": 0}}})
    assert "gc-pending" not in out[1]
    out = format_budget_lines({**base, "by_class": {**classes, "gc_pending": {"count": 3, "bytes": 900}}})
    assert "gc-pending 900" in out[1] and "(3)" in out[1]
