import os
import json
import logging
from google.cloud import storage
from google.api_core.exceptions import NotFound
from dotenv import load_dotenv

load_dotenv()

# Configure Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gcs-manager")

class GCSManager:
    def __init__(self, bucket_name="clinic_sim"):
        """
        Initializes the GCS Client.
        If bucket_name is not passed, it looks for BUCKET_NAME in .env
        """
        self.project_id = os.getenv("PROJECT_ID")
        self.bucket_name = bucket_name or os.getenv("BUCKET_NAME", "clinic_sim")
        
        try:
            self.storage_client = storage.Client(project=self.project_id)
            self.bucket = self.storage_client.bucket(self.bucket_name)
            logger.info(f"✅ Connected to GCS Bucket: {self.bucket_name}")
        except Exception as e:
            logger.error(f"❌ GCS Connection Failed: {e}")
            raise

    def write_file(self, blob_name, content, content_type="text/plain"):
        """
        Writes content to a file. 
        NOTE: This will CREATE a new file or OVERWRITE if it exists.
        """
        try:
            blob = self.bucket.blob(blob_name)
            
            # Handle JSON specifically
            if isinstance(content, dict) or isinstance(content, list):
                content = json.dumps(content, indent=4)
                content_type = "application/json"
            
            blob.upload_from_string(content, content_type=content_type)
            logger.info(f"💾 Saved: gs://{self.bucket_name}/{blob_name}")
            return True
        except Exception as e:
            logger.error(f"❌ Write Error: {e}")
            return False

    def read_json(self, blob_name):
        """
        Reads a JSON file from the bucket and returns a Python Dictionary.
        """
        try:
            blob = self.bucket.blob(blob_name)
            content = blob.download_as_text()
            return json.loads(content)
        except NotFound:
            logger.warning(f"⚠️ File not found: {blob_name}")
            return None
        except json.JSONDecodeError:
            logger.error(f"❌ Error decoding JSON in: {blob_name}")
            return None
        except Exception as e:
            logger.error(f"❌ Read Error: {e}")
            return None

    def read_text(self, blob_name):
        """
        Reads a standard text/markdown file.
        """
        try:
            blob = self.bucket.blob(blob_name)
            return blob.download_as_text()
        except NotFound:
            logger.warning(f"⚠️ File not found: {blob_name}")
            return None
        except Exception as e:
            logger.error(f"❌ Read Error: {e}")
            return None

    def list_files(self, prefix=None):
        """Lists files, optionally filtered by a folder prefix."""
        blobs = self.storage_client.list_blobs(self.bucket_name, prefix=prefix)
        return [blob.name for blob in blobs]

# ==========================================
# EXAMPLE USAGE (Run this file directly)
# ==========================================
if __name__ == "__main__":
    # Ensure you set BUCKET_NAME in your .env or pass it here
    # e.g., bucket_name="my-clinic-bucket"
    gcs = GCSManager()

    # --- 1. CREATE/UPDATE ADVISOR PROTOCOL ---
    # This creates the file your simulation.py needs to read
    advisor_data = {
        "protocol_name": "Standard Admission Assessment",
        "version": "1.0",
        "steps": [
            "Ask the patient for their full name and date of birth.",
            "Ask about the main reason for their visit today.",
            "Ask specifically about pain severity on a scale of 1 to 10.",
            "Ask if they have any known drug allergies.",
            "Ask if they are currently taking any medication.",
            "Thank the patient and tell them the doctor will see them shortly."
        ]
    }

    print("\n--- Writing Advisor Script ---")
    gcs.write_file("protocols/standard_assessment.json", advisor_data)

    # --- 2. READ IT BACK ---
    print("\n--- Reading Advisor Script ---")
    data = gcs.read_json("protocols/standard_assessment.json")
    if data:
        print(f"Loaded Protocol: {data.get('protocol_name')}")
        print("Steps:")
        for i, step in enumerate(data.get("steps", [])):
            print(f"  {i+1}. {step}")

    # --- 3. CREATE PATIENT PROFILE ---
    print("\n--- Writing Patient Profile ---")
    patient_info = """
    # Patient Profile
    **Name:** John Doe
    **Condition:** Chronic Back Pain
    **History:** No known allergies.
    """
    gcs.write_file("patient_profile/P0002/patient_info.md", patient_info)
    print("Done.")