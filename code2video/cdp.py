"""A tiny synchronous Chrome DevTools Protocol client.

Only what frame capture needs - no puppeteer dependency:

* flat sessions via Target.attachToTarget
* Page.enable / Page.navigate / Page.captureScreenshot
* Runtime.enable / Runtime.evaluate
* an event queue so no CDP event is ever lost between calls

Requires the ``websocket-client`` package.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

try:
    import websocket  # websocket-client
except ImportError as _exc:  # pragma: no cover - depends on user environment
    websocket = None  # type: ignore
    _WEBSOCKET_IMPORT_ERROR = _exc

WEBSOCKET_INSTALL_HINT = (
    "code2video needs the 'websocket-client' package. "
    "Install it with: pip install -r requirements.txt")


class CDPError(RuntimeError):
    """Raised on CDP protocol errors, timeouts and JS evaluation failures."""


class CDPSession:
    """One WebSocket connection to a browser-level DevTools endpoint."""

    def __init__(self, ws_url: str, default_timeout: float = 60.0):
        if websocket is None:
            raise CDPError(WEBSOCKET_INSTALL_HINT)
        # suppress_origin avoids Chrome's --remote-allow-origins rejection.
        self.ws = websocket.create_connection(
            ws_url, timeout=default_timeout, suppress_origin=True)
        self.default_timeout = default_timeout
        self._id = 0
        self._events: List[Dict[str, Any]] = []

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass

    def __enter__(self) -> "CDPSession":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- low level ---------------------------------------------------------

    def _send(self, method: str, params: Optional[dict], session_id: Optional[str]) -> int:
        self._id += 1
        msg: Dict[str, Any] = {"id": self._id, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        self.ws.send(json.dumps(msg))
        return self._id

    def _recv(self, timeout: float) -> Optional[Dict[str, Any]]:
        self.ws.settimeout(max(0.05, timeout))
        try:
            raw = self.ws.recv()
        except Exception:
            return None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None

    # -- API ---------------------------------------------------------------

    def call(self, method: str, params: Optional[dict] = None,
             session_id: Optional[str] = None,
             timeout: Optional[float] = None) -> Dict[str, Any]:
        """Send a command and wait for its response; events are queued."""
        timeout = timeout if timeout is not None else self.default_timeout
        msg_id = self._send(method, params, session_id)
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise CDPError(f"timeout waiting for response to {method}")
            data = self._recv(remaining)
            if data is None:
                raise CDPError(f"timeout waiting for response to {method}")
            if data.get("id") == msg_id:
                if "error" in data:
                    raise CDPError(f"{method} failed: {data['error']}")
                return data.get("result") or {}
            if "method" in data:
                self._events.append(data)

    def wait_event(self, name: str, timeout: float = 30.0,
                   session_id: Optional[str] = None) -> Dict[str, Any]:
        """Wait for a CDP event, checking the queue first."""
        for i, ev in enumerate(self._events):
            if ev.get("method") == name and (
                    session_id is None or ev.get("sessionId") == session_id):
                return self._events.pop(i)
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise CDPError(f"timeout waiting for event {name}")
            data = self._recv(remaining)
            if data is None:
                raise CDPError(f"timeout waiting for event {name}")
            if data.get("id") is not None:
                continue  # a response to some other call; ignore
            if data.get("method") == name and (
                    session_id is None or data.get("sessionId") == session_id):
                return data
            self._events.append(data)

    def evaluate(self, expression: str, session_id: Optional[str] = None,
                 timeout: Optional[float] = None) -> Any:
        """Evaluate JS in the page and return its value (returnByValue)."""
        result = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            session_id=session_id, timeout=timeout,
        )
        if result.get("exceptionDetails"):
            raise CDPError(f"JS evaluation failed: {result['exceptionDetails']}")
        return (result.get("result") or {}).get("value")
