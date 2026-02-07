"""Context fetcher for gathering market data, news, and events.

This module provides the ContextFetcher class that gathers all relevant
context for AI-based decision making: VIX, economic calendar, earnings,
news headlines, and market indicators.
"""

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import structlog
import yfinance as yf

logger = structlog.get_logger()


@dataclass
class MarketContext:
    """Market context for a specific date.

    Attributes:
        date: The date this context is for.
        vix: VIX (volatility index) value.
        vix_1w_change: VIX change over past week (percentage).
        spy_price: SPY closing price.
        spy_50d_ma: SPY 50-day moving average.
        spy_200d_ma: SPY 200-day moving average.
        spy_vs_50d_ma: SPY price vs 50-day MA (percentage).
        spy_vs_200d_ma: SPY price vs 200-day MA (percentage).
        spy_drawdown: Current drawdown from 52-week high.
        spy_1w_return: SPY return over past week.
        spy_1m_return: SPY return over past month.
        fed_funds_rate: Current Fed funds rate (if available).
        fed_next_meeting: Date of next Fed meeting (if known).
        earnings_this_week: List of major earnings this week.
        news_headlines: Recent relevant news headlines.
        fear_greed_index: CNN Fear & Greed Index (0-100, higher = greed).
        fear_greed_label: Fear & Greed classification.
        put_call_ratio: Equity put/call ratio.
        market_breadth: Percentage of S&P 500 stocks above 200-day MA.
        credit_spread: High yield credit spread (HYG-LQD or similar).
        gld_1w_return: GLD return over past week (for cross-asset context).
        fetched_at: Timestamp when this context was fetched.
    """

    date: date
    vix: float | None = None
    vix_1w_change: float | None = None
    spy_price: float | None = None
    spy_50d_ma: float | None = None
    spy_200d_ma: float | None = None
    spy_vs_50d_ma: float | None = None
    spy_vs_200d_ma: float | None = None
    spy_drawdown: float | None = None
    spy_1w_return: float | None = None
    spy_1m_return: float | None = None
    fed_funds_rate: float | None = None
    fed_next_meeting: str | None = None
    earnings_this_week: list[str] = field(default_factory=list)
    news_headlines: list[str] = field(default_factory=list)
    fear_greed_index: int | None = None
    fear_greed_label: str | None = None
    put_call_ratio: float | None = None
    market_breadth: float | None = None
    credit_spread: float | None = None
    gld_1w_return: float | None = None
    fetched_at: str | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        d = asdict(self)
        d["date"] = self.date.isoformat()
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "MarketContext":
        """Create from dictionary."""
        data = data.copy()
        if isinstance(data.get("date"), str):
            data["date"] = date.fromisoformat(data["date"])
        return cls(**data)

    def to_prompt_text(self) -> str:
        """Format context for inclusion in LLM prompt."""
        lines = [
            f"Date: {self.date.strftime('%Y-%m-%d (%A)')}",
            "",
            "MARKET CONDITIONS:",
        ]

        if self.spy_price:
            lines.append(f"  SPY Price: ${self.spy_price:.2f}")
        if self.spy_vs_50d_ma is not None:
            direction = "above" if self.spy_vs_50d_ma > 0 else "below"
            lines.append(f"  SPY vs 50-day MA: {abs(self.spy_vs_50d_ma):.1f}% {direction}")
        if self.spy_vs_200d_ma is not None:
            direction = "above" if self.spy_vs_200d_ma > 0 else "below"
            lines.append(f"  SPY vs 200-day MA: {abs(self.spy_vs_200d_ma):.1f}% {direction}")
        if self.spy_drawdown is not None:
            lines.append(f"  SPY Drawdown from 52w high: {self.spy_drawdown:.1f}%")
        if self.spy_1w_return is not None:
            lines.append(f"  SPY 1-week return: {self.spy_1w_return:+.1f}%")
        if self.spy_1m_return is not None:
            lines.append(f"  SPY 1-month return: {self.spy_1m_return:+.1f}%")

        # Volatility section
        lines.append("")
        lines.append("VOLATILITY:")
        if self.vix is not None:
            vix_level = "low" if self.vix < 15 else "elevated" if self.vix < 25 else "high"
            lines.append(f"  VIX: {self.vix:.1f} ({vix_level} volatility)")
        if self.vix_1w_change is not None:
            vix_direction = "rising" if self.vix_1w_change > 2 else "falling" if self.vix_1w_change < -2 else "stable"
            lines.append(f"  VIX 1-week change: {self.vix_1w_change:+.1f}% ({vix_direction})")

        # Sentiment section (VIX-estimated — these are NOT independent indicators)
        has_sentiment = any([
            self.fear_greed_index is not None,
            self.put_call_ratio is not None,
            self.market_breadth is not None,
        ])
        if has_sentiment:
            lines.append("")
            lines.append("SENTIMENT ESTIMATES (VIX-derived, not independent data):")
            if self.fear_greed_index is not None:
                label = self.fear_greed_label or self._get_fear_greed_label(self.fear_greed_index)
                lines.append(f"  Fear & Greed (VIX-estimated): {self.fear_greed_index} ({label})")
            if self.put_call_ratio is not None:
                pc_sentiment = "bearish" if self.put_call_ratio > 1.0 else "bullish" if self.put_call_ratio < 0.7 else "neutral"
                lines.append(f"  Put/Call Ratio (VIX-estimated): {self.put_call_ratio:.2f} ({pc_sentiment})")
            if self.market_breadth is not None:
                breadth_level = "strong" if self.market_breadth > 70 else "weak" if self.market_breadth < 30 else "moderate"
                lines.append(f"  Market Breadth (SPY-estimated): {self.market_breadth:.0f}% ({breadth_level})")

        # Cross-asset context
        if self.gld_1w_return is not None or self.credit_spread is not None:
            lines.append("")
            lines.append("CROSS-ASSET:")
            if self.gld_1w_return is not None:
                lines.append(f"  GLD 1-week return: {self.gld_1w_return:+.1f}%")
            if self.credit_spread is not None:
                spread_level = "tight" if self.credit_spread < 3 else "wide" if self.credit_spread > 5 else "normal"
                lines.append(f"  Credit Spread (placeholder, not real data): {self.credit_spread:.2f}% ({spread_level})")

        if self.fed_next_meeting:
            lines.append(f"\nFED: Next meeting {self.fed_next_meeting}")

        if self.earnings_this_week:
            lines.append(f"\nEARNINGS THIS WEEK: {', '.join(self.earnings_this_week[:10])}")

        if self.news_headlines:
            lines.append("\nRECENT NEWS:")
            for headline in self.news_headlines[:5]:
                lines.append(f"  • {headline}")

        return "\n".join(lines)

    @staticmethod
    def _get_fear_greed_label(index: int) -> str:
        """Get the label for a Fear & Greed Index value."""
        if index <= 25:
            return "Extreme Fear"
        elif index <= 45:
            return "Fear"
        elif index <= 55:
            return "Neutral"
        elif index <= 75:
            return "Greed"
        else:
            return "Extreme Greed"


class ContextFetcher:
    """Fetches market context for AI decision-making.

    Gathers VIX, price data, economic events, and news headlines
    for a given date. Results can be cached to avoid repeated fetches.
    """

    # 2025 Fed meeting dates (approximate, update as needed)
    FED_MEETINGS_2025 = [
        "2025-01-29",
        "2025-03-19",
        "2025-05-07",
        "2025-06-18",
        "2025-07-30",
        "2025-09-17",
        "2025-11-05",
        "2025-12-17",
    ]

    FED_MEETINGS_2026 = [
        "2026-01-28",
        "2026-03-18",
        "2026-05-06",
        "2026-06-17",
        "2026-07-29",
        "2026-09-16",
        "2026-11-04",
        "2026-12-16",
    ]

    def __init__(self, cache_dir: Path | None = None):
        """Initialize the context fetcher.

        Args:
            cache_dir: Directory to cache context data. If None, uses
                       data/ai_eval_cache/context/
        """
        if cache_dir is None:
            cache_dir = Path("data/ai_eval_cache/context")
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _get_cache_path(self, target_date: date) -> Path:
        """Get the cache file path for a date."""
        return self.cache_dir / f"{target_date.isoformat()}.json"

    def _load_from_cache(self, target_date: date) -> MarketContext | None:
        """Load context from cache if available."""
        cache_path = self._get_cache_path(target_date)
        if cache_path.exists():
            try:
                with open(cache_path) as f:
                    data = json.load(f)
                logger.info("loaded_context_from_cache", date=str(target_date))
                return MarketContext.from_dict(data)
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning("cache_load_failed", date=str(target_date), error=str(e))
        return None

    def _save_to_cache(self, context: MarketContext) -> None:
        """Save context to cache."""
        cache_path = self._get_cache_path(context.date)
        with open(cache_path, "w") as f:
            json.dump(context.to_dict(), f, indent=2)
        logger.info("saved_context_to_cache", date=str(context.date))

    def _fetch_spy_data(self, target_date: date) -> dict:
        """Fetch SPY price data and calculate indicators."""
        result = {}

        try:
            # Fetch 1 year of data to calculate MAs and drawdown
            start = target_date - timedelta(days=365)
            spy = yf.Ticker("SPY")
            hist = spy.history(start=start, end=target_date + timedelta(days=1))

            if hist.empty:
                return result

            # Current price
            result["spy_price"] = float(hist["Close"].iloc[-1])

            # Moving averages
            if len(hist) >= 50:
                result["spy_50d_ma"] = float(hist["Close"].tail(50).mean())
                result["spy_vs_50d_ma"] = (
                    (result["spy_price"] / result["spy_50d_ma"] - 1) * 100
                )

            if len(hist) >= 200:
                result["spy_200d_ma"] = float(hist["Close"].tail(200).mean())
                result["spy_vs_200d_ma"] = (
                    (result["spy_price"] / result["spy_200d_ma"] - 1) * 100
                )

            # Drawdown from 52-week high
            high_52w = float(hist["Close"].max())
            result["spy_drawdown"] = (1 - result["spy_price"] / high_52w) * 100

            # Recent returns
            if len(hist) >= 5:
                price_1w_ago = float(hist["Close"].iloc[-5])
                result["spy_1w_return"] = (result["spy_price"] / price_1w_ago - 1) * 100

            if len(hist) >= 21:
                price_1m_ago = float(hist["Close"].iloc[-21])
                result["spy_1m_return"] = (result["spy_price"] / price_1m_ago - 1) * 100

        except Exception as e:
            logger.warning("spy_data_fetch_failed", error=str(e))

        return result

    def _fetch_vix(self, target_date: date) -> tuple[float | None, float | None]:
        """Fetch VIX value and 1-week change for the date."""
        try:
            vix = yf.Ticker("^VIX")
            hist = vix.history(
                start=target_date - timedelta(days=10),
                end=target_date + timedelta(days=1),
            )
            if not hist.empty and len(hist) >= 2:
                current_vix = float(hist["Close"].iloc[-1])
                # Calculate 1-week change
                vix_1w_change = None
                if len(hist) >= 5:
                    vix_1w_ago = float(hist["Close"].iloc[-5])
                    vix_1w_change = ((current_vix / vix_1w_ago) - 1) * 100
                return current_vix, vix_1w_change
        except Exception as e:
            logger.warning("vix_fetch_failed", error=str(e))
        return None, None

    def _fetch_gld_return(self, target_date: date) -> float | None:
        """Fetch GLD 1-week return for cross-asset context."""
        try:
            gld = yf.Ticker("GLD")
            hist = gld.history(
                start=target_date - timedelta(days=10),
                end=target_date + timedelta(days=1),
            )
            if not hist.empty and len(hist) >= 5:
                current = float(hist["Close"].iloc[-1])
                week_ago = float(hist["Close"].iloc[-5])
                return ((current / week_ago) - 1) * 100
        except Exception as e:
            logger.warning("gld_fetch_failed", error=str(e))
        return None

    def _fetch_credit_spread(self, target_date: date) -> float | None:
        """Fetch high yield credit spread (approximated via HYG vs LQD).

        Higher spread = more risk aversion in credit markets.
        """
        try:
            # HYG = High Yield Corporate Bond ETF
            # LQD = Investment Grade Corporate Bond ETF
            # Their yield difference approximates credit spread
            hyg = yf.Ticker("HYG")
            lqd = yf.Ticker("LQD")

            hyg_hist = hyg.history(start=target_date - timedelta(days=5), end=target_date + timedelta(days=1))
            lqd_hist = lqd.history(start=target_date - timedelta(days=5), end=target_date + timedelta(days=1))

            if not hyg_hist.empty and not lqd_hist.empty:
                # Use 1-month return difference as proxy for spread change
                # This is a simplification - real spread would need yield data
                hyg_price = float(hyg_hist["Close"].iloc[-1])
                lqd_price = float(lqd_hist["Close"].iloc[-1])

                # Approximate: higher HYG underperformance = wider spreads
                # Return a pseudo-spread (not actual yield spread, but directionally correct)
                # Normal range is roughly 3-6%
                return 4.0  # Placeholder - would need actual yield data
        except Exception as e:
            logger.warning("credit_spread_fetch_failed", error=str(e))
        return None

    def _estimate_sentiment_from_vix(self, vix: float | None) -> dict:
        """Estimate sentiment indicators from VIX.

        These are VIX-derived estimates, NOT real data. They provide
        directionally useful signals but should not be treated as
        independent indicators (they all correlate with VIX).
        """
        result = {
            "fear_greed_index": None,
            "fear_greed_label": None,
            "put_call_ratio": None,
            "market_breadth": None,
        }

        if vix is None:
            return result

        # Fear & Greed estimate (VIX-derived)
        estimated_fg = int(max(0, min(100, 100 - (vix - 10) * 4)))
        result["fear_greed_index"] = estimated_fg
        result["fear_greed_label"] = MarketContext._get_fear_greed_label(estimated_fg)

        # Put/Call estimate (VIX-derived)
        result["put_call_ratio"] = max(0.5, min(1.5, 0.6 + (vix - 12) * 0.02))

        return result

    def _fetch_market_breadth(self, spy_data: dict) -> float | None:
        """Estimate market breadth from SPY vs 200d MA.

        This is a VIX-independent estimate based on SPY's trend position.
        """
        spy_vs_200d = spy_data.get("spy_vs_200d_ma")
        if spy_vs_200d is not None:
            base_breadth = 50 + spy_vs_200d * 2
            return max(10, min(90, base_breadth))
        return None

    def _get_next_fed_meeting(self, target_date: date) -> str | None:
        """Get the next Fed meeting date after target_date."""
        all_meetings = self.FED_MEETINGS_2025 + self.FED_MEETINGS_2026
        for meeting in all_meetings:
            meeting_date = date.fromisoformat(meeting)
            if meeting_date > target_date:
                days_until = (meeting_date - target_date).days
                return f"{meeting} ({days_until} days away)"
        return None

    def _get_earnings_this_week(self, target_date: date) -> list[str]:
        """Get major earnings announcements for this week.

        Note: This is a simplified implementation. In production,
        you'd want to use an earnings calendar API.
        """
        # For now, return empty - would need an API like Alpha Vantage or similar
        # This is a placeholder that could be enhanced
        return []

    def _get_news_headlines(self, target_date: date) -> list[str]:
        """Get recent news headlines.

        Note: This is a simplified implementation. In production,
        you'd want to use a news API.
        """
        # Placeholder - would need a news API
        # Could integrate with web search or news API
        return []

    def fetch(self, target_date: date, use_cache: bool = True) -> MarketContext:
        """Fetch market context for a date.

        Args:
            target_date: The date to fetch context for.
            use_cache: Whether to use cached data if available.

        Returns:
            MarketContext with all available data.
        """
        # Check cache first
        if use_cache:
            cached = self._load_from_cache(target_date)
            if cached:
                return cached

        logger.info("fetching_market_context", date=str(target_date))

        # Fetch real data (each is a single Yahoo call)
        spy_data = self._fetch_spy_data(target_date)
        vix, vix_1w_change = self._fetch_vix(target_date)  # Single VIX fetch
        fed_meeting = self._get_next_fed_meeting(target_date)
        earnings = self._get_earnings_this_week(target_date)
        news = self._get_news_headlines(target_date)

        # VIX-derived sentiment estimates (single pass, no extra API calls)
        sentiment = self._estimate_sentiment_from_vix(vix)

        # SPY-derived breadth estimate
        breadth = self._fetch_market_breadth(spy_data)

        # Cross-asset data
        gld_return = self._fetch_gld_return(target_date)
        credit_spread = self._fetch_credit_spread(target_date)

        context = MarketContext(
            date=target_date,
            vix=vix,
            vix_1w_change=vix_1w_change,
            spy_price=spy_data.get("spy_price"),
            spy_50d_ma=spy_data.get("spy_50d_ma"),
            spy_200d_ma=spy_data.get("spy_200d_ma"),
            spy_vs_50d_ma=spy_data.get("spy_vs_50d_ma"),
            spy_vs_200d_ma=spy_data.get("spy_vs_200d_ma"),
            spy_drawdown=spy_data.get("spy_drawdown"),
            spy_1w_return=spy_data.get("spy_1w_return"),
            spy_1m_return=spy_data.get("spy_1m_return"),
            fed_next_meeting=fed_meeting,
            earnings_this_week=earnings,
            news_headlines=news,
            fear_greed_index=sentiment["fear_greed_index"],
            fear_greed_label=sentiment["fear_greed_label"],
            put_call_ratio=sentiment["put_call_ratio"],
            market_breadth=breadth,
            credit_spread=credit_spread,
            gld_1w_return=gld_return,
            fetched_at=datetime.now().isoformat(),
        )

        # Cache the result
        self._save_to_cache(context)

        return context
