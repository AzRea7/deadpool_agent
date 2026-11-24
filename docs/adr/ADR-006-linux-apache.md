# ADR-006: Windows/IIS and Linux/Apache hosts

Status: Accepted · 2026-11-24 (added during phase 4, before the action layer)

## Context

The plan scoped the fleet to Windows/IIS. Linux with Apache is the other common web tier, and supporting it shows the design is about the loop, not one OS. Before phase 5 adds the action catalog was the cheapest point to add it: later, every action and test would bake in Windows assumptions.

## Decision

One loop, OS-specific edges. The host type (`os`: windows | linux, `web_server`: iis | apache) is recorded per instance and travels with every incident.

- **Detector:** rules are unchanged and OS-neutral. A host inventory on the metrics source supplies labels (`C:` or `/`). Scenario names are neutral: `disk_filling`, `web_down` (was `app_pool_down`). Incidents gained `os` and `web_server` columns.
- **Runbooks:** each carries `Platform: windows | linux | any`. Added RB-009 (Apache logs, journal, caches), RB-010 (unknown consumer, deleted-but-open files, inodes), RB-011 (Apache down or out of workers), RB-012 (memory leak, OOM killer). RB-005, RB-007 and RB-008 are shared. Search filters to the host's platform plus `any`, so a Linux disk never gets the IIS log runbook.
- **Diagnose tools:** OS-neutral names, OS-specific sources. `system_log_tail` reads the Windows event logs or the systemd journal and Apache error log; `web_log_summary` reads IIS or Apache logs; `web_server_state` returns app pools and WAS/W3SVC, or the apache2 unit, `apachectl configtest` and worker use; `disk_usage_scan` adds `df` vs `du` and deleted-but-open files on Linux. Each result names its host type and source. The model still never writes queries or shell.
- **Agent:** one graph and prompt for both; the prompt says the incident's `os` decides the vocabulary. The first runbook search expands the terse scenario label into the platform's words.
- **Baseline:** `baseline/bicep/linux.bicep` adds an Ubuntu 24.04 / Apache scale set next to the Windows one, reusing its network, workspace, action group and budget. Same boring fixes: Apache logs on a data disk, logrotate (daily, keep 7), a journal size cap, an hourly retention timer (`baseline/log_retention.sh`), automatic repairs via the Apache health probe, and a disk-used alert.
- **Actions (phase 5):** catalog entries are keyed by OS from the start, for example `delete_iis_logs_older_than` and `delete_apache_logs_older_than`, `recycle_app_pool` and `graceful_reload_service(apache2)`, run through `RunPowerShellScript` or `RunShellScript`. Tiers, allowlists, dry-run hashes, approvals, audit and verification are shared.

## Alternatives

- **A separate agent per OS.** Rejected: duplicates the graph, the citation rule, the checkpointing and the tests, and the two would drift.
- **Let the model write shell or KQL for Linux.** Rejected for the same reasons as on Windows: unbounded blast radius and an injection surface.
- **Unfiltered search over all runbooks.** Rejected: the closest Windows and Linux runbooks share vocabulary (disk full, 503), and a wrong-OS citation would pick the wrong action later.

## Consequences

- Retrieval on 42 questions (30 Windows, 12 Linux), section chunks, platform-filtered: hit rate @3 1.00, MRR @3 0.92, top-1 0.86 (Windows 0.87, Linux 0.83). The phase 2 Windows figures moved slightly (top-1 0.90 to 0.87) because RB-005 now also describes Apache errors.
- Eight recorded incidents (four per OS). In `linux_oom` the first search returns RB-011, not RB-012, on purpose: the agent has to find the OOM-killer evidence and search again before the citation rule accepts RB-012.
- More surface to keep honest: every new runbook, tool and action needs a counterpart or an explicit "Windows only" / "Linux only" note. RHEL/httpd paths are mentioned in runbooks but the baseline and fixtures use Ubuntu/apache2.
- The Linux baseline is untested on Azure, like the Windows one, until the subscription is available.
