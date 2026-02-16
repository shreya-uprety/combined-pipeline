import json
import logging
from fastapi import APIRouter, HTTPException

from medforce.dependencies import get_chat_agent

router = APIRouter()
logger = logging.getLogger("medforce-server")

# Lazy imports
try:
    from medforce.agents import pre_consult_agents as my_agents
except Exception:
    my_agents = None


@router.get("/process/{patient_id}/preconsult")
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


@router.get("/process/{patient_id}/board")
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


@router.get("/process/{patient_id}/board-update")
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


@router.get("/data/{patient_id}/{file_path}")
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


@router.get("/image/{patient_id}/{file_path}")
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
