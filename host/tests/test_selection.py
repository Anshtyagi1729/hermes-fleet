"""Tests for the backend selection policy.

Run with:   cd host && uv run pytest -v -k selection
"""

from collections import Counter

import pytest

from app.load import LoadTracker
from app.registry import NodeView
from app.selection import pick_backend


def make_node(node_id: str) -> NodeView:
    return NodeView(
        id=node_id,
        name=node_id,
        ip="127.0.0.1",
        port=11434,
        backend="ollama",
        gpu="fake",
        vram_total_mb=8192,
        vram_used_mb=0,
        ram_total_mb=16384,
        ram_used_mb=0,
        models=["hermes3"],
        online=True,
        enabled=True,
    )


def test_raises_on_empty_candidates():
    with pytest.raises(ValueError):
        pick_backend([], LoadTracker())


def test_single_candidate_is_always_picked():
    a = make_node("a")
    assert pick_backend([a], LoadTracker()).id == "a"


def test_picks_the_least_loaded_node():
    a, b, c = make_node("a"), make_node("b"), make_node("c")
    tracker = LoadTracker()
    tracker.start("a")
    tracker.start("a")
    tracker.start("a")
    tracker.start("c")
    # a=3 in-flight, b=0, c=1 -> b must win, no contest.

    chosen = pick_backend([a, b, c], tracker)
    assert chosen.id == "b"


def test_ties_are_spread_across_candidates_not_always_the_first():
    """The one that actually matters: an idle fleet must not pile every
    request onto whichever node happens to sort first.
    """
    nodes = [make_node("a"), make_node("b"), make_node("c")]
    tracker = LoadTracker()  # everyone at 0 in-flight -> a 3-way tie

    picks = Counter(pick_backend(nodes, tracker).id for _ in range(300))

    assert len(picks) > 1, (
        f"all {sum(picks.values())} picks went to {dict(picks)} -- "
        "tie-breaking must be random, not 'always the first candidate'"
    )
    # With 300 draws across 3 equally-likely nodes, each should land
    # somewhere near 100. A floor of 50 gives real slack for randomness
    # while still catching "always picks the same one or two."
    for node_id, count in picks.items():
        assert count > 50, f"{node_id} got {count}/300 picks -- too skewed for a fair tie-break"


def test_only_the_tied_minimum_is_eligible():
    """Loaded nodes must never be picked over an idle one, even by chance."""
    a, b = make_node("a"), make_node("b")
    tracker = LoadTracker()
    tracker.start("a")  # a=1, b=0 -- b is strictly better every single time

    picks = {pick_backend([a, b], tracker).id for _ in range(50)}
    assert picks == {"b"}
