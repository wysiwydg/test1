"""Local AI fallback for records the quality gate rejected.

Only gate failures reach this module. That is the economic argument for the
whole hybrid design: a model that sees 3% of records costs 3% of what a model
that sees everything would, and the 97% keep running at column speed.

Two implementations behind one protocol:

*   :class:`OnnxStandardizer` runs a locally-hosted ONNX model. This is the
    intended production path — a small sequence-labelling or seq2seq model
    quantized to int8, served by ONNX Runtime on CPU. No network call, no
    per-token billing, no data leaving the host, which matters because these are
    exactly the records containing the messiest PII.
*   :class:`HeuristicStandardizer` is a deterministic reference implementation.
    It is not a placeholder that returns fixed strings: it genuinely resolves
    the common failure shapes (initials, particled surnames, comma-inverted
    order, run-together addresses) using logic too branchy to vectorize.

Why a real second implementation rather than a stub: the pipeline, the exception
log and the rule-mining loop all need to run and be tested without a model
artifact present, and a stub would make those tests assert nothing. The heuristic
path also gives a genuine accuracy floor to measure a real model against.

**Inference is per-record and slow by design.** Nothing here is vectorized, and
it should not be. If a category of input becomes common enough for that to hurt,
that is precisely the signal the mining agent watches for, and the answer is a
deterministic rule rather than a faster model.
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from cmdm.model.ids import payload_hash

__all__ = [
    "StandardizationRequest",
    "StandardizationResult",
    "Standardizer",
    "HeuristicStandardizer",
    "OnnxStandardizer",
    "get_standardizer",
]


@dataclass(frozen=True, slots=True)
class StandardizationRequest:
    """One record-field the gate rejected."""

    field_name: str
    raw_value: str
    deterministic_value: str | None
    failed_checks: tuple[str, ...]
    #: Neighbouring values that give the model context — the postcode beside an
    #: address line, the party type beside a name.
    context: dict[str, Any] = field(default_factory=dict)

    def prompt_key(self) -> str:
        """Stable digest of the model input.

        Stored on every exception so a decision can be replayed against the
        exact input that produced it, and so identical inputs are recognizable
        as identical across runs.
        """
        return payload_hash(
            {
                "field": self.field_name,
                "raw": self.raw_value,
                "checks": sorted(self.failed_checks),
                "context": self.context,
            }
        )


@dataclass(frozen=True, slots=True)
class StandardizationResult:
    """What the fallback made of one request."""

    value: str | None
    #: Structured components, for fields that parse rather than clean.
    components: dict[str, str] = field(default_factory=dict)
    confidence: float = 0.0
    model_name: str = "none"
    model_version: str = "0"
    latency_ms: int = 0
    #: Free-text note. Retained verbatim in the exception log.
    note: str = ""

    @property
    def resolved(self) -> bool:
        return self.value is not None or bool(self.components)


class Standardizer(Protocol):
    """The narrow contract the pipeline depends on.

    Deliberately tiny. Everything the pipeline needs from a model is "given a
    failed value and some context, propose a better one, and say how sure you
    are". Keeping the surface this small is what lets the runtime be swapped
    without touching the pipeline, the exception log or the mining agent.
    """

    name: str
    version: str

    def standardize(
        self, requests: Sequence[StandardizationRequest]
    ) -> list[StandardizationResult]:
        """Process a batch. Must return one result per request, in order."""
        ...


# ---------------------------------------------------------------------------
# Heuristic reference implementation
# ---------------------------------------------------------------------------

#: Surname particles that belong with the surname rather than as middle names.
#: The vectorized pass cannot use these without a positional scan, which is
#: exactly the kind of branchy logic that belongs on the fallback path.
_PARTICLES = frozenset({
    # Dutch/Afrikaans. DER and DEN matter as much as VAN: without them
    # "VAN DER BERG" binds only BERG and the surname is silently truncated.
    "VAN", "DER", "DEN", "DE", "TER", "TEN", "OP", "AAN", "VANDER",
    # German/Nordic.
    "VON", "ZU", "AM", "IM",
    # Romance.
    "DEL", "DELLA", "DELLE", "DI", "DA", "DOS", "DAS", "LA", "LE", "LOS", "LAS",
    "DU", "DES",
    # Arabic/Hebrew/Malay patronymics.
    "BIN", "BINTI", "IBN", "AL", "EL", "BEN", "ABU",
    # Celtic and Iberian. O is load-bearing: O'BRIEN normalizes to "O BRIEN".
    "MC", "MAC", "O", "ST", "SAN", "SANTA",
})

# Single-letter particles are ambiguous with middle initials, and the ambiguity
# is not symmetric. "D" appears as a particle only in rare elided forms
# (d'Artagnan) but as a middle initial constantly, so treating it as a particle
# mis-shapes far more names than it fixes -- it is excluded above. "O" is kept
# because O'BRIEN and O'CONNOR normalize to "O BRIEN" and "O CONNOR" and are
# common in the books this system reads, where a middle initial O is not.


class HeuristicStandardizer:
    """Deterministic fallback that resolves the common failure shapes.

    Genuinely useful, not a stub. Each branch handles a pattern the vectorized
    pass cannot: recognizing that a single-character token is an initial rather
    than a name, that ``VAN DER BERG`` is one surname, that a comma inverts name
    order. Confidence is graded by how unambiguous the shape is, so the mining
    agent can prefer high-confidence corrections as rule evidence.
    """

    name = "heuristic-reference"
    version = "1"

    def standardize(
        self, requests: Sequence[StandardizationRequest]
    ) -> list[StandardizationResult]:
        out: list[StandardizationResult] = []
        for request in requests:
            started = time.perf_counter()
            result = self._one(request)
            out.append(
                StandardizationResult(
                    value=result.value,
                    components=result.components,
                    confidence=result.confidence,
                    model_name=self.name,
                    model_version=self.version,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    note=result.note,
                )
            )
        return out

    def _one(self, request: StandardizationRequest) -> StandardizationResult:
        if request.field_name in ("full_name", "full_name_normalized"):
            return self._name(request)
        if request.field_name in ("address_line1", "address_normalized"):
            return self._address(request)
        if request.field_name in ("phone_raw", "phone_e164"):
            return self._phone(request)
        return StandardizationResult(value=None, note="no handler for field")

    def _name(self, request: StandardizationRequest) -> StandardizationResult:
        """Split a full name into components."""
        value = (request.deterministic_value or request.raw_value or "").strip()
        if not value:
            return StandardizationResult(value=None, note="empty name")

        # Comma inverts order: "SMITH, JOHN MICHAEL" -> given first.
        if "," in (request.raw_value or ""):
            tail, _, head = (request.raw_value or "").partition(",")
            value = f"{head.strip()} {tail.strip()}".strip()

        tokens = [t for t in re.split(r"\s+", value.upper()) if t]
        if len(tokens) < 2:
            return StandardizationResult(
                value=value, confidence=0.2, note="single token; cannot split"
            )

        # Particles bind rightwards into the surname.
        surname_start = len(tokens) - 1
        while surname_start > 1 and tokens[surname_start - 1] in _PARTICLES:
            surname_start -= 1

        given = tokens[0]
        surname = " ".join(tokens[surname_start:])
        middle = " ".join(tokens[1:surname_start])

        # A middle that is entirely initials is the unambiguous case and the one
        # that dominates real feeds; grade it higher.
        middles = middle.split()
        all_initials = bool(middles) and all(len(m) == 1 for m in middles)
        confidence = 0.9 if (not middle or all_initials) else 0.7

        return StandardizationResult(
            value=" ".join(tokens),
            components={
                "given_name_derived": given,
                "middle_name_derived": middle,
                "surname_derived": surname,
            },
            confidence=confidence,
            note="initials middle" if all_initials else "positional split",
        )

    def _address(self, request: StandardizationRequest) -> StandardizationResult:
        """Pull a street number out of a run-together address line."""
        raw = (request.raw_value or "").strip()
        if not raw:
            return StandardizationResult(value=None, note="empty address")

        # "12A HIGHSTREET" or "FLAT2 12 HIGH ST": split letter/digit boundaries
        # that the tokenizer left fused.
        spaced = re.sub(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", raw)
        spaced = re.sub(r"\s+", " ", spaced).strip()
        numbers = re.findall(r"\d+", spaced)
        if not numbers:
            return StandardizationResult(
                value=spaced, confidence=0.3, note="no numeric token recoverable"
            )
        return StandardizationResult(
            value=spaced,
            components={"address_normalized": spaced.upper()},
            confidence=0.75,
            note="split fused alphanumeric tokens",
        )

    def _phone(self, request: StandardizationRequest) -> StandardizationResult:
        """Recover a number from a field carrying extensions or several numbers."""
        raw = request.raw_value or ""
        # Take the longest digit run; feeds routinely pack "020 7946 0958 x221"
        # or two numbers separated by a slash into one column.
        runs = re.findall(r"\d[\d ()-]{6,}", raw)
        if not runs:
            return StandardizationResult(value=None, note="no phone-like run found")
        best = max(runs, key=lambda r: len(re.sub(r"\D", "", r)))
        digits = re.sub(r"\D", "", best)
        if len(digits) < 8:
            return StandardizationResult(value=None, note="too few digits")
        cc = str(request.context.get("default_country_code", "1"))
        e164 = f"+{cc}{digits[1:]}" if digits.startswith("0") else f"+{cc}{digits}"
        return StandardizationResult(
            value=e164, confidence=0.65, note="longest digit run"
        )


# ---------------------------------------------------------------------------
# ONNX implementation
# ---------------------------------------------------------------------------


class OnnxStandardizer:
    """Locally-hosted ONNX model, served on CPU.

    The intended production path. Construction loads the model once and reuses
    the session; ONNX Runtime is thread-safe for inference, so one instance
    serves a worker's whole lifetime.

    Session options are set for this workload specifically: the batches are
    small and latency-sensitive, so intra-op parallelism is capped rather than
    left to grab every core — a fallback path that saturates the machine would
    starve the vectorized pass it is supposed to be subordinate to.

    A model whose outputs this class cannot interpret is a configuration error
    and is raised as one at construction, not discovered per record in
    production.
    """

    def __init__(
        self,
        model_path: str | Path,
        *,
        tokenizer: Any | None = None,
        name: str | None = None,
        version: str = "1",
        intra_op_threads: int = 1,
    ) -> None:
        from cmdm.onnx import load_session

        path = Path(model_path)
        self._session, _ = load_session(
            path,
            what="ONNX model",
            hint="Set CMDM_STANDARDIZER_MODEL to a model file, or leave it unset "
                 "to use the heuristic fallback.",
            intra_op_threads=intra_op_threads,
        )
        self._tokenizer = tokenizer
        self._inputs = [i.name for i in self._session.get_inputs()]
        self._outputs = [o.name for o in self._session.get_outputs()]
        self.name = name or path.stem
        self.version = version

        if not self._inputs or not self._outputs:
            raise ValueError(f"{path} exposes no inputs or outputs")

    @property
    def input_names(self) -> list[str]:
        return list(self._inputs)

    @property
    def output_names(self) -> list[str]:
        return list(self._outputs)

    def standardize(
        self, requests: Sequence[StandardizationRequest]
    ) -> list[StandardizationResult]:
        """Run the model over a batch.

        Requires a tokenizer matching the model. Without one this raises rather
        than silently degrading to the heuristic path — a deployment that
        believes it is running a model must not quietly not be.
        """
        if self._tokenizer is None:
            raise RuntimeError(
                f"{self.name} has no tokenizer configured; a text model cannot be "
                "driven without one."
            )

        started = time.perf_counter()
        encoded = self._tokenizer([r.raw_value for r in requests])
        feeds = {k: v for k, v in encoded.items() if k in self._inputs}
        raw_outputs = self._session.run(None, feeds)
        elapsed = int((time.perf_counter() - started) * 1000)
        per_record = max(elapsed // max(len(requests), 1), 0)

        return [
            self._decode(request, raw_outputs, index, per_record)
            for index, request in enumerate(requests)
        ]

    def _decode(
        self,
        request: StandardizationRequest,
        outputs: list[Any],
        index: int,
        latency_ms: int,
    ) -> StandardizationResult:
        """Turn raw model output into a result.

        Split out so a different model head — token classification versus
        sequence generation — is a change to this method alone.
        """
        decoded = self._tokenizer.decode(outputs, index) if self._tokenizer else None
        if decoded is None:
            return StandardizationResult(
                value=None, model_name=self.name, model_version=self.version,
                latency_ms=latency_ms, note="model produced no usable output",
            )
        value, components, confidence = decoded
        return StandardizationResult(
            value=value,
            components=components,
            confidence=confidence,
            model_name=self.name,
            model_version=self.version,
            latency_ms=latency_ms,
        )


def get_standardizer(model_path: str | Path | None = None) -> Standardizer:
    """Return the configured standardizer.

    Falls back to the heuristic implementation when no model is configured. The
    fallback is announced by the returned object's ``name``, which is written to
    every exception row — so "which engine standardized this record" is always
    answerable from the data rather than from deployment configuration nobody
    recorded.
    """
    import os

    path = model_path or os.environ.get("CMDM_STANDARDIZER_MODEL")
    if not path:
        return HeuristicStandardizer()
    return OnnxStandardizer(path)
