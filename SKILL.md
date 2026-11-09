---
name: anki-review
description: >
  Companion to the user's terminal Anki reviewer `ank`. Explains cards they recently
  failed, reports due counts, and handles AnkiWeb sync problems. Reviewing itself happens
  in `ank`, not in chat. Use when the user says "quiz me", "anki", "review my cards",
  "flashcards", "背单词", "复习卡片", or asks about their failed or due cards.
---

# Anki Review

Reviewing happens in the user's terminal, one keypress per step, with no agent in the loop. Don't quiz in chat and never grade cards: grades change real intervals and sync to AnkiWeb.

Scripts live in `scripts/` next to this file; let `S` be that directory. The collection lives in `$ANKI_CLI_DIR` (default `~/.local/share/anki-cli/`). Requires the `anki` pip package.

## When asked to review

Tell the user to run `ank` in a terminal or tmux pane (`ank 5` for five cards). Keys: space flips and then grades Good, 1-4 grade, `r`/`s` replay the word/sentence audio, `u` undoes, `q` quits. It syncs by itself. It needs a real terminal, so don't launch it from a tool call.

Flipping a card plays its audio: the deck's own recording if media has been downloaded (`python3 $S/ankisync.py --media`), otherwise macOS `say` reading the card's reading field. `--no-audio` silences it. Audio never plays over SSH.

## Explaining failed cards

`python3 $S/review.py --recent-fails --json` returns `{"cards": [{card_id, deck, front, back}]}`: cards pressed Again in the past two days, newest first. `back` is the complete back. Explain what the user asks about (grammar, mnemonics, usage, confusable words), and don't repeat the whole list unprompted.

## Other commands

- Due counts: `python3 $S/review.py --stats [--json]`, optionally with `--deck <name>`.
- Sync: `python3 $S/ankisync.py`. Exit 0 means synced, 2 means conflict, anything else is an error. Only say reviews are synced after exit 0.

## Sync conflicts and auth

- Exit code 2 from `ankisync.py` = both sides changed incompatibly; nothing was transferred. The user must choose: `--download` (AnkiWeb wins — **discards local unsynced reviews**) or `--upload` (local wins — discards AnkiWeb-side changes). Never choose for them.
- If the saved sync key is rejected (password changed), `ankisync.py` deletes it and says so. Have the user re-login **themselves** (`ANKI_USER=... ANKI_PASS=... python3 $S/ankisync.py`) so credentials don't enter the chat. Never ask for the password.
