#!/usr/bin/env python3
"""End-to-end tests for the anki-review scripts.

Runs entirely against a THROWAWAY COPY of your collection in a temp dir:
never writes to your real data directory, never uses credentials, never
touches the network. Requires an existing collection (run ankisync.py once
first) so there are real due cards to exercise.

Usage: python3 tests/e2e.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
REAL = os.environ.get("ANKI_CLI_DIR") or os.path.expanduser("~/.local/share/anki-cli")
REAL_COL = os.path.join(REAL, "collection.anki2")

passed, failed = [], []


def check(name, cond, detail=""):
    (passed if cond else failed).append((name, detail))
    print(("PASS " if cond else "FAIL ") + name + (("  | " + detail) if detail and not cond else ""))


def review(*args, box=None, stdin=None):
    env = dict(os.environ, ANKI_CLI_DIR=box)
    env.pop("ANKI_USER", None)
    env.pop("ANKI_PASS", None)
    return subprocess.run([sys.executable, os.path.join(SCRIPTS, "review.py"), *args],
                          capture_output=True, text=True, env=env, input=stdin, timeout=60)


def sync(*args, box=None):
    env = dict(os.environ, ANKI_CLI_DIR=box)
    env.pop("ANKI_USER", None)
    env.pop("ANKI_PASS", None)
    return subprocess.run([sys.executable, os.path.join(SCRIPTS, "ankisync.py"), *args],
                          capture_output=True, text=True, env=env, timeout=60)


def main():
    if not os.path.exists(REAL_COL):
        sys.exit(f"no collection at {REAL_COL}; run scripts/ankisync.py first")

    box = tempfile.mkdtemp(prefix="anki-review-e2e-")
    empty = tempfile.mkdtemp(prefix="anki-review-empty-")
    try:
        shutil.copy(REAL_COL, box)
        real_stat = os.stat(REAL_COL)

        # stats
        r = review("--stats", "--json", box=box)
        st = json.loads(r.stdout)
        check("stats json", r.returncode == 0 and "total_due" in st, r.stdout + r.stderr)

        # show: fetch a real due card dynamically
        r = review("--show", "--json", box=box)
        p = json.loads(r.stdout)
        if p.get("done"):
            print("NOTE: no cards due in this collection; skipping grade/undo tests")
        else:
            need = ("card_id", "reps", "deck", "question", "answer", "ratings", "remaining")
            check("show json fields", all(k in p for k in need), r.stdout[:200])
            check("no raw markup in text", not any(s in p["question"] + p["answer"]
                  for s in ("<div", "<img", "[sound:", "[anki:play:", "{{")), (p["question"] + p["answer"])[:150])
            check("no bidi marks", not any(c in json.dumps(p["ratings"]) for c in "⁦⁧⁨⁩"))

            # idempotent show
            check("show idempotent", review("--show", "--json", box=box).stdout == r.stdout)

            # reps guard rejects wrong expectation
            r2 = review("--answer", str(p["card_id"]), "3", "--reps", str(p["reps"] + 99), "--json", box=box)
            check("reps guard", r2.returncode == 1 and "error" in json.loads(r2.stdout), r2.stdout)

            # grade for real
            r2 = review("--answer", str(p["card_id"]), "3", "--reps", str(p["reps"]), "--json", box=box)
            g = json.loads(r2.stdout)
            check("grade works", r2.returncode == 0 and g.get("graded") == p["card_id"] and g.get("next_due"), r2.stdout + r2.stderr)

            # retry refused
            r2 = review("--answer", str(p["card_id"]), "3", "--reps", str(p["reps"]), "--json", box=box)
            check("retry refused", r2.returncode == 1, r2.stdout)

            # undo restores it
            r2 = review("--undo", "--json", box=box)
            check("undo works", r2.returncode == 0 and "undone" in json.loads(r2.stdout), r2.stdout + r2.stderr)
            back = json.loads(review("--show", "--json", box=box).stdout)
            check("undo restored card", back.get("card_id") == p["card_id"] and back.get("reps") == p["reps"],
                  f"{back.get('card_id')}/{back.get('reps')} vs {p['card_id']}/{p['reps']}")
            # double undo is refused
            check("double undo refused", review("--undo", "--json", box=box).returncode == 1)

        # bad inputs
        check("wrong id refused", review("--answer", "1", "3", "--json", box=box).returncode == 1)
        check("bad rating refused", review("--answer", "1", "9", "--json", box=box).returncode == 1)
        check("non-int id refused", review("--answer", "x", "3", "--json", box=box).returncode == 1)

        # interactive EOF / quit are graceful
        for name, stdin in [("EOF", ""), ("EOF-at-rating", "\n"), ("quit", "q\n")]:
            r = review(box=box, stdin=stdin)
            check(f"interactive {name} clean", r.returncode == 0 and "Traceback" not in r.stderr, r.stderr[-200:])

        # missing collection fails fast, creates nothing
        r = review("--show", "--json", box=empty)
        check("missing collection fails fast", r.returncode == 1 and "error" in json.loads(r.stdout), r.stdout + r.stderr)
        check("no stray collection", not os.path.exists(os.path.join(empty, "collection.anki2")))

        # ankisync without auth: clear message, no network
        r = sync(box=empty)
        check("ankisync no-auth", r.returncode == 1 and "No saved auth" in (r.stdout + r.stderr), r.stdout + r.stderr)
        check("ankisync conflict flags", "--download" in sync("--help", box=box).stdout)

        # real data untouched
        after = os.stat(REAL_COL)
        check("real collection untouched", (after.st_mtime, after.st_size) == (real_stat.st_mtime, real_stat.st_size))
    finally:
        shutil.rmtree(box, ignore_errors=True)
        shutil.rmtree(empty, ignore_errors=True)

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    for name, detail in failed:
        print(f"  FAILED: {name} — {detail}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
