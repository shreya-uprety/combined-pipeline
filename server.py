"""
MedForce Unified Server
Combines simulation, pre-consultation, board agents, and admin endpoints
into a single FastAPI application.
"""

import sys
import asyncio

# CRITICAL FIX for Windows: Must be applied BEFORE any google.genai imports
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# CRITICAL FIX: Monkey patch websockets to increase open_timeout
from contextlib import asynccontextmanager as _asynccontextmanager
from websockets.asyncio.client import connect as _original_ws_connect

@_asynccontextmanager
async def _patched_ws_connect(*args, **kwargs):
    """Patched version that adds longer timeout for Gemini Live API"""
    if 'open_timeout' not in kwargs:
        kwargs['open_timeout'] = 120  # 2 minutes instead of default 10 seconds
    async with _original_ws_connect(*args, **kwargs) as ws:
        yield ws

# Pre-import and patch google.genai.live before any other imports use it
import google.genai.live
google.genai.live.ws_connect = _patched_ws_connect

import uvicorn
import logging
import time
import json
import os
import base64
import uuid
import threading
import traceback
import pandas as pd
from fastapi import FastAPI, HTTPException, BackgroundTasks, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, HTMLResponse
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
from dotenv import load_dotenv
from google.cloud import storage

load_dotenv()

# Configure Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("medforce-server")

# Track startup time
_startup_time = time.time()
logger.info("Server initialization started...")

# === IMPORTS WITH ERROR HANDLING ===

# Simulation modules
logger.info("Importing simulation modules...")
_import_start = time.time()
try:
    from transcriber_engine_new import TranscriberEngine
    from utils import fetch_gcs_text_internal
    from simulation import SimulationManager
    import simulation_scenario
    logger.info(f"Simulation modules imported ({time.time() - _import_start:.2f}s)")
except Exception as e:
    logger.error(f"Failed to import simulation modules: {e}")
    TranscriberEngine = None
    fetch_gcs_text_internal = None
    SimulationManager = None
    simulation_scenario = None

# Pre-consultation modules
logger.info("Importing my_agents...")
_import_start = time.time()
try:
    import my_agents
    from my_agents import PreConsulteAgent
    logger.info(f"my_agents imported ({time.time() - _import_start:.2f}s)")
except Exception as e:
    logger.error(f"Failed to import my_agents: {e}")
    my_agents = None
    PreConsulteAgent = None

# Schedule manager
logger.info("Importing schedule_manager...")
_import_start = time.time()
try:
    import schedule_manager
    logger.info(f"schedule_manager imported ({time.time() - _import_start:.2f}s)")
except Exception as e:
    logger.error(f"Failed to import schedule_manager: {e}")
    schedule_manager = None

# GCS bucket operations
logger.info("Importing bucket_ops...")
_import_start = time.time()
try:
    import bucket_ops
    logger.info(f"bucket_ops imported ({time.time() - _import_start:.2f}s)")
except Exception as e:
    logger.error(f"Failed to import bucket_ops: {e}")
    bucket_ops = None

# Board agent modules (Agent-2.9)
logger.info("Importing board agent modules...")
_import_start = time.time()
try:
    import chat_model
    import side_agent
    import canvas_ops
    from patient_manager import patient_manager
    logger.info(f"Board agent modules imported ({time.time() - _import_start:.2f}s)")
except Exception as e:
    logger.error(f"Failed to import board agent modules: {e}")
    chat_model = None
    side_agent = None
    canvas_ops = None
    patient_manager = None

# WebSocket agent
logger.info("Importing websocket_agent...")
_import_start = time.time()
try:
    from websocket_agent import websocket_pre_consult_endpoint, websocket_chat_endpoint, get_websocket_agent
    logger.info(f"websocket_agent imported ({time.time() - _import_start:.2f}s)")
except Exception as e:
    logger.error(f"Failed to import websocket_agent: {e}")
    websocket_pre_consult_endpoint = None
    websocket_chat_endpoint = None
    get_websocket_agent = None

# Voice WebSocket handler
logger.info("Importing VoiceWebSocketHandler...")
_import_start = time.time()
try:
    from voice_websocket_handler import VoiceWebSocketHandler
    logger.info(f"VoiceWebSocketHandler imported ({time.time() - _import_start:.2f}s)")
except Exception as e:
    logger.error(f"Failed to import VoiceWebSocketHandler: {e}")
    VoiceWebSocketHandler = None

# Voice session manager (two-phase connections)
try:
    from voice_session_manager import voice_session_manager, SessionStatus
    logger.info("Voice Session Manager imported")
except ImportError as e:
    voice_session_manager = None
    SessionStatus = None
    logger.warning(f"Voice Session Manager not available: {e}")

logger.info(f"All imports completed in {time.time() - _startup_time:.2f}s total")


# === INITIALIZE FASTAPI APP ===

app = FastAPI(title="MedForce Unified Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# === LAZY INITIALIZATION ===

chat_agent = None
gcs = None


def get_chat_agent():
    """Lazy initialization of PreConsulteAgent"""
    global chat_agent
    if chat_agent is None and PreConsulteAgent:
        try:
            logger.info("Initializing PreConsulteAgent (lazy)...")
            chat_agent = PreConsulteAgent()
            logger.info("PreConsulteAgent initialized successfully")
        except Exception as e:
            logger.error(f"PreConsulteAgent initialization failed: {e}")
    return chat_agent


def get_gcs():
    """Lazy initialization of GCS Bucket Manager"""
    global gcs
    if gcs is None and bucket_ops:
        try:
            logger.info("Initializing GCS Bucket Manager (lazy)...")
            gcs = bucket_ops.GCSBucketManager(bucket_name="clinic_sim")
            logger.info("GCS Bucket Manager initialized successfully")
        except Exception as e:
            logger.error(f"GCS Bucket Manager initialization failed: {e}")
    return gcs


# === STARTUP EVENT ===

@app.on_event("startup")
async def startup_event():
    """Log startup information and pre-warm models"""
    port = os.environ.get("PORT", "8080")
    logger.info("=" * 60)
    logger.info("MedForce Unified Server Starting")
    logger.info(f"Listening on port: {port}")
    logger.info(f"Chat Agent: {'Ready' if chat_model else 'Not Available'}")
    logger.info(f"Voice Agent: {'Ready' if VoiceWebSocketHandler else 'Not Available'}")
    logger.info(f"Canvas Operations: {'Ready' if canvas_ops else 'Not Available'}")
    logger.info(f"PreConsulteAgent: {'Ready (lazy init)' if PreConsulteAgent else 'Not Available'}")
    logger.info(f"GCS Manager: {'Ready (lazy init)' if bucket_ops else 'Not Available'}")
    logger.info(f"Simulation: {'Ready' if SimulationManager else 'Not Available'}")
    logger.info("=" * 60)

    # Start voice session cleanup task
    if voice_session_manager:
        voice_session_manager.start_cleanup_task()

    # Pre-warm models to avoid cold start delay on first request
    logger.info("Pre-warming Gemini models...")
    try:
        if chat_model:
            await asyncio.get_event_loop().run_in_executor(
                None, chat_model._get_model
            )
            logger.info("  Chat model warmed up")

        if side_agent:
            side_agent._get_model("prompt_tool_call.txt")
            logger.info("  Side agent model warmed up")

        logger.info("Model pre-warming complete!")
    except Exception as e:
        logger.warning(f"Model pre-warming failed (will warm on first request): {e}")


# ============================================
# PYDANTIC MODELS
# ============================================

class PatientFileRequest(BaseModel):
    pid: str
    file_name: str

class AdminFileSaveRequest(BaseModel):
    pid: str
    file_name: str
    content: str

class AdminPatientRequest(BaseModel):
    pid: str

class PatientSwitchRequest(BaseModel):
    patient_id: str

class PatientRegistrationRequest(BaseModel):
    first_name: str
    last_name: str
    dob: str
    gender: str
    occupation: Optional[str] = None
    marital_status: Optional[str] = None
    phone: str
    email: str
    address: Optional[str] = None
    emergency_name: Optional[str] = None
    emergency_relation: Optional[str] = None
    emergency_phone: Optional[str] = None
    chief_complaint: str
    medical_history: Optional[str] = "None"
    allergies: Optional[str] = "None"

class RegistrationResponse(BaseModel):
    patient_id: str
    status: str

class SlotResponse(BaseModel):
    available_slots: List[dict]

class ScheduleRequestBase(BaseModel):
    clinician_id: str
    date: str
    time: str

class ScheduleBase(BaseModel):
    clinician_id: str

class ScheduleBasePatient(BaseModel):
    patient: str
    date: str
    time: str

class SwitchSchedule(ScheduleBase):
    item1: Optional[ScheduleBasePatient] = None
    item2: Optional[ScheduleBasePatient] = None

class UpdateSlotRequest(ScheduleRequestBase):
    patient: Optional[str] = None
    status: Optional[str] = None

class FileAttachment(BaseModel):
    filename: str
    content_base64: str

class ChatRequest(BaseModel):
    patient_id: str
    patient_message: str
    patient_attachments: Optional[List[FileAttachment]] = None
    patient_form: Optional[dict] = None

class ChatResponse(BaseModel):
    patient_id: str
    nurse_response: dict
    status: str


# ============================================
# BASIC ENDPOINTS
# ============================================

@app.get("/")
async def root():
    return {
        "status": "MedForce Unified Server is Running",
        "features": ["chat", "voice", "canvas_operations", "simulation", "pre_consultation", "admin"],
        "endpoints": {
            "chat": "/send-chat",
            "voice_ws": "/ws/voice/{patient_id}",
            "chat_ws": "/ws/chat/{patient_id}",
            "canvas_focus": "/api/canvas/focus",
            "canvas_todo": "/api/canvas/create-todo",
            "generate_diagnosis": "/generate_diagnosis",
            "generate_report": "/generate_report",
            "pre_consult": "/chat",
            "simulation": "/ws/simulation",
            "transcriber": "/ws/transcriber",
            "admin": "/admin"
        }
    }


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "service": "medforce-unified",
        "port": os.environ.get("PORT", 8080)
    }


# ============================================
# PATIENT MANAGEMENT
# ============================================

@app.get("/patient/current")
async def get_current_patient():
    """Get current patient ID"""
    if patient_manager:
        return {"patient_id": patient_manager.get_patient_id()}
    return {"patient_id": "p0001"}


@app.post("/patient/switch")
async def switch_patient(payload: PatientSwitchRequest):
    """Switch current patient"""
    if patient_manager and payload.patient_id:
        patient_manager.set_patient_id(payload.patient_id)
        return {"status": "success", "patient_id": patient_manager.get_patient_id()}
    raise HTTPException(status_code=400, detail="Missing patient_id")


# ============================================
# BOARD CHAT (Agent-2.9)
# ============================================

@app.post("/send-chat")
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


@app.websocket("/ws/chat/{patient_id}")
async def websocket_chat(websocket: WebSocket, patient_id: str):
    """WebSocket endpoint for real-time general chat with RAG + tools."""
    if websocket_chat_endpoint is None:
        await websocket.close(code=1011, reason="Service unavailable")
        return
    await websocket_chat_endpoint(websocket, patient_id)


# ============================================
# VOICE ENDPOINTS
# ============================================

@app.websocket("/ws/voice/{patient_id}")
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


@app.post("/api/voice/start/{patient_id}")
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


@app.get("/api/voice/status/{session_id}")
async def get_voice_session_status(session_id: str):
    """Phase 2: Check if voice session is ready."""
    if voice_session_manager is None:
        raise HTTPException(status_code=503, detail="Voice session manager not available")

    status = voice_session_manager.get_status(session_id)
    if status["status"] == "not_found":
        raise HTTPException(status_code=404, detail="Session not found")
    return status


@app.delete("/api/voice/session/{session_id}")
async def close_voice_session(session_id: str):
    """Close a voice session and free resources."""
    if voice_session_manager is None:
        raise HTTPException(status_code=503, detail="Voice session manager not available")

    await voice_session_manager.close_session(session_id)
    return {"status": "closed", "session_id": session_id}


@app.websocket("/ws/voice-session/{session_id}")
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


# ============================================
# CANVAS OPERATIONS
# ============================================

@app.post("/api/canvas/focus")
async def canvas_focus(payload: dict):
    """Focus on a board item"""
    try:
        if patient_manager and payload.get('patient_id'):
            patient_manager.set_patient_id(payload['patient_id'])

        object_id = payload.get('object_id') or payload.get('objectId')
        if not object_id and payload.get('query'):
            context = json.dumps(canvas_ops.get_board_items())
            object_id = await side_agent.resolve_object_id(payload['query'], context)

        if object_id:
            result = await canvas_ops.focus_item(object_id)
            return {"status": "success", "object_id": object_id, "data": result}
        return {"status": "error", "message": "Could not resolve object_id"}
    except Exception as e:
        logger.error(f"Error focusing board item: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/canvas/create-todo")
async def canvas_create_todo(payload: dict):
    """Create a TODO task on the board"""
    try:
        if patient_manager and payload.get('patient_id'):
            patient_manager.set_patient_id(payload['patient_id'])

        query = payload.get('query') or payload.get('description')
        if query:
            result = await side_agent.generate_todo(query)
            return {"status": "success", "data": result}
        return {"status": "error", "message": "Query/description required"}
    except Exception as e:
        logger.error(f"Error creating TODO: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/canvas/send-to-easl")
async def canvas_send_to_easl(payload: dict):
    """Send a clinical question to EASL"""
    try:
        if patient_manager and payload.get('patient_id'):
            patient_manager.set_patient_id(payload['patient_id'])

        question = payload.get('question') or payload.get('query')
        if question:
            result = await side_agent.trigger_easl(question)
            return {"status": "success", "data": result}
        return {"status": "error", "message": "Question required"}
    except Exception as e:
        logger.error(f"Error sending to EASL: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/canvas/prepare-easl-query")
async def canvas_prepare_easl_query(payload: dict):
    """Prepare an EASL query by generating context and refined question."""
    try:
        if patient_manager and payload.get('patient_id'):
            patient_manager.set_patient_id(payload['patient_id'])

        question = payload.get('question') or payload.get('query')
        if not question:
            return {"status": "error", "message": "Question required"}

        result = await side_agent.prepare_easl_query(question)
        return {"status": "success", "data": result}
    except Exception as e:
        logger.error(f"Error preparing EASL query: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/canvas/create-schedule")
async def canvas_create_schedule(payload: dict):
    """Create a schedule on the board using AI to generate structured data"""
    try:
        if patient_manager and payload.get('patient_id'):
            patient_manager.set_patient_id(payload['patient_id'])

        query = payload.get('schedulingContext', payload.get('query', 'Create a follow-up appointment schedule'))
        context = payload.get('context', '')
        result = await side_agent.create_schedule(query, context)
        return {"status": "success", "data": result}
    except Exception as e:
        logger.error(f"Error creating schedule: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/canvas/send-notification")
async def canvas_send_notification(payload: dict):
    """Send a notification"""
    try:
        if patient_manager and payload.get('patient_id'):
            patient_manager.set_patient_id(payload['patient_id'])

        result = await canvas_ops.create_notification(payload)
        return {"status": "success", "data": result}
    except Exception as e:
        logger.error(f"Error sending notification: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/canvas/create-lab-results")
async def canvas_create_lab_results(payload: dict):
    """Create lab results on the board"""
    try:
        if patient_manager and payload.get('patient_id'):
            patient_manager.set_patient_id(payload['patient_id'])

        # Transform lab results to board API format with range object
        raw_labs = payload.get('labResults', [])
        transformed_labs = []

        for lab in raw_labs:
            # Parse range string like "7-56" to range object
            range_str = lab.get('range') or lab.get('normalRange', '0-100')
            try:
                if isinstance(range_str, str) and '-' in range_str:
                    range_parts = range_str.split('-')
                    min_val = float(range_parts[0].strip())
                    max_val = float(range_parts[1].strip())
                else:
                    min_val, max_val = 0, 100
            except Exception as e:
                print(f"Error parsing range '{range_str}' for {lab.get('name') or lab.get('parameter')}: {e}")
                min_val, max_val = 0, 100

            value = lab.get('value')
            value_str = str(value) if value is not None else "0"

            unit = lab.get('unit', '')
            if unit is None or unit == '':
                unit = '-'

            status = lab.get('status', 'normal').lower()
            if status in ['high', 'low', 'abnormal']:
                try:
                    val_float = float(value)
                    if status == 'high':
                        status = 'critical' if val_float > (max_val * 2) else 'warning'
                    elif status == 'low':
                        status = 'critical' if val_float < (min_val * 0.5) else 'warning'
                except:
                    status = 'warning'
            elif status == 'normal':
                status = 'optimal'

            transformed_labs.append({
                "parameter": lab.get('name') or lab.get('parameter'),
                "value": value_str,
                "unit": str(unit),
                "status": status,
                "range": {
                    "min": min_val,
                    "max": max_val,
                    "warningMin": min_val,
                    "warningMax": max_val
                },
                "trend": lab.get('trend', 'stable')
            })

        lab_payload = {
            "labResults": transformed_labs,
            "date": payload.get('date', datetime.now().strftime('%Y-%m-%d')),
            "source": payload.get('source', 'Agent Generated'),
            "patientId": patient_manager.get_patient_id()
        }

        result = await canvas_ops.create_lab(lab_payload)
        return {"status": "success", "data": result}
    except Exception as e:
        logger.error(f"Error creating lab results: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/canvas/create-agent-result")
async def canvas_create_agent_result(payload: dict):
    """Create an agent analysis result on the board"""
    try:
        if patient_manager and payload.get('patient_id'):
            patient_manager.set_patient_id(payload['patient_id'])

        agent_payload = {
            "title": payload.get('title', 'Agent Analysis Result'),
            "content": payload.get('content') or payload.get('markdown', ''),
            "agentName": payload.get('agentName', 'Clinical Agent'),
            "timestamp": datetime.now().isoformat()
        }

        result = await canvas_ops.create_result(agent_payload)
        return {"status": "success", "data": result}
    except Exception as e:
        logger.error(f"Error creating agent result: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/canvas/board-items/{patient_id}")
async def get_board_items_api(patient_id: str):
    """Get all board items for a patient"""
    try:
        if patient_manager:
            patient_manager.set_patient_id(patient_id, quiet=True)

        items = canvas_ops.get_board_items(quiet=True)
        return {"status": "success", "patient_id": patient_id, "items": items, "count": len(items)}
    except Exception as e:
        logger.error(f"Error getting board items: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================
# REPORT GENERATION
# ============================================

@app.post("/generate_diagnosis")
async def gen_diagnosis(payload: dict):
    """Generate DILI diagnosis"""
    try:
        if patient_manager and payload.get('patient_id'):
            patient_manager.set_patient_id(payload['patient_id'])

        result = await side_agent.create_dili_diagnosis()
        return {"status": "done", "data": result}
    except Exception as e:
        logger.error(f"Error generating diagnosis: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/generate_report")
async def gen_report(payload: dict):
    """Generate patient report"""
    try:
        if patient_manager and payload.get('patient_id'):
            patient_manager.set_patient_id(payload['patient_id'])

        result = await side_agent.create_patient_report()
        return {"status": "done", "data": result}
    except Exception as e:
        logger.error(f"Error generating report: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/generate_legal")
async def gen_legal(payload: dict):
    """Generate legal report"""
    try:
        if patient_manager and payload.get('patient_id'):
            patient_manager.set_patient_id(payload['patient_id'])

        result = await side_agent.create_legal_doc()
        return {"status": "done", "data": result}
    except Exception as e:
        logger.error(f"Error generating legal report: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================
# PRE-CONSULTATION (Linda)
# ============================================

@app.post("/chat", response_model=ChatResponse)
async def handle_chat(payload: ChatRequest):
    """
    Receives JSON payload with Base64 encoded files.
    Decodes files -> Saves to GCS -> Passes filenames to Agent.
    """
    logger.info(f"Received message from patient: {payload.patient_id}")

    try:
        # 1. HANDLE FILE UPLOADS (Base64 -> GCS)
        filenames_for_agent = []

        if payload.patient_attachments:
            for att in payload.patient_attachments:
                try:
                    if "," in att.content_base64:
                        header, encoded = att.content_base64.split(",", 1)
                    else:
                        encoded = att.content_base64

                    file_bytes = base64.b64decode(encoded)
                    file_path = f"patient_data/{payload.patient_id}/raw_data/{att.filename}"

                    content_type = "application/octet-stream"
                    if att.filename.lower().endswith(".png"): content_type = "image/png"
                    elif att.filename.lower().endswith(".jpg"): content_type = "image/jpeg"
                    elif att.filename.lower().endswith(".pdf"): content_type = "application/pdf"

                    agent = get_chat_agent()
                    agent.gcs.create_file_from_string(
                        file_bytes,
                        file_path,
                        content_type=content_type
                    )

                    filenames_for_agent.append(att.filename)
                    logger.info(f"Saved file via Base64: {att.filename}")

                except Exception as e:
                    logger.error(f"Failed to decode file {att.filename}: {e}")

        # 2. PREPARE AGENT INPUT
        agent_input = {
            "patient_message": payload.patient_message,
            "patient_attachment": filenames_for_agent,
            "patient_form": payload.patient_form
        }

        # 3. CALL AGENT
        agent = get_chat_agent()
        if not agent:
            raise HTTPException(status_code=503, detail="PreConsulteAgent not available")

        response_data = await agent.pre_consulte_agent(
            user_request=agent_input,
            patient_id=payload.patient_id
        )

        return ChatResponse(
            patient_id=payload.patient_id,
            nurse_response=response_data,
            status="success"
        )

    except FileNotFoundError:
        logger.error(f"Patient data not found for ID: {payload.patient_id}")
        raise HTTPException(status_code=404, detail="Patient data not found.")

    except Exception as e:
        logger.error(f"Error processing chat: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/chat/{patient_id}")
async def get_chat_history(patient_id: str):
    """Retrieves the full chat history for a specific patient."""
    try:
        file_path = f"patient_data/{patient_id}/pre_consultation_chat.json"
        agent = get_chat_agent()
        if not agent:
            raise HTTPException(status_code=503, detail="PreConsulteAgent not available")

        content_str = agent.gcs.read_file_as_string(file_path)
        if not content_str:
            raise HTTPException(status_code=404, detail="Chat history file is empty or missing.")

        history_data = json.loads(content_str)
        return history_data

    except Exception as e:
        logger.error(f"Error fetching chat history for {patient_id}: {str(e)}")
        raise HTTPException(status_code=404, detail=f"Chat history not found for patient {patient_id}")


@app.post("/chat/{patient_id}/reset")
async def reset_chat_history(patient_id: str):
    """Resets the chat history for a specific patient to the default initial greeting."""
    try:
        default_chat_state = {
            "conversation": [
                {
                    'sender': 'admin',
                    'message': 'Hello, this is Linda the Hepatology Clinic admin desk. How can I help you today?'
                }
            ]
        }

        file_path = f"patient_data/{patient_id}/pre_consultation_chat.json"
        json_content = json.dumps(default_chat_state, indent=4)

        agent = get_chat_agent()
        if not agent:
            raise HTTPException(status_code=503, detail="PreConsulteAgent not available")

        agent.gcs.create_file_from_string(
            json_content,
            file_path,
            content_type="application/json"
        )

        logger.info(f"Chat history reset for patient: {patient_id}")

        return {
            "status": "success",
            "message": "Chat history has been reset.",
            "current_state": default_chat_state
        }

    except Exception as e:
        logger.error(f"Error resetting chat for {patient_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to reset chat: {str(e)}")


@app.get("/patients")
async def get_patients():
    """Retrieves a list of all patient IDs."""
    patient_pool = []
    try:
        agent = get_chat_agent()
        if not agent:
            raise HTTPException(status_code=503, detail="PreConsulteAgent not available")

        file_list = agent.gcs.list_files("patient_data")
        for p in file_list:
            try:
                patient_id = p.replace('/', "")
                basic_data = json.loads(agent.gcs.read_file_as_string(f"patient_data/{patient_id}/basic_info.json"))
                patient_pool.append(basic_data)
            except Exception as e:
                print(f"Error reading basic info for {p}: {e}")
        return patient_pool
    except Exception as e:
        traceback.print_exc()
        logger.error(f"Error fetching patient list: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to retrieve patient list: {e}")


@app.post("/register", response_model=RegistrationResponse)
async def register_patient(patient: PatientRegistrationRequest):
    """Receives patient data, saves it, and returns an ID."""
    print(f"--- Receiving Registration Request for {patient.first_name} {patient.last_name} ---")
    print(f"Complaint: {patient.chief_complaint}")

    patient_data = patient.dict()
    patient_id = f"PT-{str(uuid.uuid4())[:8].upper()}"
    patient_data["patient_id"] = patient_id

    file_path = f"patient_data/{patient_id}/patient_form.json"
    json_content = json.dumps(patient_data, indent=4)

    gcs_client = get_gcs()
    gcs_client.create_file_from_string(
        json_content,
        file_path,
        content_type="application/json"
    )

    return {
        "patient_id": patient_id,
        "status": "Patient profile created successfully."
    }


@app.websocket("/ws/pre-consult/{patient_id}")
async def websocket_pre_consult(websocket: WebSocket, patient_id: str):
    """WebSocket endpoint for real-time pre-consultation chat (Linda the admin)."""
    if websocket_pre_consult_endpoint is None:
        await websocket.close(code=1011, reason="Service unavailable")
        return
    await websocket_pre_consult_endpoint(websocket, patient_id)


# ============================================
# DATA PROCESSING
# ============================================

@app.get("/process/{patient_id}/preconsult")
async def process_pre_consult(patient_id: str):
    """Process pre-consultation data for a patient."""
    try:
        data_process = my_agents.RawDataProcessing()
        await data_process.process_raw_data(patient_id)

        return {
            "status": "success",
            "message": "Pre-consultation data processed."
        }

    except Exception as e:
        logger.error(f"Error processing patient for {patient_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to process patient: {str(e)}")


@app.get("/process/{patient_id}/board")
async def process_board(patient_id: str):
    """Process board/dashboard content for a patient."""
    try:
        data_process = my_agents.RawDataProcessing()
        await data_process.process_dashboard_content(patient_id)

        return {
            "status": "success",
            "message": "Board objects have been processed."
        }

    except Exception as e:
        logger.error(f"Error processing patient for {patient_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to process patient: {str(e)}")


@app.get("/process/{patient_id}/board-update")
async def process_board_update(patient_id: str):
    """Process board object updates for a patient."""
    try:
        data_process = my_agents.RawDataProcessing()
        await data_process.process_board_object(patient_id)

        return {
            "status": "success",
            "message": "Board objects have been processed."
        }

    except Exception as e:
        logger.error(f"Error processing patient for {patient_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to process patient: {str(e)}")


@app.get("/data/{patient_id}/{file_path}")
async def get_patient_data(patient_id: str, file_path: str):
    """Get a data file for a patient."""
    try:
        agent = get_chat_agent()
        if not agent:
            raise HTTPException(status_code=503, detail="PreConsulteAgent not available")

        blob_file_path = f"patient_data/{patient_id}/{file_path}"
        content_str = agent.gcs.read_file_as_string(blob_file_path)
        data_json = json.loads(content_str)
        return data_json

    except Exception as e:
        logger.error(f"Error get data {file_path}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to get data: {str(e)}")


@app.get("/image/{patient_id}/{file_path}")
async def get_image(patient_id: str, file_path: str):
    """Get an image file for a patient."""
    try:
        agent = get_chat_agent()
        if not agent:
            raise HTTPException(status_code=503, detail="PreConsulteAgent not available")

        byte_data = agent.gcs.read_file_as_bytes(f"patient_data/{patient_id}/raw_data/{file_path}")
        return {
            "file": file_path,
            "data": byte_data
        }

    except Exception as e:
        logger.error(f"Error getting image for {patient_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to get image: {str(e)}")


# ============================================
# SCHEDULING
# ============================================

@app.get("/schedule/{clinician_id}")
async def get_schedule(clinician_id: str):
    """Get schedule for a clinician."""
    try:
        if clinician_id.startswith("N"):
            doc_file = "nurse_schedule.csv"
        elif clinician_id.startswith("D"):
            doc_file = "doctor_schedule.csv"
        else:
            raise HTTPException(status_code=400, detail="Invalid Clinician ID prefix")

        gcs_client = get_gcs()
        schedule_ops = schedule_manager.ScheduleCSVManager(gcs_manager=gcs_client, csv_blob_path=f"clinic_data/{doc_file}")
        return schedule_ops.get_all()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting schedule for {clinician_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to get schedule: {str(e)}")


@app.post("/schedule/update")
async def update_schedule(request: UpdateSlotRequest):
    """Update a schedule slot (mark as done, break, cancelled, etc.)."""
    try:
        if request.clinician_id.startswith("N"):
            doc_file = "nurse_schedule.csv"
        elif request.clinician_id.startswith("D"):
            doc_file = "doctor_schedule.csv"
        else:
            raise HTTPException(status_code=400, detail="Invalid Clinician ID prefix")

        gcs_client = get_gcs()
        schedule_ops = schedule_manager.ScheduleCSVManager(
            gcs_manager=gcs_client,
            csv_blob_path=f"clinic_data/{doc_file}"
        )

        updates = {}
        if request.patient is not None:
            updates["patient"] = request.patient
        if request.status is not None:
            updates["status"] = request.status

        if not updates:
            return {"message": "No changes requested."}

        success = schedule_ops.update_slot(
            nurse_id=request.clinician_id,
            date=request.date,
            time=request.time,
            updates=updates
        )

        if not success:
            raise HTTPException(status_code=404, detail="Slot not found.")

        return {"message": "Schedule updated successfully."}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating schedule: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/schedule/switch")
async def switch_schedule(request: SwitchSchedule):
    """Switch two schedule slots."""
    try:
        if request.clinician_id.startswith("N"):
            doc_file = "nurse_schedule.csv"
        elif request.clinician_id.startswith("D"):
            doc_file = "doctor_schedule.csv"
        else:
            raise HTTPException(status_code=400, detail="Invalid Clinician ID prefix")

        gcs_client = get_gcs()
        schedule_ops = schedule_manager.ScheduleCSVManager(
            gcs_manager=gcs_client,
            csv_blob_path=f"clinic_data/{doc_file}"
        )

        # Update slot 1
        updates = {}
        if request.item1.patient is not None:
            updates["patient"] = request.item1.patient
        if request.item1.date is not None:
            updates["date"] = request.item1.date
        if request.item1.time is not None:
            updates["time"] = request.item1.time

        if updates:
            schedule_ops.update_slot(
                nurse_id=request.clinician_id,
                date=request.item1.date,
                time=request.item1.time,
                updates=updates
            )

        # Update slot 2
        updates = {}
        if request.item2.patient is not None:
            updates["patient"] = request.item2.patient
        if request.item2.date is not None:
            updates["date"] = request.item2.date
        if request.item2.time is not None:
            updates["time"] = request.item2.time

        if updates:
            success = schedule_ops.update_slot(
                nurse_id=request.clinician_id,
                date=request.item2.date,
                time=request.item2.time,
                updates=updates
            )

            if not success:
                raise HTTPException(status_code=404, detail="Slot not found.")

        return {"message": "Schedule updated successfully."}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating schedule: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/slots", response_model=SlotResponse)
async def get_available_slots(doctor_type: Optional[str] = "General"):
    """Returns a list of available appointment slots."""
    print(f"--- Checking slots for doctor type: {doctor_type} ---")

    gcs_client = get_gcs()
    schedule_ops = schedule_manager.ScheduleCSVManager(gcs_manager=gcs_client, csv_blob_path="clinic_data/doctor_schedule.csv")
    slots = schedule_ops.get_empty_schedule()

    return {"available_slots": slots}


# ============================================
# SIMULATION
# ============================================

@app.websocket("/ws/simulation")
async def websocket_simulation(websocket: WebSocket):
    """WebSocket endpoint for text-based simulation."""
    await websocket.accept()

    manager = None
    try:
        data = await websocket.receive_json()

        if isinstance(data, dict) and data.get("type") == "start":
            patient_id = data.get("patient_id", "P0001")
            gender = data.get("gender", "Male")

            manager = SimulationManager(websocket, patient_id, gender)
            await manager.run()

    except WebSocketDisconnect:
        logger.info("Client disconnected")
        if manager:
            manager.running = False
            if hasattr(manager, 'logic_thread'):
                manager.logic_thread.stop()
    except Exception as e:
        traceback.print_exc()
        logger.error(f"WebSocket Error: {e}")
        if manager:
            manager.running = False


@app.websocket("/ws/simulation/audio")
async def websocket_simulation_audio(websocket: WebSocket):
    """WebSocket endpoint for scripted/audio-only simulation."""
    await websocket.accept()

    manager = None
    try:
        data = await websocket.receive_json()

        if isinstance(data, dict) and data.get("type") == "start":
            patient_id = data.get("patient_id", "P0001")
            script_file = data.get("script_file", "scenario_script.json")

            logger.info(f"Starting Audio Simulation for {patient_id} using {script_file}")

            manager = simulation_scenario.SimulationAudioManager(websocket, patient_id, script_file="scenario_dumps/transcript.json")
            await manager.run()

    except WebSocketDisconnect:
        logger.info("Audio Simulation Client disconnected")
        if manager:
            manager.stop()
    except Exception as e:
        traceback.print_exc()
        logger.error(f"Audio Simulation WebSocket Error: {e}")
        if manager:
            manager.stop()


@app.websocket("/ws/transcriber")
async def websocket_transcriber(websocket: WebSocket):
    """
    Main entry point for the AI Transcriber.
    Receives configuration (JSON) to start, raw audio (Bytes) to process,
    and pushes AI updates (JSON) back to the frontend.
    """
    with open("data/questions.json", "r") as file:
        questions = json.load(file)
    with open("output/question_pool.json", "w") as file:
        json.dump(questions, file, indent=4)
    with open("output/education_pool.json", "w") as file:
        json.dump([], file, indent=4)

    await websocket.accept()

    main_loop = asyncio.get_running_loop()
    engine = None

    logger.info("Frontend connected to /ws/transcriber")

    try:
        while True:
            message = await websocket.receive()

            # Handle JSON commands
            if "text" in message:
                try:
                    data = json.loads(message["text"])

                    # Manual Stop Signal {"status": True}
                    if data.get("status") is True:
                        logger.info("Frontend requested End of Consultation.")
                        if engine:
                            engine.finish_consultation()
                        else:
                            logger.warning("Frontend sent stop signal, but engine is not running.")

                    # Start Signal
                    elif data.get("type") == "start":
                        patient_id = data.get("patient_id", "P0001")
                        logger.info(f"Starting Transcriber Engine for {patient_id}")

                        patient_info = fetch_gcs_text_internal(patient_id, "patient_info.md")

                        engine = TranscriberEngine(
                            patient_id=patient_id,
                            patient_info=patient_info,
                            websocket=websocket,
                            loop=main_loop
                        )

                        stt_thread = threading.Thread(
                            target=engine.stt_loop,
                            daemon=True,
                            name=f"STT_{patient_id}"
                        )
                        stt_thread.start()

                        await websocket.send_json({
                            "type": "system",
                            "message": f"Transcriber initialized for {patient_id}"
                        })

                except json.JSONDecodeError:
                    logger.error("Received invalid JSON from frontend")

            # Handle binary audio data
            elif "bytes" in message:
                if engine and engine.running:
                    engine.add_audio(message["bytes"])

    except WebSocketDisconnect:
        logger.info("Frontend disconnected from /ws/transcriber")
    except Exception as e:
        logger.error(f"Transcriber WebSocket Error: {e}")
        traceback.print_exc()
    finally:
        if engine:
            logger.info("Stopping Transcriber Engine...")
            engine.stop()


# ============================================
# ADMIN ENDPOINTS
# ============================================

@app.get("/admin", response_class=HTMLResponse)
async def get_admin_ui():
    """Serves the Admin UI HTML file."""
    try:
        with open("ui/admin_ui.html", "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        return HTMLResponse(content="<h1>Error: admin_ui.html not found on server.</h1>", status_code=404)


@app.post("/api/get-patient-file")
def get_patient_file(request: PatientFileRequest):
    """Retrieves a file from GCS for a patient."""
    BUCKET_NAME = "clinic_sim"
    blob_path = f"patient_profile/{request.pid}/{request.file_name}"

    logger.info(f"Fetching GCS: gs://{BUCKET_NAME}/{blob_path}")

    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(blob_path)

        if not blob.exists():
            logger.warning(f"File not found: {blob_path}")
            return JSONResponse(
                status_code=404,
                content={"error": "File not found", "path": blob_path}
            )

        file_ext = request.file_name.lower().split('.')[-1]

        if file_ext == 'json':
            content = blob.download_as_text()
            return JSONResponse(content=json.loads(content))
        elif file_ext in ['md', 'txt']:
            content = blob.download_as_text()
            return Response(content=content, media_type="text/markdown")
        elif file_ext in ['png', 'jpg', 'jpeg']:
            content = blob.download_as_bytes()
            media_type = "image/png" if file_ext == 'png' else "image/jpeg"
            return Response(content=content, media_type=media_type)
        else:
            content = blob.download_as_bytes()
            return Response(content=content, media_type="application/octet-stream")

    except Exception as e:
        logger.error(f"GCS API Error: {e}")
        return JSONResponse(
            status_code=500,
            content={"error": str(e)}
        )


@app.get("/api/admin/list-files/{pid}")
def list_patient_files(pid: str):
    """Lists all files in GCS for a specific patient ID."""
    BUCKET_NAME = "clinic_sim"
    prefix = f"patient_profile/{pid}/"

    try:
        storage_client = storage.Client()
        blobs = storage_client.list_blobs(BUCKET_NAME, prefix=prefix)

        file_list = []
        for blob in blobs:
            clean_name = blob.name.replace(prefix, "")
            if clean_name:
                file_list.append({
                    "name": clean_name,
                    "full_path": blob.name,
                    "size": blob.size,
                    "updated": blob.updated.isoformat() if blob.updated else None
                })

        return JSONResponse(content={"files": file_list})
    except Exception as e:
        logger.error(f"List Files Error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/admin/save-file")
def save_patient_file(request: AdminFileSaveRequest):
    """Creates or updates a text-based file."""
    BUCKET_NAME = "clinic_sim"
    blob_path = f"patient_profile/{request.pid}/{request.file_name}"

    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(blob_path)

        blob.upload_from_string(request.content, content_type="text/plain")

        logger.info(f"Saved file: {blob_path}")
        return JSONResponse(content={"message": "File saved successfully", "path": blob_path})
    except Exception as e:
        logger.error(f"Save File Error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.delete("/api/admin/delete-file")
def delete_admin_file(pid: str, file_name: str):
    """Deletes a file."""
    BUCKET_NAME = "clinic_sim"
    blob_path = f"patient_profile/{pid}/{file_name}"

    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(blob_path)

        if blob.exists():
            blob.delete()
            logger.info(f"Deleted file: {blob_path}")
            return JSONResponse(content={"message": "File deleted successfully"})
        else:
            return JSONResponse(status_code=404, content={"error": "File not found"})

    except Exception as e:
        logger.error(f"Delete File Error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/admin/list-patients")
def list_admin_patients():
    """Lists all patient folders."""
    BUCKET_NAME = "clinic_sim"
    prefix = "patient_profile/"

    try:
        storage_client = storage.Client()
        blobs = storage_client.list_blobs(BUCKET_NAME, prefix=prefix, delimiter="/")

        list(blobs)

        patients = []
        for p in blobs.prefixes:
            parts = p.rstrip('/').split('/')
            if parts:
                patients.append(parts[-1])

        return JSONResponse(content={"patients": patients})
    except Exception as e:
        logger.error(f"List Patients Error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/admin/create-patient")
def create_admin_patient(request: AdminPatientRequest):
    """Creates a new patient folder by creating an initial empty file."""
    BUCKET_NAME = "clinic_sim"
    blob_path = f"patient_profile/{request.pid}/patient_info.md"

    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(blob_path)

        if blob.exists():
            return JSONResponse(status_code=400, content={"error": "Patient already exists"})

        blob.upload_from_string("# Patient Profile\nName: \nAge: ", content_type="text/markdown")

        return JSONResponse(content={"message": "Patient created", "pid": request.pid})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.delete("/api/admin/delete-patient")
def delete_admin_patient(pid: str):
    """Deletes a patient folder and ALL files inside it."""
    BUCKET_NAME = "clinic_sim"
    prefix = f"patient_profile/{pid}/"

    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(BUCKET_NAME)
        blobs = list(bucket.list_blobs(prefix=prefix))

        if not blobs:
            return JSONResponse(status_code=404, content={"error": "Patient not found"})

        bucket.delete_blobs(blobs)
        logger.info(f"Deleted patient folder: {prefix}")
        return JSONResponse(content={"message": f"Deleted {len(blobs)} files for patient {pid}"})

    except Exception as e:
        logger.error(f"Delete Patient Error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


# ============================================
# UTILITY ENDPOINTS
# ============================================

@app.get("/ws/sessions")
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


@app.get("/test-gemini-live")
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


@app.get("/ui/{file_path:path}")
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


# ============================================
# RUN BLOCK
# ============================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))

    import platform
    if platform.system() == "Windows":
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="info", loop="asyncio")
    else:
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
