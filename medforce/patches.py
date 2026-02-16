"""
Consolidated websocket monkey-patch for Gemini Live API.
Must be called BEFORE any google.genai imports that use websockets.

Previously duplicated in:
- server.py:15-28
- voice_websocket_handler.py:29-44
- voice_session_manager.py:28-39
"""

import sys
import asyncio
from contextlib import asynccontextmanager


def apply_all():
    """Apply all necessary patches. Call once at startup."""
    _fix_windows_event_loop()
    _patch_websocket_timeout()


def _fix_windows_event_loop():
    """CRITICAL FIX for Windows: Must be applied BEFORE any google.genai imports."""
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _patch_websocket_timeout():
    """Monkey patch websockets to increase open_timeout for Gemini Live API."""
    from websockets.asyncio.client import connect as _original_ws_connect

    @asynccontextmanager
    async def _patched_ws_connect(*args, **kwargs):
        """Patched version that adds longer timeout for Gemini Live API"""
        if 'open_timeout' not in kwargs:
            kwargs['open_timeout'] = 120  # 2 minutes instead of default 10 seconds
        async with _original_ws_connect(*args, **kwargs) as ws:
            yield ws

    # Pre-import and patch google.genai.live before any other imports use it
    import google.genai.live
    google.genai.live.ws_connect = _patched_ws_connect
