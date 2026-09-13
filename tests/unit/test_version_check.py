"""The update check must be right about "newer", and silent about everything else."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from shortlist.server import version_check

RELEASES_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "github_releases.json"


@pytest.fixture(autouse=True)
def _reset_cache():
    version_check._cache.update(at=None, value=None)
    version_check._releases_cache.update(at=None, value=None)
    yield
    version_check._cache.update(at=None, value=None)
    version_check._releases_cache.update(at=None, value=None)


def _stub_latest(monkeypatch, value):
    monkeypatch.setattr(version_check, "_fetch_latest", lambda: value)


def test_reports_a_strictly_newer_release(monkeypatch):
    _stub_latest(monkeypatch, {"tag": "v0.2.0", "url": "https://example/rel"})
    result = version_check.check_for_update("0.1.0.dev0")
    assert result == {"latest": "0.2.0", "url": "https://example/rel"}


def test_silent_when_current_is_up_to_date(monkeypatch):
    _stub_latest(monkeypatch, {"tag": "v0.2.0", "url": "u"})
    assert version_check.check_for_update("0.2.0") is None  # equal
    version_check._cache.update(at=None, value=None)
    _stub_latest(monkeypatch, {"tag": "v0.1.0", "url": "u"})
    assert version_check.check_for_update("0.2.0") is None  # older release than running


def test_swallows_a_failed_fetch(monkeypatch):
    def boom():
        raise RuntimeError("github down")

    # _fetch_latest itself catches; simulate the caught result (None) and assert no raise, no update.
    monkeypatch.setattr(version_check, "_fetch_latest", lambda: None)
    assert version_check.check_for_update("0.1.0") is None
    # And the real _fetch_latest never propagates a network error.
    monkeypatch.setattr(version_check.httpx, "get", lambda *a, **k: (_ for _ in ()).throw(boom()))
    assert version_check._fetch_latest() is None


def test_caches_between_calls(monkeypatch):
    calls = {"n": 0}

    def counting():
        calls["n"] += 1
        return {"tag": "v9.9.9", "url": "u"}

    monkeypatch.setattr(version_check, "_fetch_latest", counting)
    version_check.check_for_update("0.1.0")
    version_check.check_for_update("0.1.0")
    assert calls["n"] == 1  # second call served from cache, not a second fetch


def test_a_bad_tag_is_ignored(monkeypatch):
    _stub_latest(monkeypatch, {"tag": "not-a-version", "url": "u"})
    version_check._cache.update(at=datetime.now(UTC), value={"tag": "not-a-version", "url": "u"})
    assert version_check.check_for_update("0.1.0") is None


class TestVersionInfo:
    """The About panel and the notification bell read the SAME check.

    There used to be a second implementation under `services/`, with its own URL (`/releases`, whose
    first entry can be a pre-release), its own cache and its own comparison — and on this very build
    the two disagreed: the bell said "up to date" while About offered an update.
    """

    def test_it_reports_the_running_build(self, monkeypatch):
        import shortlist

        _stub_latest(monkeypatch, None)
        info = version_check.version_info()
        assert info["current_version"] == shortlist.__version__
        assert info["latest_version"] is None  # GitHub unreachable / no releases
        assert info["update_available"] is False

    def test_update_available_agrees_with_the_bell(self, monkeypatch):
        """One answer, not two comparisons: whatever `check_for_update` says, this must say."""
        _stub_latest(monkeypatch, {"tag": "v9.9.9", "url": "https://example/rel"})
        info = version_check.version_info()
        assert info["latest_version"] == "9.9.9"
        assert info["update_available"] is (version_check.check_for_update(info["current_version"]) is not None)
        assert info["update_available"] is True

    def test_the_newest_release_is_reported_even_when_it_is_not_newer(self, monkeypatch):
        """About shows what the latest release IS; only `update_available` judges it."""
        _stub_latest(monkeypatch, {"tag": "v0.0.1", "url": "u"})
        info = version_check.version_info()
        assert info["latest_version"] == "0.0.1"
        assert info["update_available"] is False

    def test_install_type_comes_from_the_image_build_args(self, monkeypatch):
        monkeypatch.delenv("GIT_SHA", raising=False)
        monkeypatch.delenv("GIT_BRANCH", raising=False)
        assert version_check._install_type() == "source"
        monkeypatch.setenv("GIT_SHA", "abc123")
        assert version_check._install_type() == "docker"
        monkeypatch.setenv("GIT_BRANCH", "dev")
        assert version_check._install_type() == "dev_docker"

    def test_version_info_says_which_build_is_running(self, monkeypatch):
        """A `:dev` image reports the same version number for every push between two releases, so
        the version alone cannot identify a build. Without the commit, "which code is this" is only
        answerable by shelling into the container."""
        _stub_latest(monkeypatch, None)
        monkeypatch.setenv("GIT_SHA", "ba891f571edd4f1c4a17b02a40369e3af92ceb53")
        monkeypatch.setenv("GIT_BRANCH", "dev")
        info = version_check.version_info()
        assert info["git_sha"] == "ba891f571edd4f1c4a17b02a40369e3af92ceb53", "the FULL sha — the UI shortens it"
        assert info["git_branch"] == "dev"

    def test_version_info_is_honest_about_a_source_checkout(self, monkeypatch):
        """Empty, not a guess. A checkout has no build args, and inventing a sha from `git rev-parse`
        would report the developer's working tree rather than what is running."""
        _stub_latest(monkeypatch, None)
        monkeypatch.delenv("GIT_SHA", raising=False)
        monkeypatch.delenv("GIT_BRANCH", raising=False)
        info = version_check.version_info()
        assert (info["git_sha"], info["git_branch"], info["install_type"]) == ("", "", "source")

    def test_the_image_bakes_the_build_args_this_module_reads(self):
        """The regression this pair of fields exists for. `_install_type` read `GIT_SHA`/`GIT_BRANCH`
        from the day it was written, and NOTHING ever set them: the Dockerfile declared no ARG and CI
        passed no `build-args`, so every Docker install reported itself as a source checkout. The
        facts were in the image the whole time as OCI labels, which nothing inside a container can
        read. Runtime and build have to be asserted together or they drift apart again silently.
        """
        root = Path(__file__).resolve().parents[2]
        dockerfile = (root / "Dockerfile").read_text()
        workflow = (root / ".github" / "workflows" / "ci.yml").read_text()
        for var in ("GIT_SHA", "GIT_BRANCH"):
            assert f"ARG {var}=" in dockerfile, f"the image must declare {var} as a build arg"
            assert f"{var}=${var}" in dockerfile, f"{var} must reach the runtime as an env var, not just a build arg"
            assert f"{var}=${{{{ github." in workflow, f"CI must pass {var} to the build"


class _Response:
    def __init__(self, payload: object, status_code: int = 200):
        self._payload, self.status_code = payload, status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> object:
        return self._payload


def _recorded_releases() -> list[dict]:
    return json.loads(RELEASES_FIXTURE.read_text())["releases"]


class TestPublishedReleases:
    """The notes the What's new dialog shows: GitHub's own release bodies, read from the recorded list."""

    def test_reads_each_release_from_the_recorded_list(self, monkeypatch):
        monkeypatch.setattr(version_check.httpx, "get", lambda *a, **k: _Response(_recorded_releases()))

        releases = version_check.published_releases()

        newest = next(r for r in releases if r["version"] == "1.8.0")
        recorded = next(r for r in _recorded_releases() if r["tag_name"] == "v1.8.0")
        assert newest == {
            "version": "1.8.0",
            "url": "https://github.com/stevezau/shortlist/releases/tag/v1.8.0",
            "published_at": "2026-08-26T09:57:25Z",
            "notes": recorded["body"],
        }
        assert {r["version"] for r in releases} == {"1.8.0", "1.7.0", "1.6.1", "1.3.0", "1.2.1"}

    def test_skips_drafts_pre_releases_and_tags_that_are_not_versions(self, monkeypatch):
        base = _recorded_releases()[0]
        payload = [
            {**base, "tag_name": "v9.0.0", "prerelease": True},
            {**base, "tag_name": "v9.1.0", "draft": True},
            {**base, "tag_name": "nightly"},
            base,
        ]
        monkeypatch.setattr(version_check.httpx, "get", lambda *a, **k: _Response(payload))

        assert [r["version"] for r in version_check.published_releases()] == ["1.8.0"]

    def test_a_failed_fetch_is_an_empty_list_not_an_error(self, monkeypatch):
        monkeypatch.setattr(version_check.httpx, "get", lambda *a, **k: _Response({"message": "rate limited"}, 403))

        assert version_check.published_releases() == []

    def test_is_cached_between_calls_and_retried_sooner_after_a_failure(self, monkeypatch):
        calls = {"n": 0}

        def counting(*_a, **_k):
            calls["n"] += 1
            return _Response(_recorded_releases())

        monkeypatch.setattr(version_check.httpx, "get", counting)
        version_check.published_releases()
        version_check.published_releases()
        assert calls["n"] == 1

        version_check._releases_cache.update(at=datetime.now(UTC) - timedelta(minutes=31), value=None)
        version_check.published_releases()
        assert calls["n"] == 2  # a failure is retried after 30 minutes, not 6 hours
