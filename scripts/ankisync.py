#!/usr/bin/env python3
"""Sync the local Anki collection with AnkiWeb.

Data lives in $ANKI_CLI_DIR (default ~/.local/share/anki-cli).

First run: ANKI_USER=... ANKI_PASS=... python3 ankisync.py
Later runs: python3 ankisync.py   (reuses the saved sync key)

If both sides changed incompatibly, exits with code 2 and asks you to
re-run with an explicit direction:
  python3 ankisync.py --download   # AnkiWeb wins; local unsynced reviews are LOST
  python3 ankisync.py --upload     # local wins; AnkiWeb-side changes are LOST

Exit codes: 0 synced/up-to-date, 1 error, 2 conflict needs --download/--upload.

Requires: pip install anki
"""
import argparse
import json
import os
import sys

from anki.collection import Collection
from anki.errors import NetworkError, SyncError, SyncErrorKind
from anki.sync import SyncAuth

DATA_DIR = os.environ.get("ANKI_CLI_DIR") or os.path.expanduser("~/.local/share/anki-cli")
COL_PATH = os.path.join(DATA_DIR, "collection.anki2")
AUTH_PATH = os.path.join(DATA_DIR, ".ankiauth.json")
UNDO_PATH = os.path.join(DATA_DIR, ".undo.json")


def save_auth(auth: SyncAuth) -> None:
    fd = os.open(AUTH_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)  # O_CREAT mode doesn't apply to pre-existing files
    with os.fdopen(fd, "w") as f:
        json.dump({"hkey": auth.hkey, "endpoint": auth.endpoint}, f)


def get_auth(col: Collection) -> SyncAuth:
    if os.path.exists(AUTH_PATH):
        with open(AUTH_PATH) as f:
            d = json.load(f)
        return SyncAuth(hkey=d["hkey"], endpoint=d.get("endpoint") or None)
    user = os.environ.get("ANKI_USER")
    pw = os.environ.get("ANKI_PASS")
    if not (user and pw):
        sys.exit("No saved auth. Set ANKI_USER and ANKI_PASS env vars for first login.")
    auth = col.sync_login(user, pw, endpoint=None)
    save_auth(auth)
    return auth


def full_sync(col: Collection, auth: SyncAuth, upload: bool) -> None:
    direction = "upload to" if upload else "download from"
    print(f"one-way sync: {direction} AnkiWeb...")
    col.close_for_full_sync()
    # server_usn=None skips the background media sync we don't use
    col.full_upload_or_download(auth=auth, server_usn=None, upload=upload)
    col.reopen(after_full_sync=True)
    if os.path.exists(UNDO_PATH):
        os.remove(UNDO_PATH)  # a one-way sync invalidates any undo snapshot
    print("done.")


def summary(col: Collection) -> None:
    tree = col.sched.deck_due_tree()
    due = 0
    print("decks:")
    for d in tree.children:
        n = d.new_count + d.learn_count + d.review_count
        due += n
        print(f"  {d.name}: {n} due" if n else f"  {d.name}: —")
    print(f"total cards: {col.card_count()}, due now: {due}")


def main() -> None:
    ap = argparse.ArgumentParser()
    direction = ap.add_mutually_exclusive_group()
    direction.add_argument("--download", action="store_true",
                           help="resolve a sync conflict by taking AnkiWeb's copy (local unsynced reviews are lost)")
    direction.add_argument("--upload", action="store_true",
                           help="resolve a sync conflict by pushing the local copy (AnkiWeb-side changes are lost)")
    args = ap.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)
    col = Collection(COL_PATH)
    try:
        auth = get_auth(col)
        try:
            out = col.sync_collection(auth, sync_media=False)
        except SyncError as e:
            if e.kind == SyncErrorKind.AUTH:
                if os.path.exists(AUTH_PATH):
                    os.remove(AUTH_PATH)
                sys.exit("AnkiWeb rejected the saved sync key (password changed?). "
                         "Removed it — log in again yourself with:\n"
                         "  ANKI_USER=<email> ANKI_PASS=<password> python3 ankisync.py")
            raise

        if out.new_endpoint:
            auth = SyncAuth(hkey=auth.hkey, endpoint=out.new_endpoint)
            save_auth(auth)

        req = out.required
        if req in (out.NO_CHANGES, out.NORMAL_SYNC):
            print("synced (normal sync, no conflict).")
        elif req == out.FULL_DOWNLOAD:
            full_sync(col, auth, upload=False)
        elif req == out.FULL_UPLOAD:
            full_sync(col, auth, upload=True)
        elif req == out.FULL_SYNC:
            if args.download or args.upload:
                full_sync(col, auth, upload=args.upload)
            else:
                print("CONFLICT: local and AnkiWeb collections have diverged; a one-way sync is required.\n"
                      "Nothing was synced. Decide which side wins and re-run:\n"
                      "  python3 ankisync.py --download   (AnkiWeb wins; local unsynced reviews are LOST)\n"
                      "  python3 ankisync.py --upload     (local wins; AnkiWeb-side changes are LOST)")
                sys.exit(2)
        else:
            sys.exit(f"unexpected sync state: {out}")
        summary(col)
    except NetworkError as e:
        sys.exit(f"offline or AnkiWeb unreachable: {e}")
    except SyncError as e:
        # covers first login (bad password) and mid-transfer full-sync failures
        sys.exit(f"AnkiWeb sync failed: {e}")
    finally:
        if col.db:
            col.close()


if __name__ == "__main__":
    main()
