"""The command line, end to end, over the synthetic book.

This is the test that would have caught anything the unit tests let through: it
runs the sequence a compliance unit actually runs — monitor, screen, open,
determine, approve, report, submit, verify — and asserts on what comes out the
far end.
"""

from __future__ import annotations

import json

import pytest

from aml.console import main
from aml.demo.generate import PLANTED


@pytest.fixture
def workspace(tmp_path, capsys):
    assert main(["demo", "--dir", str(tmp_path)]) == 0
    capsys.readouterr()
    return tmp_path


def run(args, capsys) -> tuple[int, str]:
    code = main(args)
    return code, capsys.readouterr().out


def run_json(args, capsys):
    code, out = run([*args, "--json"], capsys)
    return code, json.loads(out)


def test_the_whole_pipeline(workspace, capsys):
    config = ["--config", str(workspace / "aml.toml")]

    code, payload = run_json([*config, "monitor", "--actor", "demo"], capsys)
    assert code == 0
    assert payload["covered_transactions"] >= 10
    assert payload["suspicions"] >= 9
    assert not [o for o in payload["outcomes"] if o["error"]]

    code, screening = run_json([*config, "screen", "--actor", "demo"], capsys)
    assert code == 0
    assert screening["screened"] == 60
    assert screening["potential_matches"] == 1
    assert not screening["failures"]

    code, alerts = run_json([*config, "alerts", "--limit", "100"], capsys)
    fired = {(a["subject_party_id"], a["rule_id"]) for a in alerts}
    for client in PLANTED:
        assert any(subject == client for subject, _ in fired), f"nothing found for {client}"
    assert ("C-0004", "screening.sanctions_match") in fired

    early = next(
        a["alert_id"] for a in alerts if a["rule_id"] == "str.early_surrender"
    )
    code, case = run_json(
        [*config, "case", "open", "--alert", early, "--actor", "analyst.dizon"], capsys
    )
    assert code == 0
    case_id = case["case_id"]
    assert "GROUNDS FOR SUSPICION" in case["narrative"]

    code, _ = run_json(
        [*config, "case", "determine", "--case", case_id, "--actor", "analyst.dizon",
         "--ua", "UA09"],
        capsys,
    )
    assert code == 0

    # The analyst cannot approve their own determination.
    assert main([*config, "case", "approve", "--case", case_id,
                 "--approver", "analyst.dizon"]) == 1
    capsys.readouterr()
    assert main([*config, "case", "approve", "--case", case_id,
                 "--approver", "co.villanueva"]) == 0
    capsys.readouterr()

    code, str_report = run_json([*config, "report", "str", "--actor", "co.villanueva"], capsys)
    assert code == 0 and str_report["records"] == 1
    assert not [i for i in str_report["issues"] if i["severity"] == "error"]

    code, ctr_report = run_json([*config, "report", "ctr", "--actor", "co.villanueva"], capsys)
    assert code == 0 and ctr_report["rows"] >= 10

    code, submission = run_json(
        [*config, "submit", "--report-id", ctr_report["report_id"], "--actor", "co.villanueva"],
        capsys,
    )
    assert code == 0 and submission["state"] == "PACKAGED"

    code, verified = run_json([*config, "verify"], capsys)
    assert code == 0 and verified["intact"] is True


def test_monitoring_twice_creates_no_duplicate_alerts(workspace, capsys):
    config = ["--config", str(workspace / "aml.toml")]
    _, first = run_json([*config, "monitor", "--actor", "demo"], capsys)
    _, second = run_json([*config, "monitor", "--actor", "demo"], capsys)
    assert second["created"] == 0
    assert second["updated"] == first["created"]


def test_config_reports_readiness_to_file(workspace, capsys):
    code, payload = run_json(["--config", str(workspace / "aml.toml"), "config"], capsys)
    assert code == 0
    assert payload["missing_for_filing"] == []
    assert payload["unknown_keys"] == []
    # The demo config ships no proclaimed holidays, and the tool says so rather
    # than computing deadlines on an optimistic calendar.
    assert payload["years_without_proclaimed_holidays"]


def test_an_incomplete_institution_profile_blocks_reporting(tmp_path, capsys):
    assert main(["demo", "--dir", str(tmp_path)]) == 0
    config_path = tmp_path / "aml.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'amlc_institution_code = "DEMO-INS-0001"', 'amlc_institution_code = ""'
        ),
        encoding="utf-8",
    )
    capsys.readouterr()
    config = ["--config", str(config_path)]
    assert main([*config, "monitor", "--actor", "demo"]) == 0
    capsys.readouterr()
    assert main([*config, "report", "ctr", "--actor", "demo"]) == 1
    assert "blocking issues" in capsys.readouterr().out


def test_rules_and_codes_print_the_catalogue(capsys):
    code, rules = run_json(["rules"], capsys)
    assert code == 0 and len(rules) >= 14
    code, codes = run_json(["codes"], capsys)
    assert code == 0 and len(codes["suspicious_circumstances"]) == 7
