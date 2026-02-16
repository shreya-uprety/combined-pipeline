import os
from google.cloud import storage
from google.cloud.exceptions import NotFound, GoogleCloudError
from dotenv import load_dotenv

load_dotenv()

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
                    # Looks for credentials in environment variables
                    self._client = storage.Client(project=project_id)
                
                self._bucket = self._client.bucket(self.bucket_name)
                
                # Verify bucket exists (optional, but good for fast fail)
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

    # ---------------------------------------------------------
    # CREATE / UPLOAD
    # ---------------------------------------------------------
    def upload_file(self, local_file_path, destination_blob_name):
        """
        Uploads a local file to the bucket.
        """
        try:
            blob = self.bucket.blob(destination_blob_name)
            blob.upload_from_filename(local_file_path)
            print(f"File {local_file_path} uploaded to {destination_blob_name}.")
            return True
        except Exception as e:
            print(f"Failed to upload file: {e}")
            return False

    def create_file_from_string(self, file_content, destination_blob_name, content_type="text/plain"):
        """
        Creates a file directly from a string (useful for logs, json, etc).
        """
        try:
            blob = self.bucket.blob(destination_blob_name)
            blob.upload_from_string(file_content, content_type=content_type)
            print(f"Content uploaded to {destination_blob_name}.")
            return True
        except Exception as e:
            print(f"Failed to create file from string: {e}")
            return False

    # ---------------------------------------------------------
    # READ / DOWNLOAD
    # ---------------------------------------------------------
    def download_file(self, source_blob_name, local_destination_path):
        """
        Downloads a file from the bucket to the local system.
        """
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
        """
        Reads the content of a file into memory as bytes.
        Use this for IMAGES, PDFS, or ZIP files.
        """
        try:
            blob = self.bucket.blob(source_blob_name)
            # download_as_bytes returns raw binary data
            content = blob.download_as_bytes() 
            return content
        except NotFound:
            print(f"File {source_blob_name} not found.")
            return None
        except Exception as e:
            print(f"Error reading file bytes: {e}")
            return None

    def read_file_as_string(self, source_blob_name):
        """
        Reads the content of a file into memory as a string.
        """
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

    # ---------------------------------------------------------
    # UPDATE
    # ---------------------------------------------------------
    def update_file(self, local_file_path, destination_blob_name):
        """
        Updates a file in GCS. 
        Note: GCS objects are immutable. 'Updating' actually means 
        overwriting the existing object with a new upload.
        """
        print(f"Overwriting {destination_blob_name}...")
        return self.upload_file(local_file_path, destination_blob_name)

    # ---------------------------------------------------------
    # DELETE
    # ---------------------------------------------------------
    def delete_file(self, blob_name):
        """
        Deletes a file from the bucket.
        """
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

    # ---------------------------------------------------------
    # UTILITIES
    # ---------------------------------------------------------
    def list_files(self, folder_path=None):
        """
        Lists child items in a folder, returning only their relative names.
        
        Example: 
        If bucket contains 'patient_data/P001/', and you search 'patient_data',
        This returns ['P001/'] instead of ['patient_data/P001/'].
        """
        # 1. Normalize the prefix
        prefix = folder_path if folder_path else ""
        
        # Ensure prefix ends with '/' if it's not empty, to target the folder contents
        if prefix and not prefix.endswith('/'):
            prefix += '/'
            
        # 2. Get the list from GCS with delimiter
        iterator = self.client.list_blobs(self.bucket_name, prefix=prefix, delimiter='/')
        
        items = []

        # 3. Process Files (Blobs)
        for blob in iterator:
            # Check if blob.name starts with prefix (it always should)
            if blob.name.startswith(prefix):
                # Slice off the prefix
                relative_name = blob.name[len(prefix):]
                # If relative_name is empty (e.g., the folder placeholder itself), skip it
                if relative_name:
                    items.append(relative_name)

        # 4. Process Folders (Sub-directories)
        # iterator.prefixes contains the full paths of sub-folders
        for p in iterator.prefixes:
            if p.startswith(prefix):
                relative_name = p[len(prefix):]
                if relative_name:
                    items.append(relative_name)
                    
        return items


    def move_file(self, source_blob_name, target_folder):
        """
        Moves a file to a target folder within the same bucket.
        (GCS does not have a native move, so this creates a copy and deletes the original).
        
        :param source_blob_name: Full path of the file (e.g., 'inbox/data.json')
        :param target_folder: Destination folder (e.g., 'processed/')
        """
        try:
            source_blob = self.bucket.blob(source_blob_name)

            # 1. Check if source exists
            if not source_blob.exists():
                print(f"Error: Source file '{source_blob_name}' does not exist.")
                return False

            # 2. Construct the new destination path
            # Extract just the filename (e.g., 'data.json' from 'inbox/data.json')
            filename = source_blob_name.split('/')[-1]
            
            # Ensure target folder ends with '/'
            if target_folder and not target_folder.endswith('/'):
                target_folder += '/'
            
            new_blob_name = f"{target_folder}{filename}"

            # 3. Copy the blob to the new location
            # copy_blob(source_blob, destination_bucket, new_name)
            self.bucket.copy_blob(source_blob, self.bucket, new_blob_name)
            print(f"Copied '{source_blob_name}' to '{new_blob_name}'")

            # 4. Delete the original blob
            source_blob.delete()
            print(f"Deleted original '{source_blob_name}'")
            
            return True

        except Exception as e:
            print(f"Failed to move file: {e}")
            return False