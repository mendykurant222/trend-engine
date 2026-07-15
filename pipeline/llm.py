"""Claude access layer. Every call is logged to llm_calls and costed in
api_costs (plan items 28, 29). Client is lazy-initialized so the rest of the
pipeline runs without an API key.
"""

import json
import logging
import os

from psycopg.types.json import Jsonb

log = logging.getLogger("llm")

# Haiku for cheap high-volume filtering/extraction, Sonnet for trend analysis
# (plan item 28). Floating aliases, never dated snapshots.
MODEL_EXTRACTION = "claude-haiku-4-5"
MODEL_ANALYSIS = "claude-sonnet-5"

# $ per million tokens (input, output)
PRICES = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (3.00, 15.00),
}

_client = None


def ready() -> str | None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return "ANTHROPIC_API_KEY not set"
    return None


def _get_client():
    global _client
    if _client is None:
        from anthropic import Anthropic
        _client = Anthropic()
    return _client


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    inp, outp = PRICES.get(model, (0, 0))
    return (input_tokens * inp + output_tokens * outp) / 1_000_000


def structured_call(conn, run_id: int | None, purpose: str, model: str,
                    system: str, user_text: str, schema: dict,
                    max_tokens: int = 8000) -> dict:
    """One Claude call with a JSON-schema-constrained response.

    Returns the parsed object. Records llm_calls + api_costs.
    TODO Phase 3: route bulk extraction through the Batch API (-50%).
    """
    client = _get_client()
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_text}],
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )
    text = "".join(b.text for b in response.content if b.type == "text")
    result = json.loads(text)

    usage = response.usage
    call_cost = cost_usd(model, usage.input_tokens, usage.output_tokens)
    conn.execute(
        """insert into llm_calls
           (run_id, model, purpose, prompt, response, input_tokens, output_tokens, cost_usd)
           values (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (run_id, model, purpose,
         Jsonb({"system": system, "user": user_text}), Jsonb(result),
         usage.input_tokens, usage.output_tokens, call_cost),
    )
    conn.execute(
        "insert into api_costs (run_id, provider, operation, units, cost_usd) values (%s, %s, %s, %s, %s)",
        (run_id, "anthropic", purpose, 1, call_cost),
    )
    log.info("%s (%s): %d in / %d out tokens, $%.4f",
             purpose, model, usage.input_tokens, usage.output_tokens, call_cost)
    return result
