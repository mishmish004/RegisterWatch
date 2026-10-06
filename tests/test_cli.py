"""The CLI's shape: one command group per jurisdiction, targets in any mix."""

from __future__ import annotations

import json

import pytest

from registerwatch import cli, jurisdictions
from registerwatch.registers import REGISTRY


def test_every_jurisdiction_has_a_name_and_a_command():
    codes = jurisdictions.by_code()
    assert set(codes) == set(jurisdictions.NAMES)
    parser = cli.build_parser()
    for code in codes:
        args = parser.parse_args([jurisdictions.cli_name(code)])
        assert args.fn is cli.cmd_jur_info and args.code == code


@pytest.mark.parametrize("spelling", ["us-nj", "US-NJ", "us_nj", " Us-Nj "])
def test_codes_are_case_and_separator_insensitive(spelling):
    assert [r.slug for r in jurisdictions.resolve(spelling)] == ["us_nj_dge"]


def test_unknown_jurisdiction_is_a_clean_error(capsys):
    assert cli.main(["registers", "-j", "zz"]) == 2
    assert "unknown jurisdiction 'zz'" in capsys.readouterr().err


def test_jurisdiction_actions_parse():
    p = cli.build_parser()
    a = p.parse_args(["gb", "rows", "licences", "-w", "status=Active", "-w", "type=Remote", "--limit", "5", "-f", "csv"])
    assert a.fn is cli.cmd_jur_rows and a.where == ["status=Active", "type=Remote"] and a.format == "csv"
    a = p.parse_args(["ch", "changes", "--since", "2026-10-01"])
    assert a.fn is cli.cmd_jur_changes and a.since.isoformat() == "2026-10-01T00:00:00+00:00"
    assert p.parse_args(["pl", "ingest", "--force"]).fn is cli.cmd_ingest


def test_ingest_targets_mix_codes_slugs_and_all():
    assert [r.slug for r in cli._targets(["gb", "pl_mf", "ch"])] == ["gb_ukgc", "pl_mf", "ch_esbk", "ch_gespa"]
    assert len(cli._targets(["all"])) == len(REGISTRY) == len(cli._targets([]))


def test_a_table_name_shared_by_two_registers_must_be_qualified():
    from registerwatch import query

    regs = jurisdictions.resolve("ch")
    with pytest.raises(KeyError, match="ch_esbk.blocked_domains"):
        query.find_table(regs, "blocked_domains")
    assert query.find_table(regs, "ch_gespa.blocked_domains")[0].slug == "ch_gespa"


@pytest.mark.parametrize("fmt", ["table", "csv", "json"])
def test_output_formats(capsys, fmt):
    cli._emit([{"a": "x", "b": ["p", "q"], "c": None}], fmt)
    out = capsys.readouterr().out
    if fmt == "json":
        assert json.loads(out) == [{"a": "x", "b": ["p", "q"], "c": None}]
    else:
        assert "p;q" in out


def test_info_runs_without_a_database(capsys):
    assert cli.main(["ch"]) == 0
    out = capsys.readouterr().out
    assert "ch_esbk" in out and "ch_gespa" in out and "blocked_domains" in out


def test_model_commands_parse():
    p = cli.build_parser()
    a = p.parse_args(["domain", "https://www.bet365.com/", "-j", "gb,ch", "--all", "-f", "json"])
    assert a.fn is cli.cmd_domain and a.all and a.jurisdiction == "gb,ch"
    assert p.parse_args(["operators", "betway"]).fn is cli.cmd_operators
    assert p.parse_args(["operator", "betway-ltd"]).operator_id == "betway-ltd"
    a = p.parse_args(["licences", "-j", "gb", "--status", "suspended,revoked", "--product", "casino", "--all"])
    assert a.fn is cli.cmd_licences and a.status == "suspended,revoked" and a.all
    a = p.parse_args(["events", "--since", "2026-10-01", "-t", "licence.status_changed", "--domain", "bet365.com"])
    assert a.fn is cli.cmd_events and a.since.isoformat() == "2026-10-01T00:00:00+00:00"
    assert p.parse_args(["gb", "profile"]).fn is cli.cmd_jur_profile
    assert p.parse_args(["build"]).fn is cli.cmd_build and p.parse_args(["coverage"]).fn is cli.cmd_coverage
    assert p.parse_args(["ingest", "gb", "--no-build"]).no_build
