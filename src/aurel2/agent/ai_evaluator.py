"""AI Evaluator for making reasoned trading decisions.

This module provides the AIEvaluator class that uses an LLM to analyze
strategy signals and market context to make nuanced trading decisions,
compared to the deterministic weighted voting approach.
"""

import json
import os
from dataclasses import dataclass
from datetime import date
from typing import Any

import structlog

logger = structlog.get_logger()

# Try to import anthropic, but don't fail if not available
try:
    import anthropic

    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False
    anthropic = None


@dataclass
class AIDecision:
    """Decision made by the AI evaluator.

    Attributes:
        action: The recommended action (buy, sell, hold).
        asset: The asset to trade (if action is buy).
        confidence: Confidence level (0.0 to 1.0).
        reasoning: Detailed explanation for the decision.
        key_factors: List of key factors that influenced the decision.
        risks: List of risks or concerns.
    """

    action: str
    asset: str | None
    confidence: float
    reasoning: str
    key_factors: list[str]
    risks: list[str]

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "action": self.action,
            "asset": self.asset,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "key_factors": self.key_factors,
            "risks": self.risks,
        }


SYSTEM_PROMPT = """You are an AI trading advisor for a momentum-based ETF rotation strategy.

STRATEGY OVERVIEW:
- We run 3 momentum strategies that generate signals (BUY/SELL/HOLD)
- Dual Momentum: 12-month lookback, quarterly rebalancing, crash protection
- Mean Reversion: RSI-based, catches oversold bounces
- Multi-Timeframe: Blends 1/3/6/12 month momentum for faster reaction
- We also have market context: VIX level, drawdowns, Fed meeting proximity, etc.

YOUR TASK:
Decide the best trading action. You can either:
1. Follow the strategy consensus (what most strategies agree on)
2. Override if you see something the momentum algorithms would miss

WHEN MOMENTUM ALGORITHMS TEND TO BE WRONG:
- **Recovery inflection points**: After a drawdown, momentum stays negative even when the market starts bouncing back. Look for signs of a V-shaped recovery.
- **Fear extremes**: High VIX with positive short-term returns often means the worst is over.
- **Lagging indicators**: Dual Momentum uses 12-month lookback, so it's very slow to adapt.

WHEN TO TRUST THE CONSENSUS:
- Normal market conditions (no extremes in VIX, no major drawdowns)
- When strategies largely agree
- When you don't have clear evidence to contradict them

IMPORTANT:
- Don't override just to be different
- If you override, explain clearly what the algorithms are missing
- The consensus isn't always right, but overriding should be based on reasoning, not gut feel

OUTPUT FORMAT:
{
    "action": "buy" | "sell" | "hold",
    "asset": "SPY" | "GLD" | "EFA" | etc. (only if action is buy),
    "confidence": 0.0 to 1.0,
    "reasoning": "Your detailed explanation",
    "key_factors": ["factor1", "factor2"],
    "risks": ["risk1", "risk2"],
    "override_reason": "Why you're overriding consensus (if applicable, else null)"
}"""


class AIEvaluator:
    """AI-based trading decision evaluator.

    Uses Claude to analyze strategy signals and market context
    to make nuanced trading decisions.
    """

    def __init__(
        self,
        model: str = "claude-sonnet-4-5-20250929",
        api_key: str | None = None,
    ):
        """Initialize the AI evaluator.

        Args:
            model: The Claude model to use.
            api_key: Anthropic API key. If None, uses ANTHROPIC_API_KEY env var.

        Raises:
            ImportError: If anthropic package is not installed.
            ValueError: If no API key is available.
        """
        if not ANTHROPIC_AVAILABLE:
            raise ImportError(
                "anthropic package not installed. Install with: pip install anthropic"
            )

        self.model = model
        api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")

        if not api_key:
            raise ValueError(
                "No Anthropic API key provided. Set ANTHROPIC_API_KEY environment variable "
                "or pass api_key parameter."
            )

        self.client = anthropic.Anthropic(api_key=api_key)

    def _format_strategy_signals(self, signals: dict[str, Any]) -> str:
        """Format strategy signals for the prompt."""
        lines = ["STRATEGY SIGNALS:"]

        for name, signal in signals.items():
            action = signal.get("action", "unknown").upper()
            confidence = signal.get("confidence", 0.5)
            asset = signal.get("asset_symbol") or signal.get("asset_class") or "N/A"
            reasoning = signal.get("reasoning", "")

            lines.append(f"\n{name.upper().replace('_', ' ')}:")
            lines.append(f"  Action: {action}")
            lines.append(f"  Asset: {asset}")
            lines.append(f"  Confidence: {confidence:.0%}")
            if reasoning:
                lines.append(f"  Reasoning: {reasoning}")

        return "\n".join(lines)

    def _build_prompt(
        self,
        signals: dict[str, Any],
        context_text: str,
        current_holding: str | None,
    ) -> str:
        """Build the full prompt for the AI."""
        signals_text = self._format_strategy_signals(signals)

        holding_text = f"CURRENT HOLDING: {current_holding}" if current_holding else "CURRENT HOLDING: Cash (no position)"

        return f"""{context_text}

{signals_text}

{holding_text}

Based on the above information, what trading decision should we make? Analyze carefully and respond with JSON."""

    def evaluate(
        self,
        signals: dict[str, Any],
        context_text: str,
        current_holding: str | None,
    ) -> AIDecision:
        """Evaluate strategy signals and context to make a decision.

        Args:
            signals: Dictionary of strategy signals.
            context_text: Formatted market context text.
            current_holding: Current holding symbol, or None if cash.

        Returns:
            AIDecision with action, reasoning, etc.
        """
        prompt = self._build_prompt(signals, context_text, current_holding)

        logger.info("calling_ai_evaluator", model=self.model)

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )

            # Extract the response text
            response_text = response.content[0].text

            # Parse JSON from response
            # Handle case where response might have markdown code blocks
            if "```json" in response_text:
                json_start = response_text.find("```json") + 7
                json_end = response_text.find("```", json_start)
                response_text = response_text[json_start:json_end].strip()
            elif "```" in response_text:
                json_start = response_text.find("```") + 3
                json_end = response_text.find("```", json_start)
                response_text = response_text[json_start:json_end].strip()

            result = json.loads(response_text)

            decision = AIDecision(
                action=result.get("action", "hold").lower(),
                asset=result.get("asset"),
                confidence=float(result.get("confidence", 0.5)),
                reasoning=result.get("reasoning", ""),
                key_factors=result.get("key_factors", []),
                risks=result.get("risks", []),
            )

            logger.info(
                "ai_decision_made",
                action=decision.action,
                asset=decision.asset,
                confidence=decision.confidence,
            )

            return decision

        except json.JSONDecodeError as e:
            logger.error("ai_response_parse_failed", error=str(e), response=response_text)
            # Return a safe default
            return AIDecision(
                action="hold",
                asset=None,
                confidence=0.0,
                reasoning=f"Failed to parse AI response: {e}",
                key_factors=[],
                risks=["AI response parsing failed"],
            )

        except Exception as e:
            logger.error("ai_evaluation_failed", error=str(e))
            return AIDecision(
                action="hold",
                asset=None,
                confidence=0.0,
                reasoning=f"AI evaluation failed: {e}",
                key_factors=[],
                risks=["AI evaluation error"],
            )


class ClaudeCodeEvaluator:
    """AI evaluator that uses the Claude Code CLI.

    Shells out to the `claude` command instead of using the Anthropic API,
    which uses your Claude plan subscription instead of pay-per-token.
    """

    def __init__(self, model: str = "sonnet"):
        """Initialize the Claude Code evaluator.

        Args:
            model: Model to use (sonnet, opus, haiku). Defaults to sonnet.
        """
        self.model = f"claude-code-{model}"
        self._model_flag = model

    def _format_strategy_signals(self, signals: dict[str, Any]) -> str:
        """Format strategy signals for the prompt."""
        lines = ["STRATEGY SIGNALS:"]

        for name, signal in signals.items():
            action = signal.get("action", "unknown").upper()
            confidence = signal.get("confidence", 0.5)
            asset = signal.get("asset_symbol") or signal.get("asset_class") or "N/A"
            reasoning = signal.get("reasoning", "")

            lines.append(f"\n{name.upper().replace('_', ' ')}:")
            lines.append(f"  Action: {action}")
            lines.append(f"  Asset: {asset}")
            lines.append(f"  Confidence: {confidence:.0%}")
            if reasoning:
                lines.append(f"  Reasoning: {reasoning}")

        return "\n".join(lines)

    def _build_prompt(
        self,
        signals: dict[str, Any],
        context_text: str,
        current_holding: str | None,
    ) -> str:
        """Build the full prompt for the AI."""
        signals_text = self._format_strategy_signals(signals)

        holding_text = f"CURRENT HOLDING: {current_holding}" if current_holding else "CURRENT HOLDING: Cash (no position)"

        return f"""{SYSTEM_PROMPT}

---

{context_text}

{signals_text}

{holding_text}

Based on the above information, what trading decision should we make? Analyze carefully and respond with ONLY valid JSON (no markdown, no explanation outside the JSON)."""

    def evaluate(
        self,
        signals: dict[str, Any],
        context_text: str,
        current_holding: str | None,
    ) -> AIDecision:
        """Evaluate strategy signals and context using Claude Code CLI.

        Args:
            signals: Dictionary of strategy signals.
            context_text: Formatted market context text.
            current_holding: Current holding symbol, or None if cash.

        Returns:
            AIDecision with action, reasoning, etc.
        """
        import subprocess

        prompt = self._build_prompt(signals, context_text, current_holding)

        logger.info("calling_claude_code_evaluator", model=self.model)

        try:
            # Call claude CLI with the prompt directly
            # Using --print-only avoids interactive mode
            result = subprocess.run(
                [
                    "claude",
                    "-p", prompt,
                    "--output-format", "text",
                    "--model", self._model_flag,
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )

            if result.returncode != 0:
                logger.error("claude_code_failed", stderr=result.stderr)
                raise RuntimeError(f"Claude CLI failed: {result.stderr}")

            response_text = result.stdout.strip()

            # Parse JSON from response
            # Handle case where response might have markdown code blocks
            if "```json" in response_text:
                json_start = response_text.find("```json") + 7
                json_end = response_text.find("```", json_start)
                response_text = response_text[json_start:json_end].strip()
            elif "```" in response_text:
                json_start = response_text.find("```") + 3
                json_end = response_text.find("```", json_start)
                response_text = response_text[json_start:json_end].strip()

            parsed = json.loads(response_text)

            decision = AIDecision(
                action=parsed.get("action", "hold").lower(),
                asset=parsed.get("asset"),
                confidence=float(parsed.get("confidence", 0.5)),
                reasoning=parsed.get("reasoning", ""),
                key_factors=parsed.get("key_factors", []),
                risks=parsed.get("risks", []),
            )

            logger.info(
                "claude_code_decision_made",
                action=decision.action,
                asset=decision.asset,
                confidence=decision.confidence,
            )

            return decision

        except json.JSONDecodeError as e:
            logger.error("claude_code_response_parse_failed", error=str(e), response=response_text)
            return AIDecision(
                action="hold",
                asset=None,
                confidence=0.0,
                reasoning=f"Failed to parse Claude Code response: {e}",
                key_factors=[],
                risks=["Claude Code response parsing failed"],
            )

        except subprocess.TimeoutExpired:
            logger.error("claude_code_timeout")
            return AIDecision(
                action="hold",
                asset=None,
                confidence=0.0,
                reasoning="Claude Code CLI timed out",
                key_factors=[],
                risks=["Claude Code timeout"],
            )

        except Exception as e:
            logger.error("claude_code_evaluation_failed", error=str(e))
            return AIDecision(
                action="hold",
                asset=None,
                confidence=0.0,
                reasoning=f"Claude Code evaluation failed: {e}",
                key_factors=[],
                risks=["Claude Code evaluation error"],
            )


class MockAIEvaluator:
    """Mock AI evaluator for testing without API calls.

    Returns decisions based on simple heuristics.
    """

    def __init__(self):
        """Initialize the mock evaluator."""
        self.model = "mock"

    def evaluate(
        self,
        signals: dict[str, Any],
        context_text: str,
        current_holding: str | None,
    ) -> AIDecision:
        """Make a mock decision based on majority vote.

        Args:
            signals: Dictionary of strategy signals.
            context_text: Formatted market context text (ignored).
            current_holding: Current holding symbol.

        Returns:
            AIDecision based on majority vote.
        """
        # Count votes
        action_votes: dict[str, int] = {"buy": 0, "sell": 0, "hold": 0}
        asset_votes: dict[str, int] = {}

        for name, signal in signals.items():
            action = signal.get("action", "hold").lower()
            if action in action_votes:
                action_votes[action] += 1

            asset = signal.get("asset_symbol") or signal.get("asset_class")
            if asset and action == "buy":
                asset_votes[asset] = asset_votes.get(asset, 0) + 1

        # Find winner
        best_action = max(action_votes, key=lambda k: action_votes[k])
        best_asset = max(asset_votes, key=lambda k: asset_votes[k]) if asset_votes else None

        confidence = action_votes[best_action] / len(signals)

        return AIDecision(
            action=best_action,
            asset=best_asset if best_action == "buy" else None,
            confidence=confidence,
            reasoning=f"Mock decision: {action_votes[best_action]}/{len(signals)} strategies agree on {best_action}",
            key_factors=["majority_vote"],
            risks=["mock_evaluator_used"],
        )


EXPERT_SYSTEM_PROMPT = """You are an AI advisor that has studied the historical failures of a momentum trading system. Your job is to anticipate when the deterministic system is about to make a mistake, based on patterns from past failures.

THE MOMENTUM SYSTEM'S KNOWN WEAKNESSES (from 6 years of data):

1. **TOO SLOW TO EXIT (bad_hold failures - 13 occurrences)**
   - Uses 12-month lookback, so it stays in losers too long
   - WORST EXAMPLE: Feb 2020 - held SPY which lost -12.5% while TLT gained +6.4%
   - VIX was 40.1, clearly signaling danger, but momentum said "hold"
   - PATTERN: When VIX spikes >30 AND current holding is down, the system holds too long

2. **TOO SLOW TO ENTER (late_entry failures - 24 occurrences)**
   - Misses early parts of recoveries because lookback is backward-looking
   - WORST EXAMPLE: April 2020 - was in cash while XLE returned +25%
   - VIX was 31.6, fear was high but recovery had started
   - PATTERN: After VIX peaks and starts falling, opportunities emerge before momentum signals

3. **MISSES SECTOR ROTATIONS (missed_opportunity - 16 occurrences)**
   - Only tracks SPY/EFA/AGG, misses when sectors like XLE outperform
   - EXAMPLE: Dec 2021 - held SPY (-5.3%) when XLE was +18.8%
   - PATTERN: When energy/commodities are spiking, momentum system doesn't capture it

4. **WHIPSAWS IN CHOPPY MARKETS (suboptimal_asset - 11 occurrences)**
   - Switches based on small momentum differences that reverse
   - PATTERN: Small momentum gaps (<5%) often reverse, causing unnecessary trades

YOUR JOB: ANTICIPATE THESE FAILURES

When you see conditions matching past failures, you should override:

**OVERRIDE TO SAFETY WHEN:**
- VIX >30 AND current holding already down this week → EXIT (like Feb 2020)
- High VIX + held asset weak + bonds/gold strong → SWITCH to safety

**OVERRIDE TO OPPORTUNITY WHEN:**
- VIX falling from peak (was >35, now <25) AND system is in cash/bonds → LOOK for entry
- Clear sector rotation signal (energy/gold surging >10% while SPY flat)

**DON'T OVERRIDE WHEN:**
- VIX is calm (<20) and stable
- Momentum gaps are small (<5%)
- No clear pattern match to past failures

OUTPUT FORMAT (JSON only):
{
    "historical_pattern_match": {
        "similar_to_past_failure": true | false,
        "matched_failure_type": "bad_hold | late_entry | missed_opportunity | none",
        "similarity_reasoning": "Why this looks like a past failure (or doesn't)"
    },
    "current_analysis": {
        "vix_level": "calm (<20) | elevated (20-30) | spiking (>30)",
        "position_performance": "gaining | flat | losing",
        "sector_rotation_signal": true | false,
        "recovery_signal": true | false
    },
    "override_decision": {
        "should_override": true | false,
        "override_type": "to_safety | to_opportunity | none",
        "override_asset": "AGG | GLD | SPY | EFA | XLE | null",
        "reasoning": "1-2 sentences"
    },
    "final_recommendation": {
        "action": "buy" | "sell" | "hold",
        "asset": "SPY" | "GLD" | "EFA" | "AGG" | "XLE" | null,
        "confidence": 0.0 to 1.0
    }
}"""


class ExpertAIEvaluator:
    """Expert AI evaluator using advanced prompting techniques.

    Uses:
    - Expert persona (senior portfolio manager)
    - Structured chain-of-thought reasoning
    - Pre-mortem analysis
    - Devil's advocate questioning
    - Extended thinking for deep analysis
    """

    def __init__(
        self,
        model: str = "claude-sonnet-4-5-20250929",
        api_key: str | None = None,
        use_extended_thinking: bool = True,
    ):
        """Initialize the expert AI evaluator.

        Args:
            model: The Claude model to use.
            api_key: Anthropic API key. If None, uses ANTHROPIC_API_KEY env var.
            use_extended_thinking: Whether to use extended thinking for deeper analysis.

        Raises:
            ImportError: If anthropic package is not installed.
            ValueError: If no API key is available.
        """
        if not ANTHROPIC_AVAILABLE:
            raise ImportError(
                "anthropic package not installed. Install with: pip install anthropic"
            )

        self.model = model
        self.use_extended_thinking = use_extended_thinking
        api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")

        if not api_key:
            raise ValueError(
                "No Anthropic API key provided. Set ANTHROPIC_API_KEY environment variable "
                "or pass api_key parameter."
            )

        self.client = anthropic.Anthropic(api_key=api_key)

    def _format_strategy_signals(self, signals: dict[str, Any]) -> str:
        """Format strategy signals for the prompt."""
        lines = ["STRATEGY SIGNALS:"]

        for name, signal in signals.items():
            action = signal.get("action", "unknown").upper()
            confidence = signal.get("confidence", 0.5)
            asset = signal.get("asset_symbol") or signal.get("asset_class") or "N/A"
            reasoning = signal.get("reasoning", "")

            lines.append(f"\n{name.upper().replace('_', ' ')}:")
            lines.append(f"  Action: {action}")
            lines.append(f"  Asset: {asset}")
            lines.append(f"  Confidence: {confidence:.0%}")
            if reasoning:
                lines.append(f"  Reasoning: {reasoning}")

        return "\n".join(lines)

    def _build_prompt(
        self,
        signals: dict[str, Any],
        context_text: str,
        current_holding: str | None,
        deterministic_decision: dict[str, Any] | None = None,
    ) -> str:
        """Build the full prompt for the AI."""
        signals_text = self._format_strategy_signals(signals)

        holding_text = f"CURRENT HOLDING: {current_holding}" if current_holding else "CURRENT HOLDING: Cash (no position)"

        det_text = ""
        if deterministic_decision:
            det_action = deterministic_decision.get("action", "unknown").upper()
            det_asset = deterministic_decision.get("asset", "")
            det_text = f"\nDETERMINISTIC DECISION: {det_action}"
            if det_asset:
                det_text += f" {det_asset}"

        return f"""MARKET CONTEXT:
{context_text}

{signals_text}

{holding_text}
{det_text}

Analyze this situation using your expert framework. Think deeply about whether the deterministic decision is correct or if this is one of the rare cases where you should override.

Respond with ONLY valid JSON matching the specified format."""

    def evaluate(
        self,
        signals: dict[str, Any],
        context_text: str,
        current_holding: str | None,
        deterministic_decision: dict[str, Any] | None = None,
    ) -> AIDecision:
        """Evaluate using expert analysis with optional extended thinking.

        Args:
            signals: Dictionary of strategy signals.
            context_text: Formatted market context text.
            current_holding: Current holding symbol, or None if cash.
            deterministic_decision: What the deterministic system decided.

        Returns:
            AIDecision with action, reasoning, etc.
        """
        prompt = self._build_prompt(signals, context_text, current_holding, deterministic_decision)

        logger.info(
            "calling_expert_ai_evaluator",
            model=self.model,
            extended_thinking=self.use_extended_thinking
        )

        try:
            if self.use_extended_thinking:
                # Use extended thinking for deeper analysis
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=16000,
                    thinking={
                        "type": "enabled",
                        "budget_tokens": 10000  # Allow substantial thinking
                    },
                    system=EXPERT_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": prompt}],
                )
            else:
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=2048,
                    system=EXPERT_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": prompt}],
                )

            # Extract the response text (skip thinking blocks)
            response_text = ""
            thinking_text = ""
            for block in response.content:
                if block.type == "thinking":
                    thinking_text = block.thinking
                elif block.type == "text":
                    response_text = block.text

            if thinking_text:
                logger.debug("expert_thinking", thinking=thinking_text[:500] + "...")

            # Parse JSON from response
            if "```json" in response_text:
                json_start = response_text.find("```json") + 7
                json_end = response_text.find("```", json_start)
                response_text = response_text[json_start:json_end].strip()
            elif "```" in response_text:
                json_start = response_text.find("```") + 3
                json_end = response_text.find("```", json_start)
                response_text = response_text[json_start:json_end].strip()

            result = json.loads(response_text)

            # Extract from the actual JSON structure matching our prompt
            final_rec = result.get("final_recommendation", {})
            override_decision = result.get("override_decision", {})
            pattern_match = result.get("historical_pattern_match", {})
            current_analysis = result.get("current_analysis", {})

            action = final_rec.get("action", "hold").lower()
            asset = final_rec.get("asset")
            confidence = float(final_rec.get("confidence", 0.5))

            # Build comprehensive, layman-friendly reasoning
            reasoning_parts = []
            
            # Main reasoning from override decision
            if override_decision.get("reasoning"):
                reasoning_parts.append(override_decision["reasoning"])
            
            # Add pattern match context if relevant
            if pattern_match.get("similar_to_past_failure"):
                failure_type = pattern_match.get("matched_failure_type", "unknown")
                similarity = pattern_match.get("similarity_reasoning", "")
                if failure_type != "none":
                    readable_types = {
                        "bad_hold": "holding too long during a downturn",
                        "late_entry": "entering a recovery too late",
                        "missed_opportunity": "missing a sector rotation opportunity",
                        "suboptimal_asset": "switching assets unnecessarily"
                    }
                    readable = readable_types.get(failure_type, failure_type)
                    reasoning_parts.append(f"This looks like a past mistake: {readable}. {similarity}")
            
            # Add override context
            if override_decision.get("should_override"):
                override_type = override_decision.get("override_type", "")
                if override_type == "to_safety":
                    reasoning_parts.append("Moving to safety to protect the portfolio.")
                elif override_type == "to_opportunity":
                    reasoning_parts.append("Capturing an opportunity the rules-based system missed.")

            reasoning = " ".join(reasoning_parts) if reasoning_parts else "No specific concerns - following the rules-based strategy."

            # Key factors from analysis  
            key_factors = []
            if current_analysis.get("vix_level"):
                key_factors.append(f"Market volatility: {current_analysis['vix_level']}")
            if current_analysis.get("position_performance"):
                key_factors.append(f"Position: {current_analysis['position_performance']}")
            if pattern_match.get("matched_failure_type") and pattern_match.get("matched_failure_type") != "none":
                key_factors.append(f"Past pattern: {pattern_match['matched_failure_type']}")
            if override_decision.get("should_override"):
                key_factors.append(f"Override: {override_decision.get('override_type', 'yes')}")

            # Risks from the analysis
            risks = []
            if current_analysis.get("vix_level") == "spiking (>30)":
                risks.append("High market volatility - increased risk")
            if current_analysis.get("position_performance") == "losing":
                risks.append("Current position underperforming")
            if current_analysis.get("sector_rotation_signal"):
                risks.append("Sector rotation in progress")
            if not override_decision.get("should_override") and pattern_match.get("similar_to_past_failure"):
                risks.append("Situation resembles past mistake but confidence too low to override")

            decision = AIDecision(
                action=action,
                asset=asset,
                confidence=confidence,
                reasoning=reasoning,
                key_factors=key_factors,
                risks=risks,
            )

            logger.info(
                "expert_ai_decision_made",
                action=decision.action,
                asset=decision.asset,
                confidence=decision.confidence,
                override=override_decision.get("should_override", False),
                failure_type=pattern_match.get("matched_failure_type"),
            )

            return decision

        except json.JSONDecodeError as e:
            logger.error("expert_ai_response_parse_failed", error=str(e), response=response_text[:500])
            return AIDecision(
                action="hold",
                asset=None,
                confidence=0.0,
                reasoning=f"Failed to parse expert AI response: {e}",
                key_factors=[],
                risks=["Expert AI response parsing failed"],
            )

        except Exception as e:
            logger.error("expert_ai_evaluation_failed", error=str(e))
            return AIDecision(
                action="hold",
                asset=None,
                confidence=0.0,
                reasoning=f"Expert AI evaluation failed: {e}",
                key_factors=[],
                risks=["Expert AI evaluation error"],
            )


class ClaudeCodeExpertEvaluator:
    """Expert AI evaluator using Claude Code CLI.

    Uses the `claude` CLI command instead of Anthropic API,
    which uses your Claude plan subscription instead of pay-per-token.

    Includes all expert prompting techniques:
    - Expert persona (senior portfolio manager)
    - Structured chain-of-thought reasoning
    - Pre-mortem analysis
    - Devil's advocate questioning
    """

    def __init__(self, model: str = "haiku"):
        """Initialize the Claude Code expert evaluator.

        Args:
            model: Model to use (sonnet, opus, haiku). Defaults to opus for expert mode.
        """
        self.model = f"claude-code-expert-{model}"
        self._model_flag = model

    def _format_strategy_signals(self, signals: dict[str, Any]) -> str:
        """Format strategy signals for the prompt."""
        lines = ["STRATEGY SIGNALS:"]

        for name, signal in signals.items():
            action = signal.get("action", "unknown").upper()
            confidence = signal.get("confidence", 0.5)
            asset = signal.get("asset_symbol") or signal.get("asset_class") or "N/A"
            reasoning = signal.get("reasoning", "")

            lines.append(f"\n{name.upper().replace('_', ' ')}:")
            lines.append(f"  Action: {action}")
            lines.append(f"  Asset: {asset}")
            lines.append(f"  Confidence: {confidence:.0%}")
            if reasoning:
                lines.append(f"  Reasoning: {reasoning}")

        return "\n".join(lines)

    def _build_prompt(
        self,
        signals: dict[str, Any],
        context_text: str,
        current_holding: str | None,
        deterministic_decision: dict[str, Any] | None = None,
    ) -> str:
        """Build the full prompt for the AI."""
        signals_text = self._format_strategy_signals(signals)

        holding_text = f"CURRENT HOLDING: {current_holding}" if current_holding else "CURRENT HOLDING: Cash (no position)"

        det_text = ""
        if deterministic_decision:
            det_action = deterministic_decision.get("action", "unknown").upper()
            det_asset = deterministic_decision.get("asset", "")
            det_text = f"\nDETERMINISTIC DECISION: {det_action}"
            if det_asset:
                det_text += f" {det_asset}"

        return f"""{EXPERT_SYSTEM_PROMPT}

---

{context_text}

{signals_text}

{holding_text}
{det_text}

CRITICAL: The context above includes HISTORICAL FAILURE ANALYSIS showing past mistakes by the momentum system.
- Study the failure patterns carefully - they show when the system went wrong before
- If today's situation matches a past failure pattern, consider overriding
- Remember: you only see failures from BEFORE today (no future data leakage)
- Use these lessons to anticipate - not just react to - system failures

Analyze this situation using your expert framework. Does this match any historical failure patterns? Think deeply about whether the deterministic decision is about to make a mistake similar to past failures.

Respond with ONLY valid JSON matching the specified format (no markdown code blocks, just raw JSON)."""

    def evaluate(
        self,
        signals: dict[str, Any],
        context_text: str,
        current_holding: str | None,
        deterministic_decision: dict[str, Any] | None = None,
    ) -> AIDecision:
        """Evaluate using expert analysis via Claude Code CLI.

        Args:
            signals: Dictionary of strategy signals.
            context_text: Formatted market context text.
            current_holding: Current holding symbol, or None if cash.
            deterministic_decision: What the deterministic system decided.

        Returns:
            AIDecision with action, reasoning, etc.
        """
        import subprocess

        prompt = self._build_prompt(signals, context_text, current_holding, deterministic_decision)

        logger.info("calling_claude_code_expert_evaluator", model=self.model)

        try:
            # Call claude CLI with the prompt
            # Use shorter timeout to fail fast if auth is broken
            result = subprocess.run(
                [
                    "claude",
                    "-p", prompt,
                    "--output-format", "text",
                    "--model", self._model_flag,
                ],
                capture_output=True,
                text=True,
                timeout=60,  # Reduced from 180s - fail fast if issues
            )

            if result.returncode != 0:
                logger.error("claude_code_expert_failed", stderr=result.stderr)
                raise RuntimeError(f"Claude CLI failed: {result.stderr}")

            response_text = result.stdout.strip()

            # Parse JSON from response
            if "```json" in response_text:
                json_start = response_text.find("```json") + 7
                json_end = response_text.find("```", json_start)
                response_text = response_text[json_start:json_end].strip()
            elif "```" in response_text:
                json_start = response_text.find("```") + 3
                json_end = response_text.find("```", json_start)
                response_text = response_text[json_start:json_end].strip()

            parsed = json.loads(response_text)

            # Extract from the actual JSON structure matching our prompt
            final_rec = parsed.get("final_recommendation", {})
            override_decision = parsed.get("override_decision", {})
            pattern_match = parsed.get("historical_pattern_match", {})
            current_analysis = parsed.get("current_analysis", {})

            action = final_rec.get("action", "hold").lower()
            asset = final_rec.get("asset")
            confidence = float(final_rec.get("confidence", 0.5))

            # Build comprehensive, layman-friendly reasoning
            reasoning_parts = []
            
            # Main reasoning from override decision
            if override_decision.get("reasoning"):
                reasoning_parts.append(override_decision["reasoning"])
            
            # Add pattern match context if relevant
            if pattern_match.get("similar_to_past_failure"):
                failure_type = pattern_match.get("matched_failure_type", "unknown")
                similarity = pattern_match.get("similarity_reasoning", "")
                if failure_type != "none":
                    readable_types = {
                        "bad_hold": "holding too long during a downturn",
                        "late_entry": "entering a recovery too late",
                        "missed_opportunity": "missing a sector rotation opportunity",
                        "suboptimal_asset": "switching assets unnecessarily"
                    }
                    readable = readable_types.get(failure_type, failure_type)
                    reasoning_parts.append(f"This looks like a past mistake: {readable}. {similarity}")
            
            # Add override context
            if override_decision.get("should_override"):
                override_type = override_decision.get("override_type", "")
                if override_type == "to_safety":
                    reasoning_parts.append("Moving to safety to protect the portfolio.")
                elif override_type == "to_opportunity":
                    reasoning_parts.append("Capturing an opportunity the rules-based system missed.")

            reasoning = " ".join(reasoning_parts) if reasoning_parts else "No specific concerns - following the rules-based strategy."

            # Key factors from analysis  
            key_factors = []
            if current_analysis.get("vix_level"):
                key_factors.append(f"Market volatility: {current_analysis['vix_level']}")
            if current_analysis.get("position_performance"):
                key_factors.append(f"Position: {current_analysis['position_performance']}")
            if pattern_match.get("matched_failure_type") and pattern_match.get("matched_failure_type") != "none":
                key_factors.append(f"Past pattern: {pattern_match['matched_failure_type']}")
            if override_decision.get("should_override"):
                key_factors.append(f"Override: {override_decision.get('override_type', 'yes')}")

            # Risks from the analysis
            risks = []
            if current_analysis.get("vix_level") == "spiking (>30)":
                risks.append("High market volatility - increased risk")
            if current_analysis.get("position_performance") == "losing":
                risks.append("Current position underperforming")
            if current_analysis.get("sector_rotation_signal"):
                risks.append("Sector rotation in progress")
            if not override_decision.get("should_override") and pattern_match.get("similar_to_past_failure"):
                risks.append("Situation resembles past mistake but confidence too low to override")

            decision = AIDecision(
                action=action,
                asset=asset,
                confidence=confidence,
                reasoning=reasoning,
                key_factors=key_factors,
                risks=risks,
            )

            logger.info(
                "claude_code_expert_decision_made",
                action=decision.action,
                asset=decision.asset,
                confidence=decision.confidence,
                override=override_decision.get("should_override", False),
                failure_type=pattern_match.get("matched_failure_type"),
            )

            return decision

        except json.JSONDecodeError as e:
            logger.error("claude_code_expert_parse_failed", error=str(e), response=response_text[:500])
            return AIDecision(
                action="hold",
                asset=None,
                confidence=0.0,
                reasoning=f"Failed to parse Claude Code expert response: {e}",
                key_factors=[],
                risks=["Claude Code expert response parsing failed"],
            )

        except subprocess.TimeoutExpired:
            logger.error("claude_code_expert_timeout")
            return AIDecision(
                action="hold",
                asset=None,
                confidence=0.0,
                reasoning="Claude Code expert CLI timed out",
                key_factors=[],
                risks=["Claude Code expert timeout"],
            )

        except Exception as e:
            logger.error("claude_code_expert_failed", error=str(e))
            return AIDecision(
                action="hold",
                asset=None,
                confidence=0.0,
                reasoning=f"Claude Code expert evaluation failed: {e}",
                key_factors=[],
                risks=["Claude Code expert evaluation error"],
            )
