"""Running the rules, and being able to say afterwards exactly what ran.

A monitoring run is evidence. When an examiner asks why a transaction in March
was not reported, the answer has to be better than "the system didn't flag it":
it has to name the rules that ran, their versions, the parameters they ran with
and the population they ran over. :class:`MonitoringRun` carries all of that,
and :attr:`MonitoringRun.fingerprint` reduces the configuration to a digest so
two runs can be compared at a glance — a fingerprint change is a change in what
the system was looking for.

One deliberate operational choice: a rule that raises does not stop the run.
Rules are independent tests, and a defect in the beneficiary-churn rule must not
suppress the covered-transaction detection that has a statutory filing deadline
attached to it. The failure is recorded per rule and surfaces as a run error,
which is loud enough to fix and safe enough to survive.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from aml.config import AmlConfig
from aml.model.entities import Alert, Book, jsonable
from aml.model.enums import AlertState
from aml.phcalendar import now_manila
from aml.rules.base import Finding, RuleContext
from aml.rules.registry import active_rules

__all__ = ["RuleOutcome", "MonitoringRun", "run_monitoring", "alert_from_finding"]

log = logging.getLogger("aml.rules.engine")


@dataclass(frozen=True, slots=True)
class RuleOutcome:
    """What one rule did in one run."""

    rule_id: str
    version: str
    findings: int
    milliseconds: int
    parameters: Mapping[str, Any] = field(default_factory=dict)
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "version": self.version,
            "findings": self.findings,
            "milliseconds": self.milliseconds,
            "parameters": jsonable(self.parameters),
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class MonitoringRun:
    """One pass of the rule set over a book of business."""

    run_id: str
    started_at: dt.datetime
    as_of: dt.date
    alerts: tuple[Alert, ...]
    outcomes: tuple[RuleOutcome, ...]
    transactions_examined: int
    parties_examined: int
    fingerprint: str

    @property
    def covered_transaction_alerts(self) -> tuple[Alert, ...]:
        return tuple(a for a in self.alerts if a.is_covered_transaction)

    @property
    def suspicion_alerts(self) -> tuple[Alert, ...]:
        return tuple(a for a in self.alerts if not a.is_covered_transaction)

    @property
    def errors(self) -> tuple[RuleOutcome, ...]:
        return tuple(o for o in self.outcomes if o.error)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at.isoformat(),
            "as_of": self.as_of.isoformat(),
            "transactions_examined": self.transactions_examined,
            "parties_examined": self.parties_examined,
            "fingerprint": self.fingerprint,
            "alert_count": len(self.alerts),
            "covered_transactions": len(self.covered_transaction_alerts),
            "suspicions": len(self.suspicion_alerts),
            "outcomes": [o.as_dict() for o in self.outcomes],
        }


def alert_from_finding(
    finding: Finding, rule_id: str, rule_version: str, created_at: dt.datetime
) -> Alert:
    """Turn a rule's finding into a stored alert with a stable identity.

    The identifier is derived from the finding's own content, so the same
    finding produced by a re-run is the same alert. See
    :attr:`aml.model.entities.Alert.dedup_key`.
    """
    draft = Alert(
        alert_id="",
        rule_id=rule_id,
        rule_version=rule_version,
        subject_party_id=finding.subject_party_id,
        created_at=created_at,
        severity=finding.severity,
        score=finding.score,
        title=finding.title,
        narrative=finding.narrative,
        transaction_ids=tuple(finding.transaction_ids),
        policy_ids=tuple(finding.policy_ids),
        window_start=finding.window_start,
        window_end=finding.window_end,
        st_codes=tuple(finding.st_codes),
        unlawful_activity_codes=tuple(finding.unlawful_activity_codes),
        amount_php=finding.amount_php,
        evidence=dict(finding.evidence),
        state=AlertState.OPEN,
        is_covered_transaction=finding.is_covered_transaction,
    )
    return replace(draft, alert_id=f"ALR-{draft.dedup_key[:16].upper()}")


def _fingerprint(config: AmlConfig, rules: Sequence[Any]) -> str:
    """Digest of what this run was looking for.

    Thresholds, the covered-instrument interpretation, and every rule's
    identifier, version and effective parameters. Excludes the institution
    profile and anything operational: a change of compliance officer does not
    change what the system detects, and a fingerprint that moves for
    unrelated reasons is one nobody trusts.
    """
    payload = {
        "threshold": str(config.thresholds.covered_transaction.amount),
        "aggregate_same_day": config.thresholds.aggregate_same_day,
        "instruments": sorted(str(i) for i in config.thresholds.covered_instrument_set()),
        "structuring_band_floor": str(config.thresholds.structuring_band_floor),
        "high_risk": sorted(config.rules.high_risk_jurisdictions),
        "monitored": sorted(config.rules.monitored_jurisdictions),
        "rules": [
            {
                "rule_id": r.rule_id,
                "version": r.version,
                "params": jsonable(
                    {**dict(r.default_params), **config.rules.params_for(r.rule_id)}
                ),
            }
            for r in rules
        ],
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def run_monitoring(
    book: Book,
    config: AmlConfig,
    *,
    as_of: dt.date | None = None,
    now: dt.datetime | None = None,
) -> MonitoringRun:
    """Evaluate every enabled rule over the book.

    ``as_of`` is the date the run reasons about — the end of the period being
    monitored, not necessarily today. Passing it explicitly is what makes a
    backtest over last quarter produce the alerts that *would* have been raised
    then, rather than alerts contaminated by knowledge of what happened since.
    """
    started = now or now_manila()
    reference = as_of or (max((t.transaction_date for t in book), default=started.date()))
    rules = active_rules(config)
    alerts: list[Alert] = []
    outcomes: list[RuleOutcome] = []
    seen: set[str] = set()

    for rule in rules:
        params = {**dict(rule.default_params), **dict(config.rules.params_for(rule.rule_id))}
        ctx = RuleContext(config=config, book=book, as_of=reference, params=params)
        clock = time.perf_counter()
        produced = 0
        error = ""
        try:
            for finding in rule.evaluate(ctx):
                alert = alert_from_finding(finding, rule.rule_id, rule.version, started)
                if alert.dedup_key in seen:
                    # The same finding reached twice within one run — for
                    # instance two overlapping windows that resolve to the same
                    # transactions. One alert, not two.
                    continue
                seen.add(alert.dedup_key)
                alerts.append(alert)
                produced += 1
        except Exception as exc:  # noqa: BLE001 - a broken rule must not stop the run
            error = f"{type(exc).__name__}: {exc}"
            log.exception("rule %s failed", rule.rule_id)
        outcomes.append(
            RuleOutcome(
                rule_id=rule.rule_id,
                version=rule.version,
                findings=produced,
                milliseconds=int((time.perf_counter() - clock) * 1000),
                parameters=params,
                error=error,
            )
        )

    alerts.sort(key=lambda a: (-float(a.score), a.rule_id, a.subject_party_id, a.alert_id))
    return MonitoringRun(
        run_id=f"RUN-{started.strftime('%Y%m%dT%H%M%S')}-{_fingerprint(config, rules)[:8]}",
        started_at=started,
        as_of=reference,
        alerts=tuple(alerts),
        outcomes=tuple(outcomes),
        transactions_examined=len(book),
        parties_examined=len(book.party_ids()),
        fingerprint=_fingerprint(config, rules),
    )
