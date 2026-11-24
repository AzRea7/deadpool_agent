"""ActionService: the only way an action runs. Gates, authorization, audit and verification.

Gates, in order, for every action (the plan's safety section):
  1 catalog      the action exists, runs on this platform, parameters validate
  2 allowlist    services and paths are allowed; never-delete rules hold
  3 citation     the diagnosis runbook was retrieved and allows this action
  4 dry run      a fresh dry run matches the plan hash and finds something to change
  5 blast radius kill switch, instance state, one instance, rate limit, hysteresis
then by tier:  0/1 run under policy; 2 needs an approval naming the exact plan hash.
Every block, approval, execution and verification writes an audit row. execute() re-runs all
gates itself, so nothing a caller does (or skips) can run an unapproved Tier 2 action.
"""

import os
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fleet.governance import catalog
from fleet.governance.allowlist import DEFAULT, Allowlist, AllowlistError
from fleet.governance.audit import AuditLog, AuditRecord, Phase
from fleet.governance.dryrun import DryRun
from fleet.governance.executor import Executor, Metric

Gate = Literal["catalog", "allowlist", "citation", "dry_run", "blast_radius", "approval"]
SYSTEM_ACTORS = {"", "agent", "policy", "system"}


@dataclass(frozen=True)
class IncidentContext:
    incident_id: str
    run_id: str
    instance: str
    platform: str  # windows | linux
    scenario: str  # disk_filling | web_down
    severity: str  # warning | critical
    runbook_id: str  # from the accepted diagnosis
    retrieved: tuple[str, ...]  # runbook IDs search_runbook returned (citation allowlist)


@dataclass(frozen=True)
class Plan:
    instance: str
    action: str
    params: dict[str, Any]
    runbook_id: str
    plan_hash: str
    dry_run_at: datetime
    rationale: str


@dataclass(frozen=True)
class Approval:
    approver_id: str
    plan_hash: str
    decision: Literal["approve", "reject"]
    note: str = ""


@dataclass(frozen=True)
class GateReport:
    ok: bool
    tier: int
    failed_gate: Gate | None = None
    reason: str = ""
    dry_run: dict[str, Any] = field(default_factory=dict)

    @property
    def needs_approval(self) -> bool:
        return self.ok and self.tier == 2


@dataclass(frozen=True)
class Verification:
    metric: str
    before: float
    after: float
    recovered: bool
    detail: str


class GateError(RuntimeError):
    def __init__(self, report: GateReport) -> None:
        super().__init__(f"{report.failed_gate}: {report.reason}")
        self.report = report


def _kill_switch_from_env() -> bool:
    return os.environ.get("FLEET_KILL_SWITCH", "") == "1"


class ActionService:
    def __init__(
        self,
        executor: Executor,
        audit: AuditLog,
        allowlist: Allowlist = DEFAULT,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        kill_switch: Callable[[], bool] = _kill_switch_from_env,
        max_plan_age: timedelta = timedelta(minutes=15),
        cooldown: timedelta = timedelta(minutes=60),
    ) -> None:
        self.executor = executor
        self.audit = audit
        self.allowlist = allowlist
        self.clock = clock
        self.kill_switch = kill_switch
        self.max_plan_age = max_plan_age
        self.cooldown = cooldown

    # ---------------- read side (what the planner may use) ----------------

    def list_actions(self, platform: str, runbook_id: str) -> list[dict[str, Any]]:
        return [catalog.describe(a) for a in catalog.actions_for(platform, runbook_id)]

    def dry_run(self, ctx: IncidentContext, action: str, params: dict[str, Any]) -> DryRun:
        """Validate and dry-run an action on the incident's own instance. Raises on bad input."""
        spec = catalog.get(action)
        if spec.platform not in (ctx.platform, "any"):
            raise catalog.CatalogError(f"{action} does not run on {ctx.platform}")
        clean = catalog.validate_params(spec, params)
        targets = self.executor.dry_run_targets(ctx.instance, action, clean)
        return DryRun.of(ctx.instance, action, clean, targets, self.clock())

    # ---------------- gates ----------------

    def check(self, plan: Plan, ctx: IncidentContext) -> GateReport:
        """Gates 1-5. Writes a 'blocked' audit row when a gate fails."""
        report = self._gates(plan, ctx)
        if not report.ok:
            self._audit(
                plan,
                ctx,
                report.tier,
                "blocked",
                "agent",
                False,
                report.reason,
                {"failed_gate": report.failed_gate},
            )
        return report

    def _gates(self, plan: Plan, ctx: IncidentContext) -> GateReport:
        # 1 catalog
        try:
            spec = catalog.get(plan.action)
            if spec.platform not in (ctx.platform, "any"):
                raise catalog.CatalogError(f"{plan.action} does not run on {ctx.platform}")
            params = catalog.validate_params(spec, plan.params)
        except catalog.CatalogError as e:
            return GateReport(False, 2, "catalog", str(e))
        tier = spec.tier

        # 2 allowlist (parameter level; every target is re-checked inside the dry run)
        try:
            if plan.action == "recycle_app_pool":
                self.allowlist.check_service(ctx.platform, "pools", params["pool"])
            elif "service" in params:
                self.allowlist.check_service(ctx.platform, "services", params["service"])
            for path in params.get("paths", []):
                self.allowlist.check_never(path)
        except AllowlistError as e:
            return GateReport(False, tier, "allowlist", str(e))

        # 3 citation
        if plan.runbook_id != ctx.runbook_id or ctx.runbook_id not in ctx.retrieved:
            return GateReport(
                False,
                tier,
                "citation",
                f"plan cites {plan.runbook_id}, diagnosis cited {ctx.runbook_id}",
            )
        if ctx.runbook_id not in spec.runbooks:
            return GateReport(
                False, tier, "citation", f"{ctx.runbook_id} does not allow {plan.action}"
            )

        # 4 dry run: fresh, matching, and not empty
        if plan.instance != ctx.instance:
            return GateReport(
                False,
                tier,
                "blast_radius",
                f"plan targets {plan.instance}, incident is on {ctx.instance}",
            )
        try:
            fresh = self.dry_run(ctx, plan.action, params)
        except (AllowlistError, catalog.CatalogError, KeyError) as e:
            return GateReport(False, tier, "allowlist", str(e))
        if self.clock() - plan.dry_run_at > self.max_plan_age:
            return GateReport(
                False, tier, "dry_run", "plan is older than the dry-run limit", fresh.summary()
            )
        if fresh.hash != plan.plan_hash:
            return GateReport(
                False,
                tier,
                "dry_run",
                "host changed since the dry run (hash mismatch)",
                fresh.summary(),
            )
        if not fresh.targets:
            return GateReport(
                False, tier, "dry_run", "dry run found nothing to change", fresh.summary()
            )

        # 5 blast radius
        if self.kill_switch():
            return GateReport(
                False, tier, "blast_radius", "global kill switch is on", fresh.summary()
            )
        state = self.executor.instance_state(ctx.instance)
        if state != "running":
            return GateReport(False, tier, "blast_radius", f"instance is {state}", fresh.summary())
        recent = self.audit.actions_since(ctx.instance, self.clock() - self.cooldown)
        if tier == 1 and any(r.tier >= 1 for r in recent):
            return GateReport(
                False,
                tier,
                "blast_radius",
                "Tier 1 limit: one action per instance per hour",
                fresh.summary(),
            )
        if recent and ctx.severity != "critical" and tier < 2:
            return GateReport(
                False,
                tier,
                "blast_radius",
                "cooldown: instance was acted on in the last 60 minutes",
                fresh.summary(),
            )
        return GateReport(True, tier, dry_run=fresh.summary())

    # ---------------- approval, execution, verification ----------------

    def record_approval(self, plan: Plan, ctx: IncidentContext, approval: Approval) -> None:
        ok = approval.decision == "approve" and self._approval_problem(plan, approval) is None
        self._audit(
            plan,
            ctx,
            2,
            "approval",
            approval.approver_id or "unknown",
            ok,
            approval.note or approval.decision,
            {"decision": approval.decision},
        )

    def _approval_problem(self, plan: Plan, approval: Approval | None) -> str | None:
        if approval is None:
            return "Tier 2 action needs a human approval"
        if approval.decision != "approve":
            return f"approval decision was {approval.decision!r}"
        if approval.approver_id.strip().lower() in SYSTEM_ACTORS:
            return "approver must be a named human, not the system"
        if approval.plan_hash != plan.plan_hash:
            return "approval names a different plan hash"
        return None

    def execute(
        self, plan: Plan, ctx: IncidentContext, approval: Approval | None = None
    ) -> dict[str, Any]:
        report = self.check(plan, ctx)
        if not report.ok:
            raise GateError(report)
        actor = "policy"
        if report.tier == 2:
            problem = self._approval_problem(plan, approval)
            if problem:
                blocked = GateReport(False, 2, "approval", problem, report.dry_run)
                self._audit(
                    plan, ctx, 2, "blocked", "agent", False, problem, {"failed_gate": "approval"}
                )
                raise GateError(blocked)
            assert approval is not None
            actor = approval.approver_id
        self._audit(
            plan,
            ctx,
            report.tier,
            "before",
            actor,
            True,
            plan.rationale,
            {"dry_run": report.dry_run},
        )
        try:
            result = self.executor.apply(ctx.instance, plan.action, plan.params)
        except Exception as e:
            self._audit(plan, ctx, report.tier, "after", actor, False, f"error: {e}", {})
            raise
        self._audit(plan, ctx, report.tier, "after", actor, True, "applied", result)
        return result

    def read_metric(self, ctx: IncidentContext) -> float:
        """The metric that opened the incident, now. Read before acting, for verification."""
        return self.executor.read_metric(ctx.instance, self.metric_for(ctx))

    def metric_for(self, ctx: IncidentContext) -> Metric:
        return "disk_used_pct" if ctx.scenario == "disk_filling" else "probe_ok"

    def verify(self, plan: Plan, ctx: IncidentContext, before: float) -> Verification:
        """Re-read the metric that opened the incident. Recovered closes; else escalate."""
        metric = self.metric_for(ctx)
        after = self.executor.read_metric(ctx.instance, metric)
        if metric == "disk_used_pct":
            recovered = after < 90.0 and after < before
            detail = f"disk {before:.1f}% -> {after:.1f}% used (needs < 90% and lower)"
        else:
            recovered = after >= 1.0
            detail = "health probe passing" if recovered else "health probe still failing"
        verdict = Verification(metric, before, after, recovered, detail)
        tier = catalog.get(plan.action).tier
        self._audit(plan, ctx, tier, "verify", "policy", recovered, detail, asdict(verdict))
        return verdict

    # ---------------- audit ----------------

    def _audit(
        self,
        plan: Plan,
        ctx: IncidentContext,
        tier: int,
        phase: Phase,
        actor: str,
        ok: bool,
        reason: str,
        result: dict[str, Any],
    ) -> None:
        self.audit.append(
            AuditRecord(
                at=self.clock(),
                incident_id=ctx.incident_id,
                run_id=ctx.run_id,
                instance=ctx.instance,
                action=plan.action,
                tier=tier,
                phase=phase,
                actor=actor,
                runbook_id=plan.runbook_id,
                plan_hash=plan.plan_hash,
                ok=ok,
                reason=reason,
                params=dict(plan.params),
                result=result,
            )
        )
