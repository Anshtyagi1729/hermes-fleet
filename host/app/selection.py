"""Backend selection policy: given several nodes that can serve a model,
which one gets the next request?

Kept separate from registry.py (which only knows "who qualifies") and from
load.py (which only knows "how busy is each node") on purpose -- this file
is the one place that combines the two into an actual decision. If you ever
want a different policy (round-robin, weighted by VRAM, sticky sessions),
this is the only file that changes.
"""

import random

from .load import LoadTracker
from .registry import NodeView


def pick_backend(candidates: list[NodeView], tracker: LoadTracker) -> NodeView:
    """Pick which node should receive the next request.

    TODO(you): implement least-in-flight selection.

    `candidates` is already filtered (online, enabled, has the requested
    model) by registry.candidates_for_model() -- this function only decides
    WHICH of them goes next, nothing else.

    What it needs to do:

    1. Raise ValueError if `candidates` is empty. The caller (the router, in
       M2's next piece) is expected to have already checked this and turned
       it into a "no backend available" response to Hermes -- reaching this
       function with nothing to choose from means a caller bug, not a normal
       runtime condition.

    2. Find the minimum in-flight count across all candidates, using
       tracker.inflight(node.id) for each one.

    3. Return a node whose in-flight count equals that minimum.

       The part that's easy to get subtly wrong: if you write
       `min(candidates, key=lambda n: tracker.inflight(n.id))`, Python's
       min() returns the FIRST element that achieves the minimum when there
       are ties. registry.list_nodes()/candidates_for_model() order nodes by
       name -- so on an idle fleet (every node at 0 in-flight, which is the
       common case), you'd send every single request to whichever node is
       alphabetically first, and the rest would sit unused. That defeats the
       entire point of load-based routing.

       Fix: collect ALL candidates tied at the minimum, then pick one at
       random from that tied set with random.choice(). An idle fleet should
       spread new requests across every idle node, not pile them on one.
    """
    if not candidates:
        raise ValueError("pick_backend called with no candidates")

    min_inflight = min(tracker.inflight(n.id) for n in candidates)
    tied = [n for n in candidates if tracker.inflight(n.id) == min_inflight]
    return random.choice(tied)
