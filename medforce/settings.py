"""
Centralized configuration for MedForce.
Replaces the old config.py and scatters env-based constants.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# --- Directories ---
OUTPUT_DIR = "output"
DATA_DIR = "data"
SYSTEM_PROMPTS_DIR = "system_prompts"
UI_DIR = "ui"
PATIENT_PROFILE_DIR = "patient_profile"
RESPONSE_SCHEMA_DIR = "response_schema"
SCENARIO_DUMPS_DIR = "scenario_dumps"

os.makedirs(OUTPUT_DIR, exist_ok=True)

# --- GCS ---
GCS_BUCKET_NAME = os.getenv("GCS_BUCKET_NAME", "clinic_sim_dev")

# --- Canvas / Board ---
CANVAS_URL = os.getenv(
    "CANVAS_URL",
    "https://iso-clinic-v3-481780815788.europe-west1.run.app",
)

# --- Patient ---
DEFAULT_PATIENT_ID = os.getenv("DEFAULT_PATIENT_ID", "p0001")

# --- Server ---
PORT = int(os.getenv("PORT", "8080"))

# Backward compat: expose output_dir as the old config.py did
output_dir = OUTPUT_DIR
