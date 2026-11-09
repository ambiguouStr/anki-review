# anki-review

Review your due [Anki](https://apps.ankiweb.net/) flashcards from a terminal, one keypress per step, on **Anki's real v3 scheduler**. Grades affect real intervals and sync back to AnkiWeb, exactly like the desktop and mobile apps.

It also ships as an [agent skill](https://docs.claude.com/en/docs/claude-code/skills): the agent doesn't quiz you, but it can explain the cards you just failed.

## Why

Your phone or desktop Anki client is still the place for serious reviewing. This is for a few cards in a tmux pane while something builds: no window to switch to, nothing left on screen when you quit, and those reps still count.

## Requirements

- Linux or macOS, Python 3.9+
- The official Anki backend library: `pip install anki` (a ~90 MB Rust-backed wheel; this is the headless `anki` package, **not** the `aqt` GUI)
- An [AnkiWeb](https://ankiweb.net/) account with at least one synced collection using the **v3 scheduler**

## Install

```bash
git clone https://github.com/ambiguouStr/anki-review.git
cd anki-review
pip install anki
ln -s "$PWD/scripts/review.py" ~/.local/bin/ank
```

To make the skill discoverable by your agent, e.g. for Claude Code:

```bash
ln -s "$PWD" ~/.claude/skills/anki-review
# Codex (optional): ln -s "$PWD" ~/.codex/skills/anki-review
```

### First login

Credentials are used **once** to obtain a sync token, which is stored (mode `0600`) in the data directory. Your password is never written to disk.

```bash
ANKI_USER='you@example.com' ANKI_PASS='...' python3 scripts/ankisync.py
```

Data lives in `$ANKI_CLI_DIR` (default `~/.local/share/anki-cli/`). On first run with an empty local collection, AnkiWeb performs a full download automatically.

Media is a separate, much larger transfer and stays off unless you ask for it:

```bash
python3 scripts/ankisync.py --media   # audio and images; Ctrl-C is safe, progress is kept
```

> **Security:** keep your real data directory *outside* any git checkout. The bundled `.gitignore` also excludes `*.anki2`, `.ankiauth.json`, and media, so an accidental copy into the repo won't be committed.

## Usage

```bash
ank                    # 10 cards, then offer another batch
ank 5                  # batch of 5 (0 = no limit)
ank --deck "My Deck"   # pin to one deck
ank --no-sync          # skip the automatic sync
ank --no-audio         # stay silent on the card back
ank --stats            # due counts per deck
ank --recent-fails     # cards you pressed Again on in the past two days
```

| Key | Action |
| --- | --- |
| `space` / `enter` | flip, then grade Good |
| `1` `2` `3` `4` | Again / Hard / Good / Easy |
| `r` / `s` | replay the word / play the sentence, when the back has audio |
| `u` | undo the previous grade, at any point in the session |
| `j` / `k` | scroll a card taller than the window |
| `q` | quit |

The back always shows everything on the note, including fields the card template hides, such as pitch accent (`ア↗ナ↘タ (起伏)`) and frequency.

### Sound

Flipping a card plays its audio, and `r` replays it. One sound plays at a time, in a child process, so a long recording never ties up the keyboard.

What you hear depends on what's on disk. With the deck's recordings downloaded (`ankisync.py --media`), `ank` plays them through `mpv`, `ffplay` or `afplay` — whichever it finds first — and `s` plays the example sentence. Without them it falls back to macOS `say` with a Japanese voice, reading the note's *reading* field: a card's kana is the only thing safe to synthesise, since handing kanji to a speech engine makes it guess the very reading the card is testing (開く is あく on one card and ひらく on another).

Nothing plays over SSH, where the sound would come out of the wrong machine, and `--no-audio` turns it off entirely. Images still aren't rendered: an `[image: file]` or `[audio: file]` marker appears only when the file is in `collection.media` and the terminal could use it — an image protocol outside tmux, a player outside SSH. Otherwise media leaves no trace.

`ank` syncs when it starts and again on exit if you graded anything. Any key skips a slow sync, and grades are always saved locally first, so working offline is fine. On a divergent sync it tells you, and you choose by running `scripts/ankisync.py --download` (AnkiWeb wins) or `--upload` (local wins).

### With an agent

Ask *"explain the cards I just failed"*. The agent reads `--recent-fails --json`. See [`SKILL.md`](./SKILL.md).

## Tests

```bash
python3 tests/e2e.py   # runs on a throwaway copy of your collection; never touches AnkiWeb or your real data
```

## License

[AGPL-3.0](./LICENSE), for compatibility with the Anki library it builds on.
