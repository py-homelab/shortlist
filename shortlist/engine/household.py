"""Who watches under an account: an adult, a household sharing it with children, or a child.

A household watching under one Plex account — the living-room TV signed in as a parent, with the
children's cartoons and the grown-ups' dramas in one history — gets rows that are half cartoons.
Splitting children's titles into a row of their own fixes that for such a household and would be
wrong for everyone else: most people's history has a Pixar film or two, and a child's own account
is nothing BUT children's titles. So the split is decided per person, from their own viewing.

An engine that can tell (the external engine reports the counts over the last months of viewing)
supplies ``kids_titles`` and ``window_titles``; the thresholds are Shortlist's own settings
(`EngineConfig.family_*`), so the owner tunes them in one place whatever the engine. The owner's
per-person override wins over both. With no counts and no override, nobody is labelled: rows set to
"auto" keep children's titles, which is how every row behaved before this existed.
"""

from __future__ import annotations

from dataclasses import dataclass

from shortlist.engine.models import EngineConfig, UserProfile


@dataclass(frozen=True)
class Household:
    label: str | None  # adult | family | kids, or None when nothing could decide
    source: str  # override | engine | none
    kids_titles: int | None = None
    window_titles: int | None = None
    window_days: int | None = None
    engine_label: str | None = None

    @property
    def is_family(self) -> bool:
        return self.label == "family"

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "source": self.source,
            "kids_titles": self.kids_titles,
            "window_titles": self.window_titles,
            "window_days": self.window_days,
            "engine_label": self.engine_label,
        }


def classify(window_titles: int, kids_titles: int, cfg: EngineConfig) -> str:
    """adult | family | kids from the counts and the thresholds (see the module docstring)."""
    if window_titles < cfg.household_min_titles or window_titles <= 0:
        return "adult"
    share = kids_titles / window_titles
    if share > cfg.kids_account_min_share:
        return "kids"
    if share >= cfg.family_min_share and kids_titles >= cfg.family_min_kids_titles:
        return "family"
    return "adult"


def resolve_household(user: UserProfile, reported: dict | None, cfg: EngineConfig) -> Household:
    """The owner's override, else the engine's counts classified with Shortlist's thresholds, else
    unknown. ``reported`` is the engine's ``household`` object (``RecommendResult.household``)."""
    counts = {}
    if isinstance(reported, dict):
        try:
            counts = {
                "kids_titles": int(reported["kids_titles"]),
                "window_titles": int(reported["window_titles"]),
                "window_days": int(reported["window_days"]) if reported.get("window_days") is not None else None,
                "engine_label": str(reported.get("label")) if reported.get("label") else None,
            }
        except (KeyError, TypeError, ValueError):
            counts = {}
    if user.household_override in ("adult", "family", "kids"):
        return Household(label=user.household_override, source="override", **counts)
    if counts:
        return Household(label=classify(counts["window_titles"], counts["kids_titles"], cfg), source="engine", **counts)
    return Household(label=None, source="none")
