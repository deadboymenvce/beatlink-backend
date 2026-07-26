"""
Deterministic extraction of social contacts from a Spotify artist biography.

Spotify's linked `externalLinks` only ever carry Instagram / Twitter, and many artists
don't link anything at all — but a lot of them write their handles straight into the bio
("instagram: @x", "x on ig", "tiktok.com/@x", …). This parser reads that free text and
returns canonical profile URLs, so we can contact an artist even with zero linked socials,
and only fall back to a name-based web search when the bio yields nothing.

No LLM / no network — pure text parsing, so it adds zero latency and zero dependency to the
scan hot path. Handles: label→handle and handle→label ("x on ig"), with or without a leading
"@", raw profile URLs, HTML entities (&#64; → @), emoji labels, and messy separators. It is
deliberately conservative: a bare "@handle" with no platform context is NOT guessed (that's
how you end up returning the wrong account), matching the product rule.

Per-platform URL conventions differ and are respected exactly:
  Instagram → https://www.instagram.com/<handle>/      (no @)
  TikTok    → https://www.tiktok.com/@<handle>          (keeps the @)
  Twitter/X → https://twitter.com/<handle>              (no @)
"""

import html
import re

# A valid handle across IG / TikTok / Twitter: letters, digits, dot, underscore.
_HANDLE = r"[A-Za-z0-9._]{2,30}"

# Label synonyms per platform. Order matters only for readability; matching is per-platform.
_LABELS = {
    "instagram": ["instagram", "instagramm", "insta", "ig", "igg", "the gram", "gram"],
    "tiktok":    ["tiktok", "tik tok", "tik-tok", "tt"],
    "twitter":   ["twitter", "tweet", "twt"],  # bare "x" handled separately, it's too ambiguous
}

# Emoji labels (a bio often uses these instead of a word).
_EMOJI_LABELS = {
    "instagram": ["\U0001F4F7", "\U0001F4F8"],           # 📷 📸
    "tiktok":    ["\U0001F3B5", "\U0001F3B6"],           # 🎵 🎶
    "twitter":   ["\U0001F426"],                          # 🐦
}

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Trailing characters that are sentence punctuation, never part of a handle (IG/TikTok/Twitter
# all forbid a trailing dot; underscore CAN end an IG handle so it is NOT stripped).
_TRAIL = ".,;:!?)]}’'\"·|/\\-–—"


def _clean_handle(raw):
    if not raw:
        return None
    h = raw.strip().lstrip("@").strip()
    h = h.rstrip(_TRAIL)
    # Guard against accidental capture of a bare label / junk.
    if len(h) < 2 or len(h) > 30:
        return None
    if not re.fullmatch(_HANDLE, h):
        return None
    return h


def _ig_url(h):
    return f"https://www.instagram.com/{h}/" if h else None

def _tiktok_url(h):
    return f"https://www.tiktok.com/@{h}" if h else None

def _twitter_url(h):
    return f"https://twitter.com/{h}" if h else None


def _find_url(text, host_re):
    m = re.search(host_re, text, re.IGNORECASE)
    return _clean_handle(m.group(1)) if m else None


# Common artist-bio prose words that can sit right before "on <platform>" and get
# mistaken for a handle ("my music on instagram" → "music"). Only consulted for a
# no-"@" "handle on ig" match, where the risk exists.
_PROSE_WORDS = {
    "music", "musics", "song", "songs", "beat", "beats", "track", "tracks",
    "sound", "sounds", "video", "videos", "content", "stuff", "everything",
    "life", "work", "works", "page", "pages", "story", "stories", "post",
    "posts", "more", "here", "out", "now", "live", "daily", "updates", "news",
    "links", "link", "booking", "business", "everyday", "everyone", "follow",
    "check", "listen", "streaming", "available", "official",
}


def _looks_like_handle(h):
    # For a no-"@" "handle on ig" match, avoid grabbing a plain prose word
    # ("check me out on ig" → "out", "my music on ig" → "music"): accept only if it
    # has a handle-ish marker (digit, dot, underscore), or is reasonably long AND
    # not a common bio word.
    if re.search(r"[0-9._]", h):
        return True
    return len(h) >= 5 and h.lower() not in _PROSE_WORDS


def _find_labelled(text, labels, emojis):
    """Match 'label: @handle' / 'IG@handle' / '📷 @handle' AND 'handle on label' / '@handle (label)'."""
    label_alt = "|".join(re.escape(l) for l in labels)
    emoji_alt = "".join(re.escape(e) for e in emojis)

    # label → handle:  "ig: @x", "instagram - x", "IG@x", "📷 @x", "ig @x"
    #   Connector is a punctuation separator OR an "@" — never a bare space, which
    #   would false-match prose ("instagram is my life"). \b anchors word labels.
    m = re.search(
        r"(?:\b(?:" + label_alt + r")\b|[" + (emoji_alt or "￿") + r"])"
        r"\s*(?:[:=\-–—>→]|@)\s*@?(" + _HANDLE + r")",
        text, re.IGNORECASE,
    )
    if m:
        h = _clean_handle(m.group(1))
        if h and h.lower() not in [l.lower() for l in labels]:
            return h

    # handle → label:  "lolkillyrslf on ig", "@x on instagram", "x (ig)"
    #   Connector "on" / "@" / "(" / "[" required (not a bare space).
    m = re.search(
        r"(@?)(" + _HANDLE + r")\s*(?:on|@|\(|\[)\s*\b(?:" + label_alt + r")\b",
        text, re.IGNORECASE,
    )
    if m:
        had_at = m.group(1) == "@"
        h = _clean_handle(m.group(2))
        if h and h.lower() not in [l.lower() for l in labels] and (had_at or _looks_like_handle(h)):
            return h

    return None


def extract_contacts(bio_text):
    """
    Returns {'instagram': url|None, 'tiktok': url|None, 'twitter': url|None, 'email': str|None}.
    Every value is None when the bio is empty or holds nothing recognizable.
    """
    out = {"instagram": None, "tiktok": None, "twitter": None, "email": None}
    if not bio_text or not isinstance(bio_text, str):
        return out

    text = html.unescape(bio_text)  # &#64; -> @, &amp; -> & etc.

    # ── Instagram ──
    ig = _find_url(text, r"(?:https?://)?(?:www\.)?instagram\.com/(" + _HANDLE + r")")
    if not ig:
        ig = _find_labelled(text, _LABELS["instagram"], _EMOJI_LABELS["instagram"])
    out["instagram"] = _ig_url(ig)

    # ── TikTok (url may already contain the @) ──
    tk = _find_url(text, r"(?:https?://)?(?:www\.)?tiktok\.com/@?(" + _HANDLE + r")")
    if not tk:
        tk = _find_labelled(text, _LABELS["tiktok"], _EMOJI_LABELS["tiktok"])
    out["tiktok"] = _tiktok_url(tk)

    # ── Twitter / X ──
    tw = _find_url(text, r"(?:https?://)?(?:www\.)?(?:twitter|x)\.com/(" + _HANDLE + r")")
    if not tw:
        tw = _find_labelled(text, _LABELS["twitter"], _EMOJI_LABELS["twitter"])
    if not tw:
        # "x: @handle" / "x - @handle" only — never a bare "x", too many false hits.
        m = re.search(r"\bx\b\s*(?:[:=\-–—>→])\s*@?(" + _HANDLE + r")", text, re.IGNORECASE)
        if m:
            tw = _clean_handle(m.group(1))
    out["twitter"] = _twitter_url(tw)

    # ── Email ──
    m = _EMAIL_RE.search(text)
    if m:
        out["email"] = m.group(0).rstrip(_TRAIL)

    return out


def has_any_contact(contacts):
    return any(contacts.get(k) for k in ("instagram", "tiktok", "twitter", "email"))
