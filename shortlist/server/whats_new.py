"""The release notes an owner has not read yet, shown once in a dialog after an upgrade.

The notes are the GitHub releases' own bodies (`version_check.published_releases`), not CHANGELOG.md.
CI cuts each release from its changelog section, so the text starts out identical — but a release
can be edited after it ships, and the edited one is what an upgrading owner should read.

One setting holds the whole state: the newest version whose notes the owner has closed. Nothing is
pending while that is the running version, so a server that is up to date never asks GitHub. It is
recorded per SERVER, not per browser, so closing the dialog anywhere closes it everywhere.
"""

from __future__ import annotations

from shortlist.server.settings_store import SettingsStore
from shortlist.server.version_check import published_releases, release_tuple

#: The newest version whose notes the owner closed. ``""`` means an install set up before this
#: existed: it upgraded from a version it never recorded, so only the running version's notes show.
SEEN_KEY = "app.release_notes_seen"


def initialise(store: SettingsStore, current_version: str) -> None:
    """Record a starting point on first boot. Runs on every boot; only the first one writes.

    Args:
        store: the settings store.
        current_version: the running build's version.
    """
    if store.has_row(SEEN_KEY):
        return
    # A fresh install has not finished the wizard at its first boot, and has nothing to catch up on.
    store.set(SEEN_KEY, "" if store.get("setup.completed") else current_version)


def pending(store: SettingsStore, current_version: str) -> list[dict]:
    """The releases the owner has not read, newest first. ``[]`` when there are none, or GitHub is down.

    Args:
        store: the settings store.
        current_version: the running build's version.

    Returns:
        ``{version, url, published_at, notes}`` for every release after the last one read, up to and
        including the running version.
    """
    current = release_tuple(current_version)
    seen_value = store.get(SEEN_KEY)
    if current is None or seen_value is None:
        return []
    seen = release_tuple(seen_value) if seen_value else None
    if seen is not None and seen >= current:
        return []

    unread = []
    for release in published_releases():
        version = release_tuple(release["version"])
        if version is None or version > current:
            continue
        if (seen is None and version == current) or (seen is not None and version > seen):
            unread.append(release)
    return sorted(unread, key=lambda release: release_tuple(release["version"]), reverse=True)


def mark_seen(store: SettingsStore, version: str, current_version: str) -> None:
    """Record that the owner closed the notes up to ``version``. Never moves the record backwards.

    Takes the version the dialog SHOWED rather than the one running now: a tab left open across an
    upgrade would otherwise mark the newer release read before anyone saw it.

    Args:
        store: the settings store.
        version: the version whose notes were shown.
        current_version: the running build's version.

    Raises:
        ValueError: ``version`` is not a version, or is newer than the running build.
    """
    closed, current = release_tuple(version), release_tuple(current_version)
    if closed is None or current is None or closed > current:
        raise ValueError(f"not a version this build can have shown: {version!r}")
    seen = release_tuple(store.get(SEEN_KEY) or "")
    if seen is None or closed > seen:
        store.set(SEEN_KEY, version)
