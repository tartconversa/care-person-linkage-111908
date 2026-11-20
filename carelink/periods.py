"""左闭右开时段工具。None 表示开口端（无开始或无结束），不据此推断未来。

日期为 ISO 8601 字符串（YYYY-MM-DD），字典序即时间序。
"""
from __future__ import annotations


def _lo(a, b):
    """下界取大，None 表示负无穷。"""
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _hi(a, b):
    """上界取小，None 表示正无穷。"""
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def intersect(p, q):
    """两个时段的交集；不相交返回 None。时段为 (from_date, until_date)。"""
    lo, hi = _lo(p[0], q[0]), _hi(p[1], q[1])
    if lo is not None and hi is not None and lo >= hi:
        return None
    return (lo, hi)


def contains(period, day):
    """day 是否落在左闭右开时段内。"""
    lo, hi = period
    if lo is not None and day < lo:
        return False
    if hi is not None and day >= hi:
        return False
    return True


def union_contains(segments, day):
    """day 是否落在任一时段分片内。"""
    return any(contains(s, day) for s in segments)
