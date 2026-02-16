"""
Shared fixtures and mocks for MedForce test suite.
We mock all heavy external deps (GCS, Gemini) so tests run fast and offline.
"""

import sys
import types
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

# ─── Stub heavy third-party modules BEFORE importing medforce ───

# google.cloud.storage
_storage_mod = types.ModuleType("google.cloud.storage")
_storage_mod.Client = MagicMock
sys.modules.setdefault("google.cloud.storage", _storage_mod)
sys.modules.setdefault("google.cloud", types.ModuleType("google.cloud"))
sys.modules["google.cloud"].storage = _storage_mod

# google.cloud.exceptions
_exc_mod = types.ModuleType("google.cloud.exceptions")
_exc_mod.NotFound = type("NotFound", (Exception,), {})
_exc_mod.GoogleCloudError = type("GoogleCloudError", (Exception,), {})
sys.modules.setdefault("google.cloud.exceptions", _exc_mod)

# google.api_core.exceptions
_api_exc = types.ModuleType("google.api_core.exceptions")
_api_exc.NotFound = type("NotFound", (Exception,), {})
sys.modules.setdefault("google.api_core", types.ModuleType("google.api_core"))
sys.modules.setdefault("google.api_core.exceptions", _api_exc)

# google.cloud.speech
sys.modules.setdefault("google.cloud.speech", types.ModuleType("google.cloud.speech"))

# google.genai / google.genai.types / google.genai.live
_genai_mod = types.ModuleType("google.genai")
_genai_types = types.ModuleType("google.genai.types")
_genai_live = types.ModuleType("google.genai.live")
_genai_mod.Client = MagicMock
_genai_mod.types = _genai_types
_genai_mod.live = _genai_live
_genai_live.ws_connect = MagicMock()
sys.modules.setdefault("google.genai", _genai_mod)
sys.modules.setdefault("google.genai.types", _genai_types)
sys.modules.setdefault("google.genai.live", _genai_live)
sys.modules.setdefault("google", types.ModuleType("google"))
sys.modules["google"].genai = _genai_mod
sys.modules["google"].cloud = sys.modules["google.cloud"]

# google.generativeai
_genai_legacy = types.ModuleType("google.generativeai")
_genai_legacy.configure = MagicMock()
_genai_legacy.GenerativeModel = MagicMock
sys.modules.setdefault("google.generativeai", _genai_legacy)

# websockets
_ws_client = types.ModuleType("websockets.asyncio.client")
_ws_client.connect = MagicMock()
_ws_asyncio = types.ModuleType("websockets.asyncio")
_ws_asyncio.client = _ws_client
_ws_mod = types.ModuleType("websockets")
_ws_mod.asyncio = _ws_asyncio
sys.modules.setdefault("websockets", _ws_mod)
sys.modules.setdefault("websockets.asyncio", _ws_asyncio)
sys.modules.setdefault("websockets.asyncio.client", _ws_client)

# mutagen
_mutagen = types.ModuleType("mutagen")
_mutagen.File = MagicMock(return_value=None)
sys.modules.setdefault("mutagen", _mutagen)

# PIL / Pillow
_pil = types.ModuleType("PIL")
_pil_image = types.ModuleType("PIL.Image")
_pil.Image = _pil_image
sys.modules.setdefault("PIL", _pil)
sys.modules.setdefault("PIL.Image", _pil_image)

# audioop (removed in Python 3.13)
sys.modules.setdefault("audioop", types.ModuleType("audioop"))


@pytest.fixture(scope="session")
def test_client():
    """Create a FastAPI TestClient for the entire test session."""
    from fastapi.testclient import TestClient
    from medforce.app import app
    return TestClient(app)
