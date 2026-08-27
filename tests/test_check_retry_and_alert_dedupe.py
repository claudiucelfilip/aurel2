"""Failed daily checks retry same-day (bounded); the monitor alerts once per failure."""

from datetime import datetime, time as dt_time, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytz

from aurel2.live.daemon import LiveDaemon
from aurel2.monitor.daemon_monitor import DaemonMonitor
from aurel2.monitor.health_checker import HealthReport, HealthStatus

TZ = pytz.timezone("America/New_York")


def make_daemon(**overrides):
    d = LiveDaemon.__new__(LiveDaemon)
    d.check_time = dt_time(10, 30)
    d.timezone = TZ
    d.check_retry_interval = timedelta(minutes=30)
    d.max_check_retries = 6
    d._check_retry_count = 0
    d._next_retry_at = None
    d._last_check = None
    d._last_check_success = None
    d._last_check_message = None
    for k, v in overrides.items():
        setattr(d, k, v)
    return d


def wed(hour, minute=0):
    return TZ.localize(datetime(2026, 8, 26, hour, minute))  # a Wednesday


class TestDaemonRetry:
    def test_no_rerun_after_success(self):
        d = make_daemon(_last_check=wed(10, 31), _last_check_success=True)
        assert d._should_run_check(wed(12)) is False

    def test_failed_check_retries_after_interval(self):
        d = make_daemon(_last_check=wed(10, 31), _last_check_success=False)
        d._schedule_check_retry(wed(10, 31))
        assert d._should_run_check(wed(10, 45)) is False  # not yet
        assert d._should_run_check(wed(11, 2)) is True

    def test_retry_budget_exhausts(self):
        d = make_daemon(_last_check=wed(10, 31), _last_check_success=False)
        for i in range(6):
            d._schedule_check_retry(wed(11, i))
            assert d._next_retry_at is not None
        d._schedule_check_retry(wed(12))  # 7th failure
        assert d._next_retry_at is None
        assert d._should_run_check(wed(13)) is False

    def test_next_day_scheduled_check_still_runs(self):
        d = make_daemon(_last_check=wed(10, 31), _last_check_success=False)
        d._next_retry_at = None  # budget exhausted yesterday
        nxt = TZ.localize(datetime(2026, 8, 27, 10, 31))
        assert d._should_run_check(nxt) is True


def make_monitor():
    m = DaemonMonitor.__new__(DaemonMonitor)
    m._alerted_failed_check = None
    m.notifier = MagicMock()
    return m


def make_report(issues, last_check_success):
    return HealthReport(
        status=HealthStatus.DEGRADED if issues else HealthStatus.HEALTHY,
        timestamp=datetime.now(),
        process=SimpleNamespace(running=True),
        heartbeat=SimpleNamespace(last_check_success=last_check_success),
        log_errors=SimpleNamespace(error_count=0),
        issues=issues,
    )


class TestMonitorDedupe:
    FAIL = "Last scheduled check failed: Failed to fetch prices: Missing completed history for: SPY"

    def test_first_failure_passes_through(self):
        m = make_monitor()
        report = make_report([self.FAIL], last_check_success=False)
        m._dedupe_failed_check(report)
        assert self.FAIL in report.issues  # first sighting alerts normally
        assert m._alerted_failed_check == self.FAIL

    def test_repeat_failure_suppressed(self):
        m = make_monitor()
        m._alerted_failed_check = self.FAIL
        report = make_report([self.FAIL], last_check_success=False)
        m._dedupe_failed_check(report)
        assert report.issues == []
        assert report.status == HealthStatus.HEALTHY

    def test_repeat_suppression_keeps_other_issues(self):
        m = make_monitor()
        m._alerted_failed_check = self.FAIL
        report = make_report([self.FAIL, "Broker disconnected"], last_check_success=False)
        m._dedupe_failed_check(report)
        assert report.issues == ["Broker disconnected"]
        assert report.status == HealthStatus.DEGRADED

    def test_new_distinct_failure_alerts_again(self):
        m = make_monitor()
        m._alerted_failed_check = self.FAIL
        other = "Last scheduled check failed: Broker connection unavailable"
        report = make_report([other], last_check_success=False)
        m._dedupe_failed_check(report)
        assert other in report.issues
        assert m._alerted_failed_check == other

    def test_recovery_notifies_once(self):
        m = make_monitor()
        m._alerted_failed_check = self.FAIL
        report = make_report([], last_check_success=True)
        m._dedupe_failed_check(report)
        assert m._alerted_failed_check is None
        assert m.notifier.send.call_count == 1
        assert "Recovered" in m.notifier.send.call_args.kwargs["title"]
        m._dedupe_failed_check(make_report([], last_check_success=True))
        assert m.notifier.send.call_count == 1  # no second notice
