"""
Canvas/Board Manipulation Tools
Based on agent-2.9 implementation
Provides tools for interacting with the clinical board
"""

import os
import json
import logging
import httpx
from typing import Dict, Any, List, Optional

logger = logging.getLogger("canvas-tools")

# Board API configuration
BOARD_BASE_URL = "https://iso-clinic-v3-481780815788.europe-west1.run.app"
BOARD_API_BASE = f"{BOARD_BASE_URL}/api"


class CanvasTools:
    """
    Canvas manipulation tools for board operations
    """
    
    def __init__(self):
        """Initialize canvas tools"""
        self.board_url = BOARD_BASE_URL
        self.api_url = BOARD_API_BASE
    
    async def focus_board_item(self, patient_id: str, object_description: str) -> Dict[str, Any]:
        """
        Navigate to and focus on a specific item on the board.
        Based on agent-2.9 canvas_ops.py focus functionality.
        
        Args:
            patient_id: Patient ID
            object_description: Description of board item to focus on
            
        Returns:
            Dict with status and result
        """
        try:
            logger.info(f"Focusing on board item: {object_description} for patient {patient_id}")
            
            # Use objectId as the API expects (not objectDescription)
            # Tighter zoom for items inside zones (raw reports, referral images)
            obj_lower = object_description.lower() if object_description else ""
            zoom = 1.5 if obj_lower.startswith("raw-") or obj_lower.startswith("referral-letter") else 0.8

            focus_payload = {
                "patientId": patient_id,
                "objectId": object_description,
                "focusOptions": {
                    "zoom": zoom,
                    "highlight": True,
                    "duration": 1200,
                    "scrollIntoView": True
                }
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.api_url}/focus",
                    json=focus_payload,
                    timeout=10.0
                )
                
                if response.status_code == 200:
                    result = response.json()
                    logger.info(f"Successfully focused on {object_description}")
                    return {
                        "status": "success",
                        "message": f"Focused on {object_description}",
                        "data": result
                    }
                else:
                    return {
                        "status": "error",
                        "message": f"Failed to focus: HTTP {response.status_code}",
                        "details": response.text
                    }
                    
        except Exception as e:
            logger.error(f"Error focusing board item: {e}")
            return {
                "status": "error",
                "message": f"Failed to focus on board item: {str(e)}"
            }

    async def focus_item(self, object_id: str) -> Dict[str, Any]:
        """
        Focus on a board item by its ID (simplified version).
        Uses canvas_ops.focus_item under the hood.

        Args:
            object_id: The board object ID to focus on (e.g., 'lab-track-1')

        Returns:
            Dict with status and result
        """
        try:
            import canvas_ops
            logger.info(f"Focusing on board item: {object_id}")
            result = await canvas_ops.focus_item(object_id)
            return result
        except Exception as e:
            logger.error(f"Error focusing board item: {e}")
            return {
                "status": "error",
                "message": f"Failed to focus on board item: {str(e)}"
            }

    async def create_todo_on_board(self, patient_id: str, title: str, description: str, tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Create a TODO list on the clinical board.
        Based on agent-2.9 task generation logic.
        
        Args:
            patient_id: Patient ID
            title: TODO list title
            description: TODO description
            tasks: List of task objects with text, agent, status
            
        Returns:
            Dict with status and created TODO data
        """
        try:
            logger.info(f"Creating TODO on board: {title}")
            
            # Build todo structure matching agent-2.9 format
            todo_data = {
                "title": title,
                "description": description,
                "todos": tasks,
                "patientId": patient_id
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.api_url}/enhanced-todo",  # Correct endpoint
                    json=todo_data,
                    timeout=10.0
                )
                
                if response.status_code == 200:
                    result = response.json()
                    logger.info(f"Successfully created TODO: {title}")
                    return {
                        "status": "success",
                        "message": f"Created TODO list '{title}' on the board",
                        "data": result
                    }
                else:
                    return {
                        "status": "error",
                        "message": f"Failed to create TODO: HTTP {response.status_code}",
                        "details": response.text
                    }
                    
        except Exception as e:
            logger.error(f"Error creating TODO: {e}")
            return {
                "status": "error",
                "message": f"Failed to create TODO: {str(e)}"
            }
    
    async def send_to_easl(self, patient_id: str, question: str) -> Dict[str, Any]:
        """
        Send a clinical question to EASL guideline system.
        Based on agent-2.9 EASL integration.
        
        Args:
            patient_id: Patient ID
            question: Clinical question for EASL
            
        Returns:
            Dict with status and EASL response
        """
        try:
            logger.info(f"Sending EASL query: {question[:100]}...")
            
            easl_payload = {
                "patientId": patient_id,
                "question": question
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.api_url}/send-to-easl",
                    json=easl_payload,
                    timeout=30.0  # EASL queries may take longer
                )
                
                if response.status_code == 200:
                    result = response.json()
                    logger.info("EASL query sent successfully")
                    return {
                        "status": "success",
                        "message": "Query sent to EASL. The answer will appear in an iframe on the board.",
                        "data": result
                    }
                else:
                    return {
                        "status": "error",
                        "message": f"Failed to send EASL query: HTTP {response.status_code}",
                        "details": response.text
                    }
                    
        except Exception as e:
            logger.error(f"Error sending EASL query: {e}")
            return {
                "status": "error",
                "message": f"Failed to send EASL query: {str(e)}"
            }
    
    async def create_schedule(self, patient_id: str, schedule_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Create a scheduling panel on the board.
        Based on agent-2.9 scheduling functionality.
        
        Args:
            patient_id: Patient ID
            schedule_data: Scheduling details
            
        Returns:
            Dict with status and schedule panel data
        """
        try:
            logger.info(f"Creating schedule panel for patient {patient_id}")
            
            schedule_payload = {
                "patientId": patient_id,
                **schedule_data
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.api_url}/schedule",  # Correct endpoint
                    json=schedule_payload,
                    timeout=10.0
                )
                
                if response.status_code == 200:
                    result = response.json()
                    logger.info("Successfully created schedule panel")
                    return {
                        "status": "success",
                        "message": "Scheduling panel created on the board",
                        "data": result
                    }
                else:
                    return {
                        "status": "error",
                        "message": f"Failed to create schedule: HTTP {response.status_code}",
                        "details": response.text
                    }
                    
        except Exception as e:
            logger.error(f"Error creating schedule: {e}")
            return {
                "status": "error",
                "message": f"Failed to create schedule: {str(e)}"
            }
    
    async def send_notification(self, patient_id: str, message: str) -> Dict[str, Any]:
        """
        Send a notification to the care team.
        Based on agent-2.9 notification system.
        
        Args:
            patient_id: Patient ID
            message: Notification message
            
        Returns:
            Dict with status and notification result
        """
        try:
            logger.info(f"Sending notification: {message[:50]}...")
            
            # Note: Using doctor-notes as workaround (send-notification doesn't exist)
            notification_payload = {
                "patientId": patient_id,
                "note": message,
                "type": "notification"
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.api_url}/doctor-notes",  # Workaround endpoint
                    json=notification_payload,
                    timeout=10.0
                )
                
                if response.status_code == 200:
                    result = response.json()
                    logger.info("Notification sent successfully")
                    return {
                        "status": "success",
                        "message": "Notification sent to care team",
                        "data": result
                    }
                else:
                    return {
                        "status": "error",
                        "message": f"Failed to send notification: HTTP {response.status_code}",
                        "details": response.text
                    }
                    
        except Exception as e:
            logger.error(f"Error sending notification: {e}")
            return {
                "status": "error",
                "message": f"Failed to send notification: {str(e)}"
            }
    
    async def create_doctor_note(self, patient_id: str, content: str) -> Dict[str, Any]:
        """
        Create a doctor/nurse note on the board.

        Args:
            patient_id: Patient ID
            content: Note content

        Returns:
            Dict with status and note result
        """
        try:
            logger.info(f"Creating doctor note for patient {patient_id}: {content[:50]}...")

            note_payload = {
                "patientId": patient_id,
                "content": content
            }

            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.api_url}/doctor-notes",
                    json=note_payload,
                    timeout=10.0
                )

                if response.status_code in [200, 201]:
                    result = response.json()
                    # API returns {success, item: {id, ...}}
                    note_id = result.get("item", {}).get("id") or result.get("id")
                    logger.info(f"Doctor note created successfully: {note_id}")
                    # Auto-focus on the created note
                    if note_id:
                        try:
                            await self.focus_board_item(patient_id, note_id)
                        except Exception:
                            pass
                    return {
                        "status": "success",
                        "message": "Doctor note created on board",
                        "data": result,
                        "id": note_id
                    }
                else:
                    return {
                        "status": "error",
                        "message": f"Failed to create note: HTTP {response.status_code}",
                        "details": response.text
                    }

        except Exception as e:
            logger.error(f"Error creating doctor note: {e}")
            return {
                "status": "error",
                "message": f"Failed to create note: {str(e)}"
            }

    async def send_message_to_patient(self, patient_id: str, message: str) -> Dict[str, Any]:
        """
        Send a message to a patient via the chat API.

        Args:
            patient_id: Patient ID
            message: Message text to send

        Returns:
            Dict with status and message result
        """
        try:
            logger.info(f"Sending message to patient {patient_id}: {message[:50]}...")

            chat_payload = {
                "role": "doctor",
                "text": message
            }

            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.api_url}/chat/{patient_id}",
                    json=chat_payload,
                    timeout=10.0
                )

                if response.status_code in [200, 201]:
                    result = response.json()
                    logger.info("Message sent to patient successfully")
                    # Auto-focus on patient chat
                    try:
                        await self.focus_board_item(patient_id, "monitoring-patient-chat")
                    except Exception:
                        pass
                    return {
                        "status": "success",
                        "message": "Message sent to patient",
                        "data": result
                    }
                else:
                    return {
                        "status": "error",
                        "message": f"Failed to send message: HTTP {response.status_code}",
                        "details": response.text
                    }

        except Exception as e:
            logger.error(f"Error sending message to patient: {e}")
            return {
                "status": "error",
                "message": f"Failed to send message: {str(e)}"
            }

    async def create_diagnosis_report(self, patient_id: str, diagnosis_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Create a DILI diagnostic report on the board.
        Based on agent-2.9 diagnosis generation.
        
        Args:
            patient_id: Patient ID
            diagnosis_data: Diagnostic assessment data
            
        Returns:
            Dict with status and diagnosis report
        """
        try:
            logger.info(f"Creating diagnosis report for patient {patient_id}")
            
            diagnosis_payload = {
                "patientId": patient_id,
                "zone": "dili-analysis-zone",
                **diagnosis_data
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.api_url}/diagnostic-report",  # Correct endpoint
                    json=diagnosis_payload,
                    timeout=15.0
                )
                
                if response.status_code == 200:
                    result = response.json()
                    logger.info("Successfully created diagnosis report")
                    return {
                        "status": "success",
                        "message": "Diagnostic report created on the board",
                        "data": result
                    }
                else:
                    return {
                        "status": "error",
                        "message": f"Failed to create diagnosis report: HTTP {response.status_code}",
                        "details": response.text
                    }
                    
        except Exception as e:
            logger.error(f"Error creating diagnosis report: {e}")
            return {
                "status": "error",
                "message": f"Failed to create diagnosis report: {str(e)}"
            }
    
    async def create_patient_report(self, patient_id: str, report_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Create a patient summary report on the board.
        Based on agent-2.9 patient report generation.
        
        Args:
            patient_id: Patient ID
            report_data: Patient report data with structure:
                {
                    "title": "Patient Summary Report",
                    "component": "PatientReport",
                    "props": {
                        "patientData": {
                            "name": str,
                            "mrn": str,
                            "age": int,
                            "sex": str,
                            ...
                        }
                    }
                }
            
        Returns:
            Dict with status and patient report
        """
        try:
            logger.info(f"Creating patient report for patient {patient_id}")
            
            # Ensure proper structure with zone and patientId
            report_payload = {
                "patientId": patient_id,
                "zone": "patient-report-zone",
                **report_data
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.api_url}/patient-report",  # Correct endpoint
                    json=report_payload,
                    timeout=15.0
                )
                
                if response.status_code == 200:
                    result = response.json()
                    logger.info("Successfully created patient report")
                    return {
                        "status": "success",
                        "message": "Patient summary report created on the board",
                        "data": result
                    }
                else:
                    return {
                        "status": "error",
                        "message": f"Failed to create patient report: HTTP {response.status_code}",
                        "details": response.text
                    }
                    
        except Exception as e:
            logger.error(f"Error creating patient report: {e}")
            return {
                "status": "error",
                "message": f"Failed to create patient report: {str(e)}"
            }
    
    async def create_legal_report(self, patient_id: str, legal_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Create a legal compliance report on the board.
        Based on agent-2.9 legal report generation.
        
        Args:
            patient_id: Patient ID
            legal_data: Legal compliance data
            
        Returns:
            Dict with status and legal report
        """
        try:
            logger.info(f"Creating legal report for patient {patient_id}")
            
            legal_payload = {
                "patientId": patient_id,
                **legal_data
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.api_url}/legal-compliance",  # Correct endpoint
                    json=legal_payload,
                    timeout=15.0
                )
                
                if response.status_code == 200:
                    result = response.json()
                    logger.info("Successfully created legal report")
                    return {
                        "status": "success",
                        "message": "Legal compliance report created on the board",
                        "data": result
                    }
                else:
                    return {
                        "status": "error",
                        "message": f"Failed to create legal report: HTTP {response.status_code}",
                        "details": response.text
                    }
                    
        except Exception as e:
            logger.error(f"Error creating legal report: {e}")
            return {
                "status": "error",
                "message": f"Failed to create legal report: {str(e)}"
            }
