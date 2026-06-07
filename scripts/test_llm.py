"""
Quick smoke-test for the LLM client.

Run:
    $env:PYTHONPATH = "src"
    uv run python scripts/test_llm.py
"""
import asyncio
import sys
from pathlib import Path

# Make sure src/ is on the path when run directly.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from agent.core.llm import chat
from agent.core.models import LLMResponse
from agent.observability.costs import get_session_total


async def main() -> None:
    print("Sending test message to Claude...")
    print("-" * 48)

    response: LLMResponse = await chat(
        messages=[
            {"role": "user", "content": "Reply with exactly 5 words: hello world test message"}
        ],
        system="You are a concise assistant. Follow instructions precisely.",
        max_tokens=32,
    )

    print(f"Response : {response.content}")
    print(f"In tokens: {response.input_tokens}")
    print(f"Out token: {response.output_tokens}")
    print(f"Cost USD : ${response.cost:.6f}")
    print(f"Latency  : {response.latency_ms:.1f} ms")
    print("-" * 48)
    print("Session total:", get_session_total())


if __name__ == "__main__":
    asyncio.run(main())
