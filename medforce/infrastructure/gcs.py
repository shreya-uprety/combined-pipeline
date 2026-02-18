"""
Consolidated GCS module.
Merges: bucket_ops.py (GCSBucketManager), gcs_manager.py (GCSManager), utils.py (fetch_gcs_text_internal)
"""

import os
import json
import logging
from google.cloud import storage
from google.cloud.exceptions import NotFound, GoogleCloudError
from google.api_core.exceptions import NotFound as ApiNotFound
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("gcs-manager")


# ─────────────────────────────────────────────
# GCSBucketManager  (was bucket_ops.py)
# ─────────────────────────────────────────────

class GCSBucketManager:
    def __init__(self, bucket_name, service_account_json_path=None):
        """
        Initializes the GCS Client (lazy - only on first use).

        :param bucket_name: The name of the GCS bucket.
        :param service_account_json_path: Path to service account JSON key.
                                          If None, uses GOOGLE_APPLICATION_CREDENTIALS
                                          or default environment auth.
        """
        self.bucket_name = bucket_name
        self.service_account_json_path = service_account_json_path
        self._client = None
        self._bucket = None

    def _ensure_initialized(self):
        """Lazy initialization of GCS client and bucket"""
        if self._client is None:
            try:
                project_id = os.getenv("PROJECT_ID")
                if self.service_account_json_path:
                    self._client = storage.Client.from_service_account_json(
                        self.service_account_json_path,
                        project=project_id
                    )
                else:
                    self._client = storage.Client(project=project_id)

                self._bucket = self._client.bucket(self.bucket_name)

                if not self._bucket.exists():
                    print(f"Warning: Bucket '{self.bucket_name}' does not exist or you lack permission.")

            except Exception as e:
                print(f"Error initializing GCS Client: {e}")
                raise

    @property
    def client(self):
        self._ensure_initialized()
        return self._client

    @property
    def bucket(self):
        self._ensure_initialized()
        return self._bucket

    # CREATE / UPLOAD
    def upload_file(self, local_file_path, destination_blob_name):
        try:
            blob = self.bucket.blob(destination_blob_name)
            blob.upload_from_filename(local_file_path)
            print(f"File {local_file_path} uploaded to {destination_blob_name}.")
            return True
        except Exception as e:
            print(f"Failed to upload file: {e}")
            return False

    def create_file_from_string(self, file_content, destination_blob_name, content_type="text/plain"):
        try:
            blob = self.bucket.blob(destination_blob_name)
            blob.upload_from_string(file_content, content_type=content_type)
            print(f"Content uploaded to {destination_blob_name}.")
            return True
        except Exception as e:
            print(f"Failed to create file from string: {e}")
            return False

    # READ / DOWNLOAD
    def download_file(self, source_blob_name, local_destination_path):
        try:
            blob = self.bucket.blob(source_blob_name)
            blob.download_to_filename(local_destination_path)
            print(f"Blob {source_blob_name} downloaded to {local_destination_path}.")
            return True
        except NotFound:
            print(f"File {source_blob_name} not found in bucket.")
            return False
        except Exception as e:
            print(f"Failed to download file: {e}")
            return False

    def read_file_as_bytes(self, source_blob_name):
        try:
            blob = self.bucket.blob(source_blob_name)
            content = blob.download_as_bytes()
            return content
        except NotFound:
            print(f"File {source_blob_name} not found.")
            return None
        except Exception as e:
            print(f"Error reading file bytes: {e}")
            return None

    def read_file_as_string(self, source_blob_name):
        try:
            blob = self.bucket.blob(source_blob_name)
            content = blob.download_as_text()
            return content
        except NotFound:
            print(f"File {source_blob_name} not found.")
            return None
        except Exception as e:
            print(f"Error reading file content: {e}")
            return None

    # UPDATE
    def update_file(self, local_file_path, destination_blob_name):
        print(f"Overwriting {destination_blob_name}...")
        return self.upload_file(local_file_path, destination_blob_name)

    # DELETE
    def delete_file(self, blob_name):
        try:
            blob = self.bucket.blob(blob_name)
            blob.delete()
            print(f"Blob {blob_name} deleted.")
            return True
        except NotFound:
            print(f"Blob {blob_name} not found.")
            return False
        except Exception as e:
            print(f"Failed to delete blob: {e}")
            return False

    # UTILITIES
    def list_files(self, folder_path=None):
        prefix = folder_path if folder_path else ""
        if prefix and not prefix.endswith('/'):
            prefix += '/'

        iterator = self.client.list_blobs(self.bucket_name, prefix=prefix, delimiter='/')

        items = []
        for blob in iterator:
            if blob.name.startswith(prefix):
                relative_name = blob.name[len(prefix):]
                if relative_name:
                    items.append(relative_name)

        for p in iterator.prefixes:
            if p.startswith(prefix):
                relative_name = p[len(prefix):]
                if relative_name:
                    items.append(relative_name)

        return items

    def move_file(self, source_blob_name, target_folder):
        try:
            source_blob = self.bucket.blob(source_blob_name)
            if not source_blob.exists():
                print(f"Error: Source file '{source_blob_name}' does not exist.")
                return False

            filename = source_blob_name.split('/')[-1]
            if target_folder and not target_folder.endswith('/'):
                target_folder += '/'
            new_blob_name = f"{target_folder}{filename}"

            self.bucket.copy_blob(source_blob, self.bucket, new_blob_name)
            print(f"Copied '{source_blob_name}' to '{new_blob_name}'")

            source_blob.delete()
            print(f"Deleted original '{source_blob_name}'")
            return True
        except Exception as e:
            print(f"Failed to move file: {e}")
            return False


# ─────────────────────────────────────────────
# GCSManager  (was gcs_manager.py)
# ─────────────────────────────────────────────

class GCSManager:
    def __init__(self, bucket_name="clinic_sim_dev"):
        self.project_id = os.getenv("PROJECT_ID")
        self.bucket_name = bucket_name or os.getenv("BUCKET_NAME", "clinic_sim_dev")

        try:
            self.storage_client = storage.Client(project=self.project_id)
            self.bucket = self.storage_client.bucket(self.bucket_name)
            logger.info(f"Connected to GCS Bucket: {self.bucket_name}")
        except Exception as e:
            logger.error(f"GCS Connection Failed: {e}")
            raise

    def write_file(self, blob_name, content, content_type="text/plain"):
        try:
            blob = self.bucket.blob(blob_name)
            if isinstance(content, (dict, list)):
                content = json.dumps(content, indent=4)
                content_type = "application/json"
            blob.upload_from_string(content, content_type=content_type)
            logger.info(f"Saved: gs://{self.bucket_name}/{blob_name}")
            return True
        except Exception as e:
            logger.error(f"Write Error: {e}")
            return False

    def read_json(self, blob_name):
        try:
            blob = self.bucket.blob(blob_name)
            content = blob.download_as_text()
            return json.loads(content)
        except (NotFound, ApiNotFound):
            logger.warning(f"File not found: {blob_name}")
            return None
        except json.JSONDecodeError:
            logger.error(f"Error decoding JSON in: {blob_name}")
            return None
        except Exception as e:
            logger.error(f"Read Error: {e}")
            return None

    def read_text(self, blob_name):
        try:
            blob = self.bucket.blob(blob_name)
            return blob.download_as_text()
        except (NotFound, ApiNotFound):
            logger.warning(f"File not found: {blob_name}")
            return None
        except Exception as e:
            logger.error(f"Read Error: {e}")
            return None

    def list_files(self, prefix=None):
        blobs = self.storage_client.list_blobs(self.bucket_name, prefix=prefix)
        return [blob.name for blob in blobs]


# ─────────────────────────────────────────────
# Standalone helper  (was utils.py)
# ─────────────────────────────────────────────

def fetch_gcs_text_internal(pid: str, filename: str) -> str:
    """Fetches text content from GCS for internal logic use."""
    BUCKET_NAME = "clinic_sim_dev"
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
