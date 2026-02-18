import json
import logging
from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response, HTMLResponse
from google.cloud import storage

from medforce.schemas.patient import PatientFileRequest
from medforce.schemas.admin import AdminFileSaveRequest, AdminPatientRequest

router = APIRouter()
logger = logging.getLogger("medforce-server")


@router.get("/admin", response_class=HTMLResponse)
async def get_admin_ui():
    """Serves the Admin UI HTML file."""
    try:
        with open("ui/admin_ui.html", "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        return HTMLResponse(content="<h1>Error: admin_ui.html not found on server.</h1>", status_code=404)


@router.post("/api/get-patient-file")
def get_patient_file(request: PatientFileRequest):
    """Retrieves a file from GCS for a patient."""
    BUCKET_NAME = "clinic_sim_dev"
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


@router.get("/api/admin/list-files/{pid}")
def list_patient_files(pid: str):
    """Lists all files in GCS for a specific patient ID."""
    BUCKET_NAME = "clinic_sim_dev"
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


@router.post("/api/admin/save-file")
def save_patient_file(request: AdminFileSaveRequest):
    """Creates or updates a text-based file."""
    BUCKET_NAME = "clinic_sim_dev"
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


@router.delete("/api/admin/delete-file")
def delete_admin_file(pid: str, file_name: str):
    """Deletes a file."""
    BUCKET_NAME = "clinic_sim_dev"
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


@router.get("/api/admin/list-patients")
def list_admin_patients():
    """Lists all patient folders."""
    BUCKET_NAME = "clinic_sim_dev"
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


@router.post("/api/admin/create-patient")
def create_admin_patient(request: AdminPatientRequest):
    """Creates a new patient folder by creating an initial empty file."""
    BUCKET_NAME = "clinic_sim_dev"
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


@router.delete("/api/admin/delete-patient")
def delete_admin_patient(pid: str):
    """Deletes a patient folder and ALL files inside it."""
    BUCKET_NAME = "clinic_sim_dev"
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
