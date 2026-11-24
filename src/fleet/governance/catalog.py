"""The action catalog: every remediation the system can perform, and nothing else (ADR-004).

Each action has a risk tier, the platforms it runs on, the runbooks that allow it, a Pydantic
schema for its parameters, and a fixed script template. The model chooses an action name and
parameters; it never writes PowerShell, shell or az commands. Parameters are validated before a
template is ever rendered, so a value cannot smuggle in extra commands.

Tiers:
  0  automatic: pre-approved by policy; still dry-run, hash check, audit and verification
  1  automatic with limits: one instance, at most once per instance per hour
  2  human: pauses for an approver ID and the exact plan hash
Never: deleting app content, wwwroot, uploads, databases or user profiles is not in the catalog
and is refused by the allowlist even inside delete_paths.
"""

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

Platform = Literal["windows", "linux", "any"]
Kind = Literal["delete", "service", "control_plane"]

NAME = r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$"  # pool, service and version names


class NoParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OlderThanDays(BaseModel):
    model_config = ConfigDict(extra="forbid")
    days: int = Field(ge=3, le=90, description="Delete only files older than this many days.")


class JournalVacuum(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_mb: int = Field(ge=200, le=2000, description="Shrink the journal to this size.")


class PoolName(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pool: str = Field(pattern=NAME)


class ServiceName(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service: str = Field(pattern=NAME)


class DeletePaths(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paths: list[str] = Field(min_length=1, max_length=10)


class ScaleOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count: int = Field(ge=1, le=2)


class RollbackDeploy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to_version: str = Field(pattern=NAME)


@dataclass(frozen=True)
class ActionSpec:
    name: str
    tier: Literal[0, 1, 2]
    platform: Platform
    kind: Kind
    params: type[BaseModel]
    runbooks: tuple[str, ...]  # runbooks whose "Allowed actions" include this action
    description: str
    script: str  # fixed template; only validated params are substituted


CATALOG: dict[str, ActionSpec] = {
    a.name: a
    for a in [
        # ---------------- Windows / IIS ----------------
        ActionSpec(
            "delete_iis_logs_older_than",
            0,
            "windows",
            "delete",
            OlderThanDays,
            ("RB-001",),
            "Delete IIS log files older than N days under the IIS log roots.",
            "Get-ChildItem 'C:\\inetpub\\logs\\LogFiles','F:\\iislogs' -Recurse -File -Filter *.log "
            "-ErrorAction SilentlyContinue | Where-Object LastWriteTime -lt (Get-Date).AddDays(-{days})"
            " | Where-Object {{ -not $_.Attributes.HasFlag([IO.FileAttributes]::ReparsePoint) }}"
            " | Remove-Item -Force {whatif}",
        ),
        ActionSpec(
            "clear_temp",
            0,
            "windows",
            "delete",
            OlderThanDays,
            ("RB-001",),
            "Delete files older than N days in C:\\Windows\\Temp.",
            "Get-ChildItem 'C:\\Windows\\Temp' -Recurse -File | Where-Object LastWriteTime -lt "
            "(Get-Date).AddDays(-{days}) | Remove-Item -Force {whatif}",
        ),
        ActionSpec(
            "clear_wer_dumps",
            0,
            "windows",
            "delete",
            NoParams,
            ("RB-001",),
            "Delete Windows Error Reporting crash dumps.",
            "Get-ChildItem 'C:\\ProgramData\\Microsoft\\Windows\\WER' -Recurse -File "
            "| Remove-Item -Force {whatif}",
        ),
        ActionSpec(
            "recycle_app_pool",
            1,
            "windows",
            "service",
            PoolName,
            ("RB-003", "RB-004"),
            "Recycle one IIS app pool on the affected instance (starts it if stopped).",
            "Import-Module WebAdministration; $p = '{pool}'; "
            "if ((Get-WebAppPoolState -Name $p).Value -eq 'Stopped') {{ Start-WebAppPool -Name $p }} "
            "else {{ Restart-WebAppPool -Name $p }}",
        ),
        ActionSpec(
            "restart_iis_services",
            1,
            "windows",
            "service",
            NoParams,
            ("RB-006",),
            "Start WAS, then W3SVC, on the affected instance.",
            "Restart-Service -Name WAS -Force; Start-Service -Name W3SVC",
        ),
        # ---------------- Linux / Apache ----------------
        ActionSpec(
            "delete_apache_logs_older_than",
            0,
            "linux",
            "delete",
            OlderThanDays,
            ("RB-009",),
            "Delete rotated Apache logs older than N days (never a log Apache has open).",
            "/opt/fleet/log_retention.sh --days {days} --log-root /var/log/apache2 {apply} && "
            "/opt/fleet/log_retention.sh --days {days} --log-root /data/apache-logs {apply}",
        ),
        ActionSpec(
            "journal_vacuum",
            0,
            "linux",
            "delete",
            JournalVacuum,
            ("RB-009",),
            "Shrink the systemd journal to a maximum size.",
            "journalctl --vacuum-size={max_mb}M",
        ),
        ActionSpec(
            "clean_package_cache",
            0,
            "linux",
            "delete",
            NoParams,
            ("RB-009",),
            "Remove downloaded package files from the apt cache.",
            "apt-get clean",
        ),
        ActionSpec(
            "clear_tmp_older_than",
            0,
            "linux",
            "delete",
            OlderThanDays,
            ("RB-009",),
            "Delete regular files in /tmp older than N days (no symlinks, no recursion above /tmp).",
            "find /tmp -xdev -type f -mtime +{days} {find_action}",
        ),
        ActionSpec(
            "graceful_reload_service",
            1,
            "linux",
            "service",
            ServiceName,
            ("RB-010", "RB-011", "RB-012"),
            "Gracefully reload a service: in-flight requests finish, logs are reopened.",
            "systemctl reload '{service}'",
        ),
        ActionSpec(
            "restart_service",
            1,
            "linux",
            "service",
            ServiceName,
            ("RB-011",),
            "Restart a failed or inactive service on the affected instance.",
            "apachectl configtest && systemctl restart '{service}'",
        ),
        # ---------------- Either platform, human approval ----------------
        ActionSpec(
            "delete_paths",
            2,
            "any",
            "delete",
            DeletePaths,
            ("RB-002", "RB-010"),
            "Delete specific files outside the automatic allowlist. Needs a human.",
            "<per-OS delete of the exact dry-run file list>",
        ),
        ActionSpec(
            "reimage_instance",
            2,
            "any",
            "control_plane",
            NoParams,
            ("RB-007",),
            "Reimage the instance's OS disk. Needs a human.",
            "az vmss reimage -g {resource_group} -n {vmss} --instance-ids {instance_id}",
        ),
        ActionSpec(
            "scale_out",
            2,
            "any",
            "control_plane",
            ScaleOut,
            ("RB-007",),
            "Add instances to the scale set while one is investigated. Needs a human.",
            "az vmss scale -g {resource_group} -n {vmss} --new-capacity {new_capacity}",
        ),
        ActionSpec(
            "rollback_deploy",
            2,
            "any",
            "control_plane",
            RollbackDeploy,
            ("RB-005",),
            "Redeploy the previous good version to the whole fleet. Needs a human.",
            "<trigger the release pipeline for version {to_version}>",
        ),
    ]
}


class CatalogError(ValueError):
    pass


def get(name: str) -> ActionSpec:
    if name not in CATALOG:
        raise CatalogError(f"{name!r} is not in the action catalog")
    return CATALOG[name]


def validate_params(spec: ActionSpec, params: dict[str, Any]) -> dict[str, Any]:
    try:
        return spec.params.model_validate(params).model_dump()
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())
        raise CatalogError(f"invalid parameters for {spec.name}: {problems}") from None


def actions_for(platform: str, runbook_id: str | None = None) -> list[ActionSpec]:
    """Actions that run on this platform (and, if given, that the runbook allows)."""
    return [
        a
        for a in CATALOG.values()
        if a.platform in (platform, "any") and (runbook_id is None or runbook_id in a.runbooks)
    ]


def describe(spec: ActionSpec) -> dict[str, Any]:
    """What the planner sees: no script, just the contract."""
    return {
        "action": spec.name,
        "tier": spec.tier,
        "platform": spec.platform,
        "description": spec.description,
        "params_schema": spec.params.model_json_schema(),
        "runbooks": list(spec.runbooks),
    }
