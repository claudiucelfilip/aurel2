"""ONE canonical config artifact for the live decision spine.

Both `aurel2.live.checker.Checker` and `aurel2.engine.backtest.BacktestEngine`
build their strategy/orchestrator instances from `CANONICAL_CONFIG` — the
single source of truth for the universe, dual-momentum thresholds, and
orchestrator toggles. Before this module existed, checker.py hardcoded these
values independently of `config/default.yaml` and the backtest engine
duplicated checker.py's hardcoding in a second place; the three could (and
did) drift apart, which is exactly the backtest-vs-live mismatch class the
edge-decomposition goal's Phase 2 non-negotiable #1 requires eliminating
(docs/plans/2026-07-09-edge-decomposition-goal.md).

A frozen dataclass (not YAML) so the values are type-checked, importable
without a parse step, and impossible to accidentally diverge into two files.
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from aurel2.core.models import AssetClass

if TYPE_CHECKING:
    from aurel2.core.models import Asset

# The live universe excludes long-duration treasuries (TLT): the momentum
# strategy's own research found no edge from including them. See
# checker.py's original 2026-05-07 note and data/cadence_filter_revalidation_may2026.json.
EXCLUDED_ASSET_CLASSES: tuple[AssetClass, ...] = (AssetClass.BONDS_TREASURY,)


@dataclass(frozen=True)
class DualMomentumConfig:
    """Dual-momentum strategy parameters — the sole live trading signal."""

    lookback_months: int = 12
    switch_threshold: float = 0.02
    cash_rate: float = 0.0


@dataclass(frozen=True)
class OrchestratorConfig:
    """AgentOrchestrator parameters used by both live and backtest."""

    correlation_guard_enabled: bool = True
    correlation_threshold: float = 0.50
    sideways_hold_enabled: bool = True
    sideways_hold_momentum_threshold: float = 0.20


@dataclass(frozen=True)
class OverlaySettings:
    """AI overlay structural settings (Track C fills in real values/params).

    Structure only, per docs/plans/2026-07-10-ai-overlay-design.md: the tilt
    file schema, capped transition powers, and majority-of-5 sampling. This
    section exists so the canonical config is the single place both the
    live checker and backtest engine look up overlay wiring once Track C
    lands — no separate overlay config file to drift out of sync.
    """

    tilt_file_path: str = "data/{mode}/overlay_tilt.json"
    enabled: bool = False

    # Frozen by the single ablation step (docs/plans/2026-07-10-context-pack-spec.md):
    # v2 = v1 + per-asset RSI(14)/z-scores + momentum-spread alarm. v3 cut.
    context_pack_version: str = "v2"

    # Power 1: accelerate entry into the core's own next pick.
    # Disabled for the parallel run (Claudiu, 2026-07-10): the overlay-ON replay
    # showed one wrong acceleration (XLE detour, -3.5pp vs bare) from acting on
    # a projection the concurrent lookback override had destabilized. Requests
    # are still journaled as ignored (shadow log) so the run produces evidence
    # for re-enabling behind a worth-it hurdle.
    accelerate_entry_enabled: bool = False
    accelerate_entry_max_per_days: int = 21

    # Power 2: temporary lookback override (3m or 6m instead of 12m).
    lookback_override_max_consecutive_days: int = 30
    lookback_override_max_per_quarter: int = 2

    # Power 3: force a defensive-only contest against the held asset.
    force_defensive_contest_max_per_days: int = 14
    force_defensive_contest_universe: tuple[str, ...] = ("GLD", "AGG", "SHY", "IEF", "TIP", "CASH")

    # Majority-of-5 sampling.
    sample_count: int = 5
    sample_agreement_threshold: int = 3  # of sample_count, to activate a power


@dataclass(frozen=True)
class CanonicalConfig:
    """The single config artifact loaded by both the live checker and the
    backtest engine. No trading parameter should be hardcoded or duplicated
    anywhere else — add it here first.
    """

    excluded_asset_classes: tuple[AssetClass, ...] = EXCLUDED_ASSET_CLASSES
    dual_momentum: DualMomentumConfig = field(default_factory=DualMomentumConfig)
    orchestrator: OrchestratorConfig = field(default_factory=OrchestratorConfig)
    overlay: OverlaySettings = field(default_factory=OverlaySettings)


CANONICAL_CONFIG = CanonicalConfig()


def live_asset_registry() -> "dict[AssetClass, Asset]":
    """The live/backtest trading universe: full registry minus excluded classes."""
    from aurel2.core.assets import ASSET_REGISTRY

    return {
        ac: asset
        for ac, asset in ASSET_REGISTRY.items()
        if ac not in CANONICAL_CONFIG.excluded_asset_classes
    }
