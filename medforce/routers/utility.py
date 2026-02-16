import os
import time
import logging
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

router = APIRouter()
logger = logging.getLogger("medforce-server")

# Lazy imports
try:
    from medforce.agents.websocket_agent import get_websocket_agent
except Exception:
    get_websocket_agent = None


@router.get("/ws/sessions")
async def get_active_websocket_sessions():
    """Get information about all active WebSocket sessions."""
    if get_websocket_agent is None:
        return {"error": "WebSocket agent not available", "sessions": []}

    agent = get_websocket_agent()
    if agent is None:
        return {"error": "WebSocket agent not available", "sessions": []}

    try:
        sessions = agent.get_active_sessions()
        return {
            "active_sessions": len(sessions),
            "sessions": sessions
        }
    except Exception as e:
        logger.error(f"Error getting session info: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/test-gemini-live")
async def test_gemini_live():
    """Quick test endpoint to check Gemini Live API connection speed"""
    from google import genai

    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        return {"error": "No API key"}

    client = genai.Client(api_key=api_key)
    model = "models/gemini-2.5-flash-native-audio-preview-12-2025"

    config = {
        "response_modalities": ["AUDIO"],
        "speech_config": {"voice_config": {"prebuilt_voice_config": {"voice_name": "Aoede"}}}
    }

    start = time.time()
    try:
        async with client.aio.live.connect(model=model, config=config) as session:
            connect_time = time.time() - start
            return {"status": "connected", "connect_time_seconds": round(connect_time, 2)}
    except Exception as e:
        return {"error": str(e), "elapsed": time.time() - start}


@router.get("/ui/{file_path:path}")
async def serve_ui(file_path: str):
    """Serve UI files for testing"""
    try:
        ui_file = os.path.join("ui", file_path)
        if os.path.exists(ui_file):
            with open(ui_file, "r", encoding="utf-8") as f:
                content = f.read()
            return HTMLResponse(content=content)
        raise HTTPException(status_code=404, detail="File not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
