"""The session console at 390px — stacked, everything reachable, nothing hidden.

Reading the CSS is not the same as rendering it, so this drives a real browser at a
390x844 viewport (iPhone 14). The console this checks has no tab strip: it is a head,
a filter-chip row, one scrolling stream, and a docked composer, with an inspector that
takes over the stage on a phone. So the three things worth asserting are:

  1. the head and the stream stack vertically (no side-by-side at this width),
  2. every filter chip sits on-screen at rest and narrows the stream without
     overflowing it sideways — a hidden horizontal scrollbar is how a "responsive"
     layout fails on a phone,
  3. the inspector opens over the stream and every one of its five panels renders
     content without overflowing.

Run from the repo root as a module, so the ``app`` import resolves:

    .venv/bin/python -m tools.check_mobile <job_id>
"""

from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

from app.streams import TERMINAL_STATUSES

BASE = "http://127.0.0.1:8090"
# The five cuts in the chip row (SessionStream.STREAM_FILTERS), by their labels. A
# sixth `chip-phase` appears only once a phase is picked, so it is not in this list.
FILTERS = ["everything", "said", "ran", "decisions", "notes"]
# The inspector's five panels (Inspector.TABS), by their labels.
PANELS = ["Plan", "Agents", "Tokens", "Files", "Events"]
WIDTH, HEIGHT = 390, 844


def overflow_px(page) -> int:
    return page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )


def main(job_id: str) -> int:
    failures: list[str] = []
    # Routes are hash-based on purpose (App.tsx): the document path has to stay at
    # the mount root so Vite's relative asset URLs resolve under both `/` and
    # `/hub/`. So the URL to drive is `/#/jobs/<id>`, not `/jobs/<id>`.
    with sync_playwright() as play:
        browser = play.chromium.launch()
        page = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT})
        console: list[str] = []
        page.on("console", lambda msg: console.append(f"{msg.type}: {msg.text}"))
        page.on("pageerror", lambda err: console.append(f"pageerror: {err}"))

        # --- jobs list -------------------------------------------------------
        page.goto(f"{BASE}/#/jobs", wait_until="networkidle")
        page.wait_for_selector(".job-row, .empty", timeout=10_000)
        page.screenshot(path="/tmp/shot-390-list.png", full_page=True)
        overflow = overflow_px(page)
        print(f"jobs list: horizontal overflow = {overflow}px")
        if overflow > 0:
            failures.append(f"jobs list overflows by {overflow}px")

        # --- the console, stream view ----------------------------------------
        page.goto(f"{BASE}/#/jobs/{job_id}", wait_until="networkidle")
        page.wait_for_selector(".console", timeout=10_000)
        page.wait_for_timeout(600)

        # A finished job must not advertise a live connection. This started as a
        # screenshot review — the indicator read "live" on a complete job — and the
        # cause was server-side (a stream reopened past its terminal event waited out
        # TERMINAL_EVENT_GRACE before closing). Assert it so the next screenshot does
        # not have to be read by eye. The status comes from the API, not the pill,
        # which shows a human label unfit for comparing machine statuses against.
        status = page.request.get(f"{BASE}/api/jobs/{job_id}").json()["status"]
        indicator = page.evaluate(
            """() => {
                const el = document.querySelector('.console-head .stream-state');
                return el && { text: el.textContent.trim(), cls: el.className };
            }"""
        )
        print(f"job status={status!r} stream indicator={indicator}")
        if indicator is None:
            failures.append("no stream indicator rendered")
        elif status in TERMINAL_STATUSES and "stream-live" in indicator["cls"]:
            failures.append(f"finished job ({status}) still shows a live stream indicator")

        # Head above stream, not beside it: at this width the grid must not have kicked
        # in. Measured off the head and the chip row that opens the stream.
        stacked = page.evaluate(
            """() => {
                const head = document.querySelector('.console-head');
                const chips = document.querySelector('.stream-chips');
                if (!head || !chips) return null;
                const h = head.getBoundingClientRect();
                const c = chips.getBoundingClientRect();
                return { headBottom: Math.round(h.bottom), chipsTop: Math.round(c.top),
                         stacked: c.top >= h.bottom - 1 };
            }"""
        )
        print(f"head/stream stacking: {stacked}")
        if not stacked or not stacked["stacked"]:
            failures.append("console head and stream are not stacked vertically")

        # The composer has to be present and on-screen — it is the one control the
        # console promises is never somewhere you navigate back to.
        if page.locator(".composer").count() == 0:
            failures.append("composer is not docked on the console")

        # Measure every chip *before* clicking. The chip row can scroll sideways, so
        # measuring as you go reports whichever chips were dragged into view.
        boxes = page.evaluate(
            """() => Array.from(document.querySelectorAll('.stream-chips .chip')).map((el) => {
                const r = el.getBoundingClientRect()
                return { text: el.textContent.trim().split('\\n')[0],
                         x: Math.round(r.x), right: Math.round(r.right) }
            })"""
        )
        for entry in boxes:
            if entry["x"] < 0 or entry["right"] > WIDTH + 1:
                failures.append(
                    f"filter {entry['text']!r} is off-screen at rest "
                    f"(x={entry['x']}..{entry['right']}, viewport {WIDTH})"
                )
        print(f"chip positions at rest: {[(e['text'], e['x'], e['right']) for e in boxes]}")

        page.screenshot(path="/tmp/shot-390-stream.png", full_page=True)

        for name in FILTERS:
            chip = page.locator(".stream-chips .chip", has_text=name).first
            if chip.count() == 0:
                failures.append(f"filter {name!r} is not present")
                continue
            chip.click()
            page.wait_for_timeout(300)
            overflow = overflow_px(page)
            print(f"  filter {name:<11} clicked, overflow={overflow}px")
            if overflow > 0:
                failures.append(f"filter {name!r} overflows by {overflow}px")

        # --- the inspector, one panel at a time ------------------------------
        inspect = page.locator(".console-inspect").first
        if inspect.count() == 0:
            failures.append("no Inspect control to open the inspector")
        else:
            inspect.click()
            page.wait_for_selector(".inspector", timeout=10_000)
            page.wait_for_timeout(300)
            inspecting = page.evaluate(
                "() => document.querySelector('.console')?.getAttribute('data-inspecting')"
            )
            if inspecting != "true":
                failures.append("Inspect did not switch the stage to the inspector")

            tabs = page.evaluate(
                """() => Array.from(document.querySelectorAll('.inspector-tabs .inspector-tab')).map((el) => {
                    const r = el.getBoundingClientRect()
                    return { text: el.textContent.trim().split('\\n')[0],
                             x: Math.round(r.x), right: Math.round(r.right) }
                })"""
            )
            for entry in tabs:
                if entry["x"] < 0 or entry["right"] > WIDTH + 1:
                    failures.append(
                        f"inspector tab {entry['text']!r} is off-screen at rest "
                        f"(x={entry['x']}..{entry['right']}, viewport {WIDTH})"
                    )
            print(f"inspector tabs at rest: {[(e['text'], e['x'], e['right']) for e in tabs]}")

            for name in PANELS:
                tab = page.locator(".inspector-tabs .inspector-tab", has_text=name).first
                if tab.count() == 0:
                    failures.append(f"inspector tab {name!r} is not present")
                    continue
                tab.click()
                page.wait_for_timeout(350)
                body = page.locator(".inspector-body")
                text_len = len((body.inner_text() or "").strip()) if body.count() else 0
                overflow = overflow_px(page)
                print(f"  panel {name:<8} clicked, chars={text_len:<5} overflow={overflow}px")
                page.screenshot(path=f"/tmp/shot-390-{name.lower()}.png", full_page=True)
                if text_len == 0:
                    failures.append(f"inspector panel {name!r} rendered empty")
                if overflow > 0:
                    failures.append(f"inspector panel {name!r} overflows by {overflow}px")

        if console:
            print("browser console output:")
            for line in console[:15]:
                print("   ", line)
            if any(line.startswith(("error", "pageerror")) for line in console):
                failures.append("browser reported an error")

        browser.close()

    print()
    if failures:
        print("FAILURES:")
        for failure in failures:
            print(" -", failure)
        return 1
    print("OK: 390px console stacked, every filter and panel reachable, no overflow")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
