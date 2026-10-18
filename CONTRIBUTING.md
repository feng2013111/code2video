# Contributing to code2video

Thanks for your interest! This project keeps a deliberately small surface area,
so PRs are easy to review.

## Ground rules

1. **Determinism first.** Anything that reaches a rendered frame must be a pure
   function of `(index, t)`. No wall-clock, no unseeded randomness.
2. **No new runtime dependencies** without a strong reason. Today the only hard
   dependency is `websocket-client`.
3. **Cross-platform or nothing.** Windows / macOS / Linux paths and probes.
4. Every PR that touches capture/compose should pass:
   ```bash
   pip install -e ".[dev]"   # runtime deps + pytest
   python -m pytest
   CODE2VIDEO_SMOKE=1 python -m pytest tests/test_smoke.py -s
   ```

## Good first contributions

- new examples in `examples/` (bar-chart race, wave field, text typewriter)
- WebM/GIF output in `compose.py`
- frame-directory reuse / resume from a partial render

## Commit style

Conventional-ish prefixes: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`.
