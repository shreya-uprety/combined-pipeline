# --- utils.py ---
import logging
from google.cloud import storage

logger = logging.getLogger("medforce-backend")

def fetch_gcs_text_internal(pid: str, filename: str) -> str:
    """Fetches text content from GCS for internal logic use."""
    BUCKET_NAME = "clinic_sim"
    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(BUCKET_NAME)
        blob_path = f"patient_profile/{pid}/{filename}"
        blob = bucket.blob(blob_path)
        
        if not blob.exists():
            logger.warning(f"File not found in GCS: {blob_path}")
            return f"System: Error - File {filename} not found."
            
        return blob.download_as_text()
    except Exception as e:
        logger.error(f"GCS Internal Error: {e}")
        return "System: Error loading profile."