from pydantic import BaseModel
from typing import Optional


class PatientFileRequest(BaseModel):
    pid: str
    file_name: str


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
