"""Text utilities: tokenization, n-grams, negative-keyword match semantics, Google ad character counting."""
from __future__ import annotations

import re
import unicodedata

TOKEN_RE = re.compile(r"[\w][\w'’ʼ\-]*", re.U)

STOPWORDS = {
    # en
    "a", "an", "the", "for", "in", "on", "of", "to", "and", "or", "with", "near", "me", "at", "by", "from", "is",
    # uk
    "в", "у", "на", "і", "й", "та", "з", "із", "зі", "до", "для", "по", "від", "що", "як", "це", "не",
    # ru
    "и", "с", "со", "к", "о", "об", "от", "за", "из", "как", "что", "это", "или",
}

# Generic "probably not a buyer" vocabulary (uk / ru / en). Flags are suggestions, never auto-negatives.
IRRELEVANT_DEFAULT = {
    "безкоштовно", "безплатно", "безкоштовний", "бесплатно", "бесплатный", "free", "freeware",
    "вакансія", "вакансії", "вакансия", "вакансии", "робота", "работа", "job", "jobs", "career", "careers",
    "зарплата", "salary", "hiring", "резюме",
    "своїми", "своими", "diy", "реферат", "курсова", "курсовая", "диплом", "essay",
    "торрент", "torrent", "скачати", "скачать", "download", "crack", "apk", "pdf",
    "wikipedia", "вікіпедія", "википедия", "meaning", "значення", "значение", "reddit",
}


def tokens(text: str) -> list[str]:
    return TOKEN_RE.findall(unicodedata.normalize("NFC", (text or "").lower()))


def ngrams(toks: list[str], n: int) -> list[str]:
    return [" ".join(toks[i:i + n]) for i in range(len(toks) - n + 1)]


def negative_matches(term: str, neg_text: str, match_type: str) -> bool:
    """Google negative semantics (no close variants): exact = same words in order, nothing else;
    phrase = contiguous sequence; broad = all words, any order."""
    t = tokens(term)
    n = tokens(neg_text)
    if not n:
        return False
    mt = (match_type or "BROAD").upper()
    if mt == "EXACT":
        return t == n
    if mt == "PHRASE":
        k = len(n)
        return any(t[i:i + k] == n for i in range(len(t) - k + 1))
    return set(n).issubset(t)


def ad_text_length(text: str) -> int:
    """Character count as Google Ads counts it: each character = 1 (Cyrillic included),
    full-width / wide East-Asian characters = 2. Text is NFC-normalized first so combining
    marks (e.g. й written as и + ̆ ) count once."""
    s = unicodedata.normalize("NFC", text or "")
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in s)
