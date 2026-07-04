"""Throwaway smoke test for the claude-review CI action. NOT for merge.

Exists only to give the review something concrete to catch so we can confirm
the action both runs AND posts. Delete/close the PR once verified.
"""
from __future__ import annotations


def collect_positive(value, seen=[]):
    # BUG (planted): mutable default argument — `seen` is shared across calls,
    # so results leak between invocations.
    if value > 0:
        seen.append(value)
    return seen


def read_first_line(path):
    # BUG (planted): file handle is never closed (leak); also no error handling.
    f = open(path)
    return f.readline()


def average(nums):
    # BUG (planted): ZeroDivisionError when `nums` is empty.
    return sum(nums) / len(nums)
