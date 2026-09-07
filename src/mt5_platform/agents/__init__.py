"""Multi-agent intelligence layer (Phase B).

Deterministic agents analyze the canonical MarketContext through specialized
lenses and express typed opinions. The SynthesisAgent weighs their disagreement
into a structured TradeThesis — NOT an order. The thesis later passes through
the risk engine.

Every conclusion is traceable to measurable features. No LLM, no external AI.
"""

from mt5_platform.agents.analytical_agents import (
    BreakoutAgent,
    MarketIntelligenceAgent,
    MeanReversionAgent,
    MomentumAgent,
    RegimeAgent,
    StructureAgent,
)
from mt5_platform.agents.contracts import (
    Agent,
    AgentContext,
    AgentOpinion,
    AgentRole,
    Stance,
    TradeThesis,
)
from mt5_platform.agents.support_agents import (
    HistoricalAgent,
    NewsAgent,
    StrategyEvaluationAgent,
)
from mt5_platform.agents.synthesis import SynthesisAgent

__all__ = [
    "Agent",
    "AgentContext",
    "AgentOpinion",
    "AgentRole",
    "Stance",
    "TradeThesis",
    "MarketIntelligenceAgent",
    "RegimeAgent",
    "StructureAgent",
    "MomentumAgent",
    "BreakoutAgent",
    "MeanReversionAgent",
    "NewsAgent",
    "StrategyEvaluationAgent",
    "HistoricalAgent",
    "SynthesisAgent",
    "DEFAULT_AGENT_NETWORK",
    "run_agent_network",
]

# Default analytical network (support agents are optional / situational).
DEFAULT_AGENT_NETWORK: list[Agent] = [
    MarketIntelligenceAgent(),
    RegimeAgent(),
    StructureAgent(),
    MomentumAgent(),
    BreakoutAgent(),
    MeanReversionAgent(),
    NewsAgent(),
    StrategyEvaluationAgent(),
    HistoricalAgent(),
]


def run_agent_network(
    ctx: AgentContext,
    *,
    agents: list[Agent] | None = None,
    synthesizer: SynthesisAgent | None = None,
) -> TradeThesis:
    """Evaluate all agents and synthesize their opinions into one thesis."""
    agents = agents if agents is not None else DEFAULT_AGENT_NETWORK
    opinions: list[AgentOpinion] = [agent.evaluate(ctx) for agent in agents]
    synth = synthesizer if synthesizer is not None else SynthesisAgent()
    return synth.synthesize(ctx, opinions)
