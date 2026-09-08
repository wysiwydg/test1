"""The command line: one binary a compliance unit can actually operate.

The commands follow the working day of an AML unit rather than the structure of
the code:

    aml monitor            # run the rules over last night's extract
    aml screen             # screen the book against the lists
    aml alerts             # what came out, worst first
    aml case open|determine|approve|close
    aml due                # what is running out of time
    aml report ctr|str     # build the files
    aml submit             # package and send
    aml verify             # prove the record has not been altered

Two conventions throughout. Every command takes ``--json`` so it can be driven
from a scheduler and its output kept as evidence. And every command that
changes anything takes ``--actor``, because an unattributed action in a
compliance system is a finding waiting to happen — there is no default of
"system" for a human decision.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import pathlib
import sys
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from aml.case.store import AmlStore, WorkflowError
from aml.case.workflow import (
    approve,
    close_case,
    compose_narrative,
    determine,
    due_soon,
)
from aml.config import EXAMPLE_TOML, AmlConfig, unknown_keys
from aml.demo.generate import PLANTED, generate
from aml.ingest.loader import load_book
from aml.model.codes import SUSPICIOUS_CIRCUMSTANCES, UNLAWFUL_ACTIVITIES
from aml.model.entities import Book
from aml.model.enums import CaseState, ReportKind, Severity
from aml.phcalendar import now_manila
from aml.report.build import ctr_records, str_records
from aml.report.render import render
from aml.report.spec import load_spec
from aml.report.validate import blocking, validate_records
from aml.rules.engine import run_monitoring
from aml.rules.registry import rule_catalogue
from aml.screening.service import ScreeningCache, ScreeningService, alerts_from_results

__all__ = ["main"]

log = logging.getLogger("aml.console")


# ---------------------------------------------------------------------------
# Presentation helpers
# ---------------------------------------------------------------------------


def _emit(payload: Any, args: argparse.Namespace, lines: Sequence[str] = ()) -> int:
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        for line in lines:
            print(line)
    return 0


def _table(rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> list[str]:
    if not rows:
        return ["(nothing to show)"]
    widths = {
        column: max(len(column), max(len(str(row.get(column, ""))) for row in rows))
        for column in columns
    }
    header = "  ".join(column.ljust(widths[column]) for column in columns)
    out = [header, "  ".join("-" * widths[column] for column in columns)]
    for row in rows:
        out.append("  ".join(str(row.get(column, "")).ljust(widths[column]) for column in columns))
    return out


def _config(args: argparse.Namespace) -> AmlConfig:
    config = AmlConfig.load(getattr(args, "config", None))
    if getattr(args, "store", None):
        config = config.with_overrides(store_path=args.store)
    if getattr(args, "output_dir", None):
        config = config.with_overrides(output_dir=args.output_dir)
    return config


def _store(config: AmlConfig) -> AmlStore:
    return AmlStore(config.store_file())


def _book(config: AmlConfig, args: argparse.Namespace, *, quiet: bool = False) -> Book:
    parties = getattr(args, "parties", None) or config.sources.parties
    transactions = getattr(args, "transactions", None) or config.sources.transactions
    policies = getattr(args, "policies", None) or config.sources.policies or None
    rates = getattr(args, "rates", None) or config.sources.rates or None
    if not parties or not transactions:
        raise SystemExit(
            "no extract to read: pass --parties and --transactions, or set [sources] in "
            "the configuration"
        )
    book, report = load_book(
        parties_path=parties,
        policies_path=policies,
        transactions_path=transactions,
        rates_path=rates,
    )
    if not quiet:
        for issue in report.issues[:20]:
            print(f"  {issue}", file=sys.stderr)
        if len(report.issues) > 20:
            print(f"  ... and {len(report.issues) - 20} more", file=sys.stderr)
    if report.errors:
        print(
            f"{len(report.errors)} rows could not be loaded and are NOT being monitored",
            file=sys.stderr,
        )
    return book


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    target = pathlib.Path(args.path)
    if target.exists() and not args.force:
        print(f"{target} exists; pass --force to overwrite", file=sys.stderr)
        return 1
    target.write_text(EXAMPLE_TOML, encoding="utf-8")
    print(f"wrote {target}")
    print("Fill in [institution] before filing anything: the AMLC institution code and a")
    print("named compliance officer are refused as blank at submission.")
    return 0


def cmd_rules(args: argparse.Namespace) -> int:
    catalogue = rule_catalogue()
    rows = [
        {
            "rule": entry["rule_id"],
            "v": entry["version"],
            "ST": ",".join(entry["st_codes"]) or "-",
            "title": entry["title"],
        }
        for entry in catalogue
    ]
    return _emit(catalogue, args, _table(rows, ["rule", "v", "ST", "title"]))


def cmd_codes(args: argparse.Namespace) -> int:
    payload = {
        "suspicious_circumstances": {k: v.label for k, v in SUSPICIOUS_CIRCUMSTANCES.items()},
        "unlawful_activities": {k: v.label for k, v in UNLAWFUL_ACTIVITIES.items()},
    }
    lines = ["Suspicious circumstances (an STR must cite at least one):"]
    lines += [f"  {code}  {entry.label}" for code, entry in SUSPICIOUS_CIRCUMSTANCES.items()]
    lines.append("")
    lines.append("Predicate unlawful activities:")
    lines += [
        f"  {code}  {entry.label}" + (f" [{entry.reference}]" if entry.reference else "")
        for code, entry in UNLAWFUL_ACTIVITIES.items()
    ]
    return _emit(payload, args, lines)


def cmd_ingest(args: argparse.Namespace) -> int:
    config = _config(args)
    book = _book(config, args)
    payload = {
        "parties": len(book.parties),
        "policies": len(book.policies),
        "transactions": len(book),
        "clients_with_activity": len(book.party_ids()),
    }
    return _emit(
        payload,
        args,
        [
            f"{payload['transactions']} transactions for {payload['clients_with_activity']} "
            f"clients, {payload['parties']} customer records, {payload['policies']} policies",
        ],
    )


def cmd_monitor(args: argparse.Namespace) -> int:
    config = _config(args)
    book = _book(config, args)
    store = _store(config)
    as_of = dt.date.fromisoformat(args.as_of) if args.as_of else None
    run = run_monitoring(book, config, as_of=as_of)
    store.record_run(run)
    result = store.upsert_alerts(run.alerts, run_id=run.run_id, actor=args.actor)

    payload = {
        **run.as_dict(),
        "created": result.created,
        "updated": result.updated,
    }
    lines = [
        f"run {run.run_id}  as of {run.as_of}  fingerprint {run.fingerprint[:12]}",
        f"{run.transactions_examined} transactions, {run.parties_examined} clients",
        f"{len(run.covered_transaction_alerts)} covered transactions, "
        f"{len(run.suspicion_alerts)} suspicion alerts "
        f"({result.created} new, {result.updated} already known)",
        "",
    ]
    lines += _table(
        [
            {
                "alert": a.alert_id,
                "sev": str(a.severity),
                "score": str(a.score),
                "rule": a.rule_id,
                "client": a.subject_party_id,
                "amount": str(a.amount_php.quantized()) if a.amount_php else "",
            }
            for a in run.alerts[: args.limit]
        ],
        ["alert", "sev", "score", "rule", "client", "amount"],
    )
    if run.errors:
        lines.append("")
        lines += [f"RULE FAILED: {o.rule_id}: {o.error}" for o in run.errors]
    return _emit(payload, args, lines)


def cmd_alerts(args: argparse.Namespace) -> int:
    config = _config(args)
    store = _store(config)
    covered = None if args.covered is None else args.covered
    alerts = store.alerts(
        state=args.state, subject_party_id=args.client, covered=covered, limit=args.limit
    )
    payload = [a.as_dict() for a in alerts]
    if args.show:
        lines = []
        for alert in alerts:
            lines += [
                f"{alert.alert_id}  [{alert.severity}] {alert.rule_id}  score {alert.score}",
                f"  client {alert.subject_party_id}   state {alert.state}"
                + (f"   case {alert.case_id}" if alert.case_id else ""),
                f"  ST codes: {', '.join(alert.st_codes) or '-'}",
                f"  {alert.narrative}",
                "",
            ]
        return _emit(payload, args, lines)
    rows = [
        {
            "alert": a.alert_id,
            "sev": str(a.severity),
            "score": str(a.score),
            "rule": a.rule_id,
            "client": a.subject_party_id,
            "state": str(a.state),
            "case": a.case_id or "",
        }
        for a in alerts
    ]
    return _emit(payload, args, _table(rows, ["alert", "sev", "score", "rule", "client",
                                              "state", "case"]))


def cmd_screen(args: argparse.Namespace) -> int:
    config = _config(args)
    store = _store(config)
    book = _book(config, args)
    parties = list(book.parties.values())
    if args.client:
        parties = [p for p in parties if p.party_id in set(args.client)]
    cache = ScreeningCache(
        config.output_path() / "screening-cache.sqlite3", config.screening.cache_ttl_hours
    )
    service = ScreeningService(config, cache=cache)
    results = service.screen_parties(parties)
    store.record_screening(results, actor=args.actor)
    alerts = alerts_from_results(results)
    upserted = store.upsert_alerts(alerts, run_id="screening", actor=args.actor)

    failures = [r for r in results if r.error]
    payload = {
        "screened": len(results),
        "providers": list(config.screening.providers),
        "potential_matches": sum(1 for r in results if r.hits),
        "alerts_created": upserted.created,
        "failures": [r.error for r in failures],
        "results": [r.as_dict() for r in results if r.hits or r.error],
    }
    searched = ", ".join(sorted({name for r in results for name in r.lists_searched}))
    lines = [
        f"screened {len(results)} subjects against {', '.join(config.screening.providers)} "
        f"({searched or 'no lists'})",
        f"{payload['potential_matches']} with potential matches, "
        f"{upserted.created} new alerts",
    ]
    if failures:
        lines.append(
            f"WARNING: {len(failures)} subjects could not be screened and are NOT cleared:"
        )
        lines += [f"  {r.subject.subject_id} {r.subject.name}: {r.error}" for r in failures]
    lines.append("")
    lines += _table(
        [
            {
                "client": r.subject.subject_id,
                "name": r.subject.name,
                "score": str(r.best.score) if r.best else "",
                "list": r.best.entry.list_name if r.best else "",
                "matched": r.best.entry.name if r.best else "",
            }
            for r in results
            if r.hits
        ],
        ["client", "name", "score", "list", "matched"],
    )
    return _emit(payload, args, lines)


def cmd_case_open(args: argparse.Namespace) -> int:
    config = _config(args)
    store = _store(config)
    alerts = [store.get_alert(alert_id) for alert_id in args.alert]
    missing = [a for a, found in zip(args.alert, alerts, strict=True) if found is None]
    if missing:
        print(f"no such alert(s): {', '.join(missing)}", file=sys.stderr)
        return 1
    found = [a for a in alerts if a is not None]
    subject = args.client or found[0].subject_party_id
    book = _book(config, args, quiet=True) if _has_sources(config, args) else None
    case = store.open_case(
        kind=ReportKind(args.kind),
        subject_party_id=subject,
        alerts=found,
        actor=args.actor,
        priority=Severity(args.priority),
    )
    if book is not None:
        # Draft the narrative now, while the extract that produced the alerts
        # is at hand. The analyst edits it; nobody should face a blank box.
        narrative = compose_narrative(case, found, book, config)
        case = store.update_case(
            replace(case, narrative=narrative),
            actor=args.actor,
            action="case.narrative_drafted",
            detail={"characters": len(narrative)},
        )
    lines = [f"opened {case.case_id} over {len(found)} alerts on {subject}"]
    if case.narrative:
        lines += ["", case.narrative]
    return _emit(case.as_dict(), args, lines)


def _has_sources(config: AmlConfig, args: argparse.Namespace) -> bool:
    return bool(
        (getattr(args, "parties", None) or config.sources.parties)
        and (getattr(args, "transactions", None) or config.sources.transactions)
    )


def cmd_case_determine(args: argparse.Namespace) -> int:
    config = _config(args)
    store = _store(config)
    case = determine(
        store,
        args.case,
        actor=args.actor,
        config=config,
        st_codes=args.st or (),
        unlawful_activity_codes=args.ua or (),
        narrative=pathlib.Path(args.narrative_file).read_text(encoding="utf-8")
        if args.narrative_file
        else "",
    )
    return _emit(
        case.as_dict(),
        args,
        [
            f"{case.case_id} determined reportable by {args.actor}",
            f"  circumstances: {', '.join(case.st_codes)}",
            f"  filing deadline: {case.filing_deadline} "
            f"({config.deadlines.str_working_days} working days)",
            "  awaiting approval by a second person before it can be filed",
        ],
    )


def cmd_case_approve(args: argparse.Namespace) -> int:
    config = _config(args)
    store = _store(config)
    case = approve(store, args.case, approver=args.approver, note=args.note)
    return _emit(
        case.as_dict(),
        args,
        [f"{case.case_id} approved for filing by {args.approver}"],
    )


def cmd_case_close(args: argparse.Namespace) -> int:
    config = _config(args)
    store = _store(config)
    case = close_case(store, args.case, actor=args.actor, reason=args.reason)
    return _emit(case.as_dict(), args, [f"{case.case_id} closed as not reportable"])


def cmd_case_list(args: argparse.Namespace) -> int:
    config = _config(args)
    store = _store(config)
    cases = store.cases(state=args.state, kind=args.kind)
    rows = [
        {
            "case": c.case_id,
            "kind": str(c.kind),
            "client": c.subject_party_id,
            "state": str(c.state),
            "due": c.filing_deadline.isoformat() if c.filing_deadline else "",
            "ST": ",".join(c.st_codes),
        }
        for c in cases
    ]
    return _emit(
        [c.as_dict() for c in cases],
        args,
        _table(rows, ["case", "kind", "client", "state", "due", "ST"]),
    )


def cmd_case_show(args: argparse.Namespace) -> int:
    config = _config(args)
    store = _store(config)
    case = store.get_case(args.case)
    if case is None:
        print(f"no case {args.case}", file=sys.stderr)
        return 1
    alerts = store.alerts(case_id=case.case_id)
    trail = store.audit_trail(case.case_id, limit=50)
    payload = {
        "case": case.as_dict(),
        "alerts": [a.as_dict() for a in alerts],
        "audit": [
            {"seq": e.seq, "at": e.occurred_at, "actor": e.actor, "action": e.action}
            for e in trail
        ],
    }
    lines = [
        f"{case.case_id}  {case.kind}  {case.state}",
        f"  client {case.subject_party_id}   opened {case.opened_at}   "
        f"deadline {case.filing_deadline}",
        f"  determined by {case.determined_by or '-'}  approved by {case.approved_by or '-'}",
        f"  circumstances {', '.join(case.st_codes) or '-'}",
        "",
        case.narrative or "(no narrative)",
        "",
        "Alerts:",
    ]
    lines += [f"  {a.alert_id} [{a.severity}] {a.rule_id}" for a in alerts]
    lines += ["", "Audit trail (most recent first):"]
    lines += [f"  {e.seq:>4} {e.occurred_at} {e.actor:<20} {e.action}" for e in trail]
    return _emit(payload, args, lines)


def cmd_due(args: argparse.Namespace) -> int:
    config = _config(args)
    store = _store(config)
    statuses = due_soon(store, config)
    rows = [
        {
            "case": s.case_id,
            "kind": str(s.kind),
            "state": str(s.state),
            "due": s.due_on.isoformat() if s.due_on else "not determined",
            "days": "" if s.working_days_remaining is None else str(s.working_days_remaining),
            "flag": "OVERDUE" if s.overdue else ("AT RISK" if s.at_risk else ""),
        }
        for s in statuses
    ]
    return _emit([s.as_dict() for s in statuses], args,
                 _table(rows, ["case", "kind", "state", "due", "days", "flag"]))


def cmd_report(args: argparse.Namespace) -> int:
    config = _config(args)
    store = _store(config)
    book = _book(config, args, quiet=True)
    kind = ReportKind(args.kind.upper())
    spec = load_spec(kind, args.spec_version, args.spec_dir)
    today = now_manila().date()

    if kind is ReportKind.CTR:
        alerts = store.alerts(covered=True, limit=100_000)
        if args.since:
            since = dt.date.fromisoformat(args.since)
            alerts = [a for a in alerts if a.window_end and a.window_end >= since]
        records = ctr_records(alerts, book, config, prepared_on=today)
        period_start = min((r["transaction_date"] for r in records), default=today)
        period_end = max((r["transaction_date"] for r in records), default=today)
    else:
        cases = (
            [store.get_case(args.case)]
            if args.case
            else store.cases(state=CaseState.APPROVED_FOR_FILING, kind=ReportKind.STR)
        )
        cases = [c for c in cases if c is not None]
        if args.require_approval:
            cases = [c for c in cases if c.state is CaseState.APPROVED_FOR_FILING]
        alerts_by_case = {c.case_id: store.alerts(case_id=c.case_id) for c in cases}
        records = str_records(cases, alerts_by_case, book, config, prepared_on=today)
        period_start = min((r["determination_date"] for r in records), default=today)
        period_end = max((r["determination_date"] for r in records), default=today)

    if not records:
        return _emit({"records": 0}, args, ["nothing to report"])

    issues = validate_records(records, spec)
    fatal = blocking(issues)
    payload: dict[str, Any] = {
        "kind": str(kind),
        "records": len(records),
        "issues": [i.as_dict() for i in issues],
    }
    lines = [f"{len(records)} {kind} records"]
    lines += [f"  {issue}" for issue in issues[:25]]
    if fatal and not args.force:
        lines.append("")
        lines.append(
            f"{len(fatal)} blocking issues; not written. Fix them, or pass --force to write "
            "the file anyway for inspection (it will be rejected on submission)."
        )
        _emit(payload, args, lines)
        return 1

    out_dir = config.output_path() / "reports"
    filename = spec.filename(config.institution.amlc_institution_code, today, args.sequence)
    rendered = render(records, spec, out_dir / filename)
    report_id = f"RPT-{rendered.sha256[:12].upper()}"
    store.record_report(
        report_id=report_id,
        kind=kind,
        rendered=rendered.as_dict(),
        actor=args.actor,
        case_id=args.case if kind is ReportKind.STR and args.case else None,
        period_start=period_start,
        period_end=period_end,
        references=[str(r.get("report_reference", "")) for r in records],
    )
    payload.update({"report_id": report_id, **rendered.as_dict()})
    lines += [
        "",
        f"wrote {rendered.path}",
        f"  report id {report_id}   sha256 {rendered.sha256}",
        f"  {rendered.rows} rows, {rendered.size_bytes} bytes, spec {spec.kind} v{spec.version}",
        f"  submit with: aml submit --report-id {report_id} --actor <you>",
    ]
    return _emit(payload, args, lines)


def cmd_submit(args: argparse.Namespace) -> int:
    from aml.model.enums import SubmissionState
    from aml.portal.client import submit_package

    config = _config(args)
    store = _store(config)

    if args.acknowledge:
        store.record_submission(
            submission_id=args.submission_id,
            report_id=args.report_id or "",
            state=SubmissionState.ACKNOWLEDGED,
            mode=config.portal.mode,
            actor=args.actor,
            receipt_reference=args.acknowledge,
            acknowledged_at=now_manila(),
        )
        return _emit(
            {"submission_id": args.submission_id, "reference": args.acknowledge},
            args,
            [f"{args.submission_id} acknowledged by the AMLC as {args.acknowledge}"],
        )

    reports = {r["report_id"]: r for r in store.reports(limit=1000)}
    report = reports.get(args.report_id) if args.report_id else (
        next(iter(store.reports(limit=1)), None)
    )
    if report is None:
        print("no report to submit; run 'aml report ctr' or 'aml report str' first",
              file=sys.stderr)
        return 1

    submission_id, receipt = submit_package(
        store,
        config,
        report_id=report["report_id"],
        files=[report["path"]],
        kind=ReportKind(report["kind"]),
        actor=args.actor,
        period_start=dt.date.fromisoformat(report["period_start"])
        if report["period_start"]
        else None,
        period_end=dt.date.fromisoformat(report["period_end"]) if report["period_end"] else None,
        force=args.force,
    )
    payload = {"submission_id": submission_id, **receipt.as_dict()}
    lines = [
        f"{submission_id}  {receipt.state}",
        f"  report {report['report_id']} ({report['kind']}, {report['rows']} rows)",
        f"  {receipt.message}",
    ]
    if receipt.state is SubmissionState.PACKAGED:
        lines.append(
            "  once the portal acknowledges it: aml submit --acknowledge <reference> "
            f"--submission-id {submission_id} --actor <you>"
        )
    return _emit(payload, args, lines)


def cmd_verify(args: argparse.Namespace) -> int:
    config = _config(args)
    store = _store(config)
    intact, bad_seq, message = store.verify_audit_chain()
    payload = {"intact": intact, "first_bad_sequence": bad_seq, "message": message}
    return _emit(
        payload,
        args,
        [("audit chain intact: " if intact else "AUDIT CHAIN BROKEN: ") + message],
    ) or (0 if intact else 2)


def cmd_config(args: argparse.Namespace) -> int:
    import tomllib

    config = _config(args)
    stray: list[str] = []
    if config.source_path:
        with open(config.source_path, "rb") as handle:
            stray = unknown_keys(tomllib.load(handle))
    missing = config.institution.missing_for_filing()
    years = {now_manila().year, now_manila().year + 1}
    unproclaimed = config.calendar.unproclaimed_years(years)
    payload = {
        "source": config.source_path or "(defaults; no aml.toml found)",
        "store": str(config.store_file()),
        "threshold": str(config.thresholds.covered_transaction),
        "providers": list(config.screening.providers),
        "portal_mode": config.portal.mode,
        "missing_for_filing": missing,
        "unknown_keys": stray,
        "years_without_proclaimed_holidays": unproclaimed,
    }
    lines = [
        f"configuration: {payload['source']}",
        f"store:         {payload['store']}",
        f"threshold:     {payload['threshold']} per banking day, "
        f"aggregate_same_day={config.thresholds.aggregate_same_day}",
        f"screening:     {', '.join(config.screening.providers)} "
        f"(review at {config.screening.review_threshold})",
        f"portal:        {config.portal.mode}",
    ]
    if missing:
        lines.append(f"NOT READY TO FILE: institution profile missing {', '.join(missing)}")
    if stray:
        lines.append(f"unknown configuration keys (ignored): {', '.join(stray)}")
    if unproclaimed:
        lines.append(
            "no proclaimed holidays loaded for "
            f"{', '.join(str(y) for y in unproclaimed)}; filing deadlines in those years "
            "will treat Eid'l Fitr, Eid'l Adha and any special days as working days"
        )
    return _emit(payload, args, lines)


def cmd_demo(args: argparse.Namespace) -> int:
    """Generate a synthetic book and run the whole pipeline over it."""
    base = pathlib.Path(args.dir)
    paths = generate(base / "extract")
    config_path = base / "aml.toml"
    config_path.write_text(
        EXAMPLE_TOML.replace(
            'covered_person_name = ""',
            'covered_person_name = "Demonstration Life Assurance Company, Inc."',
        )
        .replace('amlc_institution_code = ""', 'amlc_institution_code = "DEMO-INS-0001"')
        .replace(
            'compliance_officer_name = ""',
            'compliance_officer_name = "R. Villanueva"',
        )
        .replace('store_path = "out/aml.sqlite3"', f'store_path = "{base}/aml.sqlite3"')
        .replace('output_dir = "out"', f'output_dir = "{base}/out"')
        .replace(
            "[sources]",
            "[sources]\n"
            f'parties = "{paths["parties"]}"\n'
            f'policies = "{paths["policies"]}"\n'
            f'transactions = "{paths["transactions"]}"\n'
            f'rates = "{paths["rates"]}"',
        )
        .replace(
            "[screening.local_lists]",
            f'[screening.local_lists]\nUN_DESIGNATED = "{paths["watchlist"]}"',
        ),
        encoding="utf-8",
    )
    print(f"demo workspace: {base}")
    print(f"  extract:      {base / 'extract'} (answer key in ANSWER-KEY.md)")
    print(f"  config:       {config_path}")
    print("")
    print("Planted typologies:")
    for client, what in PLANTED.items():
        print(f"  {client}  {what}")
    print("")
    print("Now run, in order:")
    for command in (
        f"aml --config {config_path} monitor --actor demo",
        f"aml --config {config_path} screen --actor demo",
        f"aml --config {config_path} alerts --limit 15",
        f"aml --config {config_path} report ctr --actor demo",
    ):
        print(f"  {command}")
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _add_source_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("extract (overrides [sources] in the configuration)")
    group.add_argument("--parties", help="customer extract (CSV or JSON)")
    group.add_argument("--policies", help="policy extract")
    group.add_argument("--transactions", help="transaction extract")
    group.add_argument("--rates", help="currency,date,rate CSV of reference rates")


def _global_flags(parser: argparse.ArgumentParser, *, suppress: bool) -> None:
    """The flags that make sense on either side of the subcommand.

    ``aml --json monitor`` and ``aml monitor --json`` both work, because people
    type both. The per-subcommand copies default to ``SUPPRESS`` so that
    omitting them leaves whatever was given before the subcommand intact
    instead of silently resetting it to False.
    """
    default: Any = argparse.SUPPRESS if suppress else None
    flag_default: Any = argparse.SUPPRESS if suppress else False
    parser.add_argument("--config", default=default,
                        help="path to aml.toml (default: $AML_CONFIG or ./aml.toml)")
    parser.add_argument("--store", default=default, help="override the operational store path")
    parser.add_argument("--output-dir", default=default, help="override the output directory")
    parser.add_argument("--json", action="store_true", default=flag_default,
                        help="emit JSON instead of a table")
    parser.add_argument("--verbose", action="store_true", default=flag_default,
                        help="log at DEBUG")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aml",
        description="AML and transaction monitoring for a Philippine insurance covered person.",
    )
    _global_flags(parser, suppress=False)
    common = argparse.ArgumentParser(add_help=False)
    _global_flags(common, suppress=True)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser(
        "init", parents=[common], help="write a documented configuration file"
    )
    init.add_argument("path", nargs="?", default="aml.toml")
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init)

    rules = sub.add_parser(
        "rules", parents=[common], help="the detection catalogue, for the AML manual"
    )
    rules.set_defaults(func=cmd_rules)

    codes = sub.add_parser(
        "codes", parents=[common], help="statutory circumstance and predicate offence codes"
    )
    codes.set_defaults(func=cmd_codes)

    config_cmd = sub.add_parser(
        "config", parents=[common], help="show effective configuration and readiness"
    )
    config_cmd.set_defaults(func=cmd_config)

    ingest = sub.add_parser(
        "ingest", parents=[common], help="load an extract and report what is wrong with it"
    )
    _add_source_args(ingest)
    ingest.set_defaults(func=cmd_ingest)

    monitor = sub.add_parser(
        "monitor", parents=[common], help="run the rules and record the alerts"
    )
    _add_source_args(monitor)
    monitor.add_argument("--as-of", help="the date the run reasons about (YYYY-MM-DD)")
    monitor.add_argument("--actor", default="system", help="who ran it")
    monitor.add_argument("--limit", type=int, default=25, help="alerts to print")
    monitor.set_defaults(func=cmd_monitor)

    alerts = sub.add_parser(
        "alerts", parents=[common], help="list alerts"
    )
    alerts.add_argument("--state", help="OPEN, IN_REVIEW, ESCALATED, CLOSED_*")
    alerts.add_argument("--client", help="only this client")
    alerts.add_argument(
        "--covered", action="store_true", default=None, help="covered transactions only"
    )
    alerts.add_argument("--show", action="store_true", help="print full narratives")
    alerts.add_argument("--limit", type=int, default=50)
    alerts.set_defaults(func=cmd_alerts)

    screen = sub.add_parser(
        "screen", parents=[common], help="screen customers against the configured lists"
    )
    _add_source_args(screen)
    screen.add_argument("--client", action="append", help="screen only these clients")
    screen.add_argument("--actor", default="system")
    screen.set_defaults(func=cmd_screen)

    case = sub.add_parser(
        "case", parents=[common], help="the investigation and filing workflow"
    )
    case_sub = case.add_subparsers(dest="case_command", required=True)

    case_open = case_sub.add_parser(
        "open", parents=[common], help="open a case over one or more alerts"
    )
    case_open.add_argument("--alert", action="append", required=True)
    case_open.add_argument("--client")
    case_open.add_argument("--kind", default="STR", choices=["STR", "CTR"])
    case_open.add_argument("--priority", default="MEDIUM",
                           choices=[str(s) for s in Severity])
    case_open.add_argument("--actor", required=True)
    _add_source_args(case_open)
    case_open.set_defaults(func=cmd_case_open)

    case_determine = case_sub.add_parser(
        "determine", parents=[common], help="record the determination that the case is reportable"
    )
    case_determine.add_argument("--case", required=True)
    case_determine.add_argument("--actor", required=True)
    case_determine.add_argument("--st", action="append", help="ST1-ST7, repeatable")
    case_determine.add_argument("--ua", action="append", help="predicate offence code")
    case_determine.add_argument("--narrative-file", help="replace the narrative from a file")
    case_determine.set_defaults(func=cmd_case_determine)

    case_approve = case_sub.add_parser(
        "approve", parents=[common], help="approve for filing (a second person)"
    )
    case_approve.add_argument("--case", required=True)
    case_approve.add_argument("--approver", required=True)
    case_approve.add_argument("--note", default="")
    case_approve.set_defaults(func=cmd_case_approve)

    case_close = case_sub.add_parser(
        "close", parents=[common], help="close a case as not reportable"
    )
    case_close.add_argument("--case", required=True)
    case_close.add_argument("--actor", required=True)
    case_close.add_argument("--reason", required=True)
    case_close.set_defaults(func=cmd_case_close)

    case_list = case_sub.add_parser(
        "list", parents=[common], help="list cases"
    )
    case_list.add_argument("--state")
    case_list.add_argument("--kind")
    case_list.set_defaults(func=cmd_case_list)

    case_show = case_sub.add_parser(
        "show", parents=[common], help="a case with its alerts and audit trail"
    )
    case_show.add_argument("case")
    case_show.set_defaults(func=cmd_case_show)

    due = sub.add_parser(
        "due", parents=[common], help="filing deadlines, worst first"
    )
    due.set_defaults(func=cmd_due)

    report = sub.add_parser(
        "report", parents=[common], help="build a CTR or STR file"
    )
    report.add_argument("kind", choices=["ctr", "str"])
    _add_source_args(report)
    report.add_argument("--case", help="STR: a specific case")
    report.add_argument("--since", help="CTR: only transactions on or after this date")
    report.add_argument("--spec-version", default="v1")
    report.add_argument("--spec-dir", help="directory of report layouts to use instead")
    report.add_argument("--sequence", type=int, default=1)
    report.add_argument("--actor", default="system")
    report.add_argument(
        "--require-approval",
        action="store_true",
        default=True,
        help="STR: only cases approved for filing (default)",
    )
    report.add_argument("--force", action="store_true", help="write despite blocking issues")
    report.set_defaults(func=cmd_report)

    submit = sub.add_parser(
        "submit", parents=[common], help="package and send a report to the AMLC"
    )
    submit.add_argument("--report-id")
    submit.add_argument("--submission-id", default="")
    submit.add_argument("--acknowledge", help="record the portal's acknowledgement reference")
    submit.add_argument("--actor", required=True)
    submit.add_argument("--force", action="store_true", help="resubmit an identical package")
    submit.set_defaults(func=cmd_submit)

    verify = sub.add_parser(
        "verify", parents=[common], help="verify the audit chain has not been altered"
    )
    verify.set_defaults(func=cmd_verify)

    demo = sub.add_parser(
        "demo", parents=[common], help="generate a synthetic book with known typologies"
    )
    demo.add_argument("--dir", default="data/aml-demo")
    demo.set_defaults(func=cmd_demo)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s %(message)s",
    )
    try:
        code: int = args.func(args)
        return code
    except WorkflowError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    except (ValueError, FileNotFoundError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
