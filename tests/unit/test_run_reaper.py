"""A run the previous process died inside must not still claim to be running.

`create_app` already aborts orphaned runs at boot; this pins that behaviour, which had no test. A
run only ever leaves `running` via the code that finishes it, so without the reap a container
restart (or an OOM kill) would leave a run that ended days ago reporting itself as in progress for
ever — the Runs page spinning on a run with no process behind it, and "is a run happening right
now?" (which gates starting another) answering wrongly.

Nothing on Plex needs repairing here: a run that dies leaves its rows DELIVERED BUT UNPROMOTED
(plex-safety rule 1), so a half-finished run is visible to nobody. This is about the record telling
the truth.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from shortlist.server.db.models import Run
from shortlist.server.main import create_app

pytestmark = pytest.mark.integration


def _boot(tmp_path: Path) -> TestClient:
    with TestClient(create_app(config_dir=tmp_path)) as client:
        return client


def _seed_run(client: TestClient, status: str) -> int:
    with client.app.state.sessions() as session:
        run = Run(status=status, started_at=datetime.now(UTC), trigger="manual")
        session.add(run)
        session.commit()
        return run.id


def _run(client: TestClient, run_id: int) -> Run:
    with client.app.state.sessions() as session:
        return session.get(Run, run_id)


@pytest.mark.parametrize("stale_status", ["running", "queued"])
def test_a_run_left_mid_flight_is_aborted_on_the_next_boot(tmp_path: Path, stale_status: str):
    first = _boot(tmp_path)
    run_id = _seed_run(first, stale_status)

    second = _boot(tmp_path)  # same config dir -> same database, exactly as a container restart

    assert _run(second, run_id).status == "aborted"


def test_an_aborted_run_gets_a_finish_time_so_it_stops_looking_live(tmp_path: Path):
    """Without finished_at the UI has a terminal run with no end — it renders as still going."""
    first = _boot(tmp_path)
    run_id = _seed_run(first, "running")

    second = _boot(tmp_path)

    assert _run(second, run_id).finished_at is not None


def test_a_finished_run_is_left_exactly_as_it_was(tmp_path: Path):
    """The reap must only ever touch runs that never got to write their own outcome."""
    first = _boot(tmp_path)
    done = _seed_run(first, "ok")
    failed = _seed_run(first, "error")

    second = _boot(tmp_path)

    assert _run(second, done).status == "ok"
    assert _run(second, failed).status == "error"


def test_a_crash_queues_a_consistency_pass_so_it_does_not_wait_for_the_schedule(tmp_path: Path):
    """A run that died left rows delivered but UNPROMOTED — safe, but nobody sees them and nothing
    would put the server right until the next schedule, potentially a day away."""
    from shortlist.server.db.models import Job

    first = _boot(tmp_path)
    _seed_run(first, "running")

    second = _boot(tmp_path)

    with second.app.state.sessions() as session:
        assert [j.kind for j in session.query(Job).all()] == ["privacy.sync"]


def test_a_clean_boot_queues_nothing(tmp_path: Path):
    """Otherwise every restart would fire a server-wide share-filter pass for no reason — minutes of
    throttled plex.tv writes on a container that simply restarted."""
    from shortlist.server.db.models import Job

    first = _boot(tmp_path)
    _seed_run(first, "ok")

    second = _boot(tmp_path)

    with second.app.state.sessions() as session:
        assert session.query(Job).count() == 0


class TestAScheduledRunCutShortIsFinishedOnce:
    """A scheduled run a restart cut short rebuilds only the people it never reached, once (owner decision
    2026-09-14). On SFLIX Watchtower replaced the container at 04:30 while the 03:30 run was half way,
    and 23 of 46 people went a day without a rebuild, the same people every night an image was published.
    Only once: the resumed run is not resumed again, so a crash loop cannot re-curate the server over and over."""

    @pytest.fixture
    def started(self, monkeypatch) -> list[dict]:
        from shortlist.server.services.run_service import RunService

        calls: list[dict] = []

        async def start_run(self, **kwargs):
            calls.append(kwargs)
            return 0

        monkeypatch.setattr(RunService, "start_run", start_run)
        return calls

    @staticmethod
    def _seed(client: TestClient, *, trigger="schedule", dry_run=False, hours_ago=1.0, reached=("amy",)) -> dict:
        from datetime import timedelta

        from shortlist.server.db.models import Collection, RunUser, User

        with client.app.state.sessions() as session:
            people = {
                slug: User(plex_account_id=1000 + i, username=slug, slug=slug, enabled=True)
                for i, slug in enumerate(("amy", "bob", "cat", "dan"))
            }
            people["dan"].enabled = False  # turned off since the run started
            rows = [
                Collection(slug="night_a", name="Night A", enabled=True),
                Collection(slug="night_b", name="Night B", enabled=True),
                Collection(slug="other_cron", name="Other cron", enabled=True),
            ]
            session.add_all([*people.values(), *rows])
            session.flush()
            run = Run(
                status="running",
                trigger=trigger,
                dry_run=dry_run,
                started_at=datetime.now(UTC) - timedelta(hours=hours_ago),
                stats={
                    "expected_users": [
                        {"slug": s, "username": s, "display_name": s} for s in ("amy", "bob", "cat", "dan")
                    ],
                    "expected_rows": [
                        {"slug": "night_a", "title": "Night A", "build": "picked"},
                        {"slug": "night_b", "title": "Night B", "build": "picked"},
                    ],
                },
            )
            session.add(run)
            session.flush()
            for slug in reached:
                session.add(RunUser(run_id=run.id, user_id=people[slug].id, status="ok"))
            session.commit()
            return {"users": {s: u.id for s, u in people.items()}, "rows": {r.slug: r.id for r in rows}}

    def test_it_rebuilds_only_the_people_the_run_never_reached(self, tmp_path: Path, started):
        ids = self._seed(_boot(tmp_path))

        _boot(tmp_path)

        assert started == [
            {
                "trigger": "resume",
                "dry_run": False,
                "user_ids": sorted([ids["users"]["bob"], ids["users"]["cat"]]),
                "collection_ids": sorted([ids["rows"]["night_a"], ids["rows"]["night_b"]]),
            }
        ]

    def test_the_consistency_pass_is_still_queued(self, tmp_path: Path, started):
        from shortlist.server.db.models import Job

        self._seed(_boot(tmp_path))
        second = _boot(tmp_path)

        with second.app.state.sessions() as session:
            assert [j.kind for j in session.query(Job).all()] == ["privacy.sync"]

    @pytest.mark.parametrize(
        "seed",
        [
            {"trigger": "resume"},  # the resumed run itself: once, never a loop
            {"trigger": "manual"},  # a run someone started by hand is theirs to start again
            {"dry_run": True},  # safe mode wrote nothing to rebuild
            {"hours_ago": 21},  # the row's next scheduled run is the better answer by now
            {"reached": ("amy", "bob", "cat")},  # everyone enabled was reached before the restart
        ],
        ids=["resumed-run", "manual", "dry-run", "too-old", "all-reached"],
    )
    def test_nothing_is_rerun_when(self, tmp_path: Path, started, seed):
        self._seed(_boot(tmp_path), **seed)

        _boot(tmp_path)

        assert started == []

    def test_rows_turned_off_since_are_not_rebuilt(self, tmp_path: Path, started):
        from shortlist.server.db.models import Collection

        first = _boot(tmp_path)
        ids = self._seed(first)
        with first.app.state.sessions() as session:
            session.get(Collection, ids["rows"]["night_b"]).enabled = False
            session.commit()

        _boot(tmp_path)

        assert [call["collection_ids"] for call in started] == [[ids["rows"]["night_a"]]]
