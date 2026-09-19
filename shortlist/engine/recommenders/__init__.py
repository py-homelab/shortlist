"""Candidate engines behind the ``Recommender`` protocol (see ``shortlist.engine.recommender``).

``builtin`` is Shortlist's own — TMDB similar/discover, Trakt, web search, the ranking dials. ``http``
talks to an engine running elsewhere. ``fallback`` chains one behind the other.
"""

from __future__ import annotations

from shortlist.engine.recommenders.builtin import BuiltinRecommender
from shortlist.engine.recommenders.fallback import FallbackRecommender
from shortlist.engine.recommenders.http import HttpRecommender

__all__ = ["BuiltinRecommender", "FallbackRecommender", "HttpRecommender"]
