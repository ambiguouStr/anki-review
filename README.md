# anki-review

Review your due [Anki](https://apps.ankiweb.net/) flashcards in a chat with an AI coding agent — or straight from the terminal — driven by **Anki's real v3 scheduler**. Grades affect real intervals and sync back to AnkiWeb, exactly like the desktop and mobile apps.

It's packaged as an [agent skill](https://docs.claude.com/en/docs/claude-code/skills): drop it where Claude Code (or a compatible agent like Codex) discovers skills, and say *"quiz me"*. It also works as two plain CLI scripts with no agent at all.

## Why

Your phone/desktop Anki client is still the place for serious reviewing. This is for quick, low-friction sessions while you're already at a keyboard — an agent shows you a card, you answer in chat, it grades and explains. Because it speaks the real scheduler and syncs to AnkiWeb, those reps count.

## Requirements

- Python 3.9+
- The official Anki backend library: `pip install anki` (a ~90 MB Rust-backed wheel; this is the headless `anki` package, **not** the `aqt` GUI)
- An [AnkiWeb](https://ankiweb.net/) account with at least one synced collection using the **v3 scheduler**

## Install

```bash
git clone https://github.com/ambiguouStr/anki-review.git
pip install anki
```

Then make the skill discoverable by your agent, e.g. for Claude Code:

```bash
ln -s "$PWD/anki-review" ~/.claude/skills/anki-review
# Codex (optional): ln -s "$PWD/anki-review" ~/.codex/skills/anki-review
```

### First login

Credentials are used **once** to obtain a sync token, which is stored (mode `0600`) in the data directory. Your password is never written to disk.

```bash
ANKI_USER='you@example.com' ANKI_PASS='...' python3 scripts/ankisync.py
```

Data lives in `$ANKI_CLI_DIR` (default `~/.local/share/anki-cli/`). On first run with an empty local collection, AnkiWeb performs a full download automatically.

> **Security:** keep your real data directory *outside* any git checkout. The bundled `.gitignore` also excludes `*.anki2`, `.ankiauth.json`, `.undo.json`, and media, so an accidental copy into the repo won't be committed.

## Usage

### With an agent

Say **"quiz me"** (or "anki", "review my cards", "flashcards"). The agent syncs down, shows one card at a time, judges your answers, grades via the scheduler, and syncs up when you stop. See [`SKILL.md`](./SKILL.md) for the exact workflow the agent follows.

### From the terminal

```bash
python3 scripts/review.py                 # interactive review (enter flips, 1-4 grades, u undoes, q quits)
python3 scripts/review.py --limit 20      # stop after 20 cards
python3 scripts/review.py --deck "My Deck" # pin to one deck
python3 scripts/review.py --stats          # due counts per deck
python3 scripts/ankisync.py                # sync with AnkiWeb
```

Machine-readable interface (used by the agent):

```bash
python3 scripts/review.py --show --json
python3 scripts/review.py --answer <card_id> <1-4> --reps <n> --json
python3 scripts/review.py --undo --json
```

`--reps` (taken from the preceding `--show`) makes grading idempotent — a retried command is refused instead of double-grading. `--undo` reverts the last grade even across invocations. On a divergent sync, `ankisync.py` exits `2` and asks you to choose `--download` or `--upload`.

## Tests

```bash
python3 tests/e2e.py   # runs against a throwaway copy of your collection; never touches AnkiWeb or your real data
```

## License

[AGPL-3.0](./LICENSE), for compatibility with the Anki library it builds on.
