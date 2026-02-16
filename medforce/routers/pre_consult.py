import json
import uuid
import base64
import logging
import traceback
from fastapi import APIRouter, HTTPException, WebSocket

from medforce.schemas.patient import PatientRegistrationRequest, RegistrationResponse
from medforce.schemas.chat import ChatRequest, ChatResponse
from medforce.dependencies import get_chat_agent, get_gcs

router = APIRouter()
logger = logging.getLogger("medforce-server")

# Lazy imports
try:
    from medforce.agents.websocket_agent import websocket_pre_consult_endpoint
except Exception:
    websocket_pre_consult_endpoint = None


@router.post("/chat", response_model=ChatResponse)
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


@router.get("/chat/{patient_id}")
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


@router.post("/chat/{patient_id}/reset")
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


@router.get("/patients")
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


@router.post("/register", response_model=RegistrationResponse)
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


@router.websocket("/ws/pre-consult/{patient_id}")
async def websocket_pre_consult(websocket: WebSocket, patient_id: str):
    """WebSocket endpoint for real-time pre-consultation chat (Linda the admin)."""
    if websocket_pre_consult_endpoint is None:
        await websocket.close(code=1011, reason="Service unavailable")
        return
    await websocket_pre_consult_endpoint(websocket, patient_id)
