"""
Keyframe + delta merger for F1 live timing data.

F1's SignalR stream sends an initial keyframe (full state snapshot) when you
subscribe, then incremental JSON deltas that must be deep-merged onto it.

The delta format uses nested dicts with string keys. A value of None or
an explicit delete marker means "remove this key". Otherwise, recursively
merge dicts and overwrite leaf values.

This is the trickiest part of the protocol — get this wrong and your state
drifts from reality within minutes.
"""

import copy
from typing import Any


def deep_merge(base: Any, update: Any) -> Any:
    """Recursively merge `update` into `base`, returning the merged result.

    Rules (reverse-engineered from F1's protocol):
    - If both are dicts: recurse into matching keys
    - If update value is a dict but base value isn't: replace with update
    - Leaf values in update overwrite base
    - Keys present in base but not in update are preserved
    """
    if not isinstance(base, dict) or not isinstance(update, dict):
        return update

    merged = copy.copy(base)
    for key, value in update.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


class StateStore:
    """Maintains the current merged state for each subscribed topic.

    Usage:
        store = StateStore()
        store.set_keyframe("TimingData", initial_data)  # from Subscribe response
        current = store.apply_delta("TimingData", delta)  # from feed events
        full_state = store.get("TimingData")
    """

    def __init__(self):
        self._state: dict[str, Any] = {}

    def set_keyframe(self, topic: str, data: Any):
        """Set the initial full state for a topic (from Subscribe CompletionMessage)."""
        self._state[topic] = copy.deepcopy(data)

    def apply_delta(self, topic: str, delta: Any) -> Any:
        """Merge a delta update into the current state and return the new state."""
        if topic not in self._state:
            self._state[topic] = delta
        else:
            self._state[topic] = deep_merge(self._state[topic], delta)
        return self._state[topic]

    def get(self, topic: str) -> Any:
        """Get the current full state for a topic."""
        return self._state.get(topic)

    def topics(self) -> list[str]:
        """List all topics with stored state."""
        return list(self._state.keys())
