"""Action catalog, allowlist and ActionService gates against simulated hosts."""

import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from fleet.detector.store import DEFAULT_DSN
from fleet.governance import catalog
from fleet.governance.allowlist import DEFAULT as ALLOW
from fleet.governance.allowlist import AllowlistError
from fleet.governance.audit import InMemoryAuditLog, PostgresAuditLog
from fleet.governance.executor import SimExecutor, SimFile, host_from_case, render_script
from fleet.governance.service import (
    ActionService,
    Approval,
    GateError,
    IncidentContext,
    Plan,
)

T0 = datetime(2026, 12, 15, 9, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


def load(case: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        Path(f"evals/incidents/{case}.json").read_text(encoding="utf-8")
    )
    return data


def setup(
    case: str, runbook: str, severity: str = "warning", **service_kw: Any
) -> tuple[ActionService, SimExecutor, InMemoryAuditLog, IncidentContext, Clock]:
    spec = load(case)
    executor = SimExecutor([host_from_case(spec)])
    audit = InMemoryAuditLog()
    clock = Clock()
    svc = ActionService(executor, audit, clock=clock, kill_switch=lambda: False, **service_kw)
    inc = spec["incident"]
    ctx = IncidentContext(
        "inc-1",
        "run-1",
        inc["instance"],
        inc["os"],
        inc["scenario"],
        severity,
        runbook,
        (runbook,),
    )
    return svc, executor, audit, ctx, clock


def plan_for(svc: ActionService, ctx: IncidentContext, action: str, **params: Any) -> Plan:
    dry = svc.dry_run(ctx, action, params)
    return Plan(ctx.instance, action, dry.params, ctx.runbook_id, dry.hash, dry.at, "test plan")


# ---------------- catalog and runbooks agree ----------------


def allowed_section(runbook_id: str) -> str:
    text = Path(f"runbooks/{runbook_id}.md").read_text(encoding="utf-8")
    return text.split("## Allowed actions", 1)[1].split("##", 1)[0]


def test_every_action_is_named_in_the_runbooks_that_allow_it() -> None:
    for spec in catalog.CATALOG.values():
        for rb in spec.runbooks:
            assert spec.name in allowed_section(rb), (spec.name, rb)


def test_every_action_a_runbook_names_is_in_the_catalog() -> None:
    for path in sorted(Path("runbooks").glob("RB-*.md")):
        for name in re.findall(r"\b([a-z]+(?:_[a-z0-9]+)+)\(", allowed_section(path.stem)):
            assert name in catalog.CATALOG, (path.stem, name)


def test_parameters_cannot_carry_commands() -> None:
    spec = catalog.get("recycle_app_pool")
    with pytest.raises(catalog.CatalogError):
        catalog.validate_params(spec, {"pool": "DefaultAppPool'; Remove-Item C:\\ -Recurse"})
    with pytest.raises(catalog.CatalogError):
        catalog.validate_params(catalog.get("clear_temp"), {"days": 0})
    with pytest.raises(catalog.CatalogError):
        catalog.validate_params(catalog.get("clear_temp"), {"days": 7, "extra": "x"})
    script = render_script(spec, {"pool": "DefaultAppPool"}, apply=True)
    assert "$p = 'DefaultAppPool'" in script


def test_dry_run_scripts_are_harmless() -> None:
    logs = render_script(catalog.get("delete_iis_logs_older_than"), {"days": 7}, apply=False)
    assert logs.endswith("-WhatIf") and "ReparsePoint" in logs
    tmp = render_script(catalog.get("clear_tmp_older_than"), {"days": 7}, apply=False)
    assert tmp.endswith("-print") and "-delete" not in tmp


@pytest.mark.parametrize(
    "path",
    [
        "C:\\inetpub\\wwwroot\\index.html",
        "/var/www/html",
        "/srv/app/uploads/a.png",
        "/home/ubuntu/.ssh",
        "C:\\Users\\admin\\x",
        "/var/log/../etc/passwd",
        "D:\\data\\orders.mdf",
    ],
)
def test_never_rules(path: str) -> None:
    with pytest.raises(AllowlistError):
        ALLOW.check_never(path)


# ---------------- Tier 0: delete old logs, audited and verified ----------------


def test_tier0_delete_runs_audits_and_verifies() -> None:
    svc, executor, audit, ctx, _ = setup("disk_iis_logs", "RB-001", "critical")
    plan = plan_for(svc, ctx, "delete_iis_logs_older_than", days=7)
    before = executor.read_metric(ctx.instance, "disk_used_pct")
    result = svc.execute(plan, ctx)
    assert result["freed_gb"] > 5
    deleted = audit.records[0].result["dry_run"]["targets"]
    assert deleted and all(t["path"].startswith("C:\\inetpub\\logs\\LogFiles\\") for t in deleted)
    remaining = [f.path for f in executor.host(ctx.instance).files]
    assert not any(t["path"] in remaining for t in deleted)
    verdict = svc.verify(plan, ctx, before)
    assert verdict.recovered and verdict.after < 90 < before
    assert [r.phase for r in audit.records] == ["before", "after", "verify"]
    assert audit.records[0].actor == "policy" and audit.records[0].reason == "test plan"


def test_tier0_never_touches_files_outside_its_roots_or_open_logs() -> None:
    svc, executor, _, ctx, _ = setup("disk_iis_logs", "RB-001")
    dry = svc.dry_run(ctx, "delete_iis_logs_older_than", {"days": 7})
    assert dry.targets and all(
        t["path"].startswith("C:\\inetpub\\logs\\LogFiles") for t in dry.targets
    )
    assert all(t["age_days"] > 7 for t in dry.targets)


# ---------------- gates ----------------


def test_host_change_after_dry_run_blocks_execution() -> None:
    svc, executor, audit, ctx, _ = setup("disk_iis_logs", "RB-001")
    plan = plan_for(svc, ctx, "delete_iis_logs_older_than", days=7)
    executor.host(ctx.instance).files.append(
        SimFile("C:\\inetpub\\logs\\LogFiles\\late.log", 1.0, 30)
    )
    with pytest.raises(GateError, match="hash mismatch"):
        svc.execute(plan, ctx)
    assert executor.applied == [] and audit.records[-1].phase == "blocked"


def test_stale_plan_is_blocked() -> None:
    svc, executor, _, ctx, clock = setup("disk_iis_logs", "RB-001")
    plan = plan_for(svc, ctx, "delete_iis_logs_older_than", days=7)
    clock.now += timedelta(minutes=20)
    with pytest.raises(GateError, match="older than"):
        svc.execute(plan, ctx)
    assert executor.applied == []


def test_citation_gate() -> None:
    svc, _, _, ctx, _ = setup("disk_iis_logs", "RB-001")
    wrong_runbook = plan_for(svc, ctx, "delete_iis_logs_older_than", days=7)
    wrong_runbook = Plan(**{**wrong_runbook.__dict__, "runbook_id": "RB-002"})
    assert svc.check(wrong_runbook, ctx).failed_gate == "citation"
    svc, _, _, ctx, _ = setup("pool_crash", "RB-001")  # RB-001 does not allow recycling
    plan = plan_for(svc, ctx, "recycle_app_pool", pool="DefaultAppPool")
    assert svc.check(plan, ctx).failed_gate == "citation"


def test_wrong_platform_and_unlisted_service_are_blocked() -> None:
    svc, _, _, ctx, _ = setup("pool_crash", "RB-003")
    with pytest.raises(catalog.CatalogError):
        svc.dry_run(ctx, "graceful_reload_service", {"service": "apache2"})
    with pytest.raises(AllowlistError):
        svc.dry_run(ctx, "recycle_app_pool", {"pool": "PayrollPool"})


def test_empty_dry_run_is_blocked() -> None:
    # Deleted-but-open log: no rotated logs are older than 7 days, so deletion frees nothing.
    svc, executor, _, ctx, _ = setup("linux_deleted_open", "RB-009")
    plan = plan_for(svc, ctx, "delete_apache_logs_older_than", days=7)
    report = svc.check(plan, ctx)
    assert report.failed_gate == "dry_run" and "nothing to change" in report.reason


def test_kill_switch_and_instance_state() -> None:
    svc, executor, _, ctx, _ = setup("disk_iis_logs", "RB-001")
    plan = plan_for(svc, ctx, "delete_iis_logs_older_than", days=7)
    svc.kill_switch = lambda: True
    assert "kill switch" in svc.check(plan, ctx).reason
    svc.kill_switch = lambda: False
    executor.host(ctx.instance).state = "upgrading"
    assert "upgrading" in svc.check(plan, ctx).reason
    assert executor.applied == []


def test_tier1_once_per_instance_per_hour() -> None:
    svc, executor, _, ctx, clock = setup("linux_oom", "RB-012", "critical")
    svc.execute(plan_for(svc, ctx, "graceful_reload_service", service="apache2"), ctx)
    clock.now += timedelta(minutes=30)
    second = plan_for(svc, ctx, "graceful_reload_service", service="apache2")
    assert "one action per instance per hour" in svc.check(second, ctx).reason
    clock.now += timedelta(minutes=31)
    assert svc.check(
        second := plan_for(svc, ctx, "graceful_reload_service", service="apache2"), ctx
    ).ok
    assert len(executor.applied) == 1 and second.action == "graceful_reload_service"


def test_cooldown_unless_critical() -> None:
    svc, _, _, ctx, clock = setup("disk_iis_logs", "RB-001", "warning")
    svc.execute(plan_for(svc, ctx, "clear_temp", days=7), ctx)
    clock.now += timedelta(minutes=10)
    later = plan_for(svc, ctx, "delete_iis_logs_older_than", days=7)
    assert "cooldown" in svc.check(later, ctx).reason
    critical = IncidentContext(**{**ctx.__dict__, "severity": "critical"})
    assert svc.check(later, critical).ok


# ---------------- Tier 2: impossible without a matching human approval ----------------


@pytest.mark.parametrize(
    "approval",
    [
        None,
        Approval("agent", "HASH", "approve"),
        Approval("", "HASH", "approve"),
        Approval("j.ortiz", "0000000000000000", "approve"),
        Approval("j.ortiz", "HASH", "reject"),
    ],
    ids=["none", "agent", "blank", "wrong-hash", "rejected"],
)
def test_tier2_never_runs_without_valid_approval(approval: Approval | None) -> None:
    svc, executor, audit, ctx, _ = setup("disk_freb", "RB-002")
    plan = plan_for(svc, ctx, "delete_paths", paths=["C:\\inetpub\\logs\\FailedReqLogFiles"])
    assert svc.check(plan, ctx).needs_approval
    if approval is not None and approval.plan_hash == "HASH":
        approval = Approval(approval.approver_id, plan.plan_hash, approval.decision)
    with pytest.raises(GateError) as err:
        svc.execute(plan, ctx, approval)
    assert err.value.report.failed_gate == "approval"
    assert executor.applied == []
    assert [r.phase for r in audit.records] == ["blocked"]


def test_tier2_runs_with_a_matching_human_approval() -> None:
    svc, executor, audit, ctx, _ = setup("disk_freb", "RB-002")
    plan = plan_for(svc, ctx, "delete_paths", paths=["C:\\inetpub\\logs\\FailedReqLogFiles"])
    approval = Approval("j.ortiz", plan.plan_hash, "approve", "FREB left on, safe to remove")
    svc.record_approval(plan, ctx, approval)
    before = executor.read_metric(ctx.instance, "disk_used_pct")
    svc.execute(plan, ctx, approval)
    assert svc.verify(plan, ctx, before).recovered
    assert [r.phase for r in audit.records] == ["approval", "before", "after", "verify"]
    assert {r.actor for r in audit.records[1:3]} == {"j.ortiz"}


def test_tier2_delete_still_obeys_never_rules() -> None:
    svc, _, _, ctx, _ = setup("disk_freb", "RB-002")
    with pytest.raises(AllowlistError):
        svc.dry_run(ctx, "delete_paths", {"paths": ["C:\\inetpub\\wwwroot"]})


# ---------------- Linux ----------------


def test_linux_reload_releases_deleted_open_space() -> None:
    svc, executor, _, ctx, _ = setup("linux_deleted_open", "RB-010", "critical")
    plan = plan_for(svc, ctx, "graceful_reload_service", service="apache2")
    before = executor.read_metric(ctx.instance, "disk_used_pct")
    result = svc.execute(plan, ctx)
    assert result["released_gb"] == pytest.approx(20.4)
    assert svc.verify(plan, ctx, before).recovered


def test_service_fix_that_does_not_hold_fails_verification() -> None:
    spec = load("pool_crash")
    spec["sim"] = {"recurs_after_fix": True}  # StackOverflow bug: the pool crashes again
    executor = SimExecutor([host_from_case(spec)])
    svc = ActionService(executor, InMemoryAuditLog(), clock=Clock(), kill_switch=lambda: False)
    inc = spec["incident"]
    ctx = IncidentContext(
        "i", "r", inc["instance"], "windows", "web_down", "critical", "RB-003", ("RB-003",)
    )
    plan = plan_for(svc, ctx, "recycle_app_pool", pool="DefaultAppPool")
    svc.execute(plan, ctx)
    assert not svc.verify(plan, ctx, 0.0).recovered


# ---------------- Postgres audit log is append-only ----------------


def test_postgres_audit_log_is_append_only() -> None:
    try:
        conn = psycopg.connect(DEFAULT_DSN, autocommit=True)
    except psycopg.OperationalError:
        pytest.skip("Postgres not running")
    schema = f"test_{uuid.uuid4().hex[:8]}"
    conn.execute(f'CREATE SCHEMA "{schema}"')
    conn.execute(f'SET search_path TO "{schema}"')
    try:
        spec = load("disk_iis_logs")
        executor = SimExecutor([host_from_case(spec)])
        audit = PostgresAuditLog(conn)
        svc = ActionService(executor, audit, clock=Clock(), kill_switch=lambda: False)
        inc = spec["incident"]
        ctx = IncidentContext(
            "i", "r", inc["instance"], "windows", "disk_filling", "critical", "RB-001", ("RB-001",)
        )
        svc.execute(plan_for(svc, ctx, "clear_temp", days=7), ctx)
        assert len(audit.actions_since(ctx.instance, T0 - timedelta(hours=1))) == 1
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute("UPDATE audit_log SET ok = false")
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute("DELETE FROM audit_log")
    finally:
        conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        conn.close()
