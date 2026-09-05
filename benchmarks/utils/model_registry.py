"""Register non-standard models with litellm's model cost/info map.

litellm ships a registry of known models used for pricing, context-window
lookups and capability flags. Models served through a local bridge are absent
from it: ``anthropic/glm-5.3`` (z.ai GLM Coding Plan via proxy/zbridge) has no
entry, so ``get_model_info()`` and ``completion_cost()`` both raise
"This model isn't mapped yet". The SDK swallows those, which is why runs report
``accumulated_cost: 0.0``, ``model_name: "default"`` and no context window.

Registering here fixes all three at once, for every caller, instead of
duplicating cost fields into each ``.llm_config/*.json``.
"""

from __future__ import annotations

import logging


logger = logging.getLogger(__name__)

# Limits verified against the live z.ai API: max_tokens above 131072 is
# rejected with code 1210 ("range [1,131072]"), and the published GLM docs
# list a 200K context window.
GLM_5_3_MAX_INPUT_TOKENS = 200_000
GLM_5_3_MAX_OUTPUT_TOKENS = 131_072

# NOTE: placeholder pricing -- z.ai does not publish per-token rates for the
# Coding Plan, and real marginal cost on a subscription is zero. These exist so
# cost-per-task is comparable against metered models; treat them as notional.
GLM_5_3_INPUT_COST_PER_TOKEN = 0.0000006
GLM_5_3_OUTPUT_COST_PER_TOKEN = 0.0000022

MODEL_REGISTRY: dict[str, dict[str, object]] = {
    "anthropic/glm-5.3": {
        "max_tokens": GLM_5_3_MAX_OUTPUT_TOKENS,
        "max_input_tokens": GLM_5_3_MAX_INPUT_TOKENS,
        "max_output_tokens": GLM_5_3_MAX_OUTPUT_TOKENS,
        "input_cost_per_token": GLM_5_3_INPUT_COST_PER_TOKEN,
        "output_cost_per_token": GLM_5_3_OUTPUT_COST_PER_TOKEN,
        "litellm_provider": "anthropic",
        "mode": "chat",
        "supports_function_calling": True,
        "supports_tool_choice": True,
    },
}


def register_custom_models() -> bool:
    """Add MODEL_REGISTRY entries to litellm. Safe to call repeatedly.

    Returns True when registration ran. Never raises: a missing or changed
    litellm must not break inference, since the only cost of failure is
    losing cost/context metadata.
    """
    try:
        import litellm

        litellm.register_model(MODEL_REGISTRY)
    except Exception as exc:
        logger.debug("litellm model registration skipped: %s", exc)
        return False
    return True
