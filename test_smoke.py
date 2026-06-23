"""Smoke test: call antigravity_ask directly (no MCP transport) and verify a response."""

import asyncio
import io
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from server import (  # noqa: E402  (after stdout/stderr rewrap above)
    _detect_image_format,
    agent_swarm,
    antigravity_ask,
    antigravity_continue,
    antigravity_image,
)


def main() -> int:
    print("=== smoke 1: antigravity_ask new conversation ===")
    t0 = time.time()
    resp = asyncio.run(
        antigravity_ask(prompt="Sadece tek bir kelime yaz: 'merhaba'. Başka hiçbir şey yazma.")
    )
    print(f"elapsed: {time.time() - t0:.1f}s")
    print(f"response ({len(resp)} chars): {resp!r}")
    assert resp.strip(), "empty response"
    print("PASS")

    print("\n=== smoke 2: antigravity_continue same conversation ===")
    t0 = time.time()
    resp2 = asyncio.run(
        antigravity_continue(prompt="Şimdi tek kelime: 'dünya'. Başka bir şey yazma.")
    )
    print(f"elapsed: {time.time() - t0:.1f}s")
    print(f"response ({len(resp2)} chars): {resp2!r}")
    assert resp2.strip(), "empty response"
    print("PASS")

    print("\n=== smoke 3: antigravity_image generates a file ===")
    import os
    import tempfile

    out_path = os.path.join(tempfile.gettempdir(), "agy_smoke_image.png")
    t0 = time.time()
    result = asyncio.run(
        antigravity_image(
            prompt="A simple solid blue circle centered on a plain white background.",
            output_path=out_path,
        )
    )
    print(f"elapsed: {time.time() - t0:.1f}s")
    print(f"result: {result!r}")
    final = result.splitlines()[0].strip()
    assert os.path.isfile(final), f"image not found: {final}"
    assert _detect_image_format(final), f"not a recognized image: {final}"
    print("PASS")

    print("\n=== smoke 4: agent_swarm runs tasks in parallel ===")
    t0 = time.time()
    resp4 = agent_swarm(
        tasks=[
            {
                "backend": "antigravity",
                "prompt": "Reply with exactly this token and nothing else: ALPHA",
            },
            {
                "backend": "antigravity",
                "prompt": "Reply with exactly this token and nothing else: BETA",
            },
        ],
        max_concurrency=2,
        timeout_s=120,
    )
    print(f"elapsed: {time.time() - t0:.1f}s")
    print(f"result:\n{resp4}")
    assert "2/2 succeeded" in resp4, "swarm reported a failure"
    assert "ALPHA" in resp4 and "BETA" in resp4, "swarm missing an answer"
    print("PASS")

    return 0


if __name__ == "__main__":
    sys.exit(main())
