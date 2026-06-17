"""Smoke test: call antigravity_ask directly (no MCP transport) and verify a response."""

import io
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from server import (  # noqa: E402  (after stdout/stderr rewrap above)
    _detect_image_format,
    _run_agy_streamed,
    antigravity_ask,
    antigravity_continue,
    antigravity_image,
)


def main() -> int:
    print("=== smoke 1: antigravity_ask new conversation ===")
    t0 = time.time()
    resp = antigravity_ask(prompt="Sadece tek bir kelime yaz: 'merhaba'. Başka hiçbir şey yazma.")
    print(f"elapsed: {time.time() - t0:.1f}s")
    print(f"response ({len(resp)} chars): {resp!r}")
    assert resp.strip(), "empty response"
    print("PASS")

    print("\n=== smoke 2: antigravity_continue same conversation ===")
    t0 = time.time()
    resp2 = antigravity_continue(prompt="Şimdi tek kelime: 'dünya'. Başka bir şey yazma.")
    print(f"elapsed: {time.time() - t0:.1f}s")
    print(f"response ({len(resp2)} chars): {resp2!r}")
    assert resp2.strip(), "empty response"
    print("PASS")

    print("\n=== smoke 3: antigravity_image generates a file ===")
    import os
    import tempfile

    out_path = os.path.join(tempfile.gettempdir(), "agy_smoke_image.png")
    t0 = time.time()
    result = antigravity_image(
        prompt="A simple solid blue circle centered on a plain white background.",
        output_path=out_path,
    )
    print(f"elapsed: {time.time() - t0:.1f}s")
    print(f"result: {result!r}")
    final = result.splitlines()[0].strip()
    assert os.path.isfile(final), f"image not found: {final}"
    assert _detect_image_format(final), f"not a recognized image: {final}"
    print("PASS")

    print("\n=== smoke 4: antigravity_ask_stream emits live progress ===")
    ticks = []

    def on_progress(step, message):
        ticks.append((step, message))
        print(f"  [progress {step}] {message}")

    t0 = time.time()
    resp4 = _run_agy_streamed(
        "Use python to compute 6*7, print it, then reply with ONLY the number.",
        os.getcwd(),
        continue_conv=False,
        timeout_s=180,
        on_progress=on_progress,
    )
    print(f"elapsed: {time.time() - t0:.1f}s")
    print(f"response ({len(resp4)} chars): {resp4!r}")
    print(f"progress ticks: {len(ticks)}")
    assert resp4.strip(), "empty response"
    assert ticks, "no progress ticks emitted"
    print("PASS")

    return 0


if __name__ == "__main__":
    sys.exit(main())
