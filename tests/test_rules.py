from datetime import UTC, datetime, timedelta

from fleet.detector.rules import disk_rule, evaluate, probe_rule, slope_per_hour
from fleet.detector.samples import (
    DISK_USED_PCT,
    PROBE_OK,
    FakeSource,
    Sample,
    fails_from,
    ramp,
    steady,
    ticks,
)

NOW = datetime(2026, 12, 1, 12, 0, tzinfo=UTC)
H = timedelta(hours=1)


def disk(values_every_5min: list[float], end: datetime = NOW) -> list[Sample]:
    n = len(values_every_5min)
    return [
        Sample("web0", DISK_USED_PCT, end - timedelta(minutes=5 * (n - 1 - i)), v)
        for i, v in enumerate(values_every_5min)
    ]


def probes(results: list[float], end: datetime = NOW) -> list[Sample]:
    n = len(results)
    return [
        Sample("web0", PROBE_OK, end - timedelta(minutes=n - 1 - i), v)
        for i, v in enumerate(results)
    ]


def test_slope_per_hour() -> None:
    pts = [(NOW, 50.0), (NOW + H, 52.0), (NOW + 2 * H, 54.0)]
    assert slope_per_hour(pts) == 2.0


def test_healthy_disk_is_quiet() -> None:
    assert disk_rule("web0", disk([60.0] * 72), NOW) is None


def test_high_disk_warns_then_goes_critical() -> None:
    warn = disk_rule("web0", disk([91.0] * 12), NOW)
    assert warn is not None and warn.severity == "warning"
    crit = disk_rule("web0", disk([96.0] * 12), NOW)
    assert crit is not None and crit.severity == "critical"


def test_fast_growth_is_projected_full() -> None:
    # 60% -> 70% over 2 hours = 5%/h, so full in 6h: below 90% but inside the 12h horizon.
    values = [60 + i * (10 / 24) for i in range(25)]
    finding = disk_rule("web0", disk(values), NOW)
    assert finding is not None
    assert finding.scenario == "disk_filling"
    assert "projected full" in finding.reason
    assert finding.evidence["hours_to_full"] == 6.0


def test_slow_growth_is_quiet() -> None:
    # 1%/h from 60%: full in 40h, outside the horizon.
    values = [60 + i / 12 for i in range(25)]
    assert disk_rule("web0", disk(values), NOW) is None


def test_too_few_points_for_a_slope() -> None:
    # A jump over 3 samples is not a trend yet.
    assert disk_rule("web0", disk([60.0, 70.0, 80.0]), NOW) is None


def test_stale_disk_data_is_ignored() -> None:
    assert disk_rule("web0", disk([97.0] * 6, end=NOW - timedelta(hours=1)), NOW) is None


def test_two_failed_probes_are_not_enough() -> None:
    assert probe_rule("web0", probes([1, 1, 0, 1, 0]), NOW) is None


def test_three_failed_probes_in_five_minutes() -> None:
    finding = probe_rule("web0", probes([1, 0, 0, 0]), NOW)
    assert finding is not None
    assert finding.scenario == "web_down"
    assert finding.evidence["failed"] == 3


def test_failures_spread_over_ten_minutes_are_quiet() -> None:
    # Failures at -9, -7 and -1 minutes: only one inside the 5-minute window.
    results = [0.0, 1, 0, 1, 1, 1, 1, 1, 0, 1]
    assert probe_rule("web0", probes(results), NOW) is None


def test_evaluate_groups_by_instance() -> None:
    samples = [
        *disk([92.0] * 6),
        Sample("web1", DISK_USED_PCT, NOW, 40.0),
    ]
    findings = evaluate(samples, NOW)
    assert [(f.instance, f.scenario) for f in findings] == [("web0", "disk_filling")]


def test_ticks_never_repeat_across_windows() -> None:
    step = timedelta(minutes=5)
    a = list(ticks(NOW - 3 * step, NOW, step))
    b = list(ticks(NOW, NOW + 3 * step, step))
    assert a[-1] == NOW and b[0] == NOW + step


def test_fake_source_shapes() -> None:
    src = FakeSource(
        {
            ("web0", DISK_USED_PCT): ramp(50.0, NOW, per_hour=10.0),
            ("web0", PROBE_OK): fails_from(NOW + timedelta(minutes=2)),
            ("web1", DISK_USED_PCT): steady(30.0),
        }
    )
    samples = src.fetch(NOW, NOW + H)
    web0_disk = [s.value for s in samples if s.instance == "web0" and s.metric == DISK_USED_PCT]
    assert web0_disk[-1] == 60.0
    web0_probe = [s.value for s in samples if s.metric == PROBE_OK]
    assert web0_probe[:3] == [1.0, 0.0, 0.0]


def test_linux_host_labels_the_root_volume() -> None:
    from fleet.detector.samples import LINUX_APACHE

    finding = disk_rule("lx0", disk([92.0] * 6), NOW, LINUX_APACHE)
    assert finding is not None and finding.reason == "/ 92% used"
    assert finding.host.os == "linux" and finding.host.web_server == "apache"


def test_evaluate_uses_the_inventory() -> None:
    from fleet.detector.samples import LINUX_APACHE

    samples = [Sample("lx0", PROBE_OK, NOW - timedelta(minutes=i), 0.0) for i in range(3)]
    findings = evaluate(samples, NOW, {"lx0": LINUX_APACHE})
    assert [(f.scenario, f.host.os) for f in findings] == [("web_down", "linux")]
