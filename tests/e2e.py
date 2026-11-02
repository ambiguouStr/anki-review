#!/usr/bin/env python3
"""End-to-end tests for the anki-review scripts.

Runs entirely against a THROWAWAY COPY of your collection in a temp dir:
never writes to your real data directory, never uses credentials, never
touches the network (sync runs against a stub ankisync.py). Requires an
existing collection with due cards (run ankisync.py once first).

Usage: python3 tests/e2e.py
"""
import fcntl
import json
import os
import pty
import re
import select
import shutil
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile
import termios
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)
from render import Media, clean, pitch_text, render_back  # noqa: E402
from review import can_play_audio, terminal_shows_images  # noqa: E402

REAL = os.environ.get("ANKI_CLI_DIR") or os.path.expanduser("~/.local/share/anki-cli")
REAL_COL = os.path.join(REAL, "collection.anki2")

HIGH = '<span style="display:inline-block;"><span style="display:inline;">{}</span><span style="border-top-style:solid;{}"></span></span>'
DROP = "border-right-style:solid;"

passed, failed = [], []


def check(name, cond, detail=""):
    (passed if cond else failed).append((name, detail))
    print(("PASS " if cond else "FAIL ") + name + (("  | " + str(detail)) if detail and not cond else ""))


def env_for(box, extra=None):
    env = dict(os.environ, ANKI_CLI_DIR=box)
    for name in ("ANKI_USER", "ANKI_PASS", "TMUX", "SSH_CONNECTION", "SSH_TTY", "KITTY_WINDOW_ID",
                 "WEZTERM_PANE", "TERM_PROGRAM"):
        env.pop(name, None)
    env["TERM"] = "xterm"
    env.update(extra or {})
    return env


def run(*args, box=None, stdin=None, scripts=SCRIPTS, script="review.py"):
    return subprocess.run([sys.executable, os.path.join(scripts, script), *args],
                          capture_output=True, text=True, env=env_for(box), input=stdin, timeout=60)


def tty_session(steps, *args, box, scripts=SCRIPTS, rows=40, cols=80, env=None):
    """Run review.py on a real pty. Steps are (keys, seconds to wait before them)."""
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    before = termios.tcgetattr(slave)
    proc = subprocess.Popen([sys.executable, os.path.join(scripts, "review.py"), *args],
                            stdin=slave, stdout=slave, stderr=slave, env=env_for(box, env),
                            start_new_session=True)
    out = b""

    def pump(seconds):
        nonlocal out
        end = time.time() + seconds
        while time.time() < end:
            if select.select([master], [], [], 0.02)[0]:
                try:
                    out += os.read(master, 65536)
                except OSError:
                    return

    pump(1.0)
    for keys, wait in steps:
        pump(wait)
        os.write(master, keys.encode())
    deadline = time.time() + 15
    while proc.poll() is None and time.time() < deadline:
        pump(0.1)
    pump(0.1)
    if proc.poll() is None:
        proc.kill()
    restored = termios.tcgetattr(slave) == before
    os.close(master)
    os.close(slave)
    return proc.returncode, out.decode(errors="replace"), restored


def revlog(box):
    db = sqlite3.connect(os.path.join(box, "collection.anki2"))
    try:
        return db.execute("select cid, ease, time from revlog order by id").fetchall()
    finally:
        db.close()


def fresh(box):
    shutil.copy(REAL_COL, box)
    return revlog(box)


def stub_scripts(body):
    """A copy of the scripts whose ankisync.py is a stub, so sync never hits the network."""
    d = tempfile.mkdtemp(prefix="anki-review-stub-")
    shutil.copy(os.path.join(SCRIPTS, "review.py"), d)
    shutil.copy(os.path.join(SCRIPTS, "render.py"), d)
    path = os.path.join(d, "ankisync.py")
    with open(path, "w") as f:
        f.write(f"import os, sys, time\n{body}\n")
    return d


def test_render():
    check("ruby merges into one word",
          clean("<ruby><rb>面</rb><rt>おも</rt></ruby><ruby><rb>白</rb><rt>しろ</rt></ruby>い") == "面白(おもしろ)い")
    check("markup stripped",
          clean("<style>x</style><div>a</div>[sound:a.mp3][anki:play:a:0]<!-- c --><img src='i.webp'>") == "a")
    only_b = Media(image=lambda n: n == "b.webp", audio=lambda n: n == "b.mp3")
    check("media shown only where allowed",
          clean("<img src='a.webp'><img src='b.webp'>[sound:a.mp3][sound:b.mp3]", media=only_b)
          == "[image: b.webp][audio: b.mp3]")
    check("percent-encoded image names", clean("<img src='a%20b.png'>", media=Media(image=lambda n: n == "a b.png"))
          == "[image: a b.png]")
    check("heiban", pitch_text("ワ" + HIGH.format("タシ", "")) == "ワ↗タシ (平板)")
    check("nakadaka", pitch_text("ア" + HIGH.format("ナ", DROP) + "タ") == "ア↗ナ↘タ (起伏)")
    check("atamadaka has no rise", pitch_text(HIGH.format("カ", DROP) + "レ") == "カ↘レ (起伏)")
    check("alternatives labelled separately",
          pitch_text("ヒ" + HIGH.format("ト", "") + "・ヒ" + HIGH.format("ト", DROP)) == "ヒ↗ト (平板)・ヒ↗ト↘ (起伏)")
    back = render_back("<div><ruby><rb>父</rb><rt>ちち</rt></ruby></div><div>father</div>", {
        "Word": "父", "Word Reading": "ちち", "Word Furigana": "父[ちち]", "Word Meaning": "father",
        "Word Audio": "[sound:a.mp3]", "Frequency": "681",
        "Pitch Accent": "チ" + HIGH.format("チ", DROP)})
    check("audio field shown once, image not repeated", render_back(
        "<div>a</div><img src='i.webp'>", {"Picture": "<img src='i.webp'>", "Word Audio": "[sound:a.mp3]"},
        Media(image=lambda n: True, audio=lambda n: True))
        == "a\n[image: i.webp]\nWord Audio: [audio: a.mp3]")
    check("back adds only what the template hid",
          back == "父(ちち)\nfather\nFrequency: 681\nPitch Accent: チ↗チ↘ (起伏)", back)


def test_terminal_checks():
    bin_dir = tempfile.mkdtemp(prefix="anki-review-bin-")
    mpv = os.path.join(bin_dir, "mpv")
    with open(mpv, "w") as f:
        f.write("#!/bin/sh\n")
    os.chmod(mpv, 0o755)
    try:
        check("kitty shows images", terminal_shows_images({"KITTY_WINDOW_ID": "1"}))
        check("xterm-kitty over ssh shows images", terminal_shows_images({"TERM": "xterm-kitty"}))
        check("iTerm2 shows images", terminal_shows_images({"TERM_PROGRAM": "iTerm.app"}))
        check("plain xterm doesn't", not terminal_shows_images({"TERM": "xterm-256color"}))
        check("tmux inside kitty doesn't",
              not terminal_shows_images({"TMUX": "/tmp/s", "KITTY_WINDOW_ID": "1", "TERM": "screen"}))
        check("mpv plays audio", can_play_audio({"PATH": bin_dir}))
        check("no player, no audio", not can_play_audio({"PATH": os.path.dirname(bin_dir) + "/nope"}))
        check("ssh, no audio", not can_play_audio({"PATH": bin_dir, "SSH_CONNECTION": "1 2 3 4"}))
    finally:
        shutil.rmtree(bin_dir)


def test_media_on_screen(box):
    fresh(box)
    steps = [(" ", 0.3), ("q", 0.2)]
    _, out, _ = tty_session(steps, "--no-sync", box=box, env={"TMUX": "/tmp/s", "KITTY_WINDOW_ID": "1"})
    check("tmux hides media", "1 again" in out and "[image:" not in out and "[audio:" not in out, out[-300:])

    bin_dir = os.path.join(box, "bin")
    os.makedirs(bin_dir)
    with open(os.path.join(bin_dir, "mpv"), "w") as f:
        f.write("#!/bin/sh\n")
    os.chmod(os.path.join(bin_dir, "mpv"), 0o755)
    capable = {"KITTY_WINDOW_ID": "1", "PATH": bin_dir + os.pathsep + os.environ["PATH"]}

    _, out, _ = tty_session(steps, "--no-sync", box=box, env=capable)
    check("capable terminal, no local files: hidden", "[image:" not in out and "[audio:" not in out, out[-300:])

    media = os.path.join(box, "collection.media")
    os.makedirs(media, exist_ok=True)
    db = sqlite3.connect(os.path.join(box, "collection.anki2"))
    for (fields,) in db.execute("select flds from notes"):
        for name in re.findall(r'src="([^"]+)"|\[sound:([^\]]+)\]', fields):
            open(os.path.join(media, name[0] or name[1]), "w").close()
    db.close()
    _, out, _ = tty_session(steps, "--no-sync", box=box, env=capable)
    check("capable terminal, local files: shown", "[image:" in out and "[audio:" in out, out[-400:])
    shutil.rmtree(media)
    shutil.rmtree(bin_dir)


def test_review(box):
    base = fresh(box)

    r = run("--stats", "--json", box=box)
    check("stats json", r.returncode == 0 and "total_due" in json.loads(r.stdout), r.stdout + r.stderr)
    check("recent-fails json", "cards" in json.loads(run("--recent-fails", "--json", box=box).stdout))

    code, out, restored = tty_session([(" ", 0.3), ("1", 0.3), ("q", 0.2)], "--no-sync", box=box)
    log = revlog(box)
    check("space flips, 1 grades Again", code == 0 and len(log) == len(base) + 1 and log[-1][1] == 1, out[-300:])
    check("thinking time recorded", log[-1][2] >= 300, log[-1])
    check("terminal restored", restored)
    check("alt screen left", out.count("\x1b[?1049l") == 1)
    check("exit summary", "1 graded" in out, out[-200:])
    r = json.loads(run("--recent-fails", "--json", box=box).stdout)
    check("failed card is listed", r["cards"] and r["cards"][0]["card_id"] == log[-1][0], r)

    base = fresh(box)
    code, out, _ = tty_session([(" ", 0.1), ("\n", 0.2), ("q", 0.2)], "--no-sync", box=box)
    check("space on back grades Good", revlog(box)[-1][1] == 3 and len(revlog(box)) == len(base) + 1)

    base = fresh(box)
    code, out, _ = tty_session([(" ", 0.1), ("3", 0.2), ("u", 0.3), ("q", 0.2)], "--no-sync", box=box)
    check("u on the next front undoes", code == 0 and revlog(box) == base, len(revlog(box)) - len(base))
    check("undo refuses when empty",
          "nothing to undo" in tty_session([("u", 0.1), ("q", 0.3)], "--no-sync", box=box)[1])

    base = fresh(box)
    code, out, _ = tty_session([(" ", 0.1), ("3", 0.2), (" ", 0.1), ("3", 0.2), ("q", 0.2)],
                               "2", "--no-sync", box=box)
    check("batch stops at count", "2 done." in out and len(revlog(box)) == len(base) + 2, out[-200:])

    base = fresh(box)
    code, out, _ = tty_session([(" ", 0.1), ("\x1b[15~", 0.2), ("\x1b[A", 0.1), ("q", 0.2)], "--no-sync", box=box)
    check("function keys never grade", code == 0 and revlog(box) == base)

    base = fresh(box)
    code, out, _ = tty_session([(" ", 0.1), ("j", 0.2), ("j", 0.1), ("q", 0.2)], "--no-sync", box=box, rows=4)
    left = [int(n) for n in re.findall(r"scroll \((\d+) more\)", out)]
    check("tiny window scrolls", len(left) >= 2 and left[-1] == left[-2] - 1, left)
    check("scrolling reaches the last line", "Frequency:" in tty_session(
        [(" ", 0.1)] + [("j", 0.05)] * 24 + [("q", 0.2)], "--no-sync", box=box, rows=6, cols=40)[1])

    r = run("--no-sync", box=box, stdin=" 3q")
    check("piped keys work", r.returncode == 0 and "Traceback" not in r.stderr, r.stderr[-200:])
    for name, stdin in [("EOF", ""), ("EOF on back", " ")]:
        r = run("--no-sync", box=box, stdin=stdin)
        check(f"piped {name} clean", r.returncode == 0 and "Traceback" not in r.stderr, r.stderr[-200:])


def test_sync(box):
    fresh(box)
    with open(os.path.join(box, ".ankiauth.json"), "w") as f:
        f.write("{}")
    marker = os.path.join(box, "sync-count")
    count = f"open({marker!r}, 'a').write('x')"

    d = stub_scripts(count)
    code, out, _ = tty_session([(" ", 0.1), ("3", 0.2), ("q", 0.2)], box=box, scripts=d)
    check("syncs on start and after grading", code == 0 and len(open(marker).read()) == 2, out[-200:])
    os.remove(marker)
    tty_session([("q", 0.1)], box=box, scripts=d)
    check("no exit sync without grades", len(open(marker).read()) == 1)
    shutil.rmtree(d)

    d = stub_scripts("print('CONFLICT'); sys.exit(2)")
    code, out, _ = tty_session([("q", 0.3)], box=box, scripts=d)
    check("conflict is surfaced, session continues", code == 0 and "sync conflict" in out, out[-300:])
    shutil.rmtree(d)

    d = stub_scripts("print('offline or AnkiWeb unreachable'); sys.exit(1)")
    code, out, _ = tty_session([("q", 0.3)], box=box, scripts=d)
    check("sync failure doesn't block review", code == 0 and "offline or AnkiWeb" in out, out[-300:])
    shutil.rmtree(d)

    d = stub_scripts("time.sleep(30)")
    start = time.time()
    code, out, _ = tty_session([("x", 0.1), ("q", 0.3)], box=box, scripts=d)
    check("any key skips a hung sync", code == 0 and time.time() - start < 10 and "sync skipped" in out, out[-300:])
    shutil.rmtree(d)

    os.remove(os.path.join(box, ".ankiauth.json"))
    d = stub_scripts(count)
    tty_session([("q", 0.1)], box=box, scripts=d)
    check("no saved auth means no sync", not os.path.exists(marker) or len(open(marker).read()) == 1)
    shutil.rmtree(d)


def test_cli_edges(box, empty):
    fresh(box)
    r = run("--deck", "no such deck", "--stats", "--json", box=box)
    check("unknown deck refused", r.returncode == 1 and "error" in json.loads(r.stdout), r.stdout + r.stderr)

    r = run("--stats", "--json", box=empty)
    check("missing collection fails fast", r.returncode == 1 and "error" in json.loads(r.stdout), r.stdout + r.stderr)
    check("no stray collection", not os.path.exists(os.path.join(empty, "collection.anki2")))

    r = run(box=empty, script="ankisync.py")
    check("ankisync no-auth", r.returncode == 1 and "No saved auth" in (r.stdout + r.stderr), r.stdout + r.stderr)
    os.remove(os.path.join(empty, "collection.anki2"))
    check("ankisync conflict flags", "--download" in run("--help", box=box, script="ankisync.py").stdout)

    link = os.path.join(empty, "ank")
    os.symlink(os.path.join(SCRIPTS, "review.py"), link)
    r = subprocess.run([link, "--stats"], capture_output=True, text=True, env=env_for(box), timeout=60)
    check("runs through a symlink", r.returncode == 0, r.stderr[-300:])
    check("script is executable", os.stat(os.path.join(SCRIPTS, "review.py")).st_mode & stat.S_IXUSR)


def main():
    if not os.path.exists(REAL_COL):
        sys.exit(f"no collection at {REAL_COL}; run scripts/ankisync.py first")

    box = tempfile.mkdtemp(prefix="anki-review-e2e-")
    empty = tempfile.mkdtemp(prefix="anki-review-empty-")
    try:
        before = os.stat(REAL_COL)
        test_render()
        test_terminal_checks()
        test_media_on_screen(box)
        test_review(box)
        test_sync(box)
        test_cli_edges(box, empty)
        after = os.stat(REAL_COL)
        check("real collection untouched", (after.st_mtime, after.st_size) == (before.st_mtime, before.st_size))
    finally:
        shutil.rmtree(box, ignore_errors=True)
        shutil.rmtree(empty, ignore_errors=True)

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    for name, detail in failed:
        print(f"  FAILED: {name} — {detail}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
