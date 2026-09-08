"""Name comparison, built for the names this system actually sees.

Screening is a recall problem with a precision budget. Miss a designated person
and the institution has facilitated a sanctioned transaction; flag everyone
called Santos and the compliance unit spends its week clearing false positives
and stops reading the alerts. Everything here is aimed at that trade.

What is specific to the Philippines, and why generic string similarity is not
enough on its own:

*   **Spanish particles.** ``DELA CRUZ``, ``DE LA CRUZ``, ``DELACRUZ`` and
    ``DELA-CRUZ`` are one surname written four ways, and they are common enough
    that treating them as different tokens loses real matches. Particles are
    joined to the word they qualify, so all four normalise to ``DELACRUZ``.
*   **The middle name is the mother's maiden surname**, and it is very often
    recorded as an initial. ``JUAN D. CRUZ`` and ``JUAN DELA CRUZ`` should not
    be a mismatch on the middle token; an initial that agrees with a token's
    first letter scores as partial agreement rather than as disagreement.
*   **``Ma.`` is Maria.** So is ``Ma``. It appears in a large share of women's
    names and expanding it is the difference between a match and a near miss.
*   **Suffixes carry information.** ``JR`` and ``SR`` distinguish a father from
    a son who share every other token, so they are compared, not discarded —
    but a suffix present on one side and absent on the other is treated as
    missing data rather than as a contradiction, because source systems drop
    them constantly.

The phonetic key mirrors the one in the customer MDM (``cmdm.ingest.normalize``)
on purpose: a name that blocks together there blocks together here, so the two
systems agree about which names are worth comparing.
"""

from __future__ import annotations

import itertools
import math
import re
import unicodedata
from collections.abc import Sequence
from decimal import Decimal

__all__ = [
    "strip_accents",
    "normalize_name",
    "name_tokens",
    "phonetic_key",
    "sorted_key",
    "jaro_winkler",
    "levenshtein_ratio",
    "token_similarity",
    "name_similarity",
]

#: Honorifics and professional titles. Dropped: they are never discriminating
#: and a list entry rarely carries them.
_TITLES = frozenset(
    {
        "MR", "MRS", "MS", "MISS", "DR", "PROF", "ATTY", "ENGR", "ARCH", "HON",
        "REV", "FR", "SR.", "GEN", "COL", "MAJ", "CAPT", "LT", "SGT", "PO",
        "DATU", "HADJI", "HAJI", "SHEIKH", "SHAIKH",
    }
)

#: Generational suffixes. Kept apart from the name tokens and compared
#: separately.
_SUFFIXES = frozenset({"JR", "SR", "II", "III", "IV", "V", "VI"})

#: Particles that bind to the following token. ``DE LA CRUZ`` is one surname.
_PARTICLES = (
    "DE LOS", "DE LAS", "DE LA", "DELOS", "DELAS", "DELA", "DEL", "DE",
    "SANTO", "SANTA", "STO", "STA", "SAN", "VAN DER", "VAN", "VON", "BIN",
    "BINTI", "AL", "EL",
)

#: Abbreviations expanded before comparison.
_EXPANSIONS = {
    "MA": "MARIA",
    "MA.": "MARIA",
    "JO": "JOSE",
    "JOSE MA": "JOSE MARIA",
    "FRANCISCO JAVIER": "FRANCISCO JAVIER",
}

_PHONETIC_FOLD = (
    ("SCH", "S"), ("PH", "F"), ("GH", "F"), ("CK", "K"), ("SH", "S"),
    ("CH", "K"), ("TH", "T"), ("WR", "R"), ("KN", "N"), ("GN", "N"),
    ("PS", "S"), ("MB", "M"), ("DG", "J"), ("TZ", "S"), ("TS", "S"),
    ("CE", "SE"), ("CI", "SI"), ("CY", "SY"),
    ("C", "K"), ("Q", "K"), ("X", "KS"), ("Z", "S"), ("V", "F"), ("W", "F"),
)

#: Below this, a token pair is treated as no agreement at all rather than as
#: weak agreement.
_MIN_TOKEN_MATCH = 0.55

_NON_NAME = re.compile(r"[^A-Z0-9 ]+")
_SPACES = re.compile(r"\s+")


def strip_accents(text: str) -> str:
    """Fold accented characters to ASCII.

    ``PEÑA`` and ``PENA`` are the same surname, and a list entry transliterated
    by a wire system will have lost the tilde long before it reaches here.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_name(name: str) -> str:
    """Canonical uppercase form: no accents, no punctuation, particles joined."""
    if not name:
        return ""
    text = strip_accents(str(name)).upper()
    text = text.replace("-", " ").replace("'", "").replace(".", ". ")
    text = _NON_NAME.sub(" ", text)
    text = _SPACES.sub(" ", text).strip()
    if not text:
        return ""
    # Particles first, longest first, so "DE LOS" wins over "DE".
    for particle in _PARTICLES:
        text = re.sub(rf"\b{particle} (?=[A-Z])", particle.replace(" ", ""), text)
    words = []
    for word in text.split():
        if word in _TITLES:
            continue
        words.append(_EXPANSIONS.get(word, word))
    return " ".join(words)


def split_suffix(tokens: Sequence[str]) -> tuple[tuple[str, ...], str]:
    """Separate a generational suffix from the name tokens."""
    kept = [t for t in tokens if t not in _SUFFIXES]
    suffix = next((t for t in tokens if t in _SUFFIXES), "")
    return tuple(kept), suffix


def name_tokens(name: str) -> tuple[str, ...]:
    return tuple(t for t in normalize_name(name).split() if t)


def sorted_key(name: str) -> str:
    """Order-insensitive key. ``CRUZ JUAN`` and ``JUAN CRUZ`` agree."""
    tokens, _ = split_suffix(name_tokens(name))
    return " ".join(sorted(tokens))


def phonetic_key(name: str) -> str:
    """Sound-alike key over the consonant skeleton.

    Mirrors the MDM's blocking key: digraphs folded, non-initial vowels
    dropped, doubled letters collapsed, tokens sorted. High recall by design —
    precision is the scorer's job below.
    """
    tokens, _ = split_suffix(name_tokens(name))
    keys = []
    for token in tokens:
        folded = token
        for src, dst in _PHONETIC_FOLD:
            folded = folded.replace(src, dst)
        if len(folded) > 1:
            head, tail = folded[0], folded[1:]
            tail = tail.replace("H", "")
            tail = re.sub(r"[AEIOUY]+", "", tail)
            folded = head + tail
        folded = re.sub(r"(.)\1+", r"\1", folded)
        if folded:
            keys.append(folded)
    return " ".join(sorted(keys))


def jaro(left: str, right: str) -> float:
    """Jaro similarity."""
    if left == right:
        return 1.0
    if not left or not right:
        return 0.0
    reach = max(len(left), len(right)) // 2 - 1
    reach = max(reach, 0)
    left_flags = [False] * len(left)
    right_flags = [False] * len(right)
    matches = 0
    for i, ch in enumerate(left):
        start = max(0, i - reach)
        end = min(i + reach + 1, len(right))
        for j in range(start, end):
            if not right_flags[j] and right[j] == ch:
                left_flags[i] = right_flags[j] = True
                matches += 1
                break
    if matches == 0:
        return 0.0
    transpositions = 0
    k = 0
    for i, flag in enumerate(left_flags):
        if not flag:
            continue
        while not right_flags[k]:
            k += 1
        if left[i] != right[k]:
            transpositions += 1
        k += 1
    transpositions //= 2
    m = float(matches)
    return (m / len(left) + m / len(right) + (m - transpositions) / m) / 3.0


def jaro_winkler(left: str, right: str, prefix_weight: float = 0.1) -> float:
    """Jaro with a bonus for a shared prefix.

    Names are read and mistyped from the front, so agreement at the start is
    worth more than agreement in the middle.
    """
    score = jaro(left, right)
    if score < 0.7:
        return score
    prefix = 0
    for a, b in zip(left, right, strict=False):
        if a != b or prefix == 4:
            break
        prefix += 1
    return score + prefix * prefix_weight * (1 - score)


def levenshtein_ratio(left: str, right: str) -> float:
    """Edit-distance similarity, normalised to 0-1."""
    if left == right:
        return 1.0
    if not left or not right:
        return 0.0
    previous = list(range(len(right) + 1))
    for i, a in enumerate(left, start=1):
        current = [i]
        for j, b in enumerate(right, start=1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (a != b))
            )
        previous = current
    return 1.0 - previous[-1] / max(len(left), len(right))


def token_similarity(left: str, right: str) -> float:
    """How much two name tokens agree.

    An initial against a full token is the case worth calling out: Philippine
    records carry the mother's maiden name as an initial constantly, so
    ``D`` against ``DELACRUZ`` is partial agreement, not disagreement — but
    only partial, because ``D`` also agrees with Dizon, Domingo and Diaz.
    """
    if left == right:
        return 1.0
    if len(left) == 1 or len(right) == 1:
        return 0.82 if left[0] == right[0] else 0.0
    best = max(jaro_winkler(left, right), levenshtein_ratio(left, right))
    if best < 0.92 and phonetic_key(left) == phonetic_key(right):
        # Sound-alike spellings: HASSAN/HASAN, CATHERINE/KATHERINE.
        best = max(best, 0.92)
    return best


def _align(shorter: Sequence[str], longer: Sequence[str]) -> tuple[float, float, float]:
    """Best pairing of two token lists, and the weight left unpaired.

    Exhaustive rather than greedy, and the difference is not academic. Greedy
    assignment on ``FAISAL AHMAD RAHMAN`` against ``FAISAL A. RAHMAN`` lets
    AHMAD take RAHMAN (0.822, just ahead of the 0.82 an initial scores), which
    leaves the real RAHMAN facing an initial it does not match at all and sinks
    a true match from 0.94 to 0.58. Names are short, so the exact answer is
    cheap; the fallback to greedy exists only for the pathological case of a
    name with more than seven tokens, where the permutations stop being cheap
    and a mis-pairing among that many tokens no longer decides the outcome.
    """
    weights = [float(len(token)) ** 0.5 for token in shorter]
    total_weight = sum(weights)
    similarity = [[token_similarity(a, b) for b in longer] for a in shorter]
    count_longer = len(longer)

    def credited(pairs: Sequence[int]) -> tuple[float, set[int]]:
        score, used = 0.0, set()
        for index, other in enumerate(pairs):
            value = similarity[index][other]
            if value < _MIN_TOKEN_MATCH:
                continue
            score += value * weights[index]
            used.add(other)
        return score, used

    if math.perm(count_longer, len(shorter)) <= 5040:
        best_score, best_used = -1.0, set()
        for pairs in itertools.permutations(range(count_longer), len(shorter)):
            score, used = credited(pairs)
            if score > best_score:
                best_score, best_used = score, used
    else:  # pragma: no cover - only for absurdly long names
        available = list(range(count_longer))
        best_score, best_used = 0.0, set()
        for index in range(len(shorter)):
            if not available:
                break
            other = max(available, key=lambda j: similarity[index][j])
            if similarity[index][other] < _MIN_TOKEN_MATCH:
                continue
            available.remove(other)
            best_score += similarity[index][other] * weights[index]
            best_used.add(other)

    unmatched = sum(
        float(len(longer[j])) ** 0.5 for j in range(count_longer) if j not in best_used
    )
    return max(best_score, 0.0), unmatched, total_weight


def name_similarity(left: str, right: str) -> Decimal:
    """Overall agreement between two names, 0 to 1.

    Token alignment rather than whole-string comparison, because name order
    varies between systems and between countries: a list entry may hold
    ``CRUZ, JUAN DELA`` where the policy admin system holds ``JUAN DELA CRUZ``.

    Longer tokens carry more weight, which is what makes the surname dominate
    without having to know which token is the surname — a distinction that is
    not reliably recoverable from a single name string anyway.
    """
    left_tokens, left_suffix = split_suffix(name_tokens(left))
    right_tokens, right_suffix = split_suffix(name_tokens(right))
    if not left_tokens or not right_tokens:
        return Decimal("0")
    if left_tokens == right_tokens:
        base = 1.0
    else:
        shorter, longer = (
            (left_tokens, right_tokens)
            if len(left_tokens) <= len(right_tokens)
            else (right_tokens, left_tokens)
        )
        matched, unmatched_weight, weights = _align(shorter, longer)
        base = matched / weights if weights else 0.0
        if unmatched_weight:
            # Tokens in the longer name with nothing to match. A middle name
            # present on one side only is normal, so the penalty is mild and
            # proportional rather than disqualifying.
            base *= 1.0 - 0.25 * (unmatched_weight / (unmatched_weight + weights))

    if left_suffix and right_suffix and left_suffix != right_suffix:
        # JUAN CRUZ JR is not JUAN CRUZ SR. Recorded as a real disagreement.
        base *= 0.80
    return Decimal(str(round(base, 4)))
