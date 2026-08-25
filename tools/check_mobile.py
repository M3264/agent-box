"""§8 item 9: job detail at 390px — stacked, all six tabs reachable.

Reading the CSS is not the same as rendering it, so this drives a real browser at a
390x844 viewport (iPhone 14). It checks three things per tab: the tab is clickable,
the panel it opens has content, and nothing overflows horizontally — a sideways
scrollbar is the usual way a "responsive" layout fails on a phone.

Run from the repo root as a module, so the ``app`` import resolves:

    python -m tools.check_mobile <job_id>
"""

from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

from app.streams import TERMINAL_STATUSES

BASE = "http://127.0.0.1:8090"
TABS = ["Conversation", "Timeline", "Plan", "Agents", "Artifacts", "Approvals"]
WIDTH, HEIGHT = 390, 844


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
        page.wait_for_selector(".job-row, .jobs-empty", timeout=10_000)
        page.screenshot(path="/tmp/shot-390-list.png", full_page=True)
        overflow = page.evaluate(
            "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        print(f"jobs list: horizontal overflow = {overflow}px")
        if overflow > 0:
            failures.append(f"jobs list overflows by {overflow}px")

        # --- job detail, one tab at a time -----------------------------------
        page.goto(f"{BASE}/#/jobs/{job_id}", wait_until="networkidle")
        page.wait_for_selector(".job-screen", timeout=10_000)

        # A finished job must not advertise a live connection. This started as a
        # layout screenshot review — the indicator read "live" on a complete job — and
        # the cause was server-side (a stream reopened past its terminal event waited
        # out TERMINAL_EVENT_GRACE before closing). Assert it here so the next
        # screenshot does not have to be read by eye.
        #
        # The status comes from the API, not the pill: the pill shows a human label
        # (`blocked_on_approval` renders as "Blocked"), which is the wrong thing to
        # compare machine statuses against.
        status = page.request.get(f"{BASE}/api/jobs/{job_id}").json()["status"]
        page.wait_for_timeout(600)
        stream = page.evaluate(
            """() => {
                const el = document.querySelector('.job-head .stream');
                return el && { text: el.textContent.trim(), cls: el.className };
            }"""
        )
        print(f"job status={status!r} stream indicator={stream}")
        if stream is None:
            failures.append("no stream indicator rendered")
        elif status in TERMINAL_STATUSES and "stream-live" in stream["cls"]:
            failures.append(f"finished job ({status}) still shows a live stream indicator")

        stacked = page.evaluate(
            """() => {
                const head = document.querySelector('.job-head');
                const tabs = document.querySelector('.tabs');
                if (!head || !tabs) return null;
                const h = head.getBoundingClientRect();
                const t = tabs.getBoundingClientRect();
                return { headBottom: Math.round(h.bottom), tabsTop: Math.round(t.top),
                         stacked: t.top >= h.bottom - 1 };
            }"""
        )
        print(f"header/tabs stacking: {stacked}")
        if not stacked or not stacked["stacked"]:
            failures.append("header and tabs are not stacked vertically")

        # Measure every tab *before* clicking anything. Clicking scrolls a
        # horizontally-scrollable strip, so measuring as you go reports whichever
        # tabs happen to have been dragged into view and misses the real problem.
        boxes = page.evaluate(
            """() => Array.from(document.querySelectorAll('.tabs .tab')).map((el) => {
                const r = el.getBoundingClientRect()
                return { text: el.textContent.trim().split('\\n')[0],
                         x: Math.round(r.x), right: Math.round(r.right) }
            })"""
        )
        for entry in boxes:
            if entry["x"] < 0 or entry["right"] > WIDTH + 1:
                failures.append(
                    f"tab {entry['text']!r} is off-screen at rest "
                    f"(x={entry['x']}..{entry['right']}, viewport {WIDTH})"
                )
        print(f"tab positions at rest: {[(e['text'], e['x'], e['right']) for e in boxes]}")

        for name in TABS:
            # Scoped to the tab bar: the global nav has its own "Approvals" link, and
            # an unscoped lookup clicks that instead, leaving the job screen entirely.
            link = page.locator(".tabs .tab", has_text=name).first
            if link.count() == 0:
                failures.append(f"tab {name!r} is not present")
                continue
            link.click()
            page.wait_for_timeout(400)
            body = page.locator(".tab-body")
            text = (body.inner_text() or "").strip()
            overflow = page.evaluate(
                "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
            )
            print(
                f"  {name:<13} clicked, panel chars={len(text):<5} overflow={overflow}px"
            )
            page.screenshot(path=f"/tmp/shot-390-{name.lower()}.png", full_page=True)
            if not text:
                failures.append(f"tab {name!r} rendered an empty panel")
            if overflow > 0:
                failures.append(f"tab {name!r} overflows by {overflow}px")

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
    print("OK: 390px layout stacked, all six tabs reachable, no horizontal overflow")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
