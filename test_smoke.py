"""Smoke test: call agy_ask directly (no MCP transport) and verify a response."""

import io
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from server import agy_ask, agy_continue


def main() -> int:
    print("=== smoke 1: agy_ask new conversation ===")
    t0 = time.time()
    resp = agy_ask(prompt="Sadece tek bir kelime yaz: 'merhaba'. Başka hiçbir şey yazma.")
    print(f"elapsed: {time.time()-t0:.1f}s")
    print(f"response ({len(resp)} chars): {resp!r}")
    assert resp.strip(), "empty response"
    print("PASS")

    print("\n=== smoke 2: agy_continue same conversation ===")
    t0 = time.time()
    resp2 = agy_continue(prompt="Şimdi tek kelime: 'dünya'. Başka bir şey yazma.")
    print(f"elapsed: {time.time()-t0:.1f}s")
    print(f"response ({len(resp2)} chars): {resp2!r}")
    assert resp2.strip(), "empty response"
    print("PASS")

    return 0


if __name__ == "__main__":
    sys.exit(main())
