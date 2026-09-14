"""Is a newer Shortlist released, and what did each release say? Cached, best-effort GitHub reads.

Powers the "update available" notification, the About panel, and the What's new dialog's notes.
That is the point of this module being the only one: there used to be a second implementation under
`services/`, with its own URL, its own cache and its own comparison, and the two provably disagreed
about this very build — the bell said "up to date" while the About panel said an update was available.

Every failure mode is swallowed — GitHub being down, rate-limited, or the repo having no releases
yet must never break the notifications endpoint or slow it down. The result is cached in-process
(single uvicorn worker) so the API is hit at most a few times a day, not on every 60-second poll.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
from loguru import logger

import shortlist

# `/releases/latest`, NOT `/releases`: the list endpoint's first entry can be a pre-release, so the
# deleted second implementation offered every `:dev` user an "update" to a beta they were already on.
_RELEASES_URL = "https://api.github.com/repos/stevezau/shortlist/releases/latest"
_OK_TTL = timedelta(hours=6)  # a successful check is fresh for 6h
_FAIL_TTL = timedelta(minutes=30)  # after a failure, retry sooner
_cache: dict[str, object] = {"at": None, "value": None}
# The LIST endpoint here is right, where it is wrong for "is there an update" above: it is the only one
# that returns several releases, and an owner who skipped versions needs every one in between. It is
# ordered by publish date, not version (v1.2.1 was published after v1.3.0), so callers sort.
_RELEASES_LIST_URL = "https://api.github.com/repos/stevezau/shortlist/releases?per_page=100"
_releases_cache: dict[str, object] = {"at": None, "value": None}
# Self-contained on purpose: `packaging` isn't in the slim runtime image, and a full PEP 440 parser is
# overkill for "is the released X.Y.Z newer than ours". Compare leading numeric segments; a pre-release
# suffix (.dev/.rc) on the SAME release is treated as equal (we won't nag 0.2.0 when on 0.2.0.dev0).


def release_tuple(version: str) -> tuple[int, ...] | None:
    """``"v1.8.0"`` -> ``(1, 8, 0)``; None when the string does not start with a version."""
    match = re.match(r"v?(\d+(?:\.\d+)*)", version.strip()) if isinstance(version, str) else None
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def _fetch_latest() -> dict | None:
    """The newest published release, or None on any error / no releases."""
    try:
        response = httpx.get(_RELEASES_URL, timeout=3, headers={"Accept": "application/vnd.github+json"})
        if response.status_code == 404:  # repo has no releases yet
            return None
        response.raise_for_status()
        data = response.json()
        return {"tag": str(data.get("tag_name") or ""), "url": str(data.get("html_url") or "")}
    except Exception as error:  # network, timeout, JSON, rate-limit — all non-fatal
        logger.debug("update check skipped: {}", error)
        return None


def _cached(cache: dict[str, object], fetch: Callable[[], object]) -> object:
    """``cache``'s value, re-fetched once its TTL has passed (sooner after a failure, which is None)."""
    now = datetime.now(UTC)
    at = cache["at"]
    ttl = _OK_TTL if cache["value"] is not None else _FAIL_TTL
    if not isinstance(at, datetime) or now - at > ttl:
        cache["value"] = fetch()
        cache["at"] = now
    return cache["value"]


def _latest_release() -> dict | None:
    """The cached ``{tag, url}``, refreshing it when the TTL has passed. None when unknown."""
    latest = _cached(_cache, lambda: _fetch_latest())
    return latest if isinstance(latest, dict) and latest.get("tag") else None


def _fetch_releases() -> list[dict] | None:
    """Every published release, or None on any error."""
    try:
        response = httpx.get(_RELEASES_LIST_URL, timeout=5, headers={"Accept": "application/vnd.github+json"})
        response.raise_for_status()
        return [
            {
                "version": str(release["tag_name"]).lstrip("vV"),
                "url": str(release.get("html_url") or ""),
                "published_at": str(release.get("published_at") or ""),
                "notes": str(release.get("body") or ""),
            }
            for release in response.json()
            if not release.get("draft")
            and not release.get("prerelease")
            and release_tuple(str(release.get("tag_name") or ""))
        ]
    except Exception as error:  # network, timeout, JSON, rate-limit — all non-fatal
        logger.debug("release notes fetch skipped: {}", error)
        return None


def published_releases() -> list[dict]:
    """Every published release as ``{version, url, published_at, notes}``, in GitHub's order. Cached.

    Returns:
        The releases, drafts and pre-releases excluded; ``[]`` when GitHub could not be read.
    """
    return _cached(_releases_cache, lambda: _fetch_releases()) or []


def check_for_update(current_version: str) -> dict | None:
    """`{latest, url}` when a newer release exists, else None. Cached; never raises.

    Args:
        current_version: the running app version (e.g. ``shortlist.__version__``).

    Returns:
        ``{"latest": "0.2.0", "url": "https://github.com/.../releases/tag/v0.2.0"}`` if the newest
        release parses as strictly greater than ``current_version``; otherwise ``None``.
    """
    latest = _latest_release()
    if latest is None:
        return None
    current, newest = release_tuple(current_version), release_tuple(latest["tag"])
    if current is None or newest is None or newest <= current:
        return None
    return {"latest": str(latest["tag"]).lstrip("vV"), "url": latest["url"]}


def current_version() -> str:
    """The running build's version string."""
    return shortlist.__version__


def build_provenance() -> tuple[str, str]:
    """The commit and ref this build was made from: ``(git_sha, git_branch)``, empty from source.

    Baked into the image as build args by CI (`docker/build-push-action`'s `build-args`). The values
    were read here long before anything set them, so every Docker install reported itself as a source
    checkout — the image carried the same facts as OCI labels, which nothing inside the container can
    read. Empty from a `pip install -e .` checkout, which is exactly how the two are told apart.
    """
    return os.environ.get("GIT_SHA", ""), os.environ.get("GIT_BRANCH", "")


def _install_type() -> str:
    """How this build was installed: ``dev_docker``, ``docker``, or ``source``."""
    git_sha, git_branch = build_provenance()
    if git_sha and git_branch == "dev":
        return "dev_docker"
    if git_sha:
        return "docker"
    return "source"


def version_info() -> dict:
    """What the About panel shows: running version, newest release, whether to update, install type.

    ``update_available`` is `check_for_update`'s answer, not a second comparison — the About panel and
    the notification bell must never be able to disagree about the same build.

    ``git_sha``/``git_branch`` are the FULL values; the footer shortens the sha itself. A version
    number alone cannot identify a `:dev` build — every push between two releases reports the same
    one — so "which build is this" had no answer without shelling into the container.
    """
    current = current_version()
    latest = _latest_release()
    git_sha, git_branch = build_provenance()
    return {
        "current_version": current,
        "latest_version": str(latest["tag"]).lstrip("vV") if latest else None,
        "update_available": check_for_update(current) is not None,
        "install_type": _install_type(),
        "git_sha": git_sha,
        "git_branch": git_branch,
    }
