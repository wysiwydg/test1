"""Vectorized normalization kernels.

Every function here returns a Polars **expression**, not a value. Nothing in this
module iterates rows, calls ``map_elements``, or reaches into Python per record.
That is the whole point: normalization runs once over every inbound record, so a
row-wise implementation would dominate the cost of the entire pipeline. Composed
as expressions, the same logic runs as Arrow column kernels and can be pushed
into a lazy plan that Polars optimizes and parallelizes.

The constraint this imposes is real and worth stating: the ``regex`` crate that
Polars uses has no backreferences and no lookaround. Several classic string
algorithms (Soundex's adjacent-duplicate collapse, for one) are written assuming
those. Where that bites, this module uses a different formulation that reaches
the same blocking behaviour with the operations actually available, rather than
falling back to a Python loop and quietly losing the throughput.

All derived keys produced here are **recomputable**. None is a source of truth;
each is a function of the raw columns the landing zone preserves. Changing a
normalization rule means recomputing a column, never a migration.
"""

from __future__ import annotations

import polars as pl

__all__ = [
    "strip_accents",
    "normalize_text",
    "extract_name_prefix",
    "extract_name_suffix",
    "normalize_full_name",
    "name_tokens",
    "name_sorted_key",
    "name_phonetic_key",
    "name_initials",
    "detect_party_type",
    "normalize_email",
    "normalize_phone",
    "normalize_policy_number",
    "normalize_address",
    "address_key",
    "normalize_enum",
    "parse_date",
]


# ---------------------------------------------------------------------------
# Character-level normalization
# ---------------------------------------------------------------------------

#: Accented Latin characters folded to ASCII. Polars applies this as a single
#: Aho-Corasick pass, so the size of the table costs almost nothing at runtime.
#: Unicode NFKD would be more general but is not available as a Polars kernel,
#: and this covers the Latin-script range insurance name data actually contains.
_ACCENT_FOLD = {
    "À": "A", "Á": "A", "Â": "A", "Ã": "A", "Ä": "A", "Å": "A", "Ā": "A", "Ă": "A", "Ą": "A",
    "Ç": "C", "Ć": "C", "Č": "C", "Ĉ": "C",
    "Ð": "D", "Ď": "D", "Đ": "D",
    "È": "E", "É": "E", "Ê": "E", "Ë": "E", "Ē": "E", "Ė": "E", "Ę": "E", "Ě": "E",
    "Ĝ": "G", "Ğ": "G", "Ģ": "G",
    "Ĥ": "H",
    "Ì": "I", "Í": "I", "Î": "I", "Ï": "I", "Ī": "I", "Į": "I", "İ": "I",
    "Ĵ": "J",
    "Ķ": "K",
    "Ĺ": "L", "Ļ": "L", "Ľ": "L", "Ł": "L",
    "Ñ": "N", "Ń": "N", "Ņ": "N", "Ň": "N",
    "Ò": "O", "Ó": "O", "Ô": "O", "Õ": "O", "Ö": "O", "Ø": "O", "Ō": "O", "Ő": "O",
    "Ŕ": "R", "Ř": "R",
    "Ś": "S", "Ş": "S", "Š": "S", "Ŝ": "S",
    "Ţ": "T", "Ť": "T", "Ŧ": "T",
    "Ù": "U", "Ú": "U", "Û": "U", "Ü": "U", "Ū": "U", "Ů": "U", "Ű": "U", "Ų": "U",
    "Ŵ": "W",
    "Ý": "Y", "Ŷ": "Y", "Ÿ": "Y",
    "Ź": "Z", "Ż": "Z", "Ž": "Z",
    # Ligatures and the two characters that must expand rather than fold, or
    # "STRAßE" and "STRASSE" would block apart.
    "Æ": "AE", "Œ": "OE", "ß": "SS", "Þ": "TH",
}


def strip_accents(expr: pl.Expr) -> pl.Expr:
    """Fold accented Latin characters to ASCII.

    Applied after upper-casing, so only the upper-case forms need a table entry.
    """
    return expr.str.replace_many(_ACCENT_FOLD)


def normalize_text(expr: pl.Expr) -> pl.Expr:
    """Upper-case, fold accents, drop punctuation, collapse whitespace.

    The shared base for every normalized text column. Punctuation becomes a
    space rather than being deleted, so "SMITH-JONES" tokenizes into two tokens
    instead of fusing into one — which matters because the two halves of a
    double-barrelled name are frequently recorded separately by other sources.
    """
    return (
        strip_accents(expr.str.to_uppercase())
        .str.replace_all(r"[^A-Z0-9 ]+", " ")
        .str.replace_all(r"\s+", " ")
        .str.strip_chars()
    )


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

#: Honorifics stripped from the matchable form. Retained in their own column,
#: because a title is display-relevant and occasionally evidence (a "DR" that
#: appears consistently across sources is weak corroboration).
_PREFIXES = (
    "MR", "MRS", "MS", "MISS", "MSTR", "MASTER", "DR", "PROF", "SIR", "DAME",
    "LADY", "LORD", "REV", "FR", "HON", "MDM", "MADAM", "CAPT", "MAJ", "COL",
    "GEN", "LT", "SGT", "ENG", "IR",
)

#: Generational and professional suffixes. Stripped from the matchable form but
#: kept, because a JR/SR difference between two otherwise identical names is
#: evidence of two people rather than one — the resolver reads this column.
_SUFFIXES = (
    "JR", "SR", "II", "III", "IV", "V", "PHD", "MD", "ESQ", "CPA", "RN", "DDS",
    "JD", "MBA", "CFA", "RET",
)

_PREFIX_RE = r"^((?:(?:" + "|".join(_PREFIXES) + r") )+)"
_SUFFIX_RE = r"((?: (?:" + "|".join(_SUFFIXES) + r"))+)$"


def extract_name_prefix(normalized: pl.Expr) -> pl.Expr:
    """Pull leading honorifics out of an already text-normalized name."""
    return normalized.str.extract(_PREFIX_RE, 1).str.strip_chars()


def extract_name_suffix(normalized: pl.Expr) -> pl.Expr:
    """Pull trailing generational or professional suffixes out of a name."""
    return normalized.str.extract(_SUFFIX_RE, 1).str.strip_chars()


def normalize_full_name(expr: pl.Expr) -> pl.Expr:
    """Produce the matchable form of a full name.

    Text-normalized, then stripped of honorifics and suffixes. Both strips are
    applied twice because they stack in real data ("MR DR", "PHD MD") and the
    regex crate has no repetition-of-a-group-with-capture that would let one
    pass consume an arbitrary run reliably.
    """
    base = normalize_text(expr)
    for _ in range(2):
        base = (
            base.str.replace(_PREFIX_RE, "")
            .str.replace(_SUFFIX_RE, "")
            .str.strip_chars()
        )
    return base


def name_tokens(normalized: pl.Expr) -> pl.Expr:
    """Split a normalized name into its tokens.

    Empty strings are filtered out so that a stray separator cannot produce a
    phantom token that shifts the sorted key.
    """
    return normalized.str.split(" ").list.eval(
        pl.element().filter(pl.element().str.len_chars() > 0)
    )


def name_sorted_key(tokens: pl.Expr) -> pl.Expr:
    """Sort the tokens and rejoin them.

    Collides "JOHN MICHAEL SMITH" with "SMITH JOHN MICHAEL", which is the single
    most common disagreement between feeds — one system stores given-name-first,
    another surname-first, and neither says which.
    """
    return tokens.list.sort().list.join(" ")


#: Consonant groups folded to a single representative, applied before vowels are
#: dropped. Deliberately small: each entry is a substitution that genuinely
#: collides spellings of the same sound, and every extra entry costs recall
#: elsewhere by merging names that were distinct.
_PHONETIC_FOLD = {
    "PH": "F", "GH": "F", "CK": "K", "SCH": "S", "SH": "S", "CH": "K",
    "TH": "T", "WR": "R", "KN": "N", "GN": "N", "PS": "S", "MB": "M",
    "DG": "J", "TZ": "S", "TS": "S", "CE": "SE", "CI": "SI", "CY": "SY",
    "C": "K", "Q": "K", "X": "KS", "Z": "S",
    # V and W both fold to F. Mapping them to *different* letters is the subtle
    # way this key breaks: W->V leaves KOWALSKI as KVLSK while V->F sends
    # KOVALSKI to KFLSK, so the pair that most needs to collide does not.
    # Folding both to F also collapses STEVEN with STEPHEN, whose PH already
    # folds to F.
    "V": "F", "W": "F",
}

#: Doubled letters collapsed to one. Expressed as an explicit table because the
#: regex crate has no backreferences, so the natural `(.)\1+` formulation is
#: unavailable. One Aho-Corasick pass over 26 patterns is cheaper anyway.
_DOUBLE_FOLD = {c * 2: c for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"}


def name_phonetic_key(normalized: pl.Expr) -> pl.Expr:
    """Build a sound-alike blocking key from a normalized name.

    A consonant-skeleton key rather than a strict Double Metaphone: consonant
    groups that sound alike are folded together, vowels are dropped everywhere
    except the first letter of each token, and doubled letters collapse. The
    tokens are then sorted and rejoined, so this is order-insensitive like the
    sorted key.

    What it buys is recall on the transcription errors exact keys miss —
    SMITH/SMYTHE, PHILLIPS/FILIPS, CATHERINE/KATHERINE, STEVEN/STEPHEN and
    JOHN/JON all collide. What it is
    not is a faithful Metaphone implementation: those depend on positional rules
    and backtracking that cannot be expressed as vectorized substitutions.

    Blocking keys only need to be *consistent* and *high-recall* — precision is
    the scorer's job, and a blocking collision costs one extra pair to reject.
    Trading strict phonetic fidelity for a kernel that runs over the whole
    population in one pass is the right side of that trade. A Metaphone refiner
    remains available for the undecided band, where per-record cost is
    affordable because the band is small.

    ``\\B[AEIOUY]`` drops only vowels that are not at a token boundary, which is
    what preserves the leading letter that makes these keys discriminating.
    """
    folded = (
        normalized.str.replace_many(_PHONETIC_FOLD)
        # Any H still standing after the digraph fold is silent: JOHN and JON
        # must reach the same key. Token-initial H is kept, since it is audible
        # and discriminating there.
        .str.replace_all(r"\BH", "")
        .str.replace_all(r"\B[AEIOUY]+", "")
        .str.replace_many(_DOUBLE_FOLD)
        .str.replace_all(r"\s+", " ")
        .str.strip_chars()
    )
    return name_tokens(folded).list.sort().list.join(" ")


def name_initials(tokens: pl.Expr) -> pl.Expr:
    """First character of each token, sorted and concatenated.

    The cheapest high-recall key available, and the only one that still works
    when a source holds nothing but initials — which happens more than it
    should on older policy books.
    """
    return (
        tokens.list.eval(pl.element().str.slice(0, 1))
        .list.sort()
        .list.join("")
    )


#: Tokens that mark a party as a legal entity rather than a natural person.
#: Matched against the token set, not as substrings, so "INCLEDON" is not read
#: as "INC".
_ORG_TOKENS = frozenset({
    "LTD", "LIMITED", "LLC", "LLP", "LP", "INC", "INCORPORATED", "CORP",
    "CORPORATION", "CO", "COMPANY", "PLC", "PTY", "GMBH", "AG", "SA", "NV",
    "BV", "SARL", "SRL", "SPA", "OY", "AB", "AS", "KK", "PTE",
    "TRUST", "TRUSTEES", "TRUSTEE", "ESTATE", "FOUNDATION", "FUND", "SUPERFUND",
    "PENSION", "CHARITY", "ASSOCIATION", "SOCIETY", "INSTITUTE", "BANK",
    "HOLDINGS", "GROUP", "PARTNERS", "PARTNERSHIP", "VENTURES", "CAPITAL",
    "AGENCY", "AGENCIES", "BROKERS", "BROKING", "INSURANCE", "ASSURANCE",
    "FINANCIAL", "ADVISERS", "ADVISORS", "SERVICES", "SOLUTIONS",
})

#: Tokens that specifically indicate a trust or an estate, which get their own
#: party types. A trust that owns a policy behaves differently from a company
#: for both matching and servicing, so collapsing them into ORGANIZATION would
#: lose a distinction the business cares about.
_TRUST_TOKENS = frozenset({"TRUST", "TRUSTEES", "TRUSTEE", "SUPERFUND", "PENSION"})
_ESTATE_TOKENS = frozenset({"ESTATE"})


def _has_any_token(tokens: pl.Expr, vocabulary: frozenset[str]) -> pl.Expr:
    """True where the token list intersects a vocabulary.

    ``list.eval`` with ``is_in`` keeps this a single vectorized pass over the
    list column rather than a per-row set intersection in Python.
    """
    return tokens.list.eval(pl.element().is_in(list(vocabulary))).list.any()


def detect_party_type(tokens: pl.Expr) -> pl.Expr:
    """Classify a party as a natural person or a kind of legal entity.

    Trusts and estates are checked before the general organization vocabulary
    because their marker tokens also appear in it, and the more specific answer
    is the useful one.

    This is a deterministic first pass, not the final word. It handles the
    unambiguous majority; genuinely ambiguous names ("MORGAN STANLEY", which
    reads as a person and is not) are exactly the edge case the local-model
    fallback exists for, and it records its verdict as
    ``NameParseMethod.LLM_FALLBACK`` so the two paths stay distinguishable.
    """
    return (
        pl.when(_has_any_token(tokens, _TRUST_TOKENS))
        .then(pl.lit("TRUST"))
        .when(_has_any_token(tokens, _ESTATE_TOKENS))
        .then(pl.lit("ESTATE"))
        .when(_has_any_token(tokens, _ORG_TOKENS))
        .then(pl.lit("ORGANIZATION"))
        .when(tokens.list.len() == 0)
        .then(pl.lit("UNKNOWN"))
        .otherwise(pl.lit("PERSON"))
    )


# ---------------------------------------------------------------------------
# Contact details
# ---------------------------------------------------------------------------


def normalize_email(expr: pl.Expr) -> pl.Expr:
    """Lower-case and trim an email address.

    Deliberately does **not** fold Gmail-style dots or ``+tag`` suffixes. Those
    rules are provider-specific and wrong for most domains: at a corporate mail
    server ``a.smith@`` and ``asmith@`` are routinely two different people, and
    folding them would manufacture false matches in exactly the population where
    a false merge is most damaging.

    Values without an ``@`` are nulled rather than kept. A blocking key built
    from a placeholder like "NONE" or "N/A" would collide every record that has
    one into a single enormous block — the classic way a blocking pass becomes
    quadratic.
    """
    cleaned = expr.str.strip_chars().str.to_lowercase()
    return pl.when(cleaned.str.contains(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")).then(cleaned)


def normalize_phone(expr: pl.Expr, default_country_code: str = "1") -> pl.Expr:
    """Reduce a phone number to E.164 digits.

    Handles the three forms that actually turn up: an international ``+`` prefix,
    an ``00`` international prefix, and a national number with a trunk ``0`` that
    has to be replaced by the default country code.

    Anything shorter than eight digits is nulled. Short values are almost always
    extensions or partial captures, and like placeholder emails they would form a
    single huge block if allowed through as a blocking key.
    """
    digits = expr.str.replace_all(r"[^0-9+]", "")
    intl = digits.str.starts_with("+")
    zero_zero = digits.str.starts_with("00")
    bare = digits.str.replace_all(r"[^0-9]", "")

    e164 = (
        pl.when(intl)
        .then(bare)
        .when(zero_zero)
        .then(bare.str.slice(2))
        .when(bare.str.starts_with("0"))
        .then(pl.lit(default_country_code) + bare.str.slice(1))
        .otherwise(pl.lit(default_country_code) + bare)
    )
    return pl.when(bare.str.len_chars() >= 8).then("+" + e164)


# ---------------------------------------------------------------------------
# Policy number
# ---------------------------------------------------------------------------


def normalize_policy_number(expr: pl.Expr) -> pl.Expr:
    """Reduce a policy number to its canonical join form.

    Upper-cased, separators removed, and leading zeros stripped from a numeric
    tail so that ``POL-001234``, ``pol 1234`` and ``POL1234`` all resolve to the
    same contract.

    Leading zeros are stripped only from a trailing numeric run, never from the
    whole string. Some carriers issue policy numbers that are entirely numeric
    and *significant* in their leading zeros; the trailing-run restriction keeps
    the alphabetic prefix that distinguishes those books intact.
    """
    base = expr.str.to_uppercase().str.replace_all(r"[^A-Z0-9]", "")
    return base.str.replace(r"^([A-Z]*)0+([0-9])", "${1}${2}")


# ---------------------------------------------------------------------------
# Address
# ---------------------------------------------------------------------------

#: Thoroughfare types and unit designators folded to a canonical abbreviation,
#: so "STREET"/"ST" and "APARTMENT"/"APT" stop blocking apart.
_ADDRESS_FOLD = {
    " STREET": " ST", " ROAD": " RD", " AVENUE": " AVE", " AVENIDA": " AVE",
    " DRIVE": " DR", " LANE": " LN", " COURT": " CT", " PLACE": " PL",
    " BOULEVARD": " BLVD", " HIGHWAY": " HWY", " PARKWAY": " PKWY",
    " CRESCENT": " CRES", " TERRACE": " TER", " SQUARE": " SQ",
    " APARTMENT": " APT", " UNIT": " APT", " FLAT": " APT", " SUITE": " STE",
    " FLOOR": " FL", " LEVEL": " FL", " BUILDING": " BLDG", " NUMBER": " NO",
    " NORTH": " N", " SOUTH": " S", " EAST": " E", " WEST": " W",
    " NORTHEAST": " NE", " NORTHWEST": " NW",
    " SOUTHEAST": " SE", " SOUTHWEST": " SW",
    " SAINT": " ST", " MOUNT": " MT", " POST OFFICE BOX": " POB", " PO BOX": " POB",
}


def normalize_address(*parts: pl.Expr) -> pl.Expr:
    """Flatten address lines into one normalized string.

    Null parts are dropped rather than rendered as "null", and the fold table is
    applied to the joined result so that abbreviations spanning a line break are
    still caught.
    """
    joined = pl.concat_str(
        [normalize_text(p).fill_null("") for p in parts], separator=" "
    )
    return (
        # Leading space so the fold table, whose keys are space-prefixed to avoid
        # matching mid-word, can also match at the very start of the string.
        (pl.lit(" ") + joined)
        .str.replace_many(_ADDRESS_FOLD)
        .str.replace_all(r"\s+", " ")
        .str.strip_chars()
    )


def address_key(normalized_address: pl.Expr, postal_code: pl.Expr) -> pl.Expr:
    """Build the co-residence blocking key.

    The key is **every** numeric token in the address — street number, unit
    number, building number — sorted and joined to the normalized postal code.
    Two records agree on it when they plausibly share a doorstep.

    Sorting the full set of numbers rather than taking the first is what makes
    the key survive reordering. One source writes "FLAT 2, 12 HIGH ST" and
    another "12 HIGH ST APT 2"; taking the leading number yields ``2`` for one
    and ``12`` for the other and blocks the same door apart. The sorted set
    yields ``2,12`` for both.

    Numbers plus postcode rather than the whole address string, because the
    string form is fragile in the same way — and the scorer still has the full
    ``address_normalized`` column to judge the pair on once blocking has done
    its job.

    A plain string, not a digest. It costs a little more index space and is
    worth it: a steward reading a review queue can see why two records blocked
    together, and the key stays stable across library upgrades — which a hash
    seeded by a third-party library's internals would not.
    """
    numbers = normalized_address.str.extract_all(r"\d+").list.sort().list.join(",")
    postcode = postal_code.pipe(normalize_text).str.replace_all(" ", "")
    return (
        pl.when((numbers.str.len_chars() > 0) & (postcode.str.len_chars() > 0))
        .then(numbers + pl.lit("|") + postcode)
        .otherwise(None)
    )


# ---------------------------------------------------------------------------
# Vocabularies and dates
# ---------------------------------------------------------------------------


def normalize_enum(expr: pl.Expr, mapping: dict[str, str], default: str = "UNKNOWN") -> pl.Expr:
    """Map raw source values onto a controlled vocabulary.

    Unmapped values fall to ``default`` rather than raising. The raw value is
    still in the landing zone, and a feed that starts emitting an unrecognized
    status should surface as a rising count of ``UNKNOWN`` on a dashboard — not
    as a pipeline that stops at three in the morning.
    """
    lookup = {k.upper().replace(" ", "_"): v for k, v in mapping.items()}
    return normalize_text(expr).str.replace_all(" ", "_").replace_strict(
        lookup, default=default, return_dtype=pl.String
    )


#: Tried in order. Unambiguous ISO first, then the two mutually-ambiguous
#: regional orders. Which of those two comes first is a per-source decision, not
#: a global one, so the mapping layer supplies the order rather than this module
#: guessing from the values.
_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%Y%m%d", "%d %b %Y")


def parse_date(expr: pl.Expr, formats: tuple[str, ...] = _DATE_FORMATS) -> pl.Expr:
    """Parse a date column, trying each format in turn.

    ``strict=False`` nulls unparseable values instead of failing the batch, and
    the coalesce takes the first format that yields a value. A record whose date
    will not parse is a data-quality finding to be counted, not a reason to drop
    an entire day's file.
    """
    cleaned = expr.str.strip_chars()
    return pl.coalesce(
        [cleaned.str.to_date(fmt, strict=False) for fmt in formats]
    )
