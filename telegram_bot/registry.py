"""
Strategy registry.

Only the 4H `market_flush` paper bot is live today. 2H and 1H slots are
wired as `state_file=None` stubs so `/status`, `/pnl`, `/trades` render
`⚪ not deployed` without crashing. When those strategies ship, flip on
`state_file` + `systemd_unit` + `holding_hours` and the rest of the
telegram_bot/ package picks them up automatically.
"""
from __future__ import annotations

from dataclasses import dataclass

from bot.config import BotConfig
from bot.paper_executor import PaperExecutor


@dataclass(frozen=True)
class StrategyEntry:
    key: str                         # short arg, e.g. "4h"
    label: str                       # human-readable, e.g. "4H market_flush"
    state_file: str | None           # path to paper_state_*.json, or None if stubbed
    systemd_unit: str | None         # e.g. "liq-paper-bot.service", or None
    holding_hours: int | None        # signal holding period, or None

    @property
    def is_deployed(self) -> bool:
        return self.state_file is not None


# Order matters — `/status` iterates in this order.
REGISTRY: list[StrategyEntry] = [
    StrategyEntry(
        key="4h",
        label="4H market_flush",
        state_file="state/paper_state.json",
        systemd_unit="liq-paper-bot.service",
        holding_hours=8,
    ),
    StrategyEntry(
        key="2h",
        label="2H strategy",
        state_file=None,
        systemd_unit=None,
        holding_hours=None,
    ),
    StrategyEntry(
        key="1h",
        label="1H aggressive",
        state_file=None,
        systemd_unit=None,
        holding_hours=None,
    ),
]


def find_entry(key: str) -> StrategyEntry | None:
    """Lookup by short key. Accepts "4h" / "4H" / "4".  Returns None if unknown."""
    if not key:
        return None
    normalized = key.strip().lower().rstrip("h")
    for e in REGISTRY:
        if e.key.rstrip("h") == normalized:
            return e
    return None


def load_executor(
    entry: StrategyEntry,
    base_cfg: BotConfig,
) -> PaperExecutor | None:
    """
    Construct a `PaperExecutor` pointed at the entry's state_file.
    Returns None for non-deployed entries (state_file=None).

    Reuses `BotConfig` values for capital / position sizing / signal
    thresholds — only the state_file path is overridden per strategy.
    If the state file is missing or corrupt, PaperExecutor's own
    _load_state falls back to a fresh default state (already covered
    in bot/paper_executor.py:75-92).
    """
    if not entry.is_deployed or entry.state_file is None:
        return None
    cfg = replace_state_file(base_cfg, entry.state_file)
    if entry.holding_hours is not None:
        cfg = _with_holding_hours(cfg, entry.holding_hours)
    return PaperExecutor(cfg)


def replace_state_file(cfg: BotConfig, new_state_file: str) -> BotConfig:
    """Return a copy of cfg with state_file replaced — pydantic-settings safe."""
    # BotConfig is a pydantic BaseSettings, so model_copy() preserves all
    # env-loaded values.
    return cfg.model_copy(update={"state_file": new_state_file})


def _with_holding_hours(cfg: BotConfig, holding_hours: int) -> BotConfig:
    return cfg.model_copy(update={"holding_hours": holding_hours})
