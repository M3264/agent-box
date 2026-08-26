"""Screenshot the desktop layout so the redesign can be judged rather than imagined.

Not a check — it asserts nothing. It exists because a stylesheet reads fine and still
renders wrong, and the 390px checker only ever looks at a phone.

    .venv/bin/python -m tools.shoot_desktop <job_id>
"""

from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8090"
WIDTH, HEIGHT = 1440, 900


def main(job_id: str) -> int:
    with sync_playwright() as play:
        browser = play.chromium.launch()
        page = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT})
        console: list[str] = []
        page.on("console", lambda msg: console.append(f"{msg.type}: {msg.text}"))
        page.on("pageerror", lambda err: console.append(f"pageerror: {err}"))

        shots = {
            "list": f"{BASE}/#/jobs",
            "console": f"{BASE}/#/jobs/{job_id}",
            "plan": f"{BASE}/#/jobs/{job_id}/plan",
            "agents": f"{BASE}/#/jobs/{job_id}/agents",
            "tokens": f"{BASE}/#/jobs/{job_id}/tokens",
            "events": f"{BASE}/#/jobs/{job_id}/events",
            "attention": f"{BASE}/#/attention",
            "settings": f"{BASE}/#/settings",
        }
        for name, url in shots.items():
            page.goto(url, wait_until="networkidle")
            # Webfonts arrive after networkidle often enough to matter for a
            # screenshot whose whole point is the typography.
            page.evaluate("() => document.fonts.ready")
            page.wait_for_timeout(700)
            path = f"/tmp/shot-1440-{name}.png"
            page.screenshot(path=path)
            loaded = page.evaluate(
                "() => Array.from(document.fonts).filter(f => f.status === 'loaded')"
                ".map(f => f.family + ' ' + f.weight)"
            )
            print(f"{name:<9} -> {path}   fonts loaded: {sorted(set(loaded))}")

        for line in console[:10]:
            print("   console:", line)
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
