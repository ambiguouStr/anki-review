#!/usr/bin/env python3
"""Terminal Anki reviewer using the real Anki v3 scheduler.

Data lives in $ANKI_CLI_DIR (default ~/.local/share/anki-cli).

Usage:
  python3 review.py                    # interactive terminal review
  python3 review.py --limit 20         # stop after 20 cards
  python3 review.py --deck "Deck Name" # pin the session to one deck
  python3 review.py --show [--json]    # print next due card and exit
  python3 review.py --answer CARD_ID RATING [--reps N] [--json]
  python3 review.py --undo [--json]    # undo the last review
  python3 review.py --stats [--json]   # due counts per deck

Keys during interactive review:
  <enter>  show answer
  1/2/3/4  Again / Hard / Good / Easy
  u        undo previous grade
  q        quit (progress saved locally; run ankisync.py to push to AnkiWeb)
"""
import argparse
import base64
import html
import json as jsonlib
import os
import re
import sys

from anki.collection import Collection  # must come first: anki.cards alone hits a circular import
from anki import cards_pb2
from anki.cards import Card
from anki.decks import DeckId
from anki.errors import NotFoundError
from anki.scheduler.v3 import CardAnswer

DATA_DIR = os.environ.get("ANKI_CLI_DIR") or os.path.expanduser("~/.local/share/anki-cli")
COL_PATH = os.path.join(DATA_DIR, "collection.anki2")
UNDO_PATH = os.path.join(DATA_DIR, ".undo.json")

RATINGS = {
    "1": CardAnswer.AGAIN,
    "2": CardAnswer.HARD,
    "3": CardAnswer.GOOD,
    "4": CardAnswer.EASY,
}
RATING_ORDER = [CardAnswer.AGAIN, CardAnswer.HARD, CardAnswer.GOOD, CardAnswer.EASY]

JSON_MODE = False


def die(msg: str, code: int = 1):
    if JSON_MODE:
        print(jsonlib.dumps({"error": msg}, ensure_ascii=False))
    else:
        print(msg, file=sys.stderr)
    sys.exit(code)


def plain(s: str) -> str:
    """Drop Unicode bidi-isolate marks that Anki's i18n wraps numbers in."""
    return re.sub(r"[\u2066-\u2069]", "", s)


def clean(html_text: str) -> str:
    t = html_text
    t = re.sub(r"\[sound:[^\]]*\]", "", t)
    t = re.sub(r"\[anki:play:[^\]]*\]", "", t)
    t = re.sub(r"<!--.*?-->", "", t, flags=re.S)
    t = re.sub(r"<(style|script).*?</\1>", "", t, flags=re.S)
    t = re.sub(r"""<img[^>]*src\s*=\s*["']?([^"'\s>]+)["']?[^>]*>""", r"[image: \1]", t)
    t = re.sub(r"<br\s*/?>", "\n", t)
    t = re.sub(r"</?(div|p|li|tr)[^>]*>", "\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = t.replace("<!--", "").replace("-->", "")
    t = html.unescape(t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def rating_labels(col, states):
    labels = col.sched.describe_next_states(states)
    return {str(i + 1): plain(labels[i]) for i in range(4)}


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
    switched = None
    out = col.sched.get_queued_cards(fetch_limit=1)
    if not out.cards and not pinned_deck:
        for deck_id, name, _, new, learn, review in due_tree(col):
            if new or learn or review:
                col.decks.select(DeckId(deck_id))
                switched = name
                out = col.sched.get_queued_cards(fetch_limit=1)
                break
    if not out.cards:
        return None, 0, switched
    remaining = out.new_count + out.learning_count + out.review_count
    return out.cards[0], remaining, switched


def card_payload(col, queued, remaining, switched):
    card = col.get_card(queued.card.id)
    note = col.get_note(card.nid)
    p = {
        "card_id": card.id,
        "reps": card.reps,
        "deck": col.decks.name(card.did),
        "question": clean(card.question()),
        "answer": clean(card.answer()),
        # raw note fields (HTML) — the rendered card may omit some (e.g. pitch accent)
        "fields": {k: v for k, v in zip(note.keys(), note.fields) if v.strip()},
        "ratings": rating_labels(col, queued.states),
        "remaining": remaining,
    }
    if switched:
        p["switched_deck"] = switched
    return p


def grade(col, queued, card, rating) -> str:
    labels = rating_labels(col, queued.states)
    # snapshot pre-grade state: Anki's own undo stack is in-memory only,
    # so a later --undo invocation needs this to restore the card.
    # Written atomically BEFORE answering: if the answer fails, the
    # expect_reps check below rejects the snapshot as stale.
    snap = {
        "card_id": card.id,
        "card": base64.b64encode(card._to_backend_card().SerializeToString()).decode(),
        "max_revlog": col.db.scalar("select coalesce(max(id), 0) from revlog where cid = ?", card.id),
        "expect_reps": card.reps + 1,
    }
    tmp = UNDO_PATH + ".tmp"
    with open(tmp, "w") as f:
        jsonlib.dump(snap, f)
    os.replace(tmp, UNDO_PATH)
    answer = col.sched.build_answer(card=card, states=queued.states, rating=rating)
    col.sched.answer_card(answer)
    return labels[str(RATING_ORDER.index(rating) + 1)]


def try_undo(col):
    """Revert the most recent grade from the snapshot file.
    Returns (undone_label, None) or (None, error_message)."""
    if not os.path.exists(UNDO_PATH):
        return None, "nothing to undo (only the most recent grade can be undone)"
    try:
        with open(UNDO_PATH) as f:
            rec = jsonlib.load(f)
        proto = cards_pb2.Card()
        proto.ParseFromString(base64.b64decode(rec["card"]))
    except (ValueError, KeyError):
        os.remove(UNDO_PATH)
        return None, "undo snapshot was corrupt; discarded"
    try:
        current = col.get_card(rec["card_id"])
    except NotFoundError:
        os.remove(UNDO_PATH)
        return None, f"card {rec['card_id']} no longer exists; undo discarded"
    if current.reps != rec.get("expect_reps"):
        os.remove(UNDO_PATH)
        return None, "undo snapshot is stale (card was reviewed or synced since); discarded"
    col.update_card(Card(col, backend_card=proto), skip_undo_entry=True)
    # drop the revlog entry the grade added, if it hasn't synced yet
    col.db.execute("delete from revlog where cid = ? and id > ? and usn = -1",
                   rec["card_id"], rec["max_revlog"])
    os.remove(UNDO_PATH)
    return f"grade of card {rec['card_id']}", None


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return "q"


def interactive(col, limit, pinned_deck):
    done = 0
    while limit is None or done < limit:
        queued, remaining, switched = next_queued(col, pinned_deck)
        if not queued:
            print("\nNo more cards due. 🎉")
            break
        if switched:
            print(f"\n(switched to deck: {switched})")
        card = col.get_card(queued.card.id)
        card.start_timer()
        print(f"\n[{remaining} remaining]  card {card.id}  (deck: {col.decks.name(card.did)})")
        print("-" * 40)
        print(clean(card.question()))
        if ask("\n(enter=flip, q=quit) > ") == "q":
            break
        print("=" * 40)
        print(clean(card.answer()))
        r = rating_labels(col, queued.states)
        print(f"\n1 Again({r['1']})  2 Hard({r['2']})  3 Good({r['3']})  4 Easy({r['4']})  u undo  q quit")
        while True:
            k = ask("> ")
            if k == "q":
                return
            if k == "u":
                label, err = try_undo(col)
                if err:
                    print(err)
                    continue
                print(f"↩ undid: {label}")
                done = max(0, done - 1)
                break
            if k in RATINGS:
                nxt = grade(col, queued, card, RATINGS[k])
                print(f"→ next: {nxt}")
                done += 1
                break


def main():
    global JSON_MODE
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--deck", default=None, help="pin the session to this deck")
    ap.add_argument("--show", action="store_true", help="print next due card and exit")
    ap.add_argument("--answer", nargs=2, metavar=("CARD_ID", "RATING"), help="grade the current card 1-4")
    ap.add_argument("--reps", type=int, default=None, help="expected reps from --show; refuses to double-grade")
    ap.add_argument("--undo", action="store_true", help="undo the last review")
    ap.add_argument("--stats", action="store_true", help="due counts per deck")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()
    JSON_MODE = args.json

    if not os.path.exists(COL_PATH):
        die(f"no collection at {COL_PATH} — run ankisync.py first (or check ANKI_CLI_DIR)")

    col = Collection(COL_PATH)
    try:
        if args.deck:
            did = col.decks.id_for_name(args.deck)
            if did is None:
                names = ", ".join(d.name for d in col.decks.all_names_and_ids())
                die(f"no deck named {args.deck!r}; decks: {names}")
            col.decks.select(DeckId(did))

        if args.show:
            queued, remaining, switched = next_queued(col, pinned_deck=bool(args.deck))
            if not queued:
                print(jsonlib.dumps({"done": True}) if args.json else "No cards due.")
                return
            p = card_payload(col, queued, remaining, switched)
            if args.json:
                print(jsonlib.dumps(p, ensure_ascii=False))
            else:
                print(f"card {p['card_id']}  (deck: {p['deck']}, reps: {p['reps']}, {remaining} remaining)")
                print("-" * 40)
                print(p["question"])
                print("=" * 40)
                print(p["answer"])
                r = p["ratings"]
                print(f"\nratings: 1 Again({r['1']}) 2 Hard({r['2']}) 3 Good({r['3']}) 4 Easy({r['4']})")
        elif args.answer:
            try:
                card_id, rating = int(args.answer[0]), args.answer[1]
            except ValueError:
                die("CARD_ID must be an integer")
            if rating not in RATINGS:
                die("rating must be 1-4")
            # search the whole due queue, not just the head: a learning card
            # becoming due mid-chat must not block grading the shown card
            out = col.sched.get_queued_cards(fetch_limit=100)
            queued = next((c for c in out.cards if c.card.id == card_id), None)
            if queued is None:
                die(f"card {card_id} is not currently due; run --show first")
            card = col.get_card(card_id)
            if args.reps is not None and card.reps != args.reps:
                die(f"card {card_id} has reps={card.reps}, expected {args.reps} — already graded? run --show again")
            card.start_timer()
            nxt = grade(col, queued, card, RATINGS[rating])
            _, remaining, _ = next_queued(col, pinned_deck=bool(args.deck))
            if args.json:
                print(jsonlib.dumps({"graded": card_id, "rating": int(rating), "next_due": nxt,
                                     "remaining": remaining}, ensure_ascii=False))
            else:
                print(f"graded {card_id} → next: {nxt} ({remaining} remaining)")
        elif args.undo:
            label, err = try_undo(col)
            if err:
                die(err)
            print(jsonlib.dumps({"undone": label}, ensure_ascii=False) if args.json else f"undone: {label}")
        elif args.stats:
            decks = due_tree(col)
            # top-level deck counts already include their subdecks
            total = sum(nw + l + r for _, _, level, nw, l, r in decks if level == 1)
            if args.json:
                print(jsonlib.dumps({"decks": [
                    {"deck": n, "new": nw, "learning": l, "review": r}
                    for _, n, _, nw, l, r in decks
                ], "total_due": total}, ensure_ascii=False))
            else:
                for _, name, level, nw, l, r in decks:
                    mark = f"new {nw}, learning {l}, review {r}" if nw + l + r else "—"
                    print(f"{'  ' * (level - 1)}{name.split('::')[-1]}: {mark}")
                print(f"total due: {total}")
        else:
            interactive(col, args.limit, pinned_deck=bool(args.deck))
    finally:
        col.close()


if __name__ == "__main__":
    main()
