"""BM25 over Madar's policy chunks, with Arabic and English normalisation (configuration B's retrieval).

The kit's `madar.keyword_search` (configuration A) counts shared 5-letter word prefixes. It has no IDF (common words
like "permit" outweigh rare, telling ones like "illuminated") and no Arabic normalisation (أ/إ/آ/ا, ة/ه, ى/ي, the
article ال, diacritics), so Arabic questions miss the Arabic summaries. BM25 fixes both. Standard library only.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from functools import lru_cache

import madar

K1, B = 1.5, 0.75
DIACRITICS = re.compile(r"[ً-ْـ]")  # harakat and tatweel
ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
STOP = {"the", "and", "for", "are", "can", "what", "how", "much", "does", "with", "from", "this", "that", "will", "you",
        "your", "have", "has", "who", "when", "which", "there", "need", "about", "into", "per",
        "في", "من", "على", "الى", "إلى", "عن", "هل", "ما", "ماذا", "كم", "هو", "هي", "او", "أو", "مع", "لا", "ان", "أن"}


def normalise_ar(word: str) -> str:
    w = DIACRITICS.sub("", word)
    w = re.sub("[أإآ]", "ا", w).replace("ة", "ه").replace("ى", "ي")
    for prefix in ("وال", "بال", "كال", "فال", "لل", "ال"):
        if w.startswith(prefix) and len(w) - len(prefix) >= 3:
            return w[len(prefix):]
    return w


def stem_en(word: str) -> str:
    for suffix in ("ations", "ation", "ings", "ing", "ies", "es", "ed", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


def tokens(text: str) -> list[str]:
    out = []
    for w in re.findall(r"\w+", text.translate(ARABIC_DIGITS).lower()):
        if w in STOP or (len(w) < 3 and not w.isdigit()):
            continue
        out.append(normalise_ar(w) if re.search(r"[؀-ۿ]", w) else stem_en(w))
    return out


@lru_cache(maxsize=1)
def _index():
    chunks = madar.chunk_policies()
    docs = [Counter(tokens(f"{c['title']} {c['text']}")) for c in chunks]
    lengths = [sum(d.values()) for d in docs]
    df = Counter(t for d in docs for t in d)
    n = len(docs)
    idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
    return chunks, docs, lengths, sum(lengths) / n, idf


def bm25_search(query: str, k: int = 3) -> list[dict]:
    """Top-k policy chunks for a query (English or Arabic), best first."""
    chunks, docs, lengths, avg, idf = _index()
    q = set(tokens(query))
    scored = []
    for c, d, n in zip(chunks, docs, lengths):
        s = sum(idf[t] * d[t] * (K1 + 1) / (d[t] + K1 * (1 - B + B * n / avg)) for t in q if t in d)
        if s > 0:
            scored.append((s, c))
    scored.sort(key=lambda t: -t[0])
    return [c for _, c in scored[:k]]
