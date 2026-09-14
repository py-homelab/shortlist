"""The fake PMS refuses and allows collection renames the way a real one was measured to.

`tests/fixtures/pms_collection_title_tags.json` recorded that a collection's title is a server-wide tag
that outlives the collection: a rename onto a name any other tag still has answers 409, and a create
with that name reuses the tag instead. A fake that renames anything lets every test pass against a
server that never refuses, which is how "a {top_seed} row keeps its old name every night" shipped.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.fakes.fake_plex import make_fake_plex, seed_state

FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures" / "pms_collection_title_tags.json").read_text())
MARKER = "​‌​"


@pytest.fixture
def pms():
    state = seed_state()
    return state, TestClient(make_fake_plex(state))


def _create(client: TestClient, state, section_id: int, title: str) -> int:
    item = next(iter(state.items_in(section_id)))
    response = client.post(
        "/library/collections",
        params={
            "type": 1,
            "title": title,
            "smart": 0,
            "sectionId": section_id,
            "uri": f"server://x/library/metadata/{item}",
        },
    )
    assert response.status_code == 200
    return max(state.collections)


def _rename(client: TestClient, section_id: int, rating_key: int, title: str):
    return client.put(
        f"/library/sections/{section_id}/all", params={"type": 18, "id": rating_key, "title.value": title}
    )


class TestRenamesMatchTheRecording:
    def test_the_recorded_conflict_is_what_the_fake_answers(self, pms):
        state, client = pms
        gone = _create(client, state, state.section_id, "🎯 Gone" + MARKER)
        assert client.delete(f"/library/collections/{gone}").status_code == 200
        row = _create(client, state, state.section_id, "🎯 Row" + MARKER)

        response = _rename(client, state.section_id, row, "🎯 Gone" + MARKER)

        recorded = FIXTURE["rename_conflict_response"]
        assert response.status_code == recorded["status"]
        assert response.text == recorded["body"]
        assert state.collections[row].title == "🎯 Row" + MARKER, "a refused rename must not change the title"

    def test_a_deleted_collections_name_can_still_be_created(self, pms):
        state, client = pms
        gone = _create(client, state, state.section_id, "Gone")
        client.delete(f"/library/collections/{gone}")

        again = _create(client, state, state.section_id, "Gone")

        assert state.collections[again].title == "Gone"

    def test_a_never_used_name_is_renamed_onto(self, pms):
        state, client = pms
        row = _create(client, state, state.section_id, "Before")

        assert _rename(client, state.section_id, row, "After").status_code == 200
        assert state.collections[row].title == "After"

    def test_a_name_freed_by_a_successful_rename_can_be_taken(self, pms):
        state, client = pms
        first = _create(client, state, state.section_id, "Shared Name")
        _rename(client, state.section_id, first, "Moved On")
        second = _create(client, state, state.section_id, "Other")

        assert _rename(client, state.section_id, second, "Shared Name").status_code == 200

    def test_a_live_name_in_another_library_refuses_a_rename_but_not_a_create(self, pms):
        state, client = pms
        _create(client, state, state.section_id, "Twin")
        tv_row = _create(client, state, state.show_section_id, "Something Else")

        assert _rename(client, state.show_section_id, tv_row, "Twin").status_code == 409
        twin = _create(client, state, state.show_section_id, "Twin")
        assert state.collections[twin].title == "Twin"

    def test_the_name_check_ignores_ascii_case_like_the_tags_column(self, pms):
        """`tags.tag` is `COLLATE NOCASE`, which folds ASCII letters only."""
        state, client = pms
        gone = _create(client, state, state.section_id, "Hidden Gems")
        client.delete(f"/library/collections/{gone}")
        row = _create(client, state, state.section_id, "Row")

        assert _rename(client, state.section_id, row, "hidden gems").status_code == 409

    def test_twins_renamed_together_both_succeed(self, pms):
        """The shape a fixed-name row delivered into two libraries takes: both share one tag, so the
        second rename lands on a name that tag already carries, and that is not a conflict."""
        state, client = pms
        movies = _create(client, state, state.section_id, "Hidden Gems")
        shows = _create(client, state, state.show_section_id, "Hidden Gems")

        assert _rename(client, state.section_id, movies, "Buried Treasure").status_code == 200
        assert _rename(client, state.show_section_id, shows, "Buried Treasure").status_code == 200

    def test_reclaiming_an_orphaned_name_through_a_helper_keeps_the_row(self, pms):
        state, client = pms
        gone = _create(client, state, state.section_id, "Wanted")
        client.delete(f"/library/collections/{gone}")
        row = _create(client, state, state.section_id, "Current")
        items = list(state.collections[row].item_keys)

        helper = _create(client, state, state.section_id, "Wanted")
        assert _rename(client, state.section_id, helper, "Retired 1a2b").status_code == 200
        assert _rename(client, state.section_id, row, "Wanted").status_code == 200
        client.delete(f"/library/collections/{helper}")

        assert state.collections[row].title == "Wanted"
        assert state.collections[row].item_keys == items

    def test_renaming_one_twin_leaves_the_other_twins_title(self, pms):
        state, client = pms
        movies = _create(client, state, state.section_id, "X")
        shows = _create(client, state, state.show_section_id, "X")

        assert _rename(client, state.section_id, movies, "Y").status_code == 200

        assert state.collections[shows].title == "X"
        assert _rename(client, state.show_section_id, shows, "Z").status_code == 200
        assert state.collections[movies].title == "Y"
