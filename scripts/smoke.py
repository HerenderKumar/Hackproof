"""The M10 smoke test (section 20.1): a browser walks the organiser's setup, a team's whole flow
and a judge's brief against the running stack.

Run it with `make smoke` (it runs in the runner image, which carries Playwright and Chromium).
The stack must be up with REPO_PROVIDER=local and LLM_MODE=replay, so drafting is replayed from
the recordings and nothing reaches the network. The team builds F01's good fixture. The audit
and the brief writer have no recording for this team's text, so the brief is the raw one that
section 7's degradation rule asks for: the smoke checks that path too.

Setting up the event has no page (D-055 trimmed the rubric editor), so the script does it with
the organiser's signed-in browser session, through the same API the pages use.
"""

import csv
import io
import os
import re
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from playwright.sync_api import BrowserContext, Locator, Page, expect, sync_playwright

BASE = os.environ.get("SMOKE_BASE_URL", "http://host.docker.internal:3000").rstrip("/")
PASSWORD = os.environ["ORGANISER_PASSWORD"]
REMOTE = os.environ.get("SMOKE_REMOTE", "local://F01-node-splitter-good")
RUBRIC = [
    {"id": "impact", "name": "Impact", "description": "Does it matter?", "max": 10},
    {"id": "craft", "name": "Craft", "description": "Is it well built?", "max": 10},
]
# F01's start text and claim, as in fixtures/F01-node-splitter: drafting replays its recording.
PROBLEM = (
    "Friends who share a bill often end up a paisa short or a paisa over, because the split "
    "does not add up to what was paid."
)
EXPLANATION = (
    "We are building a small web service that splits a bill fairly. POST /split takes a JSON "
    "body with the total and the number of people, and answers 200 with a JSON object whose "
    "shares list has one amount for each person. The shares always add up to the exact total, "
    "and no two shares differ by more than 0.01."
)
CLAIM = {
    "What your app does": (
        "Splits a bill between people so the shares always add up to the exact total, "
        "down to the paisa."
    ),
    "Where (optional)": "POST /split",
    "An example input (optional)": '{"total": 100, "people": 3}',
    "What should happen (optional)": (
        "Returns 200 with shares [33.34, 33.33, 33.33]: they sum to exactly 100.00 and no "
        "two differ by more than 0.01."
    ),
}
CSV_HEADER = [
    "team", "submission_id", "commit_sha", "judge", "impact", "craft", "judge_total",
    "claims_verified_live", "claims_not_verified",
]  # fmt: skip


def step(name: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {name}", flush=True)


def local(url: str) -> str:
    """An invitation link points at PUBLIC_BASE_URL; the browser here reaches the stack at BASE."""
    parts = urlsplit(url)
    return f"{BASE}{parts.path}?{parts.query}"


def button(page: Page, name: str) -> Locator:
    return page.get_by_role("button", name=name)


def answered(page: Page, method: str, path: str, click: Callable[[], None]) -> None:
    """Click, then wait for the API call it makes to answer with success."""
    with page.expect_response(
        lambda r: r.request.method == method and urlsplit(r.url).path == f"/api{path}"
    ) as info:
        click()
    assert info.value.ok, f"{method} {path}: {info.value.status} {info.value.text()}"


def until(page: Page, ready: Callable[[], bool], what: str, seconds: int) -> None:
    """Wait until `ready` holds, reloading every 15 s in case the page stopped polling."""
    deadline = time.monotonic() + seconds
    last_reload = time.monotonic()
    while not ready():
        if time.monotonic() > deadline:
            out = os.environ.get("SMOKE_OUT")
            if out:
                page.screenshot(path=f"{out}/timeout.png")
                with open(f"{out}/timeout.html", "w") as f:
                    f.write(f"<!-- {page.url} -->\n{page.content()}")
            raise AssertionError(f"timed out after {seconds} s waiting for {what}")
        page.wait_for_timeout(1000)
        if time.monotonic() - last_reload > 15:
            page.reload()
            page.wait_for_load_state("networkidle")
            last_reload = time.monotonic()


def watched(context: BrowserContext) -> BrowserContext:
    """Every page error in this context is printed: a blank page then says why."""

    def watch(page: Page) -> None:
        page.on("pageerror", lambda error: print(f"page error: {error}", flush=True))
        page.on(
            "console",
            lambda m: m.type == "error" and print(f"console error: {m.text}", flush=True),
        )
        page.on(
            "requestfailed",
            lambda r: print(f"request failed: {r.url} {r.failure}", flush=True),
        )

    context.on("page", watch)
    return context


def organiser_setup(context: BrowserContext, stamp: str) -> dict[str, str]:
    page = context.new_page()
    step("organiser signs in")
    page.goto(f"{BASE}/login")
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url(f"{BASE}/org")

    step("organiser creates the event, the team, the judge and their links")
    now = datetime.now(UTC)
    api = context.request
    event = api.post(
        f"{BASE}/api/events",
        data={
            "name": f"Smoke test {stamp}",
            "kickoff_at": (now - timedelta(minutes=10)).isoformat(),
            "deadline_at": (now + timedelta(hours=2)).isoformat(),
            "rubric": RUBRIC,
            "policy_text": "Declare starter code, earlier work and AI assistance.",
        },
    )
    assert event.status == 201, event.text()
    event_id = event.json()["id"]
    imported = api.post(
        f"{BASE}/api/events/{event_id}/teams/import",
        multipart={
            "file": {
                "name": "teams.csv",
                "mimeType": "text/csv",
                "buffer": b"team_name,contact_email,repo_url\nSmoke Splitters,,\n",
            }
        },
    )
    assert imported.status == 200, imported.text()
    team_id = imported.json()["created"][0]["id"]
    judge = api.post(f"{BASE}/api/events/{event_id}/judges", data={"name": "Smoke Judge"})
    assert judge.status == 201, judge.text()
    links = {}
    for role, subject in (("team", team_id), ("judge", judge.json()["id"])):
        made = api.post(
            f"{BASE}/api/events/{event_id}/links", data={"role": role, "subject_id": subject}
        )
        assert made.status == 201, made.text()
        links[role] = local(made.json()["url"])

    page.reload()
    expect(page.get_by_role("heading", name=f"Smoke test {stamp}")).to_be_visible()
    expect(page.get_by_text("Smoke Splitters")).to_be_visible()
    page.close()
    return {"event_id": event_id, **links}


def team_flow(context: BrowserContext, link: str) -> None:
    page = context.new_page()
    step("team opens its link and acknowledges the rules")
    page.goto(link)
    page.wait_for_url(f"{BASE}/team")
    answered(page, "POST", "/team/rules-ack", button(page, "I have read the rules").click)

    step("team registers its repository")
    page.get_by_label("Public repository URL").fill(REMOTE)
    answered(page, "POST", "/team/repo", button(page, "Register repository").click)
    expect(page.get_by_text(REMOTE.removeprefix("local://"))).to_be_visible()

    step("team writes its claim")
    page.goto(f"{BASE}/team/claims")
    page.get_by_label("Problem statement").fill(PROBLEM)
    page.get_by_label("Explanation").fill(EXPLANATION)
    for label, value in CLAIM.items():
        page.get_by_label(label).first.fill(value)
    answered(page, "PUT", "/team/claims", button(page, "Save and draft tests").click)

    step("team waits for the drafted test and approves it")
    page.goto(f"{BASE}/team/tests")
    approve = page.get_by_role("button", name="Approve this test")
    until(page, approve.is_visible, "the drafted test", 180)
    approve.click()
    expect(page.get_by_text("Approved").first).to_be_visible()

    step("team snapshots the latest commit")
    snap = page.get_by_role("button", name="Snapshot the latest commit")
    answered(page, "POST", "/team/snapshots", snap.click)

    step("team submits")
    page.goto(f"{BASE}/team/submit")
    radio = page.get_by_role("radio")
    until(page, lambda: radio.count() > 0, "the snapshot", 180)
    page.get_by_label("We used AI assistance to write code.").check()
    page.get_by_label("Notes (optional)").fill("Copilot for the tests.")
    submit = page.get_by_role("button", name="Submit", exact=True)
    until(page, submit.is_enabled, "the submission to be ready", 120)
    if not page.get_by_label("We used AI assistance to write code.").is_checked():
        page.get_by_label("We used AI assistance to write code.").check()
    submit.click()
    page.wait_for_url(re.compile(r"/team/receipt"))
    expect(page.get_by_text("The submission that will be judged")).to_be_visible()
    page.close()


def judge_brief(context: BrowserContext, link: str) -> None:
    page = context.new_page()
    step("judge opens the team list and waits for the brief")
    page.goto(link)
    page.wait_for_url(f"{BASE}/judge")
    card = page.get_by_role("link", name=re.compile("Smoke Splitters"))
    until(page, lambda: card.count() > 0, "the submission", 60)
    until(page, lambda: "Brief ready" in card.inner_text(), "the brief", 600)

    step("judge reads the brief, marks the claim and scores")
    card.click()
    page.wait_for_url(re.compile(r"/judge/brief/"))
    expect(page.get_by_role("heading", name="Smoke Splitters")).to_be_visible()
    expect(page.get_by_text("Platform-built image from archive")).to_be_visible()
    expect(page.get_by_text("Pass").first).to_be_visible()
    expect(page.get_by_role("heading", name="Two questions to ask")).to_be_visible()
    expect(page.get_by_role("heading", name="Build timeline")).to_be_visible()
    expect(page.get_by_text("Used AI assistance: yes")).to_be_visible()
    expect(page.get_by_text("Copilot for the tests.")).to_be_visible()
    answered(
        page,
        "PUT",
        f"/judge/submissions/{page.url.rsplit('/', 1)[-1]}/claim-marks",
        page.get_by_role("button", name="Verified live").click,
    )
    expect(button(page, "Verified live")).to_have_attribute("aria-pressed", "true")
    page.get_by_label(re.compile("^Impact")).fill("8")
    page.get_by_label(re.compile("^Craft")).fill("7")
    page.get_by_role("button", name="Save scores").click()
    expect(page.get_by_text("Total 15")).to_be_visible()
    page.close()


def organiser_close(context: BrowserContext, event_id: str) -> None:
    page = context.new_page()
    step("organiser sees the brief on the dashboard, locks the scores and exports them")
    page.goto(f"{BASE}/org")
    expect(page.get_by_role("link", name="Brief")).to_be_visible()
    page.get_by_role("button", name="Tests").first.click()
    expect(page.get_by_text("Final cases (hidden from the team)")).to_be_visible()
    page.on("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name="Lock scores").click()
    expect(page.get_by_text("Scores are locked.")).to_be_visible()
    exported = context.request.get(f"{BASE}/api/events/{event_id}/scores.csv")
    assert exported.status == 200, exported.text()
    header, *rows = list(csv.reader(io.StringIO(exported.text())))
    assert header == CSV_HEADER, header
    assert len(rows) == 1 and rows[0][3] == "Smoke Judge", rows
    assert rows[0][4:9] == ["8", "7", "15", "1", "0"], rows
    page.close()


def main() -> int:
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        phone = {"viewport": {"width": 390, "height": 844}, "is_mobile": True, "has_touch": True}
        organiser = watched(browser.new_context())
        made = organiser_setup(organiser, stamp)
        team_flow(watched(browser.new_context()), made["team"])
        # Section 7: the brief is read on a phone.
        judge_brief(watched(browser.new_context(**phone)), made["judge"])
        organiser_close(organiser, made["event_id"])
        browser.close()
    step("smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
