import logging
from fastapi import APIRouter, HTTPException

router = APIRouter()
logger = logging.getLogger("medforce-server")

# Lazy imports
try:
    from medforce.agents import side_agent
    from medforce.managers.patient_state import patient_manager
except Exception:
    side_agent = None
    patient_manager = None


@router.post("/generate_diagnosis")
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


@router.post("/generate_report")
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


@router.post("/generate_legal")
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
