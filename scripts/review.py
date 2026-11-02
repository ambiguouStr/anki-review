#!/usr/bin/env python3
"""Single-keypress Anki reviewer for the terminal, on the real Anki v3 scheduler.

Data lives in $ANKI_CLI_DIR (default ~/.local/share/anki-cli).

Usage:
  ank                   # review 10 cards, then offer another batch
  ank 5                 # batch of 5 (0 = no limit)
  ank --deck "Name"     # pin the session to one deck
  ank --no-sync         # skip the automatic AnkiWeb sync
  ank --stats [--json]  # due counts per deck
  ank --recent-fails [--json]  # cards you recently pressed Again on

Keys:
  space/enter  flip, then grade Good
  1/2/3/4      Again / Hard / Good / Easy
  u            undo the previous grade (any time)
  j/k          scroll, when the card is taller than the window
  q            quit
"""
import argparse
import functools
import json as jsonlib
import os
import select
import shutil
import subprocess
import sys
import termios
import time
import tty
import unicodedata

from anki.collection import Collection  # must come first: anki.cards alone hits a circular import
from anki.decks import DeckId
from anki.errors import DBError
from anki.scheduler.v3 import CardAnswer

from render import Media, clean, render_back

DATA_DIR = os.environ.get("ANKI_CLI_DIR") or os.path.expanduser("~/.local/share/anki-cli")
COL_PATH = os.path.join(DATA_DIR, "collection.anki2")
AUTH_PATH = os.path.join(DATA_DIR, ".ankiauth.json")
MEDIA_DIR = os.path.join(DATA_DIR, "collection.media")
SYNC_SCRIPT = os.path.join(os.path.dirname(os.path.realpath(__file__)), "ankisync.py")

RATINGS = {
    "1": CardAnswer.AGAIN,
    "2": CardAnswer.HARD,
    "3": CardAnswer.GOOD,
    "4": CardAnswer.EASY,
}
FLIP_KEYS = (" ", "\n", "\r")
BIDI_MARKS = dict.fromkeys(range(0x2066, 0x206A))  # Anki's i18n wraps numbers in these

ENTER_ALT_SCREEN = "\x1b[?1049h\x1b[?25l"
LEAVE_ALT_SCREEN = "\x1b[?25h\x1b[?1049l"
CLEAR = "\x1b[2J\x1b[H"
DIM = "\x1b[2m"
RESET = "\x1b[0m"

# kitty, iTerm2, WezTerm and Ghostty speak an inline image protocol
IMAGE_TERMINALS = ("kitty", "iterm.app", "wezterm", "ghostty")
# paplay, pw-play and aplay are absent here on purpose: decks ship mp3
AUDIO_PLAYERS = ("mpv", "ffplay", "afplay")

JSON_MODE = False


def die(msg: str, code: int = 1):
    if JSON_MODE:
        print(jsonlib.dumps({"error": msg}, ensure_ascii=False))
    else:
        print(msg, file=sys.stderr)
    sys.exit(code)


def terminal_shows_images(env) -> bool:
    """tmux swallows image escapes by default, and it leaves the outer terminal's
    variables in place, so it has to be ruled out before looking at them."""
    if env.get("TMUX"):
        return False
    names = (env.get("TERM_PROGRAM", ""), env.get("TERM", ""))
    return bool(env.get("KITTY_WINDOW_ID") or env.get("WEZTERM_PANE")
                or any(t in name.lower() for t in IMAGE_TERMINALS for name in names))


def can_play_audio(env) -> bool:
    """Over SSH a local player would make its sound on the remote machine."""
    if env.get("SSH_CONNECTION") or env.get("SSH_TTY"):
        return False
    return any(shutil.which(player, path=env.get("PATH")) for player in AUDIO_PLAYERS)


@functools.lru_cache(maxsize=None)
def media_support() -> Media:
    """Media is mentioned only if the file is local and this terminal can use it."""
    tty_out = sys.stdout.isatty()
    images = tty_out and terminal_shows_images(os.environ)
    audio = tty_out and can_play_audio(os.environ)

    def local(name):
        return os.path.exists(os.path.join(MEDIA_DIR, name))

    return Media(image=lambda name: images and local(name),
                 audio=lambda name: audio and local(name))


class Keys:
    """Single-keypress input; reads stdin character by character when piped."""

    def __enter__(self):
        self.fd = sys.stdin.fileno()
        self.is_tty = os.isatty(self.fd)
        if self.is_tty:
            self.saved = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        return self

    def __exit__(self, *exc):
        if self.is_tty:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)

    def pressed(self, wait: float) -> bool:
        return bool(select.select([self.fd], [], [], wait)[0])

    def read(self) -> str:
        """One lowercase key; EOF and Ctrl-C read as q."""
        try:
            if not self.is_tty:
                return sys.stdin.read(1).lower() or "q"
            data = os.read(self.fd, 32)
        except KeyboardInterrupt:
            return "q"
        if not data:
            return "q"
        # arrows and F-keys arrive as one escape sequence; F5 is ESC [ 1 5 ~,
        # which must not grade the card as 1
        if data.startswith(b"\x1b"):
            return ""
        return data.decode(errors="ignore")[:1].lower()


def wrap(line: str, width: int) -> list:
    """Hard-wrap by display width; CJK characters take two columns."""
    rows, row, used = [], "", 0
    for ch in line:
        w = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if used + w > width:
            rows.append(row)
            row, used = "", 0
        row += ch
        used += w
    rows.append(row)
    return rows


def show(keys: Keys, text: str, footer: str, allowed: tuple) -> str:
    """Draw a screen until a key in `allowed` is pressed. j/k scroll, so a card
    taller than a small tmux pane can still be read in full."""
    if not sys.stdout.isatty():
        print(f"{text}\n{footer}", flush=True)
        while True:
            key = keys.read()
            if key in allowed:
                return key
    offset = 0
    while True:
        size = shutil.get_terminal_size()
        body = [row for line in text.split("\n") for row in wrap(line, size.columns)]
        # the hint is always budgeted for, so scrolling can't reflow the footer
        foot = wrap(footer + "  j/k scroll (99 more)", size.columns)
        room = max(1, size.lines - len(foot))
        last = max(0, len(body) - room)
        offset = min(offset, last)
        hint = f"  j/k scroll ({last - offset} more)" if last else ""
        sys.stdout.write(CLEAR + "\n".join(body[offset:offset + room]) + "\n"
                         + f"{DIM}{footer}{hint}{RESET}")
        sys.stdout.flush()
        key = keys.read()
        if key == "j" and offset < last:
            offset += 1
        elif key == "k" and offset:
            offset -= 1
        elif key in allowed:
            return key


def run_sync(keys: Keys) -> str:
    """Run ankisync.py in a child process (the collection is locked while it
    syncs) so any key can skip a slow network. Returns a problem or ""."""
    print("syncing… (any key to skip)", flush=True)
    proc = subprocess.Popen([sys.executable, SYNC_SCRIPT], text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        while proc.poll() is None:
            if keys.pressed(0.05):
                keys.read()
                proc.terminate()
                proc.wait()
                return "sync skipped"
    except KeyboardInterrupt:
        proc.terminate()
        proc.wait()
        return "sync skipped"
    if proc.returncode == 0:
        return ""
    if proc.returncode == 2:
        return ("sync conflict, nothing transferred: run ankisync.py with --download "
                "(AnkiWeb wins) or --upload (local wins)")
    lines = proc.stdout.read().strip().splitlines()
    return f"sync failed: {lines[-1] if lines else proc.returncode}"


def due_tree(col):
    """Flat list of (deck_id, full_name, level, new, learning, review) for every deck.
    Tree nodes carry short name components, so the full name is built while walking."""
    out = []

    def walk(node, prefix):
        for c in node.children:
            full = f"{prefix}::{c.name}" if prefix else c.name
            out.append((c.deck_id, full, c.level, c.new_count, c.learn_count, c.review_count))
            walk(c, full)

    walk(col.sched.deck_due_tree(), "")
    return out


def next_queued(col, pinned_deck: bool):
    """Head of the current deck's queue; if empty and no deck is pinned,
    switch to the next deck that still has due cards and retry once."""
    out = col.sched.get_queued_cards(fetch_limit=1)
    if not out.cards and not pinned_deck:
        for deck_id, _, _, new, learn, review in due_tree(col):
            if new or learn or review:
                col.decks.select(DeckId(deck_id))
                out = col.sched.get_queued_cards(fetch_limit=1)
                break
    if not out.cards:
        return None, 0
    return out.cards[0], out.new_count + out.learning_count + out.review_count


def card_front(card) -> str:
    return clean(card.question(), media=media_support())


def card_back(col, card) -> str:
    note = col.get_note(card.nid)
    return render_back(card.answer(), dict(zip(note.keys(), note.fields)), media_support())


def undo_last(col, graded: list) -> bool:
    """Undo until the most recently graded card's reps drop: Anki may have
    stacked an undoable deck switch on top of the grade."""
    if not graded:
        return False
    card_id = graded.pop()
    reps = col.get_card(card_id).reps
    while col.undo_status().undo:
        col.undo()
        if col.get_card(card_id).reps < reps:
            return True
    return False


def review(col, keys: Keys, batch, pinned_deck: bool, notice: str):
    """Review until the user stops. Returns (grades kept, message for after exit)."""
    graded = []
    target = batch
    while True:
        if target is not None and len(graded) >= target:
            key = show(keys, f"{len(graded)} done.", "enter another batch  u undo  q quit",
                       (*FLIP_KEYS, "u", "q"))
            if key in FLIP_KEYS:
                target += batch
            elif key == "u":
                undo_last(col, graded)
            else:
                return graded, ""
            continue

        queued, remaining = next_queued(col, pinned_deck)
        if not queued:
            return graded, "No more cards due."
        card = col.get_card(queued.card.id)
        card.start_timer()
        header = f"{remaining} left · {col.decks.name(card.did)}"
        if notice:
            header = f"{notice}\n{header}"
            notice = ""

        key = show(keys, f"{header}\n\n{card_front(card)}", "space flip  u undo  q quit",
                   (*FLIP_KEYS, "u", "q"))
        if key == "q":
            return graded, ""
        if key == "u":
            if not undo_last(col, graded):
                notice = "nothing to undo"
            continue

        labels = col.sched.describe_next_states(queued.states)
        labels = [label.translate(BIDI_MARKS) for label in labels]
        footer = (f"1 again {labels[0]}  2 hard {labels[1]}  3 good {labels[2]}  "
                  f"4 easy {labels[3]}  u undo  q quit")
        key = show(keys, f"{header}\n\n{card_back(col, card)}", footer,
                   (*RATINGS, *FLIP_KEYS, "u", "q"))
        if key == "q":
            return graded, ""
        if key == "u":
            if not undo_last(col, graded):
                notice = "nothing to undo"
            continue
        rating = RATINGS.get(key, CardAnswer.GOOD)  # space/enter means Good, as in Anki
        col.sched.answer_card(col.sched.build_answer(card=card, states=queued.states, rating=rating))
        graded.append(card.id)


def session(args):
    batch = args.count or None
    with Keys() as keys:
        can_sync = (keys.is_tty and sys.stdout.isatty()
                    and not args.no_sync and os.path.exists(AUTH_PATH))
        notice = run_sync(keys) if can_sync else ""
        col = open_collection(args.deck)
        alt = sys.stdout.isatty()
        if alt:
            sys.stdout.write(ENTER_ALT_SCREEN)
        try:
            graded, message = review(col, keys, batch, bool(args.deck), notice)
        finally:
            if alt:
                sys.stdout.write(LEAVE_ALT_SCREEN)
                sys.stdout.flush()
            remaining = next_queued(col, bool(args.deck))[1]
            col.close()
        print(message or f"{len(graded)} graded, {remaining} left.")
        problem = run_sync(keys) if can_sync and graded else ""
        if problem:
            print(f"{problem}; grades are saved locally.")


def open_collection(deck):
    try:
        col = Collection(COL_PATH)
    except DBError:
        die("the collection is in use by another ank or ankisync.py; try again in a moment")
    if deck:
        did = col.decks.id_for_name(deck)
        if did is None:
            names = ", ".join(d.name for d in col.decks.all_names_and_ids())
            col.close()
            die(f"no deck named {deck!r}; decks: {names}")
        col.decks.select(DeckId(did))
    return col


def stats(col, as_json: bool):
    decks = due_tree(col)
    # top-level deck counts already include their subdecks
    total = sum(nw + l + r for _, _, level, nw, l, r in decks if level == 1)
    if as_json:
        print(jsonlib.dumps({"decks": [
            {"deck": n, "new": nw, "learning": l, "review": r}
            for _, n, _, nw, l, r in decks
        ], "total_due": total}, ensure_ascii=False))
        return
    for _, name, level, nw, l, r in decks:
        mark = f"new {nw}, learning {l}, review {r}" if nw + l + r else "—"
        print(f"{'  ' * (level - 1)}{name.split('::')[-1]}: {mark}")
    print(f"total due: {total}")


def recent_fails(col, as_json: bool, days: int = 2, limit: int = 20):
    """Cards last pressed Again in the past `days` days, newest first."""
    since = int((time.time() - days * 86400) * 1000)
    rows = col.db.all("select cid, max(id) from revlog where ease = 1 and id > ? "
                      "group by cid order by 2 desc limit ?", since, limit)
    cards = []
    for card_id, _ in rows:
        card = col.get_card(card_id)
        cards.append({"card_id": card_id, "deck": col.decks.name(card.did),
                      "front": card_front(card), "back": card_back(col, card)})
    if as_json:
        print(jsonlib.dumps({"cards": cards}, ensure_ascii=False))
        return
    for c in cards:
        print(f"{c['front']}\n---\n{c['back']}\n")
    print(f"{len(cards)} recently failed.")


def main():
    global JSON_MODE
    ap = argparse.ArgumentParser(description="Single-keypress Anki reviewer.")
    ap.add_argument("count", nargs="?", type=int, default=10, help="cards per batch; 0 = no limit")
    ap.add_argument("--deck", default=None, help="pin the session to this deck")
    ap.add_argument("--no-sync", action="store_true", help="skip the automatic AnkiWeb sync")
    ap.add_argument("--stats", action="store_true", help="due counts per deck")
    ap.add_argument("--recent-fails", action="store_true", help="cards recently graded Again")
    ap.add_argument("--json", action="store_true", help="machine-readable --stats/--recent-fails")
    args = ap.parse_args()
    JSON_MODE = args.json

    if not os.path.exists(COL_PATH):
        die(f"no collection at {COL_PATH} — run ankisync.py first (or check ANKI_CLI_DIR)")

    if args.stats or args.recent_fails:
        col = open_collection(args.deck)
        try:
            (stats if args.stats else recent_fails)(col, args.json)
        finally:
            col.close()
    else:
        session(args)


if __name__ == "__main__":
    main()
