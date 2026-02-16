import logging
from typing import Optional
from fastapi import APIRouter, HTTPException

from medforce.schemas.schedule import (
    SlotResponse, UpdateSlotRequest, SwitchSchedule
)
from medforce.dependencies import get_gcs

router = APIRouter()
logger = logging.getLogger("medforce-server")

# Lazy imports
try:
    from medforce.managers import schedule as schedule_manager
except Exception:
    schedule_manager = None


@router.get("/schedule/{clinician_id}")
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


@router.post("/schedule/update")
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


@router.post("/schedule/switch")
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


@router.get("/slots", response_model=SlotResponse)
async def get_available_slots(doctor_type: Optional[str] = "General"):
    """Returns a list of available appointment slots."""
    print(f"--- Checking slots for doctor type: {doctor_type} ---")

    gcs_client = get_gcs()
    schedule_ops = schedule_manager.ScheduleCSVManager(gcs_manager=gcs_client, csv_blob_path="clinic_data/doctor_schedule.csv")
    slots = schedule_ops.get_empty_schedule()

    return {"available_slots": slots}
