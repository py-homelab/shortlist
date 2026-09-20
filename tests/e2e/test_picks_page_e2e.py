"""E2E: the picks page on a phone.

The mobile audit drives every OWNER route; `/me` is a person's page and needs a person session, so
it was never in that sweep — and the action row overflowed its viewport on a phone for exactly that
long. This is the missing half: a real person, a real deck and grid, at the two widths the audit
uses, asking the browser whether anything is wider than the screen.
"""

from __future__ import annotations

import sqlite3

import pytest
from playwright.sync_api import Browser, Page

from shortlist.server.auth import SESSION_COOKIE, session_serializer
from tests.e2e.conftest import ShortlistApp

#: iPhone 14/15, and the narrowest phone still worth designing for — the audit's own two widths.
WIDTHS = [390, 320]
SUGGESTIONS = [
    (603, "movie", "The Matrix", 1999, "Because you watched Blade Runner"),
    (1396, "show", "Breaking Bad", 2008, "Because you watched The Wire"),
    (27205, "movie", "Inception", 2010, "Because you watched Tenet"),
    (1399, "show", "Game of Thrones", 2011, "Because you watched The Witcher"),
]


def _seed_suggestions(app: ShortlistApp) -> tuple[int, str]:
    """Write one person's request surface straight to the database, and return who they are.

    A run would take minutes and depends on what the fakes happen to suggest; this page reads
    `user_suggestions` and nothing upstream, so seeding it is the whole setup.
    """
    db = next(p for p in app.config_dir.rglob("*.db") if "backup" not in str(p))
    con = sqlite3.connect(db)
    try:
        user_id, account_id, username = con.execute(
            "SELECT id, plex_account_id, username FROM users WHERE removed_at IS NULL ORDER BY id LIMIT 1"
        ).fetchone()
        con.execute("DELETE FROM user_suggestions")
        for rank, (tmdb_id, media_type, title, year, reason) in enumerate(SUGGESTIONS):
            con.execute(
                "INSERT INTO user_suggestions (user_id, rank, tmdb_id, media_type, title, year, genres,"
                " rating, vote_count, poster_path, overview, language, reason, kids, sources, built_at)"
                " VALUES (?,?,?,?,?,?,'[\"Drama\"]',8.2,12000,'',?,'en',?,0,'engine:test',datetime('now'))",
                (
                    user_id,
                    rank,
                    tmdb_id,
                    media_type,
                    title,
                    year,
                    "A synopsis long enough to clamp, so the buttons sit where they really sit.",
                    reason,
                ),
            )
        con.commit()
    finally:
        con.close()
    return account_id, username


def _person_page(browser: Browser, app: ShortlistApp, width: int) -> Page:
    account_id, username = _seed_suggestions(app)
    cookie = session_serializer(app.session_secret).dumps(
        # `role` is what makes this a PERSON session rather than a pre-roles owner cookie.
        {"account_id": account_id, "username": username, "role": "person"}
    )
    context = browser.new_context(
        base_url=app.url, viewport={"width": width, "height": 800}, is_mobile=True, has_touch=True
    )
    context.add_cookies([{"name": SESSION_COOKIE, "value": cookie, "url": app.url}])
    page = context.new_page()
    page.set_default_timeout(60_000)
    page.goto("/me")
    explainer = page.get_by_role("button", name="Got it")  # the first-run explainer covers the deck
    explainer.click()
    explainer.wait_for(state="hidden")
    return page


def _too_wide(page: Page) -> list[dict]:
    """Every element sticking out past the viewport, as the browser measures it."""
    return page.evaluate(
        """() => {
            const limit = document.documentElement.clientWidth + 1;
            return [...document.querySelectorAll('body *')]
                .map((el) => ({ rect: el.getBoundingClientRect(), el }))
                .filter(({ rect }) => rect.width > 0 && (rect.right > limit || rect.left < -1))
                .slice(0, 5)
                .map(({ rect, el }) => ({
                    tag: el.tagName.toLowerCase(),
                    text: (el.textContent || '').trim().slice(0, 40),
                    right: Math.round(rect.right),
                    limit,
                }));
        }"""
    )


@pytest.mark.e2e
@pytest.mark.parametrize("width", WIDTHS, ids=[f"{w}px" for w in WIDTHS])
def test_the_deck_and_its_actions_fit_the_phone(browser: Browser, app: ShortlistApp, width: int) -> None:
    page = _person_page(browser, app, width)
    page.get_by_role("button", name="Request").first.wait_for()

    assert _too_wide(page) == []
    assert page.evaluate("() => document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    # One verb per action, wherever it appears (the labels themselves only show when there is room).
    for name in ("Reject", "Skip", "Later", "Request"):
        assert page.get_by_role("button", name=name, exact=True).first.is_visible(), name
    page.context.close()


@pytest.mark.e2e
@pytest.mark.parametrize("width", WIDTHS, ids=[f"{w}px" for w in WIDTHS])
def test_the_grid_cards_and_their_actions_fit_the_phone(browser: Browser, app: ShortlistApp, width: int) -> None:
    page = _person_page(browser, app, width)
    page.get_by_role("button", name="layout", exact=False).first.click()
    page.get_by_role("button", name="Request: The Matrix").wait_for()

    assert _too_wide(page) == []
    assert page.evaluate("() => document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    # Every card action is named for a screen reader, even where the label is an icon.
    for name in ("Reject: The Matrix", "Later: The Matrix", "Request: The Matrix"):
        assert page.get_by_role("button", name=name).first.is_visible(), name
    page.context.close()


def _hold_mid_swipe(page: Page, dx: int, dy: int) -> None:
    """Take the top card part-way in a direction and hold it there, as a thumb would mid-swipe.

    The events are dispatched at the card rather than driven through `page.mouse`: Playwright's
    synthetic mouse does not reach this element, and what is under test is the card's own pointer
    handling and the CSS that positions the labels, both of which this exercises exactly.
    """
    page.evaluate(
        """([dx, dy]) => {
            const card = [...document.querySelectorAll('.picks-stack .picks-card')].pop();
            const r = card.getBoundingClientRect();
            const at = (x, y) => new PointerEvent('pointermove', {
                bubbles: true, cancelable: true, clientX: x, clientY: y,
                pointerId: 1, button: 0, isPrimary: true, pointerType: 'touch',
            });
            card.setPointerCapture = () => {};
            card.dispatchEvent(new PointerEvent('pointerdown', {
                bubbles: true, cancelable: true, clientX: r.x + r.width / 2, clientY: r.y + r.height / 2,
                pointerId: 1, button: 0, isPrimary: true, pointerType: 'touch',
            }));
            for (let step = 1; step <= 8; step++) {
                card.dispatchEvent(at(r.x + r.width / 2 + (dx * step) / 8, r.y + r.height / 2 + (dy * step) / 8));
            }
        }""",
        [dx, dy],
    )


@pytest.mark.e2e
@pytest.mark.parametrize(
    ("label", "dx", "dy"),
    [("never", -110, 0), ("request", 110, 0), ("later", 0, -150), ("skip", 0, 150)],
    ids=["reject", "request", "later", "skip"],
)
def test_every_swipe_label_stays_on_screen_for_the_whole_swipe(
    browser: Browser, app: ShortlistApp, label: str, dx: int, dy: int
) -> None:
    """Each label sits on the edge the card is moving AWAY from. On the leading edge it rides off the
    screen with the card — which is exactly when the person is trying to read it — and the downward
    one disappeared behind the action bar, so a skip looked like it had no label at all."""
    page = _person_page(browser, app, 390)
    _hold_mid_swipe(page, dx, dy)

    overlay = page.locator(f'.picks-stack .picks-card [data-label="{label}"]').last
    assert float(overlay.evaluate("el => el.style.opacity || 0")) > 0.4, f"{label} barely showing"
    box, view = overlay.bounding_box(), page.viewport_size
    assert box and view
    assert box["x"] >= 0 and box["x"] + box["width"] <= view["width"], f"{label} off the side: {box}"
    assert box["y"] >= 0 and box["y"] + box["height"] <= view["height"], f"{label} off the top/bottom: {box}"
    # On screen is not the same as visible: the downward label used to sit at the card's bottom, inside
    # the viewport and painted under the action row. `pointer-events: none` hides these from hit
    # testing, so compare boxes with the thing that covered it.
    actions = page.get_by_role("button", name="Request").first.bounding_box()
    assert actions
    covered = box["y"] + box["height"] > actions["y"] and box["y"] < actions["y"] + actions["height"]
    assert not covered, f"{label} runs under the action row: label {box}, actions {actions}"
    page.context.close()
