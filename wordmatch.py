"""
Word ↔ UK plate conversion utilities.

Substitution cipher used by the personalised plate market:
  0=O  1=I/L  2=Z/R  3=E  4=A  5=S  6=G  7=T/L  8=B  9=G/P
"""

import re
from itertools import product

# When encoding a word → possible plate chars for each letter
_LETTER_VARIANTS: dict[str, list[str]] = {
    'A': ['A', '4'],
    'B': ['B', '8'],
    'E': ['E', '3'],
    'G': ['G', '6', '9'],
    'I': ['I', '1'],
    'L': ['L', '1', '7'],
    'O': ['O', '0'],
    'S': ['S', '5'],
    'T': ['T', '7'],
    'Z': ['Z', '2'],
    'R': ['R', '2'],
}

# When decoding a plate char → possible letters it could represent
_NUM_TO_LETTERS: dict[str, list[str]] = {
    '0': ['O'],
    '1': ['I', 'L'],
    '2': ['Z', 'R'],
    '3': ['E'],
    '4': ['A'],
    '5': ['S'],
    '6': ['G'],
    '7': ['T', 'L'],
    '8': ['B'],
    '9': ['G', 'P'],
}


def word_to_plate_patterns(word: str) -> list[str]:
    """Return all plate strings that could represent *word*.

    For 'BOSS' → {'BOSS', 'B0SS', 'BO5S', 'B05S', 'BOS5', 'B055', …}
    Limited to 512 results so we don't explode on long words.
    """
    word = word.upper().strip()
    word = re.sub(r'[^A-Z0-9]', '', word)
    if not word:
        return []

    per_char: list[list[str]] = []
    for ch in word:
        per_char.append(_LETTER_VARIANTS.get(ch, [ch]))

    combos: list[str] = []
    for parts in product(*per_char):
        combos.append(''.join(parts))
        if len(combos) >= 512:
            break

    return list(dict.fromkeys(combos))  # deduplicate, preserve order


def plate_to_words(plate: str) -> list[str]:
    """Decode all possible words a plate could represent.

    Ignores spaces and works on the condensed alphanumeric string.
    """
    chars = re.sub(r'[^A-Z0-9]', '', plate.upper())
    if not chars:
        return []

    per_char: list[list[str]] = []
    for ch in chars:
        if ch.isdigit():
            per_char.append(_NUM_TO_LETTERS.get(ch, []))
        else:
            per_char.append([ch])

    words: list[str] = []
    for parts in product(*per_char):
        if parts:
            words.append(''.join(parts))

    return list(dict.fromkeys(words))


# ---------------------------------------------------------------------------
# Dictionary helpers
# ---------------------------------------------------------------------------

_DICTIONARY: set[str] | None = None
_DICTIONARY_PATHS = [
    # Bundled word list shipped with the app (works on any platform)
    __import__('os').path.join(__import__('os').path.dirname(__file__), 'words.txt'),
    '/usr/share/dict/words',
    '/usr/share/dict/linux.words',
    '/usr/share/dict/british-english',
    '/usr/share/dict/american-english',
]


def load_dictionary() -> set[str]:
    global _DICTIONARY
    if _DICTIONARY is not None:
        return _DICTIONARY

    words: set[str] = set()
    for path in _DICTIONARY_PATHS:
        try:
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    w = line.strip().upper()
                    # Keep words 2-8 chars, letters only (no possessives etc.)
                    if 2 <= len(w) <= 8 and w.isalpha():
                        words.add(w)
            if words:
                break
        except OSError:
            continue

    _DICTIONARY = words
    return _DICTIONARY


def is_english_word(candidate: str) -> bool:
    d = load_dictionary()
    return candidate.upper() in d


def plate_is_word(plate: str) -> bool:
    """Return True if any decoding of the plate is an English word."""
    for candidate in plate_to_words(plate):
        if is_english_word(candidate):
            return True
    return False


# ---------------------------------------------------------------------------
# Match scoring
# ---------------------------------------------------------------------------

def _decode_window(chars: str) -> list[str]:
    """Decode a fixed-length character string to all possible letter sequences.

    Returns empty list if any digit has no mapping (e.g. unmapped digit).
    """
    per_char: list[list[str]] = []
    for c in chars:
        if c.isdigit():
            letters = _NUM_TO_LETTERS.get(c, [])
            if not letters:
                return []
            per_char.append(letters)
        else:
            per_char.append([c])
    return [''.join(p) for p in product(*per_char)]


def plate_match_score(plate: str, word: str) -> float:
    """Return a relevance score 0–100 for how closely *plate* spells *word*.

    Scoring logic:
    - Slide a window of len(word) chars across the condensed plate.
    - Decode each window using number-to-letter substitutions.
    - If a decoding equals the word exactly, compute:
        coverage  = len(word) / len(plate_clean)   (1.0 if plate IS the word)
        fidelity  = real_letters / len(word)       (1.0 if no substitutions used)
        score     = coverage * (60 + 40*fidelity)  → max 100
    - Returns the best score across all windows.
    - Plates with no window spelling the word score 0.
    """
    plate_clean = re.sub(r'\s', '', plate.upper())
    word = re.sub(r'[^A-Z0-9]', '', word.upper())
    wlen = len(word)

    if not plate_clean or not word or wlen > len(plate_clean):
        return 0.0

    best = 0.0
    plen = len(plate_clean)
    word_has_digits = any(c.isdigit() for c in word)

    for start in range(plen - wlen + 1):
        window = plate_clean[start:start + wlen]

        matched = False

        if word_has_digits:
            # Word itself contains digits (e.g. "W1LLY") — compare literally.
            # A window that IS the word character-for-character is a match.
            if window == word:
                matched = True
        else:
            # All-letter word (e.g. "WILLY") — decode plate digits to letters
            # and check whether any decoding equals the word.
            for decoded in _decode_window(window):
                if decoded == word:
                    matched = True
                    break

        if matched:
            coverage = wlen / plen
            # When the user explicitly typed digits, treat every character as
            # intentional (fidelity 1.0). Otherwise score by letter/digit ratio.
            fidelity = 1.0 if word_has_digits else sum(1 for c in window if c.isalpha()) / wlen
            score = coverage * (60.0 + 40.0 * fidelity)
            if score > best:
                best = score

    return round(best, 2)
