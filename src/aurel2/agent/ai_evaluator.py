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
    risk_commentary: str = ""

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "action": self.action,
            "asset": self.asset,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "key_factors": self.key_factors,
            "risks": self.risks,
            "risk_commentary": self.risk_commentary,
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


EXPERT_SYSTEM_PROMPT = """You are an AI advisor reviewing the output of a deterministic momentum trading system.

YOUR DEFAULT POSITION: The deterministic system is correct. Agreeing with it is the right answer in the vast majority of cases. You must PROVE the system wrong before overriding.

ABOUT THE SYSTEM:
- 3 momentum strategies (dual momentum, mean reversion, multi-timeframe) vote on trades
- An orchestrator weighs their votes and produces a decision
- The system has a 10-year track record of ~255% total return (vs SPY ~206%)
- It works. Your job is to confirm it, not to second-guess it.

BACKGROUND AWARENESS (known system limitations):
The system uses a 12-month lookback, which can be slow to react in extreme conditions:
- It may hold too long during sharp drawdowns (VIX >30, held asset falling)
- It may enter recoveries late after major crashes
- It tracks a limited asset universe (SPY/EFA/AGG/GLD)
- Small momentum gaps (<5%) can lead to whipsaw trades

These are RARE edge cases, not reasons to routinely override.

OVERRIDE CRITERIA (all must be met):
1. There must be OVERWHELMING evidence the system is wrong — not just "could be better"
2. The evidence must be concrete and measurable (VIX levels, drawdown %, momentum gaps)
3. You must articulate a specific, falsifiable reason — not vague concern
4. When in doubt, AGREE with the system

STRUCTURED REASONING PROCESS:
Step 1: State the null hypothesis — "The deterministic system's decision is correct"
Step 2: Look for evidence AGAINST the null hypothesis
Step 3: Is the evidence overwhelming? (Not just suggestive — overwhelming)
Step 4: If not overwhelming → AGREE with the system
Step 5: If overwhelming → Override, stating exactly what evidence falsified the null

OUTPUT FORMAT (JSON only):
{
    "null_hypothesis_test": {
        "system_decision": "What the deterministic system decided",
        "evidence_against": ["List of concrete evidence points against the system decision"],
        "evidence_strength": "none | weak | moderate | overwhelming",
        "null_rejected": true | false
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
    },
    "risk_commentary": "1-2 sentence risk assessment for the human approver, regardless of agreement. What could go wrong, what to watch for, or why this is solid."
}

IMPORTANT: Always provide a brief risk commentary for the human reviewing this trade — what could go wrong, what to watch for, or why this is solid. This commentary is shown to the approver regardless of whether you agree or disagree with the system."""


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
            null_test = result.get("null_hypothesis_test", {})
            current_analysis = result.get("current_analysis", {})

            action = final_rec.get("action", "hold").lower()
            asset = final_rec.get("asset")
            confidence = float(final_rec.get("confidence", 0.5))

            # Build comprehensive, layman-friendly reasoning
            reasoning_parts = []

            # Main reasoning from override decision
            if override_decision.get("reasoning"):
                reasoning_parts.append(override_decision["reasoning"])

            # Add null hypothesis context if rejected
            if null_test.get("null_rejected"):
                evidence = null_test.get("evidence_against", [])
                strength = null_test.get("evidence_strength", "unknown")
                if evidence:
                    reasoning_parts.append(f"Evidence ({strength}): {'; '.join(evidence[:2])}")

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
            if null_test.get("evidence_strength") and null_test["evidence_strength"] != "none":
                key_factors.append(f"Evidence strength: {null_test['evidence_strength']}")
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
            if not null_test.get("null_rejected") and null_test.get("evidence_strength") == "moderate":
                risks.append("Some evidence against system decision but not enough to override")

            decision = AIDecision(
                action=action,
                asset=asset,
                confidence=confidence,
                reasoning=reasoning,
                key_factors=key_factors,
                risks=risks,
                risk_commentary=result.get("risk_commentary", ""),
            )

            logger.info(
                "expert_ai_decision_made",
                action=decision.action,
                asset=decision.asset,
                confidence=decision.confidence,
                override=override_decision.get("should_override", False),
                null_rejected=null_test.get("null_rejected", False),
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

Apply the null hypothesis framework: assume the deterministic system is correct unless you find overwhelming evidence otherwise. Be specific about what evidence you found (or didn't find).

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
            null_test = parsed.get("null_hypothesis_test", {})
            current_analysis = parsed.get("current_analysis", {})

            action = final_rec.get("action", "hold").lower()
            asset = final_rec.get("asset")
            confidence = float(final_rec.get("confidence", 0.5))

            # Build comprehensive, layman-friendly reasoning
            reasoning_parts = []

            # Main reasoning from override decision
            if override_decision.get("reasoning"):
                reasoning_parts.append(override_decision["reasoning"])

            # Add null hypothesis context if rejected
            if null_test.get("null_rejected"):
                evidence = null_test.get("evidence_against", [])
                strength = null_test.get("evidence_strength", "unknown")
                if evidence:
                    reasoning_parts.append(f"Evidence ({strength}): {'; '.join(evidence[:2])}")

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
            if null_test.get("evidence_strength") and null_test["evidence_strength"] != "none":
                key_factors.append(f"Evidence strength: {null_test['evidence_strength']}")
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
            if not null_test.get("null_rejected") and null_test.get("evidence_strength") == "moderate":
                risks.append("Some evidence against system decision but not enough to override")

            decision = AIDecision(
                action=action,
                asset=asset,
                confidence=confidence,
                reasoning=reasoning,
                key_factors=key_factors,
                risks=risks,
                risk_commentary=parsed.get("risk_commentary", ""),
            )

            logger.info(
                "claude_code_expert_decision_made",
                action=decision.action,
                asset=decision.asset,
                confidence=decision.confidence,
                override=override_decision.get("should_override", False),
                null_rejected=null_test.get("null_rejected", False),
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
