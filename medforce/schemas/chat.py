from pydantic import BaseModel
from typing import Optional, List


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
