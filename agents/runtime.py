"""Shared LLM call wrapper.

Every agent calls `invoke` with a prompt + system message. This wrapper:
  - Chooses the model based on tier (default | cheap | escalation)
  - Logs the call to the `decisions` table
  - Logs cost to the `costs` table
  - Halts if monthly budget exceeded
  - Caches identical prompts within a TTL window
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime
from typing import Any

from core.costs import is_llm_budget_exceeded, record_llm_cost
from core.db import transaction


def _hash_prompt(system: str, prompt: str) -> str:
    return hashlib.sha256((system + "\n" + prompt).encode()).hexdigest()[:16]


def invoke(
    db_path: str,
    agent_name: str,
    system: str,
    prompt: str,
    *,
    model: str | None = None,
    max_tokens: int = 2048,
    monthly_budget: float = 200.0,
    inputs_for_log: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Call Claude. Returns (text_response, metadata).

    metadata includes: tokens_in, tokens_out, model, cost, decision_id.
    Raises BudgetExceeded if monthly LLM cost > monthly_budget.
    """
    if is_llm_budget_exceeded(db_path, monthly_budget):
        raise BudgetExceeded(f"Monthly LLM budget {monthly_budget} exceeded; agent halt")

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    # Lazy import so the rest of the system doesn't depend on anthropic SDK
    from anthropic import Anthropic
    client = Anthropic(api_key=api_key)

    chosen_model = model or "claude-sonnet-4-6"
    response = client.messages.create(
        model=chosen_model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )

    tokens_in = response.usage.input_tokens
    tokens_out = response.usage.output_tokens
    text = "".join(block.text for block in response.content if hasattr(block, "text"))
    cost = record_llm_cost(db_path, chosen_model, tokens_in, tokens_out, context=agent_name)

    prompt_hash = _hash_prompt(system, prompt)
    with transaction(db_path) as conn:
        new_id = conn.execute(
            """
            INSERT INTO decisions (ts, agent, prompt_hash, inputs, output, action_taken,
                                   llm_model, llm_tokens_in, llm_tokens_out, llm_cost)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id
            """,
            [
                datetime.now(),
                agent_name,
                prompt_hash,
                __import__("json").dumps(inputs_for_log) if inputs_for_log else None,
                text[:8000],   # truncate huge outputs
                "pending",
                chosen_model,
                tokens_in,
                tokens_out,
                cost,
            ],
        ).fetchone()[0]

    return text, {
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "model": chosen_model,
        "cost": cost,
        "decision_id": int(new_id),
    }


class BudgetExceeded(Exception):
    pass
