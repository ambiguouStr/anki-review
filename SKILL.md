---
name: anki-review
description: >
  Quiz the user on their due Anki flashcards (synced from AnkiWeb) directly in chat,
  using Anki's real spaced-repetition scheduler that syncs grades back to AnkiWeb.
  Works with any deck or subject. Use when the user says "quiz me", "anki",
  "review my cards", "flashcards", "背单词", "复习卡片", or asks to memorize/practice cards.
---

# Anki Review

Run a spaced-repetition quiz session in chat. Scripts live in `scripts/` next to this file; the collection lives in `$ANKI_CLI_DIR` (default `~/.local/share/anki-cli/`). Requires the `anki` pip package (`pip install anki` if missing).

Let `S` = the `scripts/` directory of this skill.

## Session workflow

1. **Sync down** (best effort): `python3 $S/ankisync.py`. Exit 0 = synced; exit 2 = conflict (see Sync conflicts below — ask the user before resolving); any other failure: tell the user and continue with the local collection.
2. **Loop** until the user stops or no cards remain:
   a. `python3 $S/review.py --show --json` → `card_id`, `reps`, `deck`, `question`, `answer`, `ratings` (interval label per rating 1-4), `remaining`, optional `switched_deck`. `{"done": true}` means finished. Errors come back as `{"error": ...}`.
   b. Show the user **only the question** (plus the remaining count; mention the deck if `switched_deck` appeared). NEVER reveal the answer before they respond. `[image: file]` markers mean the card has a picture that isn't available locally — tell the user, and treat image-dependent questions leniently.
   c. Wait for their answer in chat.
   d. Reveal the back IN FULL — every part of the `answer` field: reading, meaning, example sentence with furigana and translation, notes, and `[image: ...]` markers. Never summarize or omit lines. Then compare with the user's answer and judge:
      - wrong or blank → rating 1 (Again)
      - correct but hesitant/partial (e.g. meaning right, reading wrong) → 2 (Hard)
      - correct → 3 (Good)
      - instant + fully correct, user says it was trivial → 4 (Easy)
      State your suggested rating; if the user objects, use theirs.
   e. `python3 $S/review.py --answer <card_id> <rating> --reps <reps> --json` — always pass `--reps` from the same `--show`; it makes grading idempotent (a retried command is refused instead of double-graded).
   f. Mis-graded (wrong rating sent, or user changes their mind)? `python3 $S/review.py --undo --json`, then re-show and re-grade.
   g. Add brief explanations (grammar, mnemonics, usage) when the user gets a card wrong or asks.
   h. Every ~20 cards, run `python3 $S/ankisync.py` so long sessions show up on AnkiWeb/other devices promptly (grades are always saved locally immediately regardless).
3. **Sync up** when the session ends: `python3 $S/ankisync.py`. Only claim reviews are synced if it exits 0; on exit 2 the reviews are safe locally but NOT on AnkiWeb yet — explain the conflict to the user.

## Self-rate mode (user's preferred flow)

Classic-Anki style, one click per card, via the AskUserQuestion tool:

1. `question` field = card index/remaining + the card FRONT only. Options = the 4 ratings (重来 Again / 困难 Hard / 良好 Good / 简单 Easy), each description showing its interval label from `ratings`. The user recalls silently and self-rates, exactly like the desktop client's buttons.
2. After the pick: grade with `--answer`, then ALWAYS display the complete back — regardless of which rating was chosen (yes, also for Good/Easy). Build it from the `fields` dict in the `--show` payload (raw note fields; the rendered `answer` omits some): word + furigana reading, meaning, pitch accent (render the HTML overline as text, e.g. キ→ライ, note 平板/起伏), example sentence with furigana + translation, frequency rank, picture filename, audio presence. Never summarize.
3. If the user says they misjudged after seeing the back, `--undo` and re-grade.
4. Present the next card in a new AskUserQuestion. Keep the text between cards minimal.

Typing mode (user types reading+meaning, agent judges per the rubric above) remains available on request.

## Sync conflicts and auth

- Exit code 2 from `ankisync.py` = both sides changed incompatibly; nothing was transferred. The user must choose: `--download` (AnkiWeb wins — **discards local unsynced reviews**) or `--upload` (local wins — discards AnkiWeb-side changes). Never choose for them.
- If the saved sync key is rejected (password changed), `ankisync.py` deletes it and says so. Have the user re-login **themselves** (`ANKI_USER=... ANKI_PASS=... python3 $S/ankisync.py`) so credentials don't enter the chat. Never ask for the password.

## Other commands

- Due counts per deck: `python3 $S/review.py --stats [--json]`
- Pin a session to one deck: add `--deck <name>` to any command (otherwise decks with due cards are picked automatically).
- Terminal (human-driven) mode the user can run directly: `python3 $S/review.py [--limit N] [--deck NAME]` — enter flips, 1-4 grades, u undoes, q quits.

## Notes

- The scheduler is Anki's real v3 scheduler — grades affect real intervals and sync back to AnkiWeb, so never grade cards the user did not actually answer.
- `--answer` accepts any currently-due card (a learning card jumping the queue mid-chat doesn't block grading); still grade right after a `--show`, one card at a time, always with `--reps`.
- Audio ([sound:...]) is stripped and media files are not downloaded; images appear as `[image: file]` placeholders.
