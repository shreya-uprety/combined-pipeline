from pydantic import BaseModel
from typing import Optional, List


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
