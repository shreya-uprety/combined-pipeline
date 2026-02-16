from pydantic import BaseModel


class AdminFileSaveRequest(BaseModel):
    pid: str
    file_name: str
    content: str


class AdminPatientRequest(BaseModel):
    pid: str
