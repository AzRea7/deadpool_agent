"""Executors run catalog actions on an instance.

Executor is the interface. SimExecutor runs actions against simulated hosts built from the
recorded incidents, so dry runs list real targets, fixes change disk and service state, and
verification reads real numbers. The Azure executor (Run Command with a managed identity scoped
to the scale set) implements the same interface in phase 6, using render_script().
"""

import copy
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from fleet.governance import catalog
from fleet.governance.allowlist import DEFAULT, Allowlist, AllowlistError

Metric = Literal["disk_used_pct", "probe_ok"]


class StaleDryRun(RuntimeError):
    """The host changed since the dry run: the approved plan no longer describes reality."""


class Executor(Protocol):
    def dry_run_targets(
        self, instance: str, action: str, params: dict[str, Any]
    ) -> list[dict[str, Any]]: ...
    def apply(self, instance: str, action: str, params: dict[str, Any]) -> dict[str, Any]: ...
    def read_metric(self, instance: str, metric: Metric) -> float: ...
    def instance_state(self, instance: str) -> str: ...


def render_script(spec: catalog.ActionSpec, params: dict[str, Any], apply: bool) -> str:
    """Fill a fixed template with already-validated parameters. Dry-run mode unless apply."""
    values = dict(params)
    values.setdefault("whatif", "" if apply else "-WhatIf")
    values.setdefault("apply", "--apply" if apply else "")
    values.setdefault("find_action", "-delete" if apply else "-print")
    for key in ("resource_group", "vmss", "instance_id", "new_capacity"):
        values.setdefault(key, f"<{key}>")
    return spec.script.format(**values).strip()


# ---------------------------------------------------------------- simulated hosts


@dataclass
class SimFile:
    path: str
    gb: float
    age_days: float
    open: bool = False  # the web server has it open (current log)
    symlink: bool = False


@dataclass
class SimHost:
    instance: str
    os: str
    size_gb: float
    other_used_gb: float  # used space not in modelled files (OS, apps)
    files: list[SimFile] = field(default_factory=list)
    held_deleted_gb: float = 0.0  # deleted files still held open by a process
    services: dict[str, str] = field(default_factory=dict)  # name -> Running/Stopped/active/failed
    pools: dict[str, str] = field(default_factory=dict)  # IIS app pools -> Started/Stopped
    probe_ok: bool = True
    state: str = "running"  # running | upgrading | deleting
    recurs_after_fix: bool = False  # the root cause is still there after a restart

    @property
    def sep(self) -> str:
        return "\\" if self.os == "windows" else "/"

    @property
    def used_gb(self) -> float:
        return self.other_used_gb + sum(f.gb for f in self.files) + self.held_deleted_gb

    @property
    def used_pct(self) -> float:
        return round(100 * self.used_gb / self.size_gb, 1)

    def under(self, root: str, path: str) -> bool:
        if self.os == "windows":
            root, path = root.lower(), path.lower()
        return path == root or path.startswith(root.rstrip(self.sep) + self.sep)


def host_from_case(case: dict[str, Any]) -> SimHost:
    """Build a simulated host from a recorded incident (evals/incidents/*.json)."""
    inc = case["incident"]
    instance, os_ = inc["instance"], inc.get("os", "windows")
    scan = case.get("disk_scan", {}).get(instance, {})
    size = float(scan.get("df_size_gb", 64.0))
    disk = case.get("metrics", {}).get(f"{instance}|disk_used_pct", [])
    used_pct = float(inc.get("evidence", {}).get("used_pct", disk[-1][1] if disk else 50.0))
    sep = "\\" if os_ == "windows" else "/"

    files: list[SimFile] = []
    for folder in scan.get("folders", []):
        n = max(1, min(int(folder.get("files", 1)), 12))
        if folder["gb"] <= 0:
            continue
        oldest = float(folder.get("oldest_days", 0))
        apache = folder["path"].endswith("apache2") or folder["path"].endswith("apache-logs")
        for i in range(n):
            age = oldest * i / (n - 1) if n > 1 else oldest
            name = (f"access.log.{i}" if i else "access.log") if apache else f"file{i:03d}.log"
            files.append(
                SimFile(
                    f"{folder['path']}{sep}{name}", folder["gb"] / n, age, open=apache and i == 0
                )
            )
    held = sum(float(f["gb"]) for f in scan.get("deleted_open_files", []))
    other = max(0.0, size * used_pct / 100 - sum(f.gb for f in files) - held)

    state = case.get("web_server", {}).get(instance, {})
    services: dict[str, str] = {}
    pools: dict[str, str] = {}
    if os_ == "windows":
        services = {s["name"]: s["status"] for s in state.get("services", [])}
        pools = {p["name"]: p["state"] for p in state.get("app_pools", [])}
    elif "unit" in state:
        services = {
            state["unit"].removesuffix(".service"): state.get("active", "active").split()[0]
        }
    probes = case.get("metrics", {}).get(f"{instance}|probe_ok", [])
    return SimHost(
        instance=instance,
        os=os_,
        size_gb=size,
        other_used_gb=other,
        files=files,
        held_deleted_gb=held,
        services=services,
        pools=pools,
        probe_ok=bool(probes[-1][1]) if probes else True,
        recurs_after_fix=bool(case.get("sim", {}).get("recurs_after_fix", False)),
    )


class SimExecutor:
    """Runs catalog actions against simulated hosts. Enforces the allowlist on every target."""

    def __init__(self, hosts: list[SimHost], allowlist: Allowlist = DEFAULT) -> None:
        self.hosts = {h.instance: h for h in hosts}
        self.allowlist = allowlist
        self.applied: list[tuple[str, str, dict[str, Any]]] = []  # what actually ran

    def host(self, instance: str) -> SimHost:
        if instance not in self.hosts:
            raise KeyError(f"unknown instance {instance!r}")
        return self.hosts[instance]

    def snapshot(self) -> dict[str, SimHost]:
        return copy.deepcopy(self.hosts)

    # ---- targets ----

    def _file_targets(self, host: SimHost, roots: tuple[str, ...], pred: Any) -> list[SimFile]:
        out = []
        for f in host.files:
            if f.symlink or f.open or not any(host.under(r, f.path) for r in roots):
                continue
            if pred(f):
                out.append(f)
        return out

    def _delete_targets(self, host: SimHost, action: str, p: dict[str, Any]) -> list[SimFile]:
        older = lambda f: f.age_days > p.get("days", 0)  # noqa: E731
        everything = lambda f: True  # noqa: E731
        roots: dict[str, tuple[tuple[str, ...], Any]] = {
            "delete_iis_logs_older_than": (("C:\\inetpub\\logs\\LogFiles", "F:\\iislogs"), older),
            "clear_temp": (("C:\\Windows\\Temp",), older),
            "clear_wer_dumps": (("C:\\ProgramData\\Microsoft\\Windows\\WER",), everything),
            "delete_apache_logs_older_than": (("/var/log/apache2", "/data/apache-logs"), older),
            "clean_package_cache": (("/var/cache/apt",), everything),
            "clear_tmp_older_than": (("/tmp",), older),
        }
        if action == "journal_vacuum":
            journal = sorted(
                self._file_targets(host, ("/var/log/journal",), everything),
                key=lambda f: -f.age_days,
            )
            excess, picked = sum(f.gb for f in journal) - p["max_mb"] / 1024, []
            for f in journal:
                if excess <= 0:
                    break
                picked.append(f)
                excess -= f.gb
            return picked
        if action == "delete_paths":
            for path in p["paths"]:
                self.allowlist.check_never(path)
            return [
                f
                for f in host.files
                if not f.symlink and not f.open and any(host.under(x, f.path) for x in p["paths"])
            ]
        root_list, pred = roots[action]
        return self._file_targets(host, root_list, pred)

    def dry_run_targets(
        self, instance: str, action: str, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        host = self.host(instance)
        spec = catalog.get(action)
        if spec.kind == "delete":
            files = self._delete_targets(host, action, params)
            for f in files:  # defense in depth: re-check every single target
                self.allowlist.check_never(f.path)
                if spec.tier < 2 and not self.allowlist.under_auto_root(host.os, f.path):
                    raise AllowlistError(f"{f.path!r} is outside the automatic delete roots")
            return [
                {"path": f.path, "gb": round(f.gb, 3), "age_days": round(f.age_days, 1)}
                for f in sorted(files, key=lambda f: f.path)
            ]
        if spec.kind == "service":
            if action == "recycle_app_pool":
                self.allowlist.check_service(host.os, "pools", params["pool"])
                return [
                    {"pool": params["pool"], "state": host.pools.get(params["pool"], "missing")}
                ]
            if action == "restart_iis_services":
                return [
                    {"service": s, "state": host.services.get(s, "missing")}
                    for s in ("WAS", "W3SVC")
                ]
            self.allowlist.check_service(host.os, "services", params["service"])
            target = {
                "service": params["service"],
                "state": host.services.get(params["service"], "missing"),
            }
            if action == "graceful_reload_service":
                target["held_deleted_gb"] = round(host.held_deleted_gb, 3)
            return [target]
        return [{"instance": instance, "operation": action, **params}]

    # ---- apply ----

    def apply(self, instance: str, action: str, params: dict[str, Any]) -> dict[str, Any]:
        host = self.host(instance)
        spec = catalog.get(action)
        before = host.used_pct
        result: dict[str, Any] = {}
        if spec.kind == "delete":
            doomed = {t["path"] for t in self.dry_run_targets(instance, action, params)}
            freed = sum(f.gb for f in host.files if f.path in doomed)
            host.files = [f for f in host.files if f.path not in doomed]
            result = {"deleted_files": len(doomed), "freed_gb": round(freed, 3)}
        elif action == "recycle_app_pool":
            host.pools[params["pool"]] = "Started"
            host.probe_ok = not host.recurs_after_fix
        elif action == "restart_iis_services":
            host.services.update({"WAS": "Running", "W3SVC": "Running"})
            host.pools = {k: "Started" for k in host.pools}
            host.probe_ok = not host.recurs_after_fix
        elif action in ("graceful_reload_service", "restart_service"):
            result = {"released_gb": round(host.held_deleted_gb, 3)}
            host.held_deleted_gb = 0.0
            host.services[params["service"]] = "active"
            host.probe_ok = not host.recurs_after_fix
        elif action in ("reimage_instance", "rollback_deploy"):
            host.probe_ok = True
        self.applied.append((instance, action, params))
        return {**result, "used_pct_before": before, "used_pct_after": host.used_pct}

    def read_metric(self, instance: str, metric: Metric) -> float:
        host = self.host(instance)
        return host.used_pct if metric == "disk_used_pct" else float(host.probe_ok)

    def instance_state(self, instance: str) -> str:
        return self.host(instance).state
