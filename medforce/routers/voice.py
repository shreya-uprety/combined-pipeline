import logging
from fastapi import APIRouter, HTTPException, WebSocket

router = APIRouter()
logger = logging.getLogger("medforce-server")

# Lazy imports
try:
    from medforce.managers.patient_state import patient_manager
except Exception:
    patient_manager = None

try:
    from medforce.agents.voice_handler import VoiceWebSocketHandler
except Exception:
    VoiceWebSocketHandler = None

try:
    from medforce.agents.voice_session import voice_session_manager, SessionStatus
except ImportError:
    voice_session_manager = None
    SessionStatus = None


@router.websocket("/ws/voice/{patient_id}")
async def websocket_voice(websocket: WebSocket, patient_id: str):
    """
    WebSocket endpoint for real-time voice communication using Gemini Live API.
    Also handles two-phase connections where frontend connects here with a session_id.
    """
    if VoiceWebSocketHandler is None:
        await websocket.close(code=1011, reason="Voice service unavailable")
        return

    # Check if patient_id is actually a session_id from two-phase connection
    session = None
    if voice_session_manager is not None:
        session = await voice_session_manager.get_session(patient_id)

    await websocket.accept()

    if session is not None:
        # This is a pre-connected session — use it
        logger.info(f"Voice WebSocket: detected session_id={patient_id}, using pre-connected session for patient {session.patient_id}")
        patient_manager.set_patient_id(session.patient_id, quiet=True)
        try:
            handler = VoiceWebSocketHandler(websocket, session.patient_id)
            handler.session = session.gemini_session
            handler.audio_in_queue = session.audio_in_queue
            handler.out_queue = session.out_queue
            handler.client = session.client
            await handler.run_with_session()
        except Exception as e:
            logger.error(f"Voice WebSocket error (pre-connected): {e}")
        finally:
            await voice_session_manager.close_session(patient_id)
            try:
                await websocket.close()
            except:
                pass
    else:
        # Direct connection — check if patient_id is actually a session_id
        actual_patient_id = patient_id
        if voice_session_manager is not None:
            mapped_patient = voice_session_manager.get_patient_for_session(patient_id)
            if mapped_patient:
                logger.info(f"Recovered patient_id={mapped_patient} from session mapping for session_id={patient_id}")
                actual_patient_id = mapped_patient
            else:
                logger.info(f"No session mapping found for {patient_id}, using as patient_id directly")

        logger.info(f"Voice WebSocket: direct connection for patient: {actual_patient_id}")
        patient_manager.set_patient_id(actual_patient_id, quiet=True)
        try:
            handler = VoiceWebSocketHandler(websocket, actual_patient_id)
            await handler.run()
        except Exception as e:
            logger.error(f"Voice WebSocket error: {e}")
            try:
                await websocket.close()
            except:
                pass


@router.post("/api/voice/start/{patient_id}")
async def start_voice_session(patient_id: str):
    """Phase 1: Start connecting to Gemini Live API in background. Returns immediately with session_id."""
    logger.info(f"Voice start request received for patient: {patient_id}")
    patient_manager.set_patient_id(patient_id, quiet=True)

    if voice_session_manager is None:
        raise HTTPException(status_code=503, detail="Voice session manager not available")

    session_id = await voice_session_manager.create_session(patient_id)
    return {
        "session_id": session_id,
        "patient_id": patient_id,
        "status": "connecting",
        "poll_url": f"/api/voice/status/{session_id}",
        "websocket_url": f"/ws/voice-session/{session_id}",
        "message": "Connection started. Poll status endpoint until ready, then connect to WebSocket."
    }


@router.get("/api/voice/status/{session_id}")
async def get_voice_session_status(session_id: str):
    """Phase 2: Check if voice session is ready."""
    if voice_session_manager is None:
        raise HTTPException(status_code=503, detail="Voice session manager not available")

    status = voice_session_manager.get_status(session_id)
    if status["status"] == "not_found":
        raise HTTPException(status_code=404, detail="Session not found")
    return status


@router.delete("/api/voice/session/{session_id}")
async def close_voice_session(session_id: str):
    """Close a voice session and free resources."""
    if voice_session_manager is None:
        raise HTTPException(status_code=503, detail="Voice session manager not available")

    await voice_session_manager.close_session(session_id)
    return {"status": "closed", "session_id": session_id}


@router.websocket("/ws/voice-session/{session_id}")
async def websocket_voice_session(websocket: WebSocket, session_id: str):
    """Phase 3: WebSocket endpoint for pre-connected voice session."""
    if voice_session_manager is None:
        await websocket.close(code=1011, reason="Voice session manager not available")
        return

    session = await voice_session_manager.get_session(session_id)
    if session is None:
        await websocket.close(code=4004, reason="Session not ready or not found")
        return

    await websocket.accept()
    logger.info(f"Voice WebSocket connected for pre-established session: {session_id}, patient: {session.patient_id}")
    patient_manager.set_patient_id(session.patient_id, quiet=True)

    try:
        if VoiceWebSocketHandler is not None:
            handler = VoiceWebSocketHandler(websocket, session.patient_id)
            handler.session = session.gemini_session
            handler.audio_in_queue = session.audio_in_queue
            handler.out_queue = session.out_queue
            handler.client = session.client
            await handler.run_with_session()
        else:
            await websocket.send_json({"type": "error", "message": "Voice handler not available"})
    except Exception as e:
        logger.error(f"Voice WebSocket error: {e}")
    finally:
        await voice_session_manager.close_session(session_id)
        try:
            await websocket.close()
        except:
            pass
