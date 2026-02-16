import pandas as pd
import io
from medforce.infrastructure.gcs import GCSBucketManager

class ScheduleCSVManager:
    # Strict column order matching your CSV
    COLUMNS = ['id', 'patient', 'date', 'time', 'status']

    def __init__(self, gcs_manager: GCSBucketManager, csv_blob_path: str):
        self.gcs = gcs_manager
        self.csv_path = csv_blob_path

    # ==========================================
    # INTERNAL HELPERS
    # ==========================================
    def _load_df(self):
        """
        Downloads CSV. 
        - Forces all columns to String type to prevent 'N0001' becoming number.
        - Fills empty cells (NaN) with empty strings "" to match your CSV structure.
        """
        csv_content = self.gcs.read_file_as_string(self.csv_path)
        
        if not csv_content:
            return pd.DataFrame(columns=self.COLUMNS)
            
        try:
            # dtype=str is crucial for IDs like 'N0001' and preserving time '8:00' vs '08:00'
            df = pd.read_csv(io.StringIO(csv_content), dtype=str)
            
            # Ensure all standard columns exist
            for col in self.COLUMNS:
                if col not in df.columns:
                    df[col] = ""
            
            # Replace NaN (empty CSV fields) with empty string
            return df.fillna("")
            
        except pd.errors.EmptyDataError:
            return pd.DataFrame(columns=self.COLUMNS)

    def _save_df(self, df):
        """Uploads DataFrame back to GCS."""
        buffer = io.StringIO()
        # index=False: Don't write row numbers
        df.to_csv(buffer, index=False)
        return self.gcs.create_file_from_string(
            buffer.getvalue(), 
            self.csv_path, 
            content_type="text/csv"
        )

    # ==========================================
    # READ OPERATIONS
    # ==========================================
    
    def get_all(self):
        """Returns the entire schedule."""
        df = self._load_df()
        return df.to_dict(orient='records')

    def get_empty_schedule(self):
        """Returns the entire schedule."""
        df = self._load_df()
        df = df[df['patient'] == ""]
        return df.to_dict(orient='records')

    def get_schedule_by_nurse_and_date(self, nurse_id, date_str):
        """
        Get a specific day's schedule for a specific nurse.
        Useful for generating the view in your provided example.
        """
        df = self._load_df()
        if df.empty: return []

        # Filter: Nurse ID matches AND Date matches
        # We use str() to ensure safety against unexpected types
        mask = (df['id'] == str(nurse_id)) & (df['date'] == str(date_str))
        filtered_df = df[mask]
        
        # Sort by time just in case (optional, depends if 'time' is sortable string)
        # Warning: '10:00' comes before '8:00' in pure string sort. 
        # For production, you might want to convert to datetime for sorting, then back to string.
        return filtered_df.to_dict(orient='records')

    # ==========================================
    # WRITE OPERATIONS
    # ==========================================

    def add_time_slot(self, nurse_id, date, time, patient="", status=""):
        """
        Adds a NEW row (time slot).
        Checks if that slot already exists for that nurse to prevent duplicates.
        """
        df = self._load_df()
        nurse_id = str(nurse_id)
        
        # Check uniqueness: Nurse + Date + Time
        if not df.empty:
            exists = df[
                (df['id'] == nurse_id) & 
                (df['date'] == date) & 
                (df['time'] == time)
            ].any().any() # .any() checks if DataFrame is not empty

            if exists:
                print(f"❌ Slot already exists: {nurse_id} on {date} at {time}")
                return False

        # Create new row
        new_row = pd.DataFrame([{
            'id': nurse_id,
            'patient': patient,
            'date': date,
            'time': time,
            'status': status
        }])
        
        df = pd.concat([df, new_row], ignore_index=True)
        print(f"✅ Added slot: {time}")
        return self._save_df(df)

    def update_slot(self, nurse_id, date, time, updates: dict):
        """
        Updates an existing slot.
        Use this to:
        1. Assign a patient (update 'patient')
        2. Change status (update 'status' to 'done' or 'break')
        """
        df = self._load_df()
        nurse_id = str(nurse_id)
        
        if df.empty: return False

        # Identify the row
        mask = (df['id'] == nurse_id) & (df['date'] == date) & (df['time'] == time)
        
        if not df[mask].any().any():
            print(f"❌ Slot not found: {nurse_id} | {date} | {time}")
            return False

        # Apply updates
        for col, val in updates.items():
            if col in self.COLUMNS:
                df.loc[mask, col] = str(val) # Force string format

        print(f"✅ Updated slot {time}: {updates}")
        return self._save_df(df)

    def delete_slot(self, nurse_id, date, time):
        """Removes the row entirely."""
        df = self._load_df()
        nurse_id = str(nurse_id)
        
        mask = (df['id'] == nurse_id) & (df['date'] == date) & (df['time'] == time)
        
        if not df[mask].any().any():
            return False
            
        # Keep rows that do NOT match
        df = df[~mask]
        print(f"🗑️ Deleted slot {time}")
        return self._save_df(df)