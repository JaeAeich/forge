"""Record the README demo: a captioned tour of every feature, driven by the keyboard. Writes docs/demo.gif.

Needs the server on :8000 with data (`forge up`) and ffmpeg. Run: `mise run demo`.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
DOCS = Path(__file__).resolve().parent.parent / "docs"
SIZE = {"width": 1280, "height": 800}

# Shows each key press in a small bubble so viewers can follow the shortcuts.
KEYCAST = """
addEventListener('DOMContentLoaded', () => {
  const box = document.createElement('div');
  box.style.cssText = 'position:fixed;right:24px;bottom:24px;z-index:99;display:flex;gap:6px;font:600 15px ui-monospace,Menlo,monospace';
  document.body.appendChild(box);
  addEventListener('keydown', (e) => {
    if (e.target.matches && e.target.matches('input') && e.key.length === 1) return;
    const k = document.createElement('span');
    const name = {Meta: '⌘', Enter: '⏎', Escape: 'esc', ArrowDown: '↓', ArrowUp: '↑'}[e.key] || e.key;
    k.textContent = (e.metaKey && e.key !== 'Meta' ? '⌘' : '') + name;
    k.style.cssText = 'background:#1c1917;color:#fff;padding:6px 11px;border-radius:8px;box-shadow:0 4px 14px rgba(0,0,0,.2);transition:opacity .4s';
    box.appendChild(k);
    setTimeout(() => { k.style.opacity = '0'; setTimeout(() => k.remove(), 400); }, 1100);
  }, true);
});
"""


def press(page: Page, *keys: str, pause: int = 700) -> None:
    for k in keys:
        page.keyboard.press(k)
        page.wait_for_timeout(260)
    page.wait_for_timeout(pause)


def type_slow(page: Page, text: str, pause: int = 600) -> None:
    page.keyboard.type(text, delay=90)
    page.wait_for_timeout(pause)


CAPTION = """
t => {
  let c = document.getElementById('caption');
  if (!c) {
    c = document.createElement('div');
    c.id = 'caption';
    c.style.cssText = 'position:fixed;left:50%;bottom:24px;transform:translateX(-50%);z-index:99;max-width:860px;'
      + 'text-align:center;background:#1c1917;color:#fafaf9;font:500 17px/1.45 Inter,system-ui,sans-serif;'
      + 'padding:12px 22px;border-radius:12px;box-shadow:0 8px 30px rgba(0,0,0,.25);transition:opacity .25s;opacity:0';
    document.body.appendChild(c);
  }
  c.style.opacity = 0;
  setTimeout(() => { c.textContent = t; c.style.opacity = 1; }, 250);
}
"""


def caption(page: Page, text: str, hold: int = 0) -> None:
    """Show one line explaining what's on screen (replaces the previous one)."""
    page.evaluate(CAPTION, text)
    page.wait_for_timeout(450 + hold)


def scroll(page: Page, dy: int, pause: int = 1400) -> None:
    page.mouse.wheel(0, dy)
    page.wait_for_timeout(pause)


def tour(page: Page) -> None:
    page.goto(f"{BASE}/#/overview")
    page.wait_for_selector(".stat")
    caption(page, "Forge turns real bug fixes from open-source repos into practice tasks for coding agents", 2600)

    # tasks
    press(page, "g", "t", pause=300)
    caption(page, "A task is kept only if its tests fail before the fix, pass after, and do so every time", 2200)
    press(page, "2", pause=700)  # solved filter
    press(page, "j", "Enter", pause=900)
    caption(page, "Each task: a bug report, hidden tests, the real fix, and how agents did on it", 1800)
    press(page, "x", pause=600)  # expand every section
    scroll(page, 700, 1800)

    # rollouts
    press(page, "g", "r", pause=300)
    caption(page, "Agents attempt tasks in isolated sandboxes, one tool call per step", 1600)
    page.goto(f"{BASE}/#/rollout/7")
    page.wait_for_selector("details.step")
    caption(page, "The sandbox is snapshotted after every step, so any attempt can be rewound and replayed", 1400)
    scroll(page, 600, 600)
    edit = page.locator("details.step summary", has_text="replace")
    (edit.first if edit.count() else page.locator("details.step summary").first).click()
    page.wait_for_timeout(2000)
    page.goto(f"{BASE}/#/rollout/53")
    page.wait_for_selector(".tree", state="attached")
    press(page, "x", pause=300)  # expand every section
    page.locator(".tree").scroll_into_view_if_needed()
    caption(page, "Failed attempts get forked from their midpoint: same history, a different future", 2600)

    # grading and hacks
    press(page, "Meta+k", pause=300)
    type_slow(page, "hack", pause=500)
    press(page, "ArrowDown", "Enter", pause=500)
    caption(page, "Grading runs in a fresh sandbox the agent never touched; cheating is flagged with evidence", 2800)
    press(page, "g", "q", pause=300)
    caption(page, "Known cheats are planted in every task: a naive grader falls for them, forge's doesn't", 3000)

    # code index
    press(page, "g", "i", pause=300)
    caption(page, "Code search by meaning (EmbeddingGemma + pgvector), also used to spot leaked tasks", 600)
    press(page, "/", pause=200)
    type_slow(page, "parse a date string", pause=200)
    press(page, "Enter", pause=2600)
    press(page, "Escape", pause=200)  # leave the search box so shortcuts work again

    # environments, jobs, benchmarks
    press(page, "g", "e", pause=300)
    caption(page, "Multi-service environments (app + Postgres + Redis) snapshot and fork as one unit", 2600)
    press(page, "g", "j", pause=300)
    caption(page, "A durable job queue: Postgres decides who runs what, so no job runs twice or gets lost", 2600)
    press(page, "g", "b", pause=300)
    caption(
        page, "Measured: 0 duplicate jobs after killing workers 21 times, 2,300 jobs/s, 100k simulated agents", 2600
    )
    scroll(page, 600, 800)
    caption(page, "Runs on a laptop with Docker, or on Kubernetes with a sandbox operator. ⌘K to go anywhere.", 2800)


def to_gif(video: Path, out: Path) -> None:
    graph = "fps=10,scale=960:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=5"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-vf", graph, "-loop", "0", str(out)], check=True
    )


def main() -> None:
    DOCS.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport=SIZE, device_scale_factor=1, record_video_dir=tmp, record_video_size=SIZE)
        ctx.add_init_script(KEYCAST)
        page = ctx.new_page()
        tour(page)
        video = Path(page.video.path()) if page.video else None
        ctx.close()
        if video:
            to_gif(video, DOCS / "demo.gif")
        browser.close()
    print(f"docs/demo.gif {(DOCS / 'demo.gif').stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
