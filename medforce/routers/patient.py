from fastapi import APIRouter, HTTPException

from medforce.schemas.patient import PatientSwitchRequest

router = APIRouter()

# Lazy import to handle startup failures
try:
    from medforce.managers.patient_state import patient_manager
except Exception:
    patient_manager = None


@router.get("/patient/current")
async def get_current_patient():
    """Get current patient ID"""
    if patient_manager:
        return {"patient_id": patient_manager.get_patient_id()}
    return {"patient_id": "p0001"}


@router.post("/patient/switch")
async def switch_patient(payload: PatientSwitchRequest):
    """Switch current patient"""
    if patient_manager and payload.patient_id:
        patient_manager.set_patient_id(payload.patient_id)
        return {"status": "success", "patient_id": patient_manager.get_patient_id()}
    raise HTTPException(status_code=400, detail="Missing patient_id")
