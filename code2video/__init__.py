"""code2video - let a text model draw videos frame by frame with code.

Pipeline:
    1. A text model writes a single self-contained HTML file that implements
       ``window.__renderFrame(index, t)`` and draws frame ``index``
       deterministically (no wall-clock, no randomness without a seed).
    2. Headless Chrome (driven over the Chrome DevTools Protocol) loads the
       HTML, calls ``__renderFrame`` for every frame and screenshots it.
    3. ffmpeg composes the PNG sequence into an MP4, preferring a hardware
       encoder when one is available.

Everything is auto-detected: GPU -> Chrome GPU compositing flags, ffmpeg ->
best available H.264 encoder.
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
