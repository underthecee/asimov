"""Concrete runtime tests of the real Rng.draw / Rng.at (real SHA-256 raw)."""
import copy
import os
import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from asiimov.rng import Rng  # noqa: E402

big = st.integers(min_value=-(2 ** 200), max_value=2 ** 200)
streams = st.text(max_size=12)


@settings(max_examples=400, deadline=None)
@given(seed=st.integers(), stream=streams, a=big, b=big, index=st.integers(min_value=0, max_value=10 ** 6))
def test_range_both_methods(seed, stream, a, b, index):
    lo, hi = min(a, b), max(a, b)
    r = Rng(seed)
    i, v = r.draw(stream, lo, hi)
    assert i == 0 and lo <= v <= hi
    assert lo <= r.at(stream, index, lo, hi) <= hi


@pytest.mark.parametrize("lo,hi", [(0, 0), (-5, -5), (2 ** 100, 2 ** 100), (-(2 ** 70), -(2 ** 70)), (0, 2 ** 64),
                                   (-(2 ** 64), -1), (-(2 ** 300), 2 ** 300), (0, 1), (-1, 0), (7, 2 ** 64 - 1)])
def test_fixed_ranges(lo, hi):
    r = Rng(1)
    for k in range(200):
        _, v = r.draw("s", lo, hi)
        assert lo <= v <= hi
        assert lo <= r.at("s", k, lo, hi) <= hi
        if lo == hi:
            assert v == lo


@settings(max_examples=200, deadline=None)
@given(stream=streams, a=big, gap=st.integers(min_value=1, max_value=2 ** 80), pre=st.booleans())
def test_malformed_range_raises_without_mutation(stream, a, gap, pre):
    r = Rng(3)
    if pre:
        r.draw(stream, 0, 9)
        r.draw("other", 0, 9)
    before = copy.deepcopy(r.counters)
    before_repr = repr(r.counters)
    with pytest.raises(ValueError):
        r.draw(stream, a, a - gap)
    with pytest.raises(ValueError):
        r.at(stream, 0, a, a - gap)
    assert r.counters == before and repr(r.counters) == before_repr


@settings(max_examples=200, deadline=None)
@given(seed=st.integers(), ops=st.lists(st.sampled_from(["a", "b", "", "c|d"]), max_size=40))
def test_independent_counters_and_index(seed, ops):
    r = Rng(seed)
    expect = {}
    for s in ops:
        snapshot = dict(r.counters)
        i, _ = r.draw(s, 0, 99)
        assert i == expect.get(s, 0)
        expect[s] = i + 1
        assert r.counters == expect
        assert {k: v for k, v in r.counters.items() if k != s} == {k: v for k, v in snapshot.items() if k != s}


@settings(max_examples=200, deadline=None)
@given(seed=st.integers(), stream=streams, index=st.integers(min_value=0, max_value=10 ** 9), pre=st.booleans())
def test_at_leaves_counters(seed, stream, index, pre):
    r = Rng(seed)
    if pre:
        r.draw(stream, 0, 5)
    before = repr(r.counters)
    r.at(stream, index, -10, 10)
    assert repr(r.counters) == before


@settings(max_examples=200, deadline=None)
@given(seed=st.integers(), stream=streams, n=st.integers(min_value=0, max_value=20), a=big, b=big)
def test_at_reproduces_draw(seed, stream, n, a, b):
    lo, hi = min(a, b), max(a, b)
    r = Rng(seed)
    for _ in range(n):
        r.draw(stream, lo, hi)
    i, v = r.draw(stream, lo, hi)
    assert i == n
    assert Rng(seed).at(stream, i, lo, hi) == v == r.at(stream, i, lo, hi)
