"""Live trading module - daemon and manual check commands."""

# Avoid hard import failures when optional runtime dependencies (e.g. ib_insync)
# are unavailable in unit-test environments.
try:
    from aurel2.live.daemon import LiveDaemon
    from aurel2.live.checker import Checker
    from aurel2.live.executor import Executor
    from aurel2.live.pending import PendingManager
    from aurel2.live.connection import IBKRConnection
except Exception:  # pragma: no cover - optional dependency guard
    LiveDaemon = None
    Checker = None
    Executor = None
    PendingManager = None
    IBKRConnection = None

__all__ = ["LiveDaemon", "Checker", "Executor", "PendingManager", "IBKRConnection"]
