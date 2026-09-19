"""One engine behind another: ask the first, and if it fails or answers with nothing, ask the second.

The failure is recorded in the pool's trace so the run page can say "the engine was down tonight;
this row is Shortlist's own" rather than silently delivering a differently-ranked row. A row whose
engine fails and whose fallback is off keeps what it has — the same as a row whose every source is
down, and for the same reason.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from loguru import logger

from shortlist.engine.recommender import Recommender, RecommendRequest, RecommendResult

if TYPE_CHECKING:
    from shortlist.engine.context import EngineContext


class FallbackRecommender:
    def __init__(self, primary: Recommender, secondary: Recommender):
        self.primary = primary
        self.secondary = secondary

    @property
    def name(self) -> str:
        return self.primary.name

    @property
    def serves_cold(self) -> bool:
        return self.primary.serves_cold

    def recommend(
        self,
        ctx: EngineContext,
        req: RecommendRequest,
        *,
        visible: Callable[[list[int]], set[int] | None] | None = None,
    ) -> RecommendResult:
        try:
            result = self.primary.recommend(ctx, req, visible=visible)
        except Exception as e:
            why = f"{type(e).__name__}: {e}"
            logger.warning(
                "{}: engine '{}' failed ({}) — falling back to '{}'",
                req.user.username,
                self.primary.name,
                why,
                self.secondary.name,
            )
        else:
            if result.ranked:
                return result
            why = "answered with no titles for these libraries"
            logger.info(
                "{}: engine '{}' {} — falling back to '{}'",
                req.user.username,
                self.primary.name,
                why,
                self.secondary.name,
            )
        fallback = self.secondary.recommend(ctx, req, visible=visible)
        fallback.stats.trace.setdefault("sources", []).insert(
            0,
            {
                "source": f"engine:{self.primary.name}",
                "status": "failed",
                "contributed": 0,
                "detail": f"{why}; fell back to {self.secondary.name}",
                "queries": [],
            },
        )
        return fallback
