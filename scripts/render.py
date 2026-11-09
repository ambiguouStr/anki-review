"""Turn Anki's card HTML into plain terminal text.

Everything shown on a card back comes from render_back(), so the output is
complete by construction instead of depending on whoever displays it.
"""
import html
import re
from html.parser import HTMLParser
from typing import Callable, NamedTuple
from urllib.parse import unquote


class Media(NamedTuple):
    """Which media files are worth mentioning; the rest leave no trace."""
    image: Callable[[str], bool] = lambda name: False
    audio: Callable[[str], bool] = lambda name: False


RUBY_RUN = re.compile(r"(?:<ruby>(?:<rb>)?[^<]*(?:</rb>)?<rt>[^<]*</rt></ruby>)+")
RUBY_PART = re.compile(r"<ruby>(?:<rb>)?([^<]*)(?:</rb>)?<rt>([^<]*)</rt></ruby>")


def _ruby(html_text: str, mode: str) -> str:
    """Adjacent ruby merges into one word: 面(おも)白(しろ) -> 面白(おもしろ)."""
    def merge(m):
        parts = RUBY_PART.findall(m.group(0))
        base = "".join(b for b, _ in parts)
        reading = "".join(r for _, r in parts)
        return {"both": f"{base}({reading})", "base": base, "reading": reading}[mode]

    return RUBY_RUN.sub(merge, re.sub(r"</?rp>", "", html_text))


def clean(html_text: str, ruby: str = "both", media: Media = Media()) -> str:
    """Plain text from card HTML. ruby: "both" | "base" | "reading"."""
    def sound(m):
        return f"[audio: {m.group(1)}]" if media.audio(m.group(1)) else ""

    def image(m):
        name = unquote(m.group(1))
        return f"[image: {name}]" if media.image(name) else ""

    t = html_text
    t = re.sub(r"\[sound:([^\]]*)\]", sound, t)
    t = re.sub(r"\[anki:play:[^\]]*\]", "", t)
    t = re.sub(r"<!--.*?-->", "", t, flags=re.S)
    t = re.sub(r"<(style|script).*?</\1>", "", t, flags=re.S)
    t = _ruby(t, ruby)
    t = re.sub(r"""<img[^>]*src\s*=\s*["']?([^"'\s>]+)["']?[^>]*>""", image, t)
    t = re.sub(r"<br\s*/?>", "\n", t)
    t = re.sub(r"</?(div|p|li|tr)[^>]*>", "\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = t.replace("<!--", "").replace("-->", "")
    t = html.unescape(t)
    t = re.sub(r"[ \t]*\n[ \t]*", "\n", t)
    t = re.sub(r"\n{2,}", "\n", t)
    return t.strip()


class _Pitch(HTMLParser):
    """Reads the overline-span markup used by pitch-accent fields: an inline-block
    span wraps the high morae, and a right border on its marker means a drop."""

    def __init__(self):
        super().__init__()
        self.out = []
        self.spans = []
        self.dropping = False

    def handle_starttag(self, tag, attrs):
        if tag != "span":
            return
        style = dict(attrs).get("style") or ""
        high = "inline-block" in style
        if high:
            self.out.append("↗")
        if "border-top-style" in style and "border-right-style" in style:
            self.dropping = True
        self.spans.append(high)

    def handle_endtag(self, tag):
        if tag == "span" and self.spans and self.spans.pop() and self.dropping:
            self.out.append("↘")
            self.dropping = False

    def handle_data(self, data):
        self.out.append(data)


def pitch_text(html_text: str) -> str:
    """ワ<overline>タシ</overline> -> ワ↗タシ (平板); a drop gives ア↗ナ↘タ (起伏).
    Alternatives separated by ・ are labelled one by one."""
    p = _Pitch()
    p.feed(html_text)
    readings = []
    for r in re.split(r"\s*・\s*", "".join(p.out).strip()):
        r = r.strip("↗").replace("↘↗", "↘")  # high from the start isn't a rise
        readings.append(f"{r} ({'起伏' if '↘' in r else '平板'})")
    return "・".join(readings)


CLOZE = re.compile(r"\{\{c\d+::(.*?)(?:::[^}]*)?\}\}", re.S)


def _squash(text: str) -> str:
    """Comparable form: no furigana brackets, whitespace or line breaks, and
    cloze markup reduced to the text it hides — otherwise a cloze note's raw
    Text field never matches the rendered answer and gets repeated below it."""
    text = CLOZE.sub(r"\1", text)
    return re.sub(r"\s+", "", re.sub(r"\[[^\]]*\]", "", text))


def render_back(answer_html: str, fields: dict, media: Media = Media()) -> str:
    """The rendered answer, plus every note field the card template left out
    (templates commonly hide pitch accent, frequency and so on)."""
    back = clean(answer_html, media=media)
    shown = (_squash(clean(answer_html, "base", media)) + "\n"
             + _squash(clean(answer_html, "reading", media)))
    extras = []
    for name, value in fields.items():
        text = pitch_text(value) if "border-top-style" in value else clean(value, media=media)
        squashed = _squash(text)  # empty for a lone [image: x] or [audio: x] marker
        if text and text not in back and (not squashed or squashed not in shown):
            extras.append(f"{name}: {text}")
    return "\n".join([back, *extras]) if extras else back
