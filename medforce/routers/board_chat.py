import time
import logging
import traceback
from fastapi import APIRouter, HTTPException, WebSocket

router = APIRouter()
logger = logging.getLogger("medforce-server")

# Lazy imports
try:
    from medforce.agents import board_chat_model as chat_model
    from medforce.managers.patient_state import patient_manager
except Exception:
    chat_model = None
    patient_manager = None

try:
    from medforce.agents.websocket_agent import websocket_chat_endpoint
except Exception:
    websocket_chat_endpoint = None


@router.post("/send-chat")
async def run_chat_agent(payload: list[dict]):
    """
    Chat endpoint using board agent architecture.
    Accepts chat history and returns agent response.
    """
    request_start = time.time()
    logger.info(f"/send-chat: REQUEST RECEIVED at {request_start}")
    try:
        if patient_manager:
            if len(payload) > 0 and isinstance(payload[0], dict):
                patient_id = payload[0].get('patient_id', patient_manager.get_patient_id())
                patient_manager.set_patient_id(patient_id)

        logger.info("/send-chat: Calling chat_agent...")
        answer = await chat_model.chat_agent(payload)
        logger.info(f"/send-chat: chat_agent returned in {time.time()-request_start:.2f}s")
        logger.info(f"Agent Answer: {answer[:200]}...")
        return {"response": answer, "status": "success"}

    except Exception as e:
        logger.error(f"Chat agent error: {e}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@router.websocket("/ws/chat/{patient_id}")
async def websocket_chat(websocket: WebSocket, patient_id: str):
    """WebSocket endpoint for real-time general chat with RAG + tools."""
    if websocket_chat_endpoint is None:
        await websocket.close(code=1011, reason="Service unavailable")
        return
    await websocket_chat_endpoint(websocket, patient_id)
