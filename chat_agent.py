"""
General-Purpose Gemini Chat Agent with RAG and Tool Execution
================================================================

This module provides a flexible chat agent that can:
1. Retrieve relevant context from patient data (RAG)
2. Execute tools/functions based on user queries
3. Maintain conversation history
4. Stream responses

Author: AI Developer Assistant
Date: January 27, 2026
"""

import os
import json
import asyncio
import logging
from typing import List, Dict, Any, Optional, Callable
from datetime import datetime
from google import genai
from google.genai import types
import httpx
from canvas_tools import CanvasTools

from dotenv import load_dotenv
load_dotenv()

# Configure logging
logger = logging.getLogger("chat-agent")

# Model Configuration - use faster model
MODEL = "gemini-2.0-flash"
MODEL_ADVANCED = "gemini-2.0-flash"

# Cached model instance
_cached_model = None

def _get_model():
    global _cached_model
    if _cached_model is None:
        _cached_model = genai.GenerativeModel(MODEL)
    return _cached_model

# Board URL configuration
BOARD_BASE_URL = "https://iso-clinic-v3-481780815788.europe-west1.run.app"


class RAGRetriever:
    """
    Retrieval-Augmented Generation (RAG) component.
    Retrieves relevant context from patient board data via API.
    """
    
    def __init__(self, board_base_url: str = BOARD_BASE_URL):
        """
        Initialize RAG retriever.
        
        Args:
            board_base_url: Base URL for the board API
        """
        self.board_base_url = board_base_url
        
    async def retrieve_patient_context(self, patient_id: str) -> Dict[str, Any]:
        """
        Retrieve all relevant patient data from the board.
        
        Args:
            patient_id: Patient identifier
            
        Returns:
            Dictionary containing patient context data
        """
        context = {
            "patient_id": patient_id,
            "retrieved_at": datetime.now().isoformat(),
            "data": {}
        }
        
        try:
            # Fetch board items from the API - use correct endpoint
            board_url = f"{self.board_base_url}/api/board-items/patient/{patient_id}"
            logger.info(f"Fetching board data from: {board_url}")
            
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(board_url)
                response.raise_for_status()
                board_data = response.json()
                
            logger.info(f"✅ Successfully fetched board data for patient {patient_id}")
            logger.info(f"Board data structure: {type(board_data)}, keys: {list(board_data.keys()) if isinstance(board_data, dict) else 'N/A'}")
            
            # Parse and organize the board data
            if isinstance(board_data, list):
                # Board returns a list of items directly
                logger.info(f"Processing {len(board_data)} board items...")
                
                for idx, item in enumerate(board_data):
                    if not isinstance(item, dict):
                        continue
                    
                    # Log the keys of this item for debugging
                    item_keys = list(item.keys())
                    logger.info(f"Item {idx} keys: {item_keys}")
                    
                    # Extract patient profile data (highest priority)
                    if 'patientProfile' in item:
                        context["data"]["patient_profile"] = item['patientProfile']
                        logger.info(f"Found patientProfile")
                    
                    # Extract patient basic data
                    if 'patient' in item and isinstance(item['patient'], dict):
                        context["data"]["basic_info"] = item['patient']
                        logger.info(f"Found patient data: {list(item['patient'].keys())}")
                    
                    # Extract patientData
                    if 'patientData' in item:
                        if 'patient_context' not in context["data"]:
                            context["data"]["patient_context"] = {}
                        context["data"]["patient_context"].update(item['patientData'])
                        logger.info(f"Found patientData")
                    
                    # Extract primary diagnosis
                    if 'primaryDiagnosis' in item:
                        if 'patient_context' not in context["data"]:
                            context["data"]["patient_context"] = {}
                        context["data"]["patient_context"]["primaryDiagnosis"] = item['primaryDiagnosis']
                        logger.info(f"Found primaryDiagnosis")
                    
                    # Extract adverse events / risk events (flexible naming)
                    if 'adverseEvents' in item:
                        context["data"]["risk_events"] = {"events": item['adverseEvents']}
                    elif 'risks' in item:
                        context["data"]["risk_events"] = item['risks']
                    elif 'events' in item:
                        if 'risk_events' not in context["data"]:
                            context["data"]["risk_events"] = {"events": item['events']}
                    
                    # Extract medications (flexible naming)
                    if 'medications' in item:
                        context["data"]["medication_track"] = {"medications": item['medications']}
                    elif 'currentMedications' in item:
                        context["data"]["medication_track"] = {"medications": item['currentMedications']}
                    elif 'medicationTimeline' in item:
                        context["data"]["medication_track"] = item['medicationTimeline']
                    
                    # Extract lab results (flexible naming) - merge all lab data
                    if 'labResults' in item:
                        if 'lab_track' not in context["data"]:
                            context["data"]["lab_track"] = {}
                        if 'labs' not in context["data"]["lab_track"]:
                            context["data"]["lab_track"]["labs"] = []
                        if isinstance(item['labResults'], list):
                            context["data"]["lab_track"]["labs"].extend(item['labResults'])
                        else:
                            context["data"]["lab_track"]["labs"].append(item['labResults'])
                        logger.info(f"Found labResults: {len(item['labResults']) if isinstance(item['labResults'], list) else 1} items")
                    
                    if 'labs' in item:
                        if 'lab_track' not in context["data"]:
                            context["data"]["lab_track"] = {}
                        if 'labs' not in context["data"]["lab_track"]:
                            context["data"]["lab_track"]["labs"] = []
                        if isinstance(item['labs'], list):
                            context["data"]["lab_track"]["labs"].extend(item['labs'])
                        else:
                            context["data"]["lab_track"]["labs"].append(item['labs'])
                        logger.info(f"Found labs: {len(item['labs']) if isinstance(item['labs'], list) else 1} items")
                    
                    if 'biomarkers' in item:
                        if 'lab_track' not in context["data"]:
                            context["data"]["lab_track"] = {}
                        if 'biomarkers' not in context["data"]["lab_track"]:
                            context["data"]["lab_track"]["biomarkers"] = []
                        if isinstance(item['biomarkers'], list):
                            context["data"]["lab_track"]["biomarkers"].extend(item['biomarkers'])
                        else:
                            context["data"]["lab_track"]["biomarkers"].append(item['biomarkers'])
                        logger.info(f"Found biomarkers: {len(item['biomarkers']) if isinstance(item['biomarkers'], list) else 1} items")
                    
                    if 'chartData' in item:
                        if 'lab_track' not in context["data"]:
                            context["data"]["lab_track"] = {}
                        context["data"]["lab_track"]["chartData"] = item['chartData']
                        logger.info(f"Found chartData for labs")
                    
                    # Extract encounters/visits (flexible naming)
                    if 'encounters' in item and isinstance(item['encounters'], list):
                        if 'encounters' not in context["data"]:
                            context["data"]["encounters"] = {"encounters": []}
                        context["data"]["encounters"]["encounters"].extend(item['encounters'])
                    elif 'encounter' in item:
                        if 'encounters' not in context["data"]:
                            context["data"]["encounters"] = {"encounters": []}
                        context["data"]["encounters"]["encounters"].append(item['encounter'])
                    elif 'visits' in item:
                        context["data"]["encounters"] = {"encounters": item['visits']}
                    
                    # Extract clinical actions/recommendations
                    if 'clinicalActions' in item:
                        context["data"]["clinical_actions"] = item['clinicalActions']
                    
                    # Extract risk analysis
                    if 'riskAnalysis' in item:
                        context["data"]["risk_analysis"] = item['riskAnalysis']
                    
                    # Extract any specialty-specific data (respiratory, cardiovascular, etc.)
                    # Store them generically so they're available regardless of specialty
                    specialty_fields = [
                        'respiratoryData', 'pulmonaryFunction', 'spirometry',
                        'cardiovascularData', 'echocardiogram', 'ekg',
                        'neurologicalData', 'imagingStudies', 'biopsyResults',
                        'vitalSigns', 'symptoms', 'physicalExam',
                        'allergyData', 'immunizations', 'socialHistory',
                        'familyHistory', 'procedures', 'consultations'
                    ]
                    
                    for field in specialty_fields:
                        if field in item:
                            context["data"][field] = item[field]
                            logger.info(f"Found specialty data: {field}")
                    
                    # Don't store raw fields - we've already normalized the important ones above
                    # This prevents duplicate data with different key names
                        
            elif isinstance(board_data, dict) and "items" in board_data:
                items = board_data["items"]
                
                # Organize items by type
                for item in items:
                    item_type = item.get("type", "unknown")
                    
                    if item_type == "patient_context":
                        context["data"]["patient_context"] = item.get("data", {})
                    elif item_type == "basic_info":
                        context["data"]["basic_info"] = item.get("data", {})
                    elif item_type == "encounters":
                        context["data"]["encounters"] = item.get("data", {})
                    elif item_type == "lab_track" or item_type == "dashboard_lab_track":
                        context["data"]["lab_track"] = item.get("data", {})
                    elif item_type == "medication_track" or item_type == "dashboard_medication_track":
                        context["data"]["medication_track"] = item.get("data", {})
                    elif item_type == "risk_events" or item_type == "dashboard_risk_event_track":
                        context["data"]["risk_events"] = item.get("data", {})
                    elif item_type == "referral":
                        context["data"]["referral"] = item.get("data", {})
                        
            elif isinstance(board_data, list):
                # If the API returns a list directly
                for item in board_data:
                    item_type = item.get("type", "unknown")
                    
                    if item_type == "patient_context":
                        context["data"]["patient_context"] = item.get("data", {})
                    elif item_type == "basic_info":
                        context["data"]["basic_info"] = item.get("data", {})
                    elif item_type == "encounters":
                        context["data"]["encounters"] = item.get("data", {})
                    elif item_type == "lab_track" or item_type == "dashboard_lab_track":
                        context["data"]["lab_track"] = item.get("data", {})
                    elif item_type == "medication_track" or item_type == "dashboard_medication_track":
                        context["data"]["medication_track"] = item.get("data", {})
                    elif item_type == "risk_events" or item_type == "dashboard_risk_event_track":
                        context["data"]["risk_events"] = item.get("data", {})
                    elif item_type == "referral":
                        context["data"]["referral"] = item.get("data", {})
            else:
                # Store the raw data
                context["data"]["raw_board_data"] = board_data
                
            logger.info(f"Parsed board data types: {list(context['data'].keys())}")
            
        except httpx.HTTPError as e:
            logger.error(f"HTTP error fetching board data: {e}")
            context["data"]["error"] = str(e)
        except Exception as e:
            logger.error(f"Error fetching board data: {e}")
            import traceback
            traceback.print_exc()
            context["data"]["error"] = str(e)
                
        return context
    
    def retrieve_medical_knowledge(self, query: str) -> str:
        """
        Retrieve relevant medical knowledge based on query.
        This is a placeholder for future vector database integration.
        
        Args:
            query: User query text
            
        Returns:
            Relevant medical knowledge snippets
        """
        # TODO: Implement vector database retrieval (e.g., ChromaDB, Pinecone)
        # For now, return empty context
        logger.info(f"Knowledge retrieval requested for: {query}")
        return ""


class ToolExecutor:
    """
    Handles tool/function execution for the chat agent.
    Supports various medical and administrative tools.
    """
    
    def __init__(self, context_data_ref: Optional[Dict] = None):
        """
        Initialize tool executor.
        
        Args:
            context_data_ref: Reference to context data (from board)
        """
        self.context_data_ref = context_data_ref
        self.canvas_tools = CanvasTools()  # Initialize canvas manipulation tools
        self.tools = self._register_tools()
        
    def _register_tools(self) -> Dict[str, Callable]:
        """
        Register available tools/functions.
        
        Returns:
            Dictionary mapping tool names to functions
        """
        return {
            "get_patient_labs": self.get_patient_labs,
            "get_patient_medications": self.get_patient_medications,
            "get_patient_encounters": self.get_patient_encounters,
            "search_patient_data": self.search_patient_data,
            "calculate_drug_interaction": self.calculate_drug_interaction,
            # Canvas manipulation tools
            "focus_board_item": self.focus_board_item,
            "create_todo": self.create_todo,
            "send_easl_query": self.send_easl_query,
            "create_schedule": self.create_schedule,
            "send_notification": self.send_notification,
            "send_message_to_patient": self.send_message_to_patient,
            "create_doctor_note": self.create_doctor_note,
            "create_diagnosis_report": self.create_diagnosis_report,
            "create_patient_report": self.create_patient_report,
            "create_legal_report": self.create_legal_report,
        }
    
    def get_tool_declarations(self) -> List[types.Tool]:
        """
        Get Gemini-compatible tool declarations.
        
        Returns:
            List of tool declarations for Gemini API
        """
        tools = [
            types.Tool(
                function_declarations=[
                    types.FunctionDeclaration(
                        name="get_patient_labs",
                        description="Retrieve laboratory test results for a patient. Returns chronological lab values including dates, biomarker names, values, and reference ranges.",
                        parameters={
                            "type": "object",
                            "properties": {
                                "patient_id": {
                                    "type": "string",
                                    "description": "The patient ID (e.g., P0001)"
                                },
                                "biomarker": {
                                    "type": "string",
                                    "description": "Optional: specific biomarker to retrieve (e.g., 'ALT', 'Bilirubin'). If not specified, returns all."
                                }
                            },
                            "required": ["patient_id"]
                        }
                    ),
                    types.FunctionDeclaration(
                        name="get_patient_medications",
                        description="Retrieve current and past medications for a patient. Returns medication timeline with dates, doses, and indications.",
                        parameters={
                            "type": "object",
                            "properties": {
                                "patient_id": {
                                    "type": "string",
                                    "description": "The patient ID (e.g., P0001)"
                                },
                                "active_only": {
                                    "type": "boolean",
                                    "description": "If true, returns only currently active medications"
                                }
                            },
                            "required": ["patient_id"]
                        }
                    ),
                    types.FunctionDeclaration(
                        name="get_patient_encounters",
                        description="Retrieve past medical encounters/visits for a patient. Returns visit dates, providers, diagnoses, and treatments.",
                        parameters={
                            "type": "object",
                            "properties": {
                                "patient_id": {
                                    "type": "string",
                                    "description": "The patient ID (e.g., P0001)"
                                },
                                "limit": {
                                    "type": "integer",
                                    "description": "Maximum number of encounters to return (default: 10)"
                                }
                            },
                            "required": ["patient_id"]
                        }
                    ),
                    types.FunctionDeclaration(
                        name="search_patient_data",
                        description="Search across all patient data for specific keywords or conditions. Useful for finding specific mentions of symptoms, diagnoses, or treatments.",
                        parameters={
                            "type": "object",
                            "properties": {
                                "patient_id": {
                                    "type": "string",
                                    "description": "The patient ID (e.g., P0001)"
                                },
                                "query": {
                                    "type": "string",
                                    "description": "Search query (e.g., 'jaundice', 'liver failure')"
                                }
                            },
                            "required": ["patient_id", "query"]
                        }
                    ),
                    types.FunctionDeclaration(
                        name="calculate_drug_interaction",
                        description="Check for potential drug-drug interactions between medications. Returns interaction severity and clinical recommendations.",
                        parameters={
                            "type": "object",
                            "properties": {
                                "drug_a": {
                                    "type": "string",
                                    "description": "First medication name"
                                },
                                "drug_b": {
                                    "type": "string",
                                    "description": "Second medication name"
                                }
                            },
                            "required": ["drug_a", "drug_b"]
                        }
                    ),
                    # Canvas manipulation tools
                    types.FunctionDeclaration(
                        name="focus_board_item",
                        description="Navigate to and focus on a specific item on the clinical board (e.g., medication timeline, lab chart, patient profile). This will zoom to and highlight the requested board element.",
                        parameters={
                            "type": "object",
                            "properties": {
                                "patient_id": {
                                    "type": "string",
                                    "description": "The patient ID"
                                },
                                "object_description": {
                                    "type": "string",
                                    "description": "Description of the board item to focus on (e.g., 'medication timeline', 'lab results chart', 'patient profile')"
                                }
                            },
                            "required": ["patient_id", "object_description"]
                        }
                    ),
                    types.FunctionDeclaration(
                        name="create_todo",
                        description="Create a TODO/task list on the clinical board with specific action items for the care team.",
                        parameters={
                            "type": "object",
                            "properties": {
                                "patient_id": {
                                    "type": "string",
                                    "description": "The patient ID"
                                },
                                "title": {
                                    "type": "string",
                                    "description": "Title of the TODO list"
                                },
                                "description": {
                                    "type": "string",
                                    "description": "Brief description of the task"
                                },
                                "tasks": {
                                    "type": "array",
                                    "description": "Array of task descriptions (strings)",
                                    "items": {
                                        "type": "string"
                                    }
                                }
                            },
                            "required": ["patient_id", "title", "tasks"]
                        }
                    ),
                    types.FunctionDeclaration(
                        name="send_easl_query",
                        description="Send a clinical question to the EASL (European Association for the Study of the Liver) guideline system for expert liver disease recommendations. The answer will appear in an iframe on the board.",
                        parameters={
                            "type": "object",
                            "properties": {
                                "patient_id": {
                                    "type": "string",
                                    "description": "The patient ID"
                                },
                                "question": {
                                    "type": "string",
                                    "description": "Clinical question for EASL guidelines (e.g., 'What is the RUCAM score for this DILI case?')"
                                }
                            },
                            "required": ["patient_id", "question"]
                        }
                    ),
                    types.FunctionDeclaration(
                        name="create_schedule",
                        description="Create a scheduling panel on the board for coordinating follow-up appointments, investigations, and care coordination.",
                        parameters={
                            "type": "object",
                            "properties": {
                                "patient_id": {
                                    "type": "string",
                                    "description": "The patient ID"
                                },
                                "title": {
                                    "type": "string",
                                    "description": "Schedule title"
                                },
                                "details": {
                                    "type": "string",
                                    "description": "Appointment or investigation details"
                                }
                            },
                            "required": ["patient_id", "title", "details"]
                        }
                    ),
                    types.FunctionDeclaration(
                        name="send_notification",
                        description="Send a notification message to the care team or other healthcare providers.",
                        parameters={
                            "type": "object",
                            "properties": {
                                "patient_id": {
                                    "type": "string",
                                    "description": "The patient ID"
                                },
                                "message": {
                                    "type": "string",
                                    "description": "Notification message content"
                                }
                            },
                            "required": ["patient_id", "message"]
                        }
                    ),
                    types.FunctionDeclaration(
                        name="send_message_to_patient",
                        description="Send a message to the patient via the patient chat system.",
                        parameters={
                            "type": "object",
                            "properties": {
                                "patient_id": {
                                    "type": "string",
                                    "description": "The patient ID"
                                },
                                "message": {
                                    "type": "string",
                                    "description": "The message to send to the patient"
                                }
                            },
                            "required": ["patient_id", "message"]
                        }
                    ),
                    types.FunctionDeclaration(
                        name="create_doctor_note",
                        description="Create a doctor or nurse note on the board.",
                        parameters={
                            "type": "object",
                            "properties": {
                                "patient_id": {
                                    "type": "string",
                                    "description": "The patient ID"
                                },
                                "content": {
                                    "type": "string",
                                    "description": "The content of the doctor/nurse note"
                                }
                            },
                            "required": ["patient_id", "content"]
                        }
                    ),
                    types.FunctionDeclaration(
                        name="create_diagnosis_report",
                        description="Create a DILI (Drug-Induced Liver Injury) diagnostic report on the board with detailed assessment.",
                        parameters={
                            "type": "object",
                            "properties": {
                                "patient_id": {
                                    "type": "string",
                                    "description": "The patient ID"
                                },
                                "summary": {
                                    "type": "string",
                                    "description": "Brief diagnostic summary"
                                }
                            },
                            "required": ["patient_id", "summary"]
                        }
                    ),
                    types.FunctionDeclaration(
                        name="create_patient_report",
                        description="Create a comprehensive patient summary report on the board.",
                        parameters={
                            "type": "object",
                            "properties": {
                                "patient_id": {
                                    "type": "string",
                                    "description": "The patient ID"
                                },
                                "summary": {
                                    "type": "string",
                                    "description": "Patient summary"
                                }
                            },
                            "required": ["patient_id", "summary"]
                        }
                    ),
                    types.FunctionDeclaration(
                        name="create_legal_report",
                        description="Create a legal compliance report on the board covering consent, duty of candour, and guideline adherence.",
                        parameters={
                            "type": "object",
                            "properties": {
                                "patient_id": {
                                    "type": "string",
                                    "description": "The patient ID"
                                },
                                "summary": {
                                    "type": "string",
                                    "description": "Legal compliance summary"
                                }
                            },
                            "required": ["patient_id", "summary"]
                        }
                    ),
                ]
            )
        ]
        return tools
    
    def execute_tool(self, tool_name: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute a tool by name with given parameters.
        
        Args:
            tool_name: Name of the tool to execute
            parameters: Tool parameters
            
        Returns:
            Tool execution result
        """
        if tool_name not in self.tools:
            return {
                "error": f"Tool '{tool_name}' not found",
                "available_tools": list(self.tools.keys())
            }
        
        try:
            result = self.tools[tool_name](**parameters)
            logger.info(f"Executed tool: {tool_name}")
            return result
        except Exception as e:
            logger.error(f"Tool execution error ({tool_name}): {e}")
            return {"error": str(e)}
    
    # Tool Implementation Methods
    
    def get_patient_labs(self, patient_id: str, biomarker: Optional[str] = None) -> Dict[str, Any]:
        """Retrieve patient laboratory results from board context."""
        try:
            # Get from loaded board context
            lab_data = None
            if self.context_data_ref and isinstance(self.context_data_ref, dict):
                context = self.context_data_ref.get("data", {})
                lab_data = context.get("lab_track") or context.get("labs") or context.get("labResults")
                logger.info(f"Lab data lookup: lab_track exists={bool(context.get('lab_track'))}, type={type(lab_data)}")
                if lab_data and isinstance(lab_data, dict):
                    logger.info(f"Lab data keys: {list(lab_data.keys())}")
            
            # If not in context, return not found
            if not lab_data:
                logger.warning(f"No lab data in context for patient {patient_id}")
                return {"status": "not_found", "message": f"No laboratory results found in patient board for {patient_id}."}
            
            # Handle both list and dict formats - check all possible keys
            biomarkers = []
            if isinstance(lab_data, list):
                biomarkers = lab_data
            elif isinstance(lab_data, dict):
                # Try multiple keys where lab data might be stored
                biomarkers = lab_data.get("biomarkers", []) or lab_data.get("labs", []) or lab_data.get("labResults", [])
                
                # If still empty, check if chartData has biomarkers
                if not biomarkers and "chartData" in lab_data:
                    chart_data = lab_data["chartData"]
                    if isinstance(chart_data, dict) and "biomarkers" in chart_data:
                        biomarkers = chart_data["biomarkers"]
            
            logger.info(f"Extracted {len(biomarkers)} biomarkers from lab data")
            
            if biomarker:
                # Filter for specific biomarker
                filtered = [item for item in biomarkers 
                           if biomarker.lower() in str(item.get("name", "")).lower()]
                return {"status": "success", "biomarkers": filtered, "count": len(filtered)}
            
            return {"status": "success", "biomarkers": biomarkers, "count": len(biomarkers)}
        except Exception as e:
            logger.error(f"Error getting lab data for patient {patient_id}: {e}")
            import traceback
            traceback.print_exc()
            return {"status": "error", "message": f"Error retrieving laboratory results: {str(e)}"}
    
    def get_patient_medications(self, patient_id: str, active_only: bool = False) -> Dict[str, Any]:
        """Retrieve patient medications from board context."""
        try:
            # Get from loaded board context
            med_data = None
            if self.context_data_ref and isinstance(self.context_data_ref, dict):
                context = self.context_data_ref.get("data", {})
                med_data = context.get("medication_track") or context.get("medications") or context.get("medicationTimeline")
            
            # If not in context, return not found
            if not med_data:
                return {"status": "not_found", "message": f"No medication records found in patient board for {patient_id}."}
            
            # Handle both list and dict formats
            if isinstance(med_data, list):
                medications = med_data
            elif isinstance(med_data, dict):
                medications = med_data.get("medications", [])
            else:
                medications = []
            
            if active_only:
                # Filter for active medications (no end date or future end date)
                current_date = datetime.now().isoformat()
                filtered = [med for med in medications
                           if not med.get("endDate") or med.get("endDate") > current_date]
                return {"status": "success", "medications": filtered, "count": len(filtered)}
            
            return {"status": "success", "medications": medications, "count": len(medications)}
        except Exception as e:
            logger.warning(f"No medication data for patient {patient_id}: {e}")
            return {"status": "not_found", "message": f"No medication records found for patient {patient_id}."}
    
    def get_patient_encounters(self, patient_id: str, limit: int = 10) -> Dict[str, Any]:
        """Retrieve patient encounters from board context."""
        try:
            # Get from loaded board context
            encounter_data = None
            if self.context_data_ref and isinstance(self.context_data_ref, dict):
                context = self.context_data_ref.get("data", {})
                encounter_data = context.get("encounters") or context.get("encounter")
            
            # If not in context, return not found
            if not encounter_data:
                return {
                    "status": "not_found",
                    "message": f"No encounter records found in patient board for {patient_id}."
                }
            
            # Handle both list and dict formats
            if isinstance(encounter_data, list):
                encounters = encounter_data[:limit]
            elif isinstance(encounter_data, dict):
                encounters = encounter_data.get("encounters", [])[:limit]
            else:
                encounters = []
            
            return {
                "status": "success",
                "encounters": encounters,
                "count": len(encounters)
            }
        except Exception as e:
            logger.warning(f"No encounter data for patient {patient_id}: {e}")
            return {
                "status": "not_found",
                "message": f"No encounter records found for patient {patient_id}. This patient may not have any documented encounters yet."
            }
    
    def search_patient_data(self, patient_id: str, query: str) -> Dict[str, Any]:
        """Search patient data for query in loaded board context."""
        try:
            results = []
            query_lower = query.lower()
            query_words = query_lower.split()  # Split query into words for better matching
            
            if not self.context_data_ref or not isinstance(self.context_data_ref, dict):
                return {
                    "status": "error",
                    "message": "Patient context not loaded from board. Please ensure the patient board data is available.",
                    "query": query
                }
            
            context = self.context_data_ref.get("data", {})
            
            # Search through all context data
            for key, value in context.items():
                value_str = json.dumps(value, indent=2) if isinstance(value, (dict, list)) else str(value)
                value_lower = value_str.lower()
                
                # Check if query or any query word matches
                if query_lower in value_lower or any(word in value_lower for word in query_words if len(word) > 2):
                    results.append({
                        "source": key,
                        "snippet": self._extract_snippet(value_str, query, context_chars=300),
                        "relevance": "high" if query_lower in value_lower else "partial"
                    })
            
            # Return results (even if empty)
            if results:
                logger.info(f"Search found {len(results)} results in board context for query: {query}")
                return {
                    "status": "success",
                    "query": query,
                    "results": results,
                    "count": len(results),
                    "source": "board_context"
                }
            else:
                logger.info(f"No results found in board context for query: {query}")
                return {
                    "status": "success",
                    "query": query,
                    "results": [],
                    "count": 0,
                    "message": "No matching data found in patient board context.",
                    "source": "board_context"
                }
        except Exception as e:
            logger.error(f"Search failed: {e}")
            return {"status": "error", "error": f"Search failed: {e}"}
    
    def calculate_drug_interaction(self, drug_a: str, drug_b: str) -> Dict[str, Any]:
        """
        Check drug-drug interactions.
        This is a placeholder - in production, integrate with a drug database API.
        """
        # TODO: Integrate with DrugBank API or similar service
        logger.info(f"Drug interaction check: {drug_a} + {drug_b}")
        
        return {
            "drug_a": drug_a,
            "drug_b": drug_b,
            "note": "Drug interaction checking requires external API integration (e.g., DrugBank, RxNorm). This is a placeholder implementation.",
            "severity": "unknown",
            "recommendation": "Please consult a pharmacist or drug interaction database."
        }
    
    # Canvas manipulation tool wrappers
    def focus_board_item(self, patient_id: str, object_description: str) -> Dict[str, Any]:
        """Focus on a specific board item."""
        return asyncio.run(self.canvas_tools.focus_board_item(patient_id, object_description))
    
    def create_todo(self, patient_id: str, title: str, tasks: List[str], description: str = "") -> Dict[str, Any]:
        """Create TODO list on board."""
        # Convert simple task strings to task objects
        task_objects = []
        for idx, task in enumerate(tasks, 1):
            task_objects.append({
                "id": f"task-{idx}",
                "text": task,
                "status": "pending",
                "agent": "Care Team",
                "subTodos": []
            })
        return asyncio.run(self.canvas_tools.create_todo_on_board(patient_id, title, description or title, task_objects))
    
    def send_easl_query(self, patient_id: str, question: str) -> Dict[str, Any]:
        """Send query to EASL guideline system."""
        return asyncio.run(self.canvas_tools.send_to_easl(patient_id, question))
    
    def create_schedule(self, patient_id: str, title: str, details: str) -> Dict[str, Any]:
        """Create scheduling panel on board."""
        schedule_data = {
            "title": title,
            "currentStatus": "Pending",
            "details": details
        }
        return asyncio.run(self.canvas_tools.create_schedule(patient_id, schedule_data))
    
    def send_notification(self, patient_id: str, message: str) -> Dict[str, Any]:
        """Send notification to care team."""
        return asyncio.run(self.canvas_tools.send_notification(patient_id, message))

    def send_message_to_patient(self, patient_id: str, message: str) -> Dict[str, Any]:
        """Send a message to the patient."""
        return asyncio.run(self.canvas_tools.send_message_to_patient(patient_id, message))

    def create_doctor_note(self, patient_id: str, content: str) -> Dict[str, Any]:
        """Create a doctor/nurse note on the board."""
        return asyncio.run(self.canvas_tools.create_doctor_note(patient_id, content))
    
    def create_diagnosis_report(self, patient_id: str, summary: str) -> Dict[str, Any]:
        """Create diagnosis report on board."""
        diagnosis_data = {
            "summary": summary,
            "timestamp": datetime.now().isoformat()
        }
        return asyncio.run(self.canvas_tools.create_diagnosis_report(patient_id, diagnosis_data))
    
    def create_patient_report(self, patient_id: str, summary: str) -> Dict[str, Any]:
        """Create patient summary report on board."""
        report_data = {
            "summary": summary,
            "timestamp": datetime.now().isoformat()
        }
        return asyncio.run(self.canvas_tools.create_patient_report(patient_id, report_data))
    
    def create_legal_report(self, patient_id: str, summary: str) -> Dict[str, Any]:
        """Create legal compliance report on board."""
        legal_data = {
            "summary": summary,
            "timestamp": datetime.now().isoformat()
        }
        return asyncio.run(self.canvas_tools.create_legal_report(patient_id, legal_data))
    
    @staticmethod
    def _extract_snippet(text: str, query: str, context_chars: int = 200) -> str:
        """Extract text snippet around query match."""
        lower_text = text.lower()
        lower_query = query.lower()
        
        pos = lower_text.find(lower_query)
        if pos == -1:
            return ""
        
        start = max(0, pos - context_chars)
        end = min(len(text), pos + len(query) + context_chars)
        
        snippet = text[start:end]
        if start > 0:
            snippet = "..." + snippet
        if end < len(text):
            snippet = snippet + "..."
        
        return snippet


class ChatAgent:
    """
    Main chat agent class with RAG and tool execution capabilities.
    Maintains conversation history and provides intelligent responses.
    """
    
    def __init__(self, patient_id: Optional[str] = None, use_tools: bool = True):
        """
        Initialize chat agent.
        
        Args:
            patient_id: Optional patient ID for context
            use_tools: Whether to enable tool execution
        """
        self.client = genai.Client(
            api_key=os.getenv("GOOGLE_API_KEY")
        )
        
        self.retriever = RAGRetriever(board_base_url=BOARD_BASE_URL)
        
        self.patient_id = patient_id
        self.conversation_history: List[Dict[str, str]] = []
        self.context_data: Optional[Dict] = None
        self._context_loading = False
        self._context_loaded = False
        self._context_lock = asyncio.Lock()
        
        # Initialize tool executor with reference to context data
        self.tool_executor = ToolExecutor(self.context_data) if use_tools else None
        
        # Load patient context if patient_id provided
        if patient_id:
            asyncio.create_task(self._load_patient_context())
    
    async def _load_patient_context(self):
        """Load patient context data for RAG."""
        async with self._context_lock:
            # Skip if already loaded or currently loading
            if self._context_loaded or self._context_loading:
                return
            
            self._context_loading = True
            
            try:
                logger.info(f"Loading context for patient {self.patient_id}...")
                self.context_data = await self.retriever.retrieve_patient_context(self.patient_id)
                
                # Update tool executor's context reference
                if self.tool_executor:
                    self.tool_executor.context_data_ref = self.context_data
                
                # Log what was retrieved
                if self.context_data and self.context_data.get("data"):
                    data_keys = list(self.context_data["data"].keys())
                    logger.info(f"✅ Loaded context for patient {self.patient_id}: {data_keys}")
                else:
                    logger.warning(f"⚠️ No context data found for patient {self.patient_id}")
                
                self._context_loaded = True
            except Exception as e:
                logger.error(f"❌ Failed to load patient context: {e}")
                import traceback
                traceback.print_exc()
                self.context_data = None
            finally:
                self._context_loading = False
    
    async def reload_context(self):
        """Reload patient context data (useful if data has been updated)."""
        if self.patient_id:
            self._context_loaded = False
            self._context_loading = False
            await self._load_patient_context()
    
    def _build_context_prompt(self) -> str:
        """Build context prompt from retrieved data."""
        if not self.context_data or not self.context_data.get("data"):
            return ""
        
        context_parts = ["\n=== PATIENT CONTEXT (USE THIS TO ANSWER QUESTIONS) ===\n\n"]
        
        data = self.context_data["data"]
        
        # Add patient profile first (most comprehensive)
        if data.get("patient_profile"):
            profile = data["patient_profile"]
            context_parts.append("## Patient Profile\n")
            if isinstance(profile, dict):
                for key, value in profile.items():
                    if key not in ['id', 'x', 'y', 'width', 'height', 'zone', 'componentType']:
                        context_parts.append(f"- {key}: {value}\n")
            else:
                context_parts.append(f"{profile}\n")
            context_parts.append("\n")
        
        # Add basic info (name, demographics)
        if data.get("basic_info"):
            basic_info = data["basic_info"]
            context_parts.append("## Basic Patient Information\n")
            if isinstance(basic_info, dict):
                for key, value in basic_info.items():
                    if key not in ['id', 'x', 'y', 'width', 'height', 'zone', 'componentType']:
                        context_parts.append(f"- {key}: {value}\n")
            else:
                context_parts.append(f"{basic_info}\n")
            context_parts.append("\n")
        
        # Add patient profile (most important)
        if data.get("patient_profile"):
            context_parts.append(f"## Patient Profile\n{data['patient_profile']}\n\n")
        
        # Add structured data summaries
        if data.get("patient_context"):
            context_parts.append(f"## Clinical Summary\n{json.dumps(data['patient_context'], indent=2)}\n\n")
        
        # Add encounters
        if data.get("encounters"):
            encounters = data["encounters"]
            if isinstance(encounters, dict) and encounters.get("encounters"):
                enc_list = encounters["encounters"]
                context_parts.append(f"## Recent Encounters ({len(enc_list)} total)\n")
                for enc in enc_list[:3]:  # Show top 3
                    context_parts.append(f"- {enc.get('date')}: {enc.get('type')} - {enc.get('summary', 'N/A')}\n")
                context_parts.append("\n")
        
        # Add medications
        if data.get("medication_track"):
            meds_data = data["medication_track"]
            if isinstance(meds_data, dict):
                meds = meds_data.get("medications", [])
                if meds:
                    context_parts.append(f"## Current Medications ({len(meds)} total)\n")
                    for med in meds[:5]:  # Show top 5
                        context_parts.append(f"- {med.get('name')}: {med.get('dose', 'N/A')}\n")
                    context_parts.append("\n")
        
        # Add lab tracking
        if data.get("lab_track"):
            lab_data = data["lab_track"]
            if isinstance(lab_data, dict) and lab_data.get("biomarkers"):
                context_parts.append(f"## Lab Results Summary\n")
                biomarkers = lab_data.get("biomarkers", [])
                for biomarker in biomarkers[:5]:  # Show top 5
                    name = biomarker.get("name", "Unknown")
                    latest = biomarker.get("latest_value", "N/A")
                    context_parts.append(f"- {name}: {latest}\n")
                context_parts.append("\n")
        
        # Add risk events
        if data.get("risk_events"):
            risk_data = data["risk_events"]
            if isinstance(risk_data, dict) and risk_data.get("events"):
                events = risk_data["events"]
                if events:
                    context_parts.append(f"## Risk Events ({len(events)} total)\n")
                    for event in events[:3]:
                        context_parts.append(f"- {event.get('date')}: {event.get('type')} - {event.get('description', 'N/A')}\n")
                    context_parts.append("\n")
        
        # Add specialty-specific data sections dynamically
        specialty_sections = {
            'respiratoryData': ('Respiratory Data', 'Pulmonary/Respiratory'),
            'pulmonaryFunction': ('Pulmonary Function Tests', 'PFT Results'),
            'spirometry': ('Spirometry Results', 'Spirometry'),
            'cardiovascularData': ('Cardiovascular Data', 'Cardiac'),
            'echocardiogram': ('Echocardiogram Results', 'Echo'),
            'ekg': ('EKG/ECG Results', 'EKG'),
            'neurologicalData': ('Neurological Data', 'Neuro'),
            'imagingStudies': ('Imaging Studies', 'Imaging'),
            'biopsyResults': ('Biopsy Results', 'Biopsy'),
            'vitalSigns': ('Vital Signs', 'Vitals'),
            'symptoms': ('Current Symptoms', 'Symptoms'),
            'physicalExam': ('Physical Examination', 'PE'),
            'allergyData': ('Allergies', 'Allergies'),
            'immunizations': ('Immunizations', 'Vaccines'),
            'socialHistory': ('Social History', 'SH'),
            'familyHistory': ('Family History', 'FH'),
            'procedures': ('Procedures', 'Procedures'),
            'consultations': ('Consultations', 'Consults'),
            'clinical_actions': ('Clinical Actions', 'Actions'),
            'risk_analysis': ('Risk Analysis', 'Risk Assessment')
        }
        
        # Add any specialty-specific data that exists
        for field_key, (section_title, short_name) in specialty_sections.items():
            if data.get(field_key):
                field_data = data[field_key]
                context_parts.append(f"## {section_title}\n")
                
                if isinstance(field_data, dict):
                    # Show key-value pairs
                    for key, value in field_data.items():
                        if key not in ['id', 'x', 'y', 'width', 'height', 'zone', 'componentType']:
                            if isinstance(value, (list, dict)):
                                # Show count for complex structures
                                if isinstance(value, list):
                                    context_parts.append(f"- {key}: {len(value)} items\n")
                                else:
                                    context_parts.append(f"- {key}: {json.dumps(value, indent=2)}\n")
                            else:
                                context_parts.append(f"- {key}: {value}\n")
                elif isinstance(field_data, list):
                    # Show list items
                    context_parts.append(f"({len(field_data)} items)\n")
                    for item in field_data[:3]:  # Show first 3
                        if isinstance(item, dict):
                            summary = item.get('summary') or item.get('name') or item.get('title') or str(item)[:100]
                            context_parts.append(f"- {summary}\n")
                        else:
                            context_parts.append(f"- {item}\n")
                    if len(field_data) > 3:
                        context_parts.append(f"... and {len(field_data) - 3} more\n")
                else:
                    # Simple string or other type
                    context_parts.append(f"{field_data}\n")
                
                context_parts.append("\n")
        
        context_parts.append("=== END PATIENT CONTEXT ===\n\n")
        context_parts.append("IMPORTANT: Use the above patient context to answer questions about the patient's name, demographics, medical history, medications, lab results, encounters, and any specialty-specific data (respiratory, cardiovascular, etc.). The board dynamically shows relevant data for this patient's condition. Do not say you don't have access to this information if it's provided above.\n\n")
        
        return "".join(context_parts)
    
    async def chat(self, message: str, system_instruction: Optional[str] = None) -> str:
        """
        Send a message and get a response.
        
        Args:
            message: User message
            system_instruction: Optional system instruction override
            
        Returns:
            Agent response text
        """
        # Default system instruction - try to load from file
        if not system_instruction:
            try:
                with open("system_prompts/system_prompt.md", "r", encoding="utf-8") as f:
                    base_prompt = f.read()
                
                system_instruction = f"""{base_prompt}

--- PATIENT-SPECIFIC CONTEXT ---
Current Patient ID: {self.patient_id}
Board URL: https://iso-clinic-v3-481780815788.europe-west1.run.app/board/{self.patient_id}

CRITICAL INSTRUCTIONS:
- You are currently helping with patient ID: {self.patient_id}
- When using tools, ALWAYS use patient_id: {self.patient_id}
"""
            except Exception as e:
                logger.warning(f"Failed to load system prompt, using fallback: {e}")
                system_instruction = """You are an expert medical AI assistant specializing in hepatology and clinical care.
            
Your capabilities:
1. Answer medical questions using the patient context provided
2. Retrieve specific data using available tools
3. Provide evidence-based clinical reasoning
4. Maintain patient confidentiality

Guidelines:
- Always reference specific data from the patient context when available
- If you need more information, use the available tools
- Be clear about limitations and uncertainties
- Use medical terminology appropriately but explain complex concepts
- Prioritize patient safety in all recommendations

When patient context is provided, use it to give personalized answers."""

        # Wait for context with timeout - don't block too long
        if self.patient_id and not self._context_loaded:
            try:
                await asyncio.wait_for(self._load_patient_context(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning(f"⚠️ Context loading timeout for patient {self.patient_id}, proceeding without full context")

        # Build full prompt with context
        context_prompt = self._build_context_prompt()
        full_message = f"{context_prompt}\n\nUser Question: {message}"
        
        # Debug logging
        if context_prompt:
            logger.info(f"✅ Including patient context in prompt ({len(context_prompt)} chars)")
        else:
            logger.warning(f"⚠️ Limited patient context for {self.patient_id} - using tools for data retrieval")
        
        # Add to conversation history
        self.conversation_history.append({
            "role": "user",
            "content": message
        })
        
        try:
            # Prepare config
            config = types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.3,  # Lower for more factual responses
            )
            
            # Add tools if enabled
            if self.tool_executor:
                config.tools = self.tool_executor.get_tool_declarations()
            
            # Make API call
            response = await self.client.aio.models.generate_content(
                model=MODEL,
                contents=full_message,
                config=config
            )
            
            # Handle tool calls if present
            if hasattr(response.candidates[0].content, 'parts'):
                for part in response.candidates[0].content.parts:
                    if hasattr(part, 'function_call') and part.function_call:
                        # Execute the tool
                        tool_result = self.tool_executor.execute_tool(
                            part.function_call.name,
                            dict(part.function_call.args)
                        )
                        
                        # Ensure tool_result is a dict (wrap if needed)
                        if not isinstance(tool_result, dict):
                            tool_result = {"result": tool_result}
                        
                        # Send tool result back to model
                        # Filter out None parts from the response content
                        model_parts = [p for p in response.candidates[0].content.parts if p is not None]
                        
                        follow_up = await self.client.aio.models.generate_content(
                            model=MODEL,
                            contents=[
                                full_message,
                                types.Content(role="model", parts=model_parts),
                                types.Part.from_function_response(
                                    name=part.function_call.name,
                                    response=tool_result
                                )
                            ],
                            config=types.GenerateContentConfig(
                                system_instruction=system_instruction,
                                temperature=0.3
                            )
                        )
                        response = follow_up
            
            response_text = response.text
            
            # Add to history
            self.conversation_history.append({
                "role": "assistant",
                "content": response_text
            })
            
            return response_text
            
        except Exception as e:
            logger.error(f"Chat error: {e}")
            error_msg = f"I apologize, but I encountered an error: {str(e)}"
            self.conversation_history.append({
                "role": "assistant",
                "content": error_msg
            })
            return error_msg
    
    async def chat_stream(self, message: str, system_instruction: Optional[str] = None):
        """
        Stream chat responses for real-time interaction.
        
        Args:
            message: User message
            system_instruction: Optional system instruction override
            
        Yields:
            Response chunks as they arrive
        """
        if not system_instruction:
            try:
                with open("system_prompts/system_prompt.md", "r", encoding="utf-8") as f:
                    base_prompt = f.read()
                
                system_instruction = f"""{base_prompt}

--- PATIENT-SPECIFIC CONTEXT ---
Current Patient ID: {self.patient_id}
Board URL: https://iso-clinic-v3-481780815788.europe-west1.run.app/board/{self.patient_id}
"""
            except Exception as e:
                logger.warning(f"Failed to load system prompt for streaming: {e}")
                system_instruction = "You are a helpful medical AI assistant."
        
        # Wait for context with timeout - don't block too long
        if self.patient_id and not self._context_loaded:
            try:
                await asyncio.wait_for(self._load_patient_context(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning(f"⚠️ Context loading timeout for streaming, proceeding without full context")
        
        context_prompt = self._build_context_prompt()
        full_message = f"{context_prompt}\n\nUser Question: {message}"
        
        self.conversation_history.append({
            "role": "user",
            "content": message
        })
        
        try:
            config = types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.3,
            )
            
            if self.tool_executor:
                config.tools = self.tool_executor.get_tool_declarations()
            
            # Get complete response (simulate streaming by chunking)
            response = await self.client.aio.models.generate_content(
                model=MODEL,
                contents=full_message,
                config=config
            )
            
            # Handle tool calls if present
            if self.tool_executor and response.candidates:
                for part in response.candidates[0].content.parts:
                    if hasattr(part, 'function_call') and part.function_call:
                        # Execute tool
                        logger.info(f"Executing tool: {part.function_call.name}")
                        tool_result = self.tool_executor.execute_tool(
                            part.function_call.name,
                            dict(part.function_call.args)
                        )
                        
                        # Smart auto-focus after get_patient_data tool
                        if part.function_call.name == "get_patient_data":
                            tool_args = dict(part.function_call.args)
                            query_lower = tool_args.get("query", "").lower() if "query" in tool_args else message.lower()

                            # Check query keywords and auto-focus on relevant section
                            try:
                                if any(kw in query_lower for kw in ["lab", "labs", "lab result", "test result", "blood work", "bilirubin", "alt", "ast", "albumin", "liver function", "lft", "blood test"]):
                                    logger.info("🎯 Query about labs detected, auto-focusing...")
                                    asyncio.create_task(self.tool_executor.canvas_tools.focus_item("lab-track-1"))
                                elif any(kw in query_lower for kw in ["medication", "med", "drug", "prescription", "lactulose", "furosemide", "propranolol", "medicine", "rx", "dosage"]):
                                    logger.info("🎯 Query about medications detected, auto-focusing...")
                                    asyncio.create_task(self.tool_executor.canvas_tools.focus_item("medication-track-1"))
                                elif any(kw in query_lower for kw in ["encounter", "visit", "admission", "hospital", "appointment", "clinic"]):
                                    logger.info("🎯 Query about encounters detected, auto-focusing...")
                                    asyncio.create_task(self.tool_executor.canvas_tools.focus_item("encounter-track-1"))
                                elif any(kw in query_lower for kw in ["risk", "adverse", "event", "complication", "danger", "warning"]):
                                    logger.info("🎯 Query about risks/events detected, auto-focusing...")
                                    asyncio.create_task(self.tool_executor.canvas_tools.focus_item("risk-track-1"))
                                elif any(kw in query_lower for kw in ["patient", "profile", "demographic", "age", "name", "history", "who is"]):
                                    logger.info("🎯 Query about patient profile detected, auto-focusing...")
                                    asyncio.create_task(self.tool_executor.canvas_tools.focus_item("sidebar-1"))
                            except Exception as e:
                                logger.error(f"Auto-focus error: {e}")
                        
                        # Ensure tool_result is a dict (wrap if needed)
                        if not isinstance(tool_result, dict):
                            tool_result = {"result": tool_result}
                        
                        # Send tool result back to model
                        # Filter out None parts from the response content
                        model_parts = [p for p in response.candidates[0].content.parts if p is not None]
                        
                        follow_up = await self.client.aio.models.generate_content(
                            model=MODEL,
                            contents=[
                                full_message,
                                types.Content(role="model", parts=model_parts),
                                types.Part.from_function_response(
                                    name=part.function_call.name,
                                    response=tool_result
                                )
                            ],
                            config=types.GenerateContentConfig(
                                system_instruction=system_instruction,
                                temperature=0.3
                            )
                        )
                        response = follow_up
            
            complete_response = response.text if response and hasattr(response, 'text') and response.text else ""
            
            # Handle empty or None response
            if not complete_response:
                logger.error("Stream error: Empty or None response from model")
                yield "I apologize, but I couldn't generate a response. Please try asking your question again."
                return
            
            # Simulate streaming by yielding in chunks
            chunk_size = 5  # Words per chunk
            words = complete_response.split()
            for i in range(0, len(words), chunk_size):
                chunk = " ".join(words[i:i + chunk_size])
                if i + chunk_size < len(words):
                    chunk += " "
                yield chunk
                # Small delay to simulate streaming
                await asyncio.sleep(0.05)
            
            # Save complete response to history
            self.conversation_history.append({
                "role": "assistant",
                "content": complete_response
            })
            
        except Exception as e:
            logger.error(f"Stream error: {e}")
            error_msg = f"Error: {str(e)}"
            yield error_msg
    
    def get_history(self) -> List[Dict[str, str]]:
        """Get conversation history."""
        return self.conversation_history
    
    def clear_history(self):
        """Clear conversation history."""
        self.conversation_history = []
        logger.info("Conversation history cleared")
    
    def save_history(self, filename: Optional[str] = None):
        """
        Save conversation history to GCS.
        
        Args:
            filename: Optional filename, defaults to timestamped file
        """
        if not filename:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"chat_history_{timestamp}.json"
        
        history_data = {
            "patient_id": self.patient_id,
            "saved_at": datetime.now().isoformat(),
            "conversation": self.conversation_history
        }
        
        path = f"patient_data/{self.patient_id}/chat_histories/{filename}"
        
        try:
            self.gcs.create_file_from_string(
                json.dumps(history_data, indent=2),
                path,
                content_type="application/json"
            )
            logger.info(f"Saved chat history to {path}")
        except Exception as e:
            logger.error(f"Failed to save history: {e}")


# Example usage
if __name__ == "__main__":
    import asyncio
    
    async def demo():
        """Demo the chat agent."""
        # Initialize agent for a patient
        agent = ChatAgent(patient_id="P0001", use_tools=True)
        
        # Example queries
        queries = [
            "What are the patient's current liver function test results?",
            "Can you summarize the patient's medication history?",
            "Are there any concerning trends in the lab values?"
        ]
        
        for query in queries:
            print(f"\n{'='*60}")
            print(f"User: {query}")
            print(f"{'='*60}")
            
            response = await agent.chat(query)
            print(f"Assistant: {response}")
            print()
        
        # Save history
        agent.save_history()
    
    asyncio.run(demo())
