"""Live smoke: ONE real DeepSeek chat completion through the agent runtime (RD8).

This is a REAL call (real key, real network) — it never mocks and never simulates. The key is
resolved through the frozen interface (`deepseek_runtime._resolve_api_key`: stored key ->
``DEEPSEEK_API_KEY`` -> None). Success prints ONLY ``{ok, model, latency_ms, sample_chars}`` —
never the key, never the prompt, never the model's text. Without a key it refuses honestly
(named code, exit 2) so an unconfigured run can never masquerade as a passing smoke.

Run from ``backend/``:  ``../.venv/Scripts/python.exe scripts/smoke_deepseek.py``
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents import claude_cli, deepseek_runtime  # noqa: E402  (sys.path set above)


def main() -> int:
    inv = claude_cli.build_invocation(
        prompt='Reply with exactly this JSON object and nothing else: {"ping": "pong"}',
        json_schema={
            "type": "object",
            "properties": {"ping": {"type": "string"}},
            "required": ["ping"],
        },
        system_prompt_static="You are a connectivity probe for the Polsia agent runtime.",
        schema_name="smoke_deepseek",
    )

    started = time.monotonic()
    try:
        result = deepseek_runtime.invoke(inv)
    except Exception as exc:  # named refusals carry .code; anything else reports its class
        print(json.dumps({
            "ok": False,
            "code": getattr(exc, "code", type(exc).__name__),
            "message": str(exc)[:200],
        }))
        return 2

    latency_ms = int((time.monotonic() - started) * 1000)
    envelope = json.loads(result.stdout)
    print(json.dumps({
        "ok": not bool(envelope.get("is_error")),
        "model": envelope.get("model"),
        "latency_ms": latency_ms,
        "sample_chars": len(str(envelope.get("result") or "")),
    }))
    return 0 if not envelope.get("is_error") else 3


if __name__ == "__main__":
    raise SystemExit(main())
