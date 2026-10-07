"""The CLI's shape: one command group per jurisdiction, targets in any mix."""

from __future__ import annotations

import json
import signal

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


# --- serve (plan.md P9.2, P9.3) ---------------------------------------------------------


@pytest.fixture
def served(monkeypatch, configure):
    """`served(*argv, **env)`: the uvicorn.run keyword arguments `serve` passes."""
    import uvicorn

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append((app, kw)))
    monkeypatch.setattr(cli, "raise_open_file_limit", lambda: 4096)

    def run(*argv, **env):
        configure(**env)
        calls.clear()
        assert cli.main(["serve", *argv]) == 0
        (app, kw), = calls
        assert app == "registerwatch.api:app"
        return kw

    return run


def test_serve_defaults(served):
    kw = served()
    assert kw["workers"] == 1 and kw["timeout_keep_alive"] == 75 and kw["limit_concurrency"] == 200
    assert kw["timeout_graceful_shutdown"] == 65.0  # SHUTDOWN_GRACE_S 60 + 5
    assert kw["proxy_headers"] is True and kw["forwarded_allow_ips"] == "127.0.0.1"
    assert kw["host"] == "0.0.0.0" and kw["port"] == 8000
    assert kw["http"] == "registerwatch.http.server:BoundedHttpToolsProtocol"  # P10.3's request bounds


def test_serve_reads_the_environment(served):
    kw = served(WEB_CONCURRENCY=3, SHUTDOWN_GRACE_S=20, FORWARDED_ALLOW_IPS="10.0.0.0/8,127.0.0.1", PORT=9000)
    assert kw["workers"] == 3 and kw["timeout_graceful_shutdown"] == 25.0 and kw["port"] == 9000
    assert kw["forwarded_allow_ips"] == "10.0.0.0/8,127.0.0.1"


def test_serve_flags_beat_the_environment(served):
    kw = served("--workers", "2", "--timeout-keep-alive", "30", "--timeout-graceful-shutdown", "9",
                "--limit-concurrency", "5", "--forwarded-allow-ips", "*", WEB_CONCURRENCY=3)
    assert (kw["workers"], kw["timeout_keep_alive"], kw["timeout_graceful_shutdown"], kw["limit_concurrency"],
            kw["forwarded_allow_ips"]) == (2, 30, 9.0, 5, "*")


def test_a_stop_by_sigterm_exits_0(monkeypatch, configure):
    """Uvicorn re-raises the SIGTERM it stopped on after a graceful shutdown;
    outside PID 1 the default action would end the process by signal (143)."""
    import uvicorn

    configure()
    monkeypatch.setattr(cli, "raise_open_file_limit", lambda: 4096)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: signal.raise_signal(signal.SIGTERM))
    before = signal.getsignal(signal.SIGTERM)
    with pytest.raises(SystemExit) as exc:
        cli.main(["serve"])
    assert exc.value.code == 0
    assert signal.getsignal(signal.SIGTERM) is before  # put back


@pytest.mark.parametrize("soft, hard, raised_to", [
    (1024, 524288, 65536),    # Docker's own default: soft 1024
    (1024, 20000, 20000),     # as far as the hard limit lets it
    (1024, 2048, 2048),       # and warns: below 4096
    (100000, 200000, None),   # already higher: left alone
    (1024, -1, 65536),        # hard unlimited (RLIM_INFINITY)
])
def test_serve_raises_its_open_file_limit(monkeypatch, caplog, soft, hard, raised_to):
    import resource

    hard = resource.RLIM_INFINITY if hard == -1 else hard
    set_to = []
    monkeypatch.setattr(resource, "getrlimit", lambda which: (soft, hard))
    monkeypatch.setattr(resource, "setrlimit", lambda which, limits: set_to.append((which, limits)))
    assert cli.raise_open_file_limit() == (raised_to or soft)
    assert set_to == ([(resource.RLIMIT_NOFILE, (raised_to, hard))] if raised_to else [])
    assert ("below 4096" in caplog.text) == (raised_to == 2048)
