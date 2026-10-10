"""Composition root: connects optional features to the core's extension points.

Lower layers expose ports (``ingest.fanout.add_message_inspector``, …) and
never import the features that plug into them. ``wire()`` is the one place
that does; it runs when the app is created and is idempotent.
"""

_wired = False


def wire() -> None:
    global _wired
    if _wired:
        return
    from backend.ingest import fanout
    from backend.scam.inspector import shield_inspector

    fanout.add_message_inspector(shield_inspector)
    _wired = True
