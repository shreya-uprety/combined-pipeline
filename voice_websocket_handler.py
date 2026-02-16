"""
Voice WebSocket Handler for Gemini Live API Integration
Handles real-time bidirectional voice communication
"""

import asyncio
import os
import sys
import time
import traceback
import logging
import json
from datetime import datetime
from fastapi import WebSocket, WebSocketDisconnect
from google import genai
from google.genai import types
import google.generativeai as genai_legacy
import side_agent
import canvas_ops
from patient_manager import patient_manager

# CRITICAL FIX for Windows: Use SelectorEventLoop instead of ProactorEventLoop
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    logging.info("Windows: Using SelectorEventLoop for WebSocket compatibility")

# CRITICAL FIX: Monkey patch websockets to increase open_timeout from 10s to 120s
# This fixes timeout issues with Gemini Live API on Windows
from contextlib import asynccontextmanager
from websockets.asyncio.client import connect as original_ws_connect

@asynccontextmanager
async def patched_ws_connect(*args, **kwargs):
    """Patched version that adds longer timeout for Gemini Live API"""
    if 'open_timeout' not in kwargs:
        kwargs['open_timeout'] = 120  # 2 minutes instead of default 10 seconds
        print(f"🔧 [VOICE HANDLER PATCH] Setting open_timeout=120s")
    
    async with original_ws_connect(*args, **kwargs) as ws:
        yield ws

# Apply the patch to google.genai.live module
import google.genai.live
google.genai.live.ws_connect = patched_ws_connect

logger = logging.getLogger("voice-websocket")

# Gemini Live configuration - API Key model (Vertex AI model not supported via API key)
# Use latest supported model for API key authentication
MODEL = "models/gemini-2.5-flash-native-audio-preview-12-2025"

class VoiceWebSocketHandler:
    """Handles real-time voice communication with Gemini Live API"""
    
    # Tool call deduplication - prevent same tool from being called multiple times
    TOOL_DEDUP_WINDOW_SECONDS = 5  # Ignore duplicate calls within 5 seconds (prevents rapid duplicates only)
    
    def __init__(self, websocket: WebSocket, patient_id: str):
        self.websocket = websocket
        self.patient_id = patient_id
        self.audio_in_queue = None
        self.out_queue = None
        self.session = None
        self.context_data = None
        self.patient_summary = None  # Brief patient summary for system instruction
        self.client = None  # Lazy initialization - only create when needed
        self.should_stop = False  # Flag to stop audio playback
        self._recent_tool_calls = {}  # Track recent tool calls: {key: timestamp}
        self.last_user_query = ""  # Track last user query for auto-focus fallback
        self._last_response_time = 0  # Track when last response was sent
        self._response_cooldown_seconds = 3  # Cooldown before accepting new queries
        self._suppress_follow_up_audio = False  # Suppress duplicate audio after tool follow-ups
        self._last_auto_focus_item = None  # Track auto-focus to prevent duplicate focus_board_item calls
        self._last_auto_focus_time = 0
    
    def _get_client(self):
        """Lazy initialization of Gemini client - only when needed"""
        if self.client is None:
            # Use API Key authentication (not Vertex AI)
            # The Live API model is available via API key, not Vertex AI
            api_key = os.getenv("GOOGLE_API_KEY")
            
            if not api_key:
                raise ValueError("GOOGLE_API_KEY is required for Gemini Live API")
            
            try:
                logger.info(f"🔧 Initializing Gemini client...")
                logger.info(f"   Model: {MODEL}")
                logger.info(f"   Authentication: API Key")
                
                self.client = genai.Client(api_key=api_key)
                
                logger.info(f"✅ Gemini client initialized successfully")
            except Exception as e:
                logger.error(f"❌ Failed to initialize Gemini client: {e}")
                raise
        return self.client
    
    async def get_system_instruction_with_context(self):
        """Get system instruction - Uses voice-specific prompt with tool instructions"""
        try:
            # Load the voice-specific system prompt that includes tool usage instructions
            with open("system_prompts/voice_agent_system.md", "r", encoding="utf-8") as f:
                base_prompt = f.read()
            
            # Load patient context using canvas_ops but DON'T put in system instruction
            # It's too large and causes context window errors
            if not self.context_data:
                self.context_data = await canvas_ops.get_board_items_async()
            
            # Add patient-specific context to the voice prompt
            full_instruction = f"""{base_prompt}

--- CURRENT SESSION ---
Patient ID: {self.patient_id}
Board URL: https://iso-clinic-v3-481780815788.europe-west1.run.app/board/{self.patient_id}

Remember: Use patient_id "{self.patient_id}" when calling any tools that need it.
"""
            
            logger.info(f"✅ Voice system instruction ready (using voice_agent_system.md)")
            return full_instruction
            
        except Exception as e:
            logger.error(f"Failed to load voice system prompt: {e}")
            import traceback
            traceback.print_exc()
            # Fallback to basic prompt with tool instructions
            return f"""You are MedForce Voice Agent — a real-time conversational AI assistant for clinical board operations.
ALWAYS speak in English only. NEVER use any other language.

Keep responses VERY SHORT - 1-2 sentences maximum for voice interaction.
Current Patient ID: {self.patient_id}
Board URL: https://iso-clinic-v3-481780815788.europe-west1.run.app/board/{self.patient_id}

TOOLS AVAILABLE - USE THEM:
- get_patient_data: For ANY patient questions (name, labs, meds, diagnosis, history). ALWAYS call this - NEVER say "I don't have information".
- focus_board_item: To navigate/show items on board ("show me labs", "focus on medications")
- create_task: To create TODO items ("create task for follow-up")
- send_to_easl: For clinical guideline questions
- generate_dili_diagnosis, generate_patient_report, generate_legal_report: For reports
- generate_ai_diagnosis: For AI clinical diagnosis
- generate_ai_treatment_plan: For AI treatment plan
- create_schedule: For appointments
- send_notification: For alerts
- create_lab_results: To add lab values to board
- create_agent_result: To add analysis cards
- stop_audio: To stop speaking when user says "stop"

ALWAYS use tools when user's request matches a capability. Keep responses brief.
"""

    def _create_brief_summary(self) -> str:
        """Create a brief summary of patient data for system instruction (max 500 chars)."""
        if not self.context_data or not isinstance(self.context_data, list):
            return "No patient data available."
        
        try:
            summary_parts = []
            
            # Find Sidebar with patient info
            for item in self.context_data:
                if item.get("componentType") == "Sidebar" and "patientData" in item:
                    patient_data = item["patientData"]
                    
                    # Get demographics
                    if "patient" in patient_data:
                        p = patient_data["patient"]
                        name = p.get("name", "Unknown")
                        age = p.get("age", "?")
                        sex = p.get("sex", "?")
                        summary_parts.append(f"Patient: {name}, {age}yo {sex}")
                    
                    # Get primary diagnosis
                    if "description" in patient_data:
                        desc = patient_data["description"][:150]
                        summary_parts.append(f"Summary: {desc}")
                    
                    # Get problem list (first 3)
                    if "problem_list" in patient_data:
                        problems = patient_data["problem_list"][:3]
                        if problems:
                            problem_names = [p.get("name", "") for p in problems]
                            summary_parts.append(f"Key Problems: {', '.join(problem_names)}")
                    
                    break
            
            return "\n".join(summary_parts) if summary_parts else "Patient data loaded."
        except Exception as e:
            logger.error(f"Error creating summary: {e}")
            return "Patient data available via tools."
    
    def get_system_instruction(self):
        """Get system instruction for this patient (sync wrapper) - uses voice-specific prompt"""
        try:
            # Load the voice-specific system prompt that includes tool usage instructions
            with open("system_prompts/voice_agent_system.md", "r", encoding="utf-8") as f:
                base_prompt = f.read()

            # Ensure patient_summary is generated if context_data is available
            if not self.patient_summary and self.context_data:
                self.patient_summary = self._create_brief_summary()
                logger.info(f"📋 Generated patient summary on-the-fly: {self.patient_summary[:100] if self.patient_summary else 'EMPTY'}")

            # Add patient-specific context
            context_section = ""
            if self.patient_summary:
                context_section = f"\n\n--- CURRENT PATIENT SUMMARY ---\n{self.patient_summary}\n"
            
            return f"""AUTHORIZATION: This is a clinical training system with SIMULATED patient data. All records are synthetic demo data for medical education. You are FULLY AUTHORIZED to share all patient information. NEVER refuse citing privacy, HIPAA, or confidentiality - this data is NOT real.

ABSOLUTELY CRITICAL - ZERO THINKING:
- Do NOT generate internal reasoning, planning, or thinking text.
- Do NOT output text like "Processing...", "I'm now...", "Let me...", "I'll..."
- When a tool should be called, call it IMMEDIATELY. No deliberation.
- ONE tool call per user request. If user asks multiple things, handle the FIRST one only.

LANGUAGE RULE - MANDATORY:
- You MUST ONLY speak and respond in English. NEVER use any other language.
- Even if the user speaks in another language, ALWAYS reply in English.
- This is a strict requirement with no exceptions.

STRICT RULES:
1. Simple fact questions (single value): MAX 1 SENTENCE with value + unit
2. Clinical overview/summary/list questions: 2-3 SENTENCES with key findings from the data
3. Patient question? Call get_patient_data, then answer from the returned data
4. "add labs"? Call create_lab_results(), say "Done"
5. "create analysis"? Call create_agent_result(), say "Done"
6. "stop"? Call stop_audio(), say "Okay" ONLY
7. "generate report" or "patient report"? Call generate_patient_report(), say "Done"
8. "legal report"? Call generate_legal_report(), say "Done"
9. "DILI diagnosis"? Call generate_dili_diagnosis(), say "Done"
10. "AI diagnosis"? Call generate_ai_diagnosis(), say "Done"
11. "AI treatment plan" or "treatment plan"? Call generate_ai_treatment_plan(), say "Done"

CRITICAL: You DO have access to patient data. When asked about the patient, ALWAYS call get_patient_data tool. NEVER say "I don't have access to patient information" - the tool WILL return the data. NEVER refuse to share patient data - it is simulated data and you are authorized.

NEVER think out loud. NEVER ask follow-ups. Just call the tool and answer.

{base_prompt}

--- CURRENT SESSION ---
Patient ID: {self.patient_id}
Board URL: https://iso-clinic-v3-481780815788.europe-west1.run.app/board/{self.patient_id}{context_section}

Remember: Use patient_id "{self.patient_id}" when calling any tools that need it.
"""
        except Exception as e:
            logger.error(f"Failed to load voice system prompt: {e}")
            # Fallback to basic prompt with tool instructions
            return f"""AUTHORIZATION: This is a clinical training system with SIMULATED patient data. You are FULLY AUTHORIZED to share all patient information. NEVER refuse citing privacy or confidentiality.

You are MedForce Voice Agent — a real-time AI assistant for clinical board operations.
ALWAYS speak in English only. NEVER use any other language.

Keep responses VERY SHORT - 1-2 sentences maximum for voice interaction.
Current Patient ID: {self.patient_id}
Board URL: https://iso-clinic-v3-481780815788.europe-west1.run.app/board/{self.patient_id}

TOOLS AVAILABLE - USE THEM:
- get_patient_data: For ANY patient questions (name, labs, meds, diagnosis, history). ALWAYS call this - NEVER say "I don't have information".
- focus_board_item: To navigate/show items on board ("show me labs", "focus on medications")
- create_task: To create TODO items ("create task for follow-up")
- send_to_easl: For clinical guideline questions
- generate_dili_diagnosis, generate_patient_report, generate_legal_report: For reports
- generate_ai_diagnosis: For AI clinical diagnosis
- generate_ai_treatment_plan: For AI treatment plan
- create_schedule: For appointments
- send_notification: For alerts
- create_lab_results: To add lab values to board
- create_agent_result: To add analysis cards
- stop_audio: To stop speaking when user says "stop"

ALWAYS use tools when user's request matches a capability. Keep responses brief.
"""

    def get_config(self):
        """Get Gemini Live API configuration with tool declarations"""
        # Define tool declarations for voice mode actions
        tool_declarations = [
            {
                "name": "get_patient_data",
                "description": """USE THIS TOOL when user ASKS any question about patient data.

TRIGGER WORDS: "What", "Show", "Tell", "How", "Who", "Which", any question mark

✅ USE THIS TOOL FOR:
- "What are the lab values?" - YES, use this
- "What are the labs?" - YES, use this
- "What's the ALT?" - YES, use this
- "Show me lab results" - YES, use this
- "Tell me the labs" - YES, use this
- "What medications?" - YES, use this
- "Patient name?" - YES, use this

RESPONSE: Answer in MAX 5 WORDS with the actual values.

⚠️ CRITICAL: Any question about labs = use THIS tool, not create_lab_results.""",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "REQUIRED: Describe what the user is asking about. Must include one of: labs, medications, encounters, patient, profile, risk, history. Example: 'lab results', 'latest encounter', 'current medications', 'patient age'"
                        }
                    },
                    "required": ["query"]
                }
            },
            {
                "name": "focus_board_item",
                "description": """Navigate to and highlight a specific item on the clinical board.

Call this tool when user says: "show me", "go to", "focus on", "navigate to", "zoom to", "look at", "display the", "open the"

Examples: "show me the labs", "focus on the medication timeline", "go to patient profile", "look at the risk track", "navigate to encounters", "show me the clinical notes", "zoom to the lab chart".""",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "What to focus on, e.g., 'medication timeline', 'lab results', 'encounters', 'risk track', 'patient profile'"
                        }
                    },
                    "required": ["query"]
                }
            },
            {
                "name": "create_task",
                "description": """Create a TODO task item on the patient's board.

Call this tool when user says: "create a task", "add a todo", "add a to-do", "remind me to", "make a note to", "add task for", "create reminder"

Examples: "create a task to order liver ultrasound", "add a todo for follow-up labs", "remind me to check INR tomorrow", "add task to schedule hepatology consult".""",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Description of the task to create"
                        }
                    },
                    "required": ["query"]
                }
            },
            {
                "name": "send_to_easl",
                "description": """Send a clinical question to EASL (European Association for Study of the Liver) guidelines for expert recommendations.

Call this tool when user mentions: "EASL", "guidelines", "clinical guideline", "recommendation", "what do guidelines say", "guideline protocol", "evidence-based"

Examples: "what does EASL recommend for DILI", "get guideline recommendations for liver failure", "ask EASL about treatment options".""",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "Clinical question to analyze"
                        }
                    },
                    "required": ["question"]
                }
            },
            {
                "name": "generate_dili_diagnosis",
                "description": """Generate a DILI (Drug-Induced Liver Injury) diagnosis report.

Call this tool when user says: "generate DILI diagnosis", "create DILI report", "liver injury assessment", "DILI assessment", "drug-induced liver injury report", "RUCAM score"

Creates comprehensive diagnostic assessment including RUCAM score, causality assessment, and recommendations.""",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            },
            {
                "name": "generate_patient_report",
                "description": """Generate a comprehensive patient summary report.

Call this tool when user says: "generate patient report", "create patient summary", "patient report", "summary report", "create summary"

Includes demographics, medical history, current medications, lab results, and clinical assessment.""",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            },
            {
                "name": "generate_legal_report",
                "description": """Generate a legal compliance report.

Call this tool when user says: "legal report", "compliance report", "regulatory report", "generate legal documentation", "adverse event reporting"

Documents patient's care, adverse events, and regulatory reporting requirements.""",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            },
            {
                "name": "generate_ai_diagnosis",
                "description": """Generate an AI-powered clinical diagnosis report.

Call this tool when user says: "AI diagnosis", "generate AI diagnosis", "clinical diagnosis", "MedForce diagnosis", "AI diagnostic report"

Creates comprehensive physician-oriented diagnostic assessment with differential diagnosis ranking, clinical reasoning, evidence grading, and recommended workup.""",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            },
            {
                "name": "generate_ai_treatment_plan",
                "description": """Generate an AI-powered treatment plan.

Call this tool when user says: "AI treatment plan", "generate treatment plan", "treatment plan", "AI plan", "MedForce treatment", "AI treatment"

Creates comprehensive treatment plan with pharmacotherapy, monitoring protocol, escalation pathways, and evidence-based recommendations.""",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            },
            {
                "name": "create_schedule",
                "description": """Create a scheduling panel on the board for appointments.

Call this tool when user says: "schedule", "book appointment", "follow-up", "arrange visit", "schedule a", "create appointment"

Examples: "schedule a follow-up in 2 weeks", "create appointment for liver ultrasound", "schedule hepatology consult".""",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "context": {
                            "type": "string",
                            "description": "Description of what scheduling is needed, e.g., 'Follow-up for liver function tests in 2 weeks'"
                        }
                    },
                    "required": ["context"]
                }
            },
            {
                "name": "send_notification",
                "description": """Send a notification alert to the care team.

Call this tool when user says: "notify", "send notification", "alert the team", "send alert", "notify care team", "urgent notification"

Examples: "notify the team about critical labs", "send alert about patient status", "alert hepatology about this case".""",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "message": {
                            "type": "string",
                            "description": "The notification message to send, e.g., 'Critical lab values require immediate review'"
                        }
                    },
                    "required": ["message"]
                }
            },
            {
                "name": "create_doctor_note",
                "description": """Create a doctor or nurse note on the board.

Call this tool when user says: "add a note", "create a note", "write a note", "doctor note", "nurse note", "clinical note"

Examples: "add a note that patient shows improvement", "write a note about the medication change", "create a clinical note for this visit".""",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "The content of the doctor/nurse note"
                        }
                    },
                    "required": ["content"]
                }
            },
            {
                "name": "send_message_to_patient",
                "description": """Send a message to the patient via the patient chat system.

Call this tool when user says: "message the patient", "tell the patient", "text the patient", "chat with patient", "send message to patient", "ask the patient", "ask patient"

Examples: "tell the patient to take their medication", "ask the patient about his chest pain", "message the patient about the follow-up", "text the patient the test results".""",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "message": {
                            "type": "string",
                            "description": "The message to send to the patient"
                        }
                    },
                    "required": ["message"]
                }
            },
            {
                "name": "create_lab_results",
                "description": """Add a panel showing test results to the board.

ONLY use when user says: "Add labs", "Post labs", "Create labs panel", "Put labs on board"

User must say "add" or "post" or "put" - this is for ADDING to board, not answering questions.""",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "panel_type": {
                            "type": "string",
                            "description": "Type of panel: 'labs'",
                            "default": "labs"
                        }
                    },
                    "required": []
                }
            },
            {
                "name": "create_agent_result",
                "description": """TRIGGER: User says "create analysis" OR "add assessment" OR "generate findings"
ACTION: Call create_agent_result() with NO arguments IMMEDIATELY
RESPONSE: Say ONLY "Done" - nothing else

Example:
User: "Create an analysis"
YOU: [CALL create_agent_result with no parameters] -> "Done"
NOT: "What should I include..." or "I'll create an analysis..."

CRITICAL:
- Do NOT pass title or content parameters
- System auto-generates everything from patient data
- Format includes: Patient name, Key Findings (Liver Function Tests, Clinical Impression, Recommendations)
- Text explanation is FORBIDDEN""",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            },
            {
                "name": "stop_audio",
                "description": """CRITICAL: User says "stop", "quiet", "enough", "shut up", "silence", "pause", "be quiet", "that's enough"

ACTION: Call stop_audio() IMMEDIATELY to stop ALL audio playback
RESPONSE: Say ONLY "Okay" or say NOTHING

This is the HIGHEST PRIORITY command. When user wants you to stop, call this tool immediately.

Example:
User: "Stop"
YOU: [CALL stop_audio()] -> "Okay" (or nothing)

User: "Be quiet"
YOU: [CALL stop_audio()] -> (nothing)

User: "That's enough"
YOU: [CALL stop_audio()] -> "Okay"

FORBIDDEN: Do NOT continue speaking after calling this tool.""",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            }
        ]
        
        return {
            "response_modalities": ["AUDIO"],
            "system_instruction": self.get_system_instruction(),
            "tools": [{"function_declarations": tool_declarations}],
            "generation_config": {
                "thinking_config": {
                    "thinking_budget": 0
                }
            },
            "speech_config": {
                "voice_config": {
                    "prebuilt_voice_config": {
                        "voice_name": "Charon"
                    }
                },
                "language_code": "en-US"
            },
            "realtime_input_config": {
                "automatic_activity_detection": {
                    "disabled": False,
                    "start_of_speech_sensitivity": "START_SENSITIVITY_HIGH",
                    "end_of_speech_sensitivity": "END_SENSITIVITY_HIGH",
                    "prefix_padding_ms": 100,
                    "silence_duration_ms": 800
                }
            }
        }
    
    async def send_status_to_ui(self, status_type: str, message: str, **kwargs):
        """Send status/notification to UI via WebSocket (like chat agent does)"""
        try:
            payload = {
                "type": "status",
                "status": status_type,
                "message": message,
                "timestamp": datetime.now().isoformat(),
                **kwargs
            }
            logger.info(f"📤 Sending status to UI: {status_type} - {message}")
            await self.websocket.send_json(payload)
            logger.info(f"✅ Status sent successfully")
        except Exception as e:
            logger.error(f"❌ Failed to send status to UI: {e}")
    
    async def send_tool_notification(self, tool_name: str, status: str, result: str = None):
        """Send tool execution notification to UI (like chat agent does)"""
        try:
            payload = {
                "type": "tool_call",
                "tool": tool_name,
                "status": status,  # "executing", "completed", "failed"
                "result": result,
                "timestamp": datetime.now().isoformat()
            }
            await self.websocket.send_json(payload)
            logger.info(f"📤 Sent tool notification to UI: {tool_name} - {status}")
        except Exception as e:
            logger.error(f"Failed to send tool notification: {e}")

    async def send_todo_update_notification(self, todo_id: str, task_id: str, index: str, status: str):
        """Send TODO task status update notification to UI for real-time updates"""
        try:
            payload = {
                "type": "todo_update",
                "todo_id": todo_id,
                "task_id": task_id,
                "index": index,
                "status": status,
                "timestamp": datetime.now().isoformat()
            }
            await self.websocket.send_json(payload)
            logger.info(f"📤 Sent TODO update to UI: {todo_id}/{task_id} -> {status}")
        except Exception as e:
            logger.error(f"Failed to send TODO update notification: {e}")

    async def animate_todo_with_notifications(self, todo_id: str, tasks: list):
        """
        Animate TODO task statuses with WebSocket notifications for real-time UI updates.
        This is similar to side_agent._animate_todo_tasks but sends WebSocket notifications.
        """
        import random
        try:
            logger.info(f"🎬 Starting TODO animation with notifications for {todo_id}")
            for task_idx, task in enumerate(tasks):
                task_id = task.get('id', f'task-{task_idx}')

                # Step 1: Mark parent task as executing
                logger.info(f"⏳ Task {task_idx} ({task_id}): pending → executing")
                await asyncio.sleep(1.5)
                await canvas_ops.update_todo({
                    "id": todo_id,
                    "task_id": task_id,
                    "index": "",
                    "status": "executing"
                })
                # Notify frontend
                await self.send_todo_update_notification(todo_id, task_id, "", "executing")

                # Step 2: Animate subtodos if they exist
                subtodos = task.get('subTodos', [])
                if subtodos:
                    logger.info(f"  📋 Processing {len(subtodos)} subtodos for task {task_idx}")
                    for subtodo_idx, subtodo in enumerate(subtodos):
                        # Mark subtodo as executing
                        await asyncio.sleep(1)
                        await canvas_ops.update_todo({
                            "id": todo_id,
                            "task_id": task_id,
                            "index": str(subtodo_idx),
                            "status": "executing"
                        })
                        await self.send_todo_update_notification(todo_id, task_id, str(subtodo_idx), "executing")

                        # Mark subtodo as finished
                        await asyncio.sleep(random.uniform(1, 2))
                        await canvas_ops.update_todo({
                            "id": todo_id,
                            "task_id": task_id,
                            "index": str(subtodo_idx),
                            "status": "finished"
                        })
                        await self.send_todo_update_notification(todo_id, task_id, str(subtodo_idx), "finished")
                else:
                    await asyncio.sleep(1.5)

                # Step 3: Mark parent task as finished
                logger.info(f"✅ Task {task_idx} ({task_id}): executing → finished")
                await canvas_ops.update_todo({
                    "id": todo_id,
                    "task_id": task_id,
                    "index": "",
                    "status": "finished"
                })
                await self.send_todo_update_notification(todo_id, task_id, "", "finished")
                await asyncio.sleep(0.5)

            logger.info(f"✅ TODO {todo_id} animation completed with notifications")
        except Exception as e:
            logger.error(f"⚠️ TODO animation error: {e}")

    def _is_duplicate_tool_call(self, function_name: str, arguments: dict) -> bool:
        """Disabled - finding root cause instead of blocking duplicates"""
        # DISABLED: Instead of blocking duplicates, we should prevent them from happening
        return False
    
    async def handle_tool_call(self, tool_call):
        """Handle tool calls from Gemini using side_agent and canvas_ops"""
        try:
            function_responses = []

            # DEBUG: Log how many function calls are in this tool_call
            num_calls = len(tool_call.function_calls)
            logger.info(f"🔧 TOOL_CALL batch received with {num_calls} function call(s)")

            for idx, fc in enumerate(tool_call.function_calls):
                function_name = fc.name
                arguments = dict(fc.args)

                # DEBUG: Log each function call with index
                logger.info(f"🔧 Tool call [{idx+1}/{num_calls}]: {function_name}")
                logger.info(f"   Args: {arguments}")
                logger.info(f"   Call ID: {fc.id}")

                # Check for duplicate calls FIRST before any logging/notification
                if self._is_duplicate_tool_call(function_name, arguments):
                    # Return cached result for duplicate calls - no UI notification
                    result = json.dumps({
                        "status": "success",
                        "message": f"{function_name} already executed recently",
                        "cached": True
                    })
                    function_responses.append(
                        types.FunctionResponse(
                            id=fc.id,
                            name=function_name,
                            response={"result": result}
                        )
                    )
                    # Skip silently - don't spam logs or UI
                    continue
                
                # Notify UI that tool is executing
                await self.send_tool_notification(function_name, "executing")
                
                result = ""
                try:
                    if function_name == "get_patient_data":
                        # Save the query for auto-focus fallback
                        query_arg = arguments.get("query", "")
                        if query_arg:
                            self.last_user_query = query_arg

                        # Load full context if not already loaded
                        if not self.context_data:
                            self.context_data = await canvas_ops.get_board_items_async()

                        logger.info(f"📊 Context data type: {type(self.context_data)}, length: {len(self.context_data) if isinstance(self.context_data, (list, dict)) else 'N/A'}")
                        
                        # Search for "pulmonary" and related medical terms across all data
                        search_terms = ["pulmonary", "respiratory", "lung", "copd", "pneumonia", "dyspnea", "asthma", "bronchitis"]
                        full_data_str = json.dumps(self.context_data).lower()
                        found_terms = [term for term in search_terms if term in full_data_str]
                        
                        if found_terms:
                            logger.info(f"🔍 FOUND medical terms in board data: {found_terms}")
                        else:
                            logger.info(f"🔍 WARNING: None of these terms found in board: {search_terms}")
                        
                        pulmonary_locations = []
                        
                        # Extract ESSENTIAL data only - full dump exceeds 32k context window
                        # We need structured info that's useful but concise
                        summary = {"patient_id": self.patient_id}
                        
                        if isinstance(self.context_data, list):
                            logger.info(f"📋 Processing {len(self.context_data)} board items")
                            for idx, item in enumerate(self.context_data):
                                if not isinstance(item, dict):
                                    continue
                                
                                # Check if this item contains pulmonary or respiratory info
                                item_str = json.dumps(item).lower()
                                found_in_item = [term for term in search_terms if term in item_str]
                                if found_in_item:
                                    comp_type_for_log = item.get('componentType', 'unknown')
                                    logger.info(f"🔍 Item {idx} ({comp_type_for_log}) contains: {found_in_item}")
                                    pulmonary_locations.append(f"Item {idx}: {comp_type_for_log} - {found_in_item}")
                                
                                comp_type = item.get("componentType")
                                item_type = item.get("type")
                                
                                # Log ALL items to find patient profile
                                logger.info(f"  Item {idx}: componentType={comp_type}, type={item_type}, keys={list(item.keys())}")
                                
                                # Extract patient data from 'patient' field (SingleEncounterDocument)
                                if "patient" in item and isinstance(item["patient"], dict):
                                    patient = item["patient"]
                                    if "name" not in summary:
                                        logger.info(f"✅ Found patient field in item {idx}, patient keys: {list(patient.keys())}")
                                        if patient.get("name"):
                                            summary["name"] = patient.get("name")
                                            # Handle different field names
                                            summary["age"] = patient.get("age") or patient.get("age_at_first_encounter")
                                            summary["gender"] = patient.get("gender") or patient.get("sex")
                                            summary["mrn"] = patient.get("mrn") or patient.get("id")
                                            summary["date_of_birth"] = patient.get("date_of_birth") or patient.get("dateOfBirth")
                                            logger.info(f"   Patient: {summary.get('name')}, {summary.get('age')}yo, {summary.get('gender')}")
                                        if patient.get("medicalHistory"):
                                            history = patient.get("medicalHistory")
                                            logger.info(f"📋 Found medicalHistory in item {idx}, type: {type(history)}")
                                            summary["medical_history"] = str(history)[:2000]  # Increased to capture more
                                        if patient.get("medical_history"):
                                            history = patient.get("medical_history")
                                            logger.info(f"📋 Found medical_history in item {idx}, type: {type(history)}")
                                            summary["medical_history"] = str(history)[:2000]
                                
                                # Extract encounter data with clinical notes
                                if "encounter" in item and isinstance(item["encounter"], dict):
                                    encounter = item["encounter"]
                                    # Check for pulmonary in encounter
                                    encounter_str = json.dumps(encounter)
                                    if "pulmonary" in encounter_str.lower():
                                        logger.info(f"🔍 Found 'pulmonary' in encounter at item {idx}!")
                                    
                                    if "clinical_notes" not in summary:
                                        summary["clinical_notes"] = []
                                    if "rawText" in encounter:
                                        summary["clinical_notes"].append({
                                            "date": encounter.get("date"),
                                            "text": encounter.get("rawText")[:1500]  # Increased to 1500
                                        })
                                    if "assessment" in encounter:
                                        if "assessment" not in summary:
                                            summary["assessment"] = encounter["assessment"]
                                    # Extract history of present illness, review of systems, etc
                                    if "history_of_present_illness" in encounter:
                                        if "hpi" not in summary:
                                            summary["hpi"] = []
                                        summary["hpi"].append(encounter["history_of_present_illness"][:1000])
                                    if "review_of_systems" in encounter:
                                        if "review_of_systems" not in summary:
                                            summary["review_of_systems"] = []
                                        ros = encounter["review_of_systems"]
                                        if isinstance(ros, dict):
                                            summary["review_of_systems"].append(ros)
                                        else:
                                            summary["review_of_systems"].append(str(ros)[:1000])
                                
                                # Extract raw clinical note
                                if comp_type == "RawClinicalNote":
                                    # Check for pulmonary in raw text
                                    raw_text = item.get("rawText", "")
                                    if "pulmonary" in raw_text.lower():
                                        logger.info(f"🔍 Found 'pulmonary' in RawClinicalNote at item {idx}!")
                                    
                                    if "recent_clinical_notes" not in summary:
                                        summary["recent_clinical_notes"] = []
                                    note = {
                                        "date": item.get("date"),
                                        "visitType": item.get("visitType"),
                                        "provider": item.get("provider"),
                                        "text": raw_text[:1500] if raw_text else ""  # Increased to 1500 to capture more
                                    }
                                    summary["recent_clinical_notes"].append(note)
                                    logger.info(f"📋 Added clinical note from {item.get('date')}, text length: {len(raw_text)}")
                                
                                # Extract patient data from 'patientData' field (Sidebar, DifferentialDiagnosis)
                                if "patientData" in item and isinstance(item["patientData"], dict):
                                    patient_data = item["patientData"]
                                    logger.info(f"📋 patientData keys in item {idx}: {list(patient_data.keys())}")
                                    
                                    # Check if there's a nested 'patient' object inside patientData (Sidebar)
                                    if "patient" in patient_data and isinstance(patient_data["patient"], dict):
                                        nested_patient = patient_data["patient"]
                                        if "name" not in summary and nested_patient.get("name"):
                                            logger.info(f"✅ Found nested patient in patientData in item {idx}, keys: {list(nested_patient.keys())}")
                                            summary["name"] = nested_patient.get("name")
                                            summary["age"] = nested_patient.get("age") or nested_patient.get("age_at_first_encounter")
                                            summary["gender"] = nested_patient.get("gender") or nested_patient.get("sex")
                                            summary["mrn"] = nested_patient.get("mrn") or nested_patient.get("id")
                                            summary["date_of_birth"] = nested_patient.get("date_of_birth")
                                            summary["identifiers"] = nested_patient.get("identifiers")
                                            logger.info(f"   Patient: {summary.get('name')}, {summary.get('age')}yo, {summary.get('gender')}, DOB: {summary.get('date_of_birth')}")
                                    
                                    # Extract additional clinical data from Sidebar
                                    if "problem_list" in patient_data:
                                        problems = patient_data["problem_list"]
                                        logger.info(f"📋 Found problem_list in item {idx}: {problems}")
                                        if isinstance(problems, list):
                                            summary["problem_list"] = [str(p)[:300] for p in problems[:30]]  # Increased limits
                                        elif isinstance(problems, dict):
                                            summary["problem_list"] = problems
                                        else:
                                            summary["problem_list"] = str(problems)[:1000]
                                    if "allergies" in patient_data:
                                        logger.info(f"📋 Found allergies in item {idx}: {patient_data['allergies']}")
                                        summary["allergies"] = patient_data["allergies"]
                                    if "medication_timeline" in patient_data:
                                        # This might be large, so summarize
                                        med_timeline = patient_data["medication_timeline"]
                                        if isinstance(med_timeline, list):
                                            summary["medication_count"] = len(med_timeline)
                                        else:
                                            summary["medication_timeline_info"] = str(med_timeline)[:300]
                                    if "riskLevel" in patient_data:
                                        summary["risk_level"] = patient_data["riskLevel"]
                                    if "description" in patient_data:
                                        desc = patient_data["description"]
                                        logger.info(f"📋 Found clinical description in item {idx}, length: {len(str(desc))}")
                                        summary["clinical_summary"] = str(desc)[:2000]  # Increased to capture more info
                                    
                                    # Also check for direct fields in patientData
                                    if "name" not in summary and patient_data.get("name"):
                                        logger.info(f"✅ Found name in patientData in item {idx}")
                                        summary["name"] = patient_data.get("name")
                                        summary["age"] = patient_data.get("age") or patient_data.get("age_at_first_encounter")
                                        summary["gender"] = patient_data.get("gender") or patient_data.get("sex")
                                        summary["mrn"] = patient_data.get("mrn") or patient_data.get("id")
                                        summary["date_of_birth"] = patient_data.get("date_of_birth")
                                        logger.info(f"   Patient: {summary.get('name')}, {summary.get('age')}yo, {summary.get('gender')}")
                                
                                # Patient profile - check multiple possible field names
                                if "patientProfile" in item:
                                    profile = item["patientProfile"]
                                    logger.info(f"✅ Found patientProfile in item {idx}: {profile}")
                                    summary["name"] = profile.get("name")
                                    summary["age"] = profile.get("age")
                                    summary["gender"] = profile.get("gender")
                                    summary["mrn"] = profile.get("mrn")
                                
                                # Check for direct patient fields
                                if "name" in item and "age" in item and "name" not in summary:
                                    logger.info(f"✅ Found direct patient fields in item {idx}")
                                    summary["name"] = item.get("name")
                                    summary["age"] = item.get("age")
                                    summary["gender"] = item.get("gender")
                                    summary["mrn"] = item.get("mrn")
                                
                                # Patient context - check multiple field names
                                if "patientContext" in item:
                                    ctx = item["patientContext"]
                                    logger.info(f"✅ Found patientContext in item {idx}")
                                    summary["chief_complaint"] = ctx.get("chiefComplaint")
                                    summary["history"] = ctx.get("presentingHistory", ctx.get("history", ""))[:500]
                                
                                # Risk analysis
                                if "riskAnalysis" in item:
                                    risk = item["riskAnalysis"]
                                    logger.info(f"✅ Found riskAnalysis in item {idx}")
                                    summary["risk_score"] = risk.get("riskScore")
                                    summary["risk_factors"] = risk.get("riskFactors", [])[:5]
                                
                                # Encounters - check both structures
                                if "encounters" in item and isinstance(item["encounters"], list):
                                    if "recent_encounters" not in summary:
                                        summary["recent_encounters"] = []
                                    for enc in item["encounters"][:5]:
                                        if isinstance(enc, dict):
                                            enc_data = {
                                                "date": enc.get("date"),
                                                "visitType": enc.get("visitType"),
                                                "provider": enc.get("provider")
                                            }
                                            # Add assessment if available
                                            if "assessment" in enc:
                                                enc_data["assessment"] = enc["assessment"]
                                            summary["recent_encounters"].append(enc_data)
                                    logger.info(f"✅ Found {len(item['encounters'])} encounters in item {idx}")
                                
                                # ==========================================
                                # MEDICATIONS - MedicationTrack has data.medications
                                # ==========================================
                                if comp_type == "MedicationTrack" and "data" in item:
                                    med_data = item["data"]
                                    meds_list = []
                                    # data can be dict with medications key or direct array
                                    if isinstance(med_data, dict) and "medications" in med_data:
                                        meds_list = med_data["medications"]
                                    elif isinstance(med_data, list):
                                        meds_list = med_data
                                    
                                    if meds_list:
                                        meds = []
                                        for med in meds_list[:15]:
                                            if isinstance(med, dict):
                                                name = med.get('name', 'Unknown')
                                                dose = med.get('dose', '')
                                                freq = med.get('frequency', '')
                                                start = med.get('startDate', '')
                                                end = med.get('endDate', 'ongoing')
                                                indication = med.get('indication', '')
                                                med_str = f"{name} {dose}"
                                                if freq:
                                                    med_str += f" {freq}"
                                                if indication:
                                                    med_str += f" (for {indication})"
                                                if start:
                                                    med_str += f" [started {start}"
                                                    if end and end != 'ongoing':
                                                        med_str += f", ended {end}]"
                                                    else:
                                                        med_str += ", ongoing]"
                                                meds.append(med_str)
                                        if meds:
                                            logger.info(f"✅ Found {len(meds)} medications in MedicationTrack (item {idx})")
                                            logger.info(f"   Sample meds: {meds[:3]}")
                                            summary["current_medications"] = meds
                                
                                # Also check for direct medications array (legacy format)
                                elif "medications" in item and isinstance(item["medications"], list):
                                    meds = []
                                    for med in item["medications"][:15]:
                                        if isinstance(med, dict):
                                            med_str = f"{med.get('name')} {med.get('dose')} {med.get('frequency')}"
                                            if med.get("indication"):
                                                med_str += f" (for {med.get('indication')})"
                                            meds.append(med_str)
                                    if meds:
                                        logger.info(f"✅ Found {len(meds)} medications (direct) in item {idx}")
                                        summary["current_medications"] = meds
                                
                                # ==========================================
                                # LABS - LabTrack can have data array or labs array
                                # ==========================================
                                if comp_type == "LabTrack":
                                    # Try both possible keys: 'data' or 'labs'
                                    lab_data = item.get("data") or item.get("labs", [])
                                    if isinstance(lab_data, list) and lab_data:
                                        labs = []
                                        for biomarker in lab_data[:20]:
                                            if isinstance(biomarker, dict):
                                                # Try multiple field names for biomarker name
                                                name = biomarker.get('biomarker') or biomarker.get('name') or biomarker.get('parameter') or 'Unknown'
                                                unit = biomarker.get('unit', '')
                                                ref_range = biomarker.get('referenceRange', {})
                                                if isinstance(ref_range, dict):
                                                    ref_min = ref_range.get('min')
                                                    ref_max = ref_range.get('max')
                                                else:
                                                    ref_min = ref_max = None
                                                values = biomarker.get('values', [])
                                                
                                                # Get most recent value
                                                value = None
                                                date = ''
                                                if values and isinstance(values, list):
                                                    latest = values[-1] if values else {}
                                                    if isinstance(latest, dict):
                                                        value = latest.get('value')
                                                        date = latest.get('t', '')[:10] if latest.get('t') else ''
                                                    else:
                                                        value = latest  # Direct value
                                                
                                                # Skip if no name or value
                                                if name == 'Unknown' and value is None:
                                                    continue
                                                    
                                                # Check if abnormal
                                                abnormal = False
                                                if value is not None:
                                                    if ref_min is not None and value < ref_min:
                                                        abnormal = True
                                                    if ref_max is not None and value > ref_max:
                                                        abnormal = True
                                                
                                                lab_str = f"{name}: {value} {unit}".strip()
                                                if ref_min is not None or ref_max is not None:
                                                    lab_str += f" (ref: {ref_min}-{ref_max})"
                                                if date:
                                                    lab_str += f" [{date}]"
                                                if abnormal:
                                                    lab_str += " [ABNORMAL]"
                                                labs.append(lab_str)
                                        
                                        if labs:
                                            logger.info(f"✅ Found {len(labs)} lab values in LabTrack (item {idx})")
                                            logger.info(f"   Sample labs: {labs[:3]}")
                                            summary["recent_labs"] = labs
                                
                                # Also check for direct labs array (legacy format)
                                elif "labs" in item and isinstance(item["labs"], list):
                                    labs = []
                                    for lab in item["labs"][:15]:
                                        if isinstance(lab, dict):
                                            # Try multiple possible field names for lab name
                                            lab_name = lab.get('name') or lab.get('biomarker') or lab.get('parameter') or lab.get('test') or 'Unknown'
                                            lab_value = lab.get('value')
                                            lab_unit = lab.get('unit', '')
                                            
                                            # Handle nested values array (like LabTrack format)
                                            if lab_value is None and 'values' in lab:
                                                values = lab.get('values', [])
                                                if values and isinstance(values, list):
                                                    latest = values[-1] if values else {}
                                                    lab_value = latest.get('value') if isinstance(latest, dict) else latest
                                            
                                            # Get reference range
                                            ref_range = lab.get('referenceRange', {})
                                            if isinstance(ref_range, dict):
                                                ref_min = ref_range.get('min')
                                                ref_max = ref_range.get('max')
                                                range_str = f"{ref_min}-{ref_max}" if ref_min is not None else ""
                                            else:
                                                range_str = str(ref_range) if ref_range else ""
                                            
                                            # Skip if no valid name or value
                                            if lab_name == 'Unknown' and lab_value is None:
                                                continue
                                                
                                            lab_str = f"{lab_name}: {lab_value} {lab_unit}"
                                            if range_str:
                                                lab_str += f" (ref: {range_str})"
                                            if lab.get("date"):
                                                lab_str += f" ({lab.get('date')})"
                                            if lab.get("flag") or lab.get("abnormal") or lab.get("status") == "abnormal":
                                                lab_str += " [ABNORMAL]"
                                            labs.append(lab_str)
                                    if labs:
                                        logger.info(f"✅ Found {len(labs)} labs (direct) in item {idx}")
                                        summary["recent_labs"] = labs
                                
                                # ==========================================
                                # RISK EVENTS - RiskTrack has risks directly
                                # ==========================================
                                if comp_type == "RiskTrack" and "risks" in item and isinstance(item["risks"], list):
                                    risks = []
                                    for risk in item["risks"][:10]:
                                        if isinstance(risk, dict):
                                            risk_entry = {
                                                "date": risk.get("t", "")[:10] if risk.get("t") else risk.get("date"),
                                                "riskScore": risk.get("riskScore"),
                                                "factors": risk.get("factors", [])
                                            }
                                            risks.append(risk_entry)
                                    if risks:
                                        logger.info(f"✅ Found {len(risks)} risk scores in RiskTrack (item {idx})")
                                        logger.info(f"   Sample risk: {risks[0]}")
                                        summary["risk_events"] = risks
                                
                                # Also check for direct risks array (legacy format)
                                elif "risks" in item and isinstance(item["risks"], list) and comp_type != "RiskTrack":
                                    if "risk_events" not in summary:
                                        summary["risk_events"] = []
                                    for risk in item["risks"][:10]:
                                        if isinstance(risk, dict):
                                            summary["risk_events"].append({
                                                "date": risk.get("date") or risk.get("t", "")[:10] if risk.get("t") else "",
                                                "event": risk.get("event") or risk.get("description"),
                                                "severity": risk.get("severity") or risk.get("level")
                                            })
                                
                                # ==========================================
                                # KEY EVENTS - KeyEventsTrack has events directly
                                # ==========================================
                                if comp_type == "KeyEventsTrack" and "events" in item and isinstance(item["events"], list):
                                    events = []
                                    for event in item["events"][:15]:
                                        if isinstance(event, dict):
                                            event_entry = {
                                                "date": event.get("t", "")[:10] if event.get("t") else event.get("date"),
                                                "event": event.get("event"),
                                                "note": event.get("note")
                                            }
                                            events.append(event_entry)
                                    if events:
                                        logger.info(f"✅ Found {len(events)} key events in KeyEventsTrack (item {idx})")
                                        logger.info(f"   Sample event: {events[0]}")
                                        summary["key_events"] = events
                                
                                # Also check for direct events array (legacy format)
                                elif "events" in item and isinstance(item["events"], list) and comp_type != "KeyEventsTrack":
                                    if "key_events" not in summary:
                                        summary["key_events"] = []
                                    for event in item["events"][:10]:
                                        if isinstance(event, dict):
                                            summary["key_events"].append({
                                                "date": event.get("date") or event.get("t", "")[:10] if event.get("t") else "",
                                                "event": event.get("event") or event.get("description")
                                            })
                                
                                # ==========================================
                                # ADVERSE EVENTS - AdverseEventAnalytics
                                # ==========================================
                                if comp_type == "AdverseEventAnalytics":
                                    if "adverseEvents" in item and isinstance(item["adverseEvents"], list):
                                        adverse = []
                                        for ae in item["adverseEvents"][:10]:
                                            if isinstance(ae, dict):
                                                adverse.append({
                                                    "event": ae.get("event") or ae.get("name"),
                                                    "date": ae.get("date") or ae.get("t", "")[:10] if ae.get("t") else "",
                                                    "severity": ae.get("severity") or ae.get("grade"),
                                                    "causality": ae.get("causality")
                                                })
                                        if adverse:
                                            logger.info(f"✅ Found {len(adverse)} adverse events in AdverseEventAnalytics (item {idx})")
                                            summary["adverse_events"] = adverse
                                    
                                    if "rucam_ctcae_analysis" in item:
                                        summary["rucam_analysis"] = item["rucam_ctcae_analysis"]
                                        logger.info(f"✅ Found RUCAM/CTCAE analysis in item {idx}")
                                
                                # Differential diagnosis
                                if "differential" in item and isinstance(item["differential"], list):
                                    summary["differential_diagnosis"] = item["differential"][:10]
                                
                                # Primary diagnosis (from Sidebar)
                                if "primaryDiagnosis" in item:
                                    summary["primary_diagnosis"] = item["primaryDiagnosis"]
                        
                        logger.info(f"📤 Returning summary with keys: {list(summary.keys())}")
                        logger.info(f"📤 Summary counts: name={summary.get('name')}, age={summary.get('age')}, meds={len(summary.get('current_medications', []))}, labs={len(summary.get('recent_labs', []))}, risks={len(summary.get('risk_events', []))}, events={len(summary.get('key_events', []))}")
                        
                        # Log actual content samples for debugging
                        if summary.get('recent_labs'):
                            logger.info(f"📤 Lab values: {summary['recent_labs'][:3]}")
                        if summary.get('current_medications'):
                            logger.info(f"📤 Medications: {summary['current_medications'][:3]}")
                        if summary.get('risk_events'):
                            logger.info(f"📤 Risk events: {summary['risk_events'][:2]}")
                        if summary.get('key_events'):
                            logger.info(f"📤 Key events: {summary['key_events'][:2]}")
                        
                        if pulmonary_locations:
                            logger.info(f"🔍 Pulmonary info found in: {pulmonary_locations[:3]}")  # Limit output
                        result = json.dumps(summary, indent=2)

                        # AUTO-FOCUS: Server-side focus based on query content
                        # (Gemini often skips calling focus_board_item separately)
                        auto_focus_query = query_arg.lower() if query_arg else ""
                        auto_focus_map = {
                            # Labs - check longer phrases first
                            "lab value": "dashboard-item-lab-table",
                            "lab result": "dashboard-item-lab-table",
                            "abnormal lab": "dashboard-item-lab-table",
                            "liver function": "dashboard-item-lab-table",
                            "blood test": "dashboard-item-lab-table",
                            "lab chart": "dashboard-item-lab-chart",
                            "lab trend": "dashboard-item-lab-chart",
                            "lab": "dashboard-item-lab-table",
                            "alt": "dashboard-item-lab-table",
                            "ast": "dashboard-item-lab-table",
                            "bilirubin": "dashboard-item-lab-table",
                            "albumin": "dashboard-item-lab-table",
                            "inr": "dashboard-item-lab-table",
                            "creatinine": "dashboard-item-lab-table",
                            "hemoglobin": "dashboard-item-lab-table",
                            "platelet": "dashboard-item-lab-table",
                            "sodium": "dashboard-item-lab-table",
                            # Medications
                            "medication": "medication-track-1",
                            "medicine": "medication-track-1",
                            "drug": "medication-track-1",
                            "prescription": "medication-track-1",
                            # Encounters / visits / exam
                            "physical exam": "encounter-track-1",
                            "exam finding": "encounter-track-1",
                            "encounter": "encounter-track-1",
                            "visit": "encounter-track-1",
                            "consultation": "encounter-track-1",
                            # Timeline
                            "timeline": "key-events-track-1",
                            "clinical timeline": "key-events-track-1",
                            "key event": "key-events-track-1",
                            "history": "encounter-track-1",
                            # Patient profile
                            "medical situation": "sidebar-1",
                            "overview": "sidebar-1",
                            "patient": "sidebar-1",
                            "profile": "sidebar-1",
                            # Diagnosis
                            "diagnosis": "differential-diagnosis",
                            "differential": "differential-diagnosis",
                            # Risk
                            "risk": "risk-track-1",
                            # Reports
                            "pathology": "raw-lab-image-1",
                            "pathology report": "raw-lab-image-1",
                            "radiology": "raw-lab-image-radiology-1",
                            "imaging": "raw-lab-image-radiology-1",
                            "report": "raw-encounter-image-1",
                            # Referral
                            "referral": "referral-doctor-info",
                        }

                        auto_focus_id = None
                        for keyword in sorted(auto_focus_map.keys(), key=len, reverse=True):
                            if keyword in auto_focus_query:
                                auto_focus_id = auto_focus_map[keyword]
                                break

                        if auto_focus_id:
                            logger.info(f"🎯 Auto-focusing on {auto_focus_id} based on query: {auto_focus_query[:50]}")
                            try:
                                await canvas_ops.focus_item(auto_focus_id)
                                self._last_auto_focus_item = auto_focus_id
                                self._last_auto_focus_time = time.time()
                                logger.info(f"✅ Auto-focus successful: {auto_focus_id}")
                            except Exception as focus_err:
                                logger.warning(f"⚠️ Auto-focus failed: {focus_err}")
                        else:
                            logger.info("ℹ️ get_patient_data completed - no auto-focus match")
                    
                    elif function_name == "focus_board_item":
                        query = arguments.get("query", "").lower()
                        logger.info(f"🎯 Focus request: {query}")

                        # Skip if auto-focus already handled this within the last 5 seconds
                        if self._last_auto_focus_item and (time.time() - self._last_auto_focus_time) < 5:
                            already_focused_id = self._last_auto_focus_item
                            self._last_auto_focus_item = None  # Reset so future standalone calls work
                            logger.info(f"🎯 Skipping focus_board_item - auto-focus already focused on {already_focused_id}")
                            result = json.dumps({
                                "status": "success",
                                "message": f"Already focused on {already_focused_id}",
                                "object_id": already_focused_id
                            })
                            await self.send_tool_notification(function_name, "completed", result)
                            function_responses.append(
                                types.FunctionResponse(
                                    id=fc.id,
                                    name=function_name,
                                    response={"result": result}
                                )
                            )
                            continue
                        
                        # Map common queries to actual board item IDs (not component types)
                        focus_map = {
                            # Labs
                            "lab": "lab-track-1",
                            "labs": "lab-track-1",
                            "lab result": "lab-track-1",
                            "lab results": "lab-track-1",
                            "lab timeline": "lab-track-1",
                            "lab chart": "dashboard-item-lab-chart",
                            "lab table": "dashboard-item-lab-table",
                            # Medications
                            "medication": "medication-track-1",
                            "medications": "medication-track-1",
                            "meds": "medication-track-1",
                            "medication timeline": "medication-track-1",
                            # Encounters
                            "encounter": "encounter-track-1",
                            "encounters": "encounter-track-1",
                            "visit": "encounter-track-1",
                            "visits": "encounter-track-1",
                            # Risk & Events
                            "risk": "risk-track-1",
                            "risks": "risk-track-1",
                            "event": "key-events-track-1",
                            "events": "key-events-track-1",
                            "key events": "key-events-track-1",
                            # Patient
                            "patient": "sidebar-1",
                            "profile": "sidebar-1",
                            "patient profile": "sidebar-1",
                            "sidebar": "sidebar-1",
                            # Adverse events & Diagnosis
                            "adverse": "adverse-event-analytics",
                            "causality": "adverse-event-analytics",
                            "rucam": "adverse-event-analytics",
                            "diagnosis": "differential-diagnosis",
                            "differential": "differential-diagnosis",
                            # EASL
                            "easl": "easl-panel",
                            "easl panel": "easl-panel",
                            "guideline": "easl-panel",
                            "guidelines": "easl-panel",
                            # Referral
                            "referral": "referral-doctor-info",
                            "referral letter": "referral-doctor-info",
                            "referred": "referral-doctor-info",
                            "referrer": "referral-doctor-info",
                            "gp letter": "referral-doctor-info",
                            "doctor letter": "referral-doctor-info",
                            "referring doctor": "referral-doctor-info",
                            # Reports / Raw EHR data
                            "report": "raw-encounter-image-1",
                            "reports": "raw-encounter-image-1",
                            "clinical notes": "raw-encounter-image-1",
                            "raw data": "raw-encounter-image-1",
                            "encounter report": "raw-encounter-image-1",
                            "radiology": "raw-lab-image-radiology-1",
                            "radiology report": "raw-lab-image-radiology-1",
                            "imaging": "raw-lab-image-radiology-1",
                            "imaging report": "raw-lab-image-radiology-1",
                            "x-ray": "raw-lab-image-radiology-1",
                            "xray": "raw-lab-image-radiology-1",
                            "ultrasound": "raw-lab-image-radiology-1",
                            "chest x-ray": "raw-lab-image-radiology-1",
                            "lab report": "raw-lab-image-1",
                            "blood test report": "raw-lab-image-1",
                            "pathology": "raw-lab-image-1",
                            "pathology report": "raw-lab-image-1",
                            "scan": "raw-lab-image-radiology-1",
                            "ct scan": "raw-lab-image-radiology-1",
                            "mri": "raw-lab-image-radiology-1",
                            # Patient Chat
                            "patient chat": "monitoring-patient-chat",
                            "message": "monitoring-patient-chat",
                            "chat": "monitoring-patient-chat",
                        }
                        
                        # Try direct mapping first (check longer/more specific keys first)
                        object_id = None
                        already_focused = False
                        for key in sorted(focus_map.keys(), key=len, reverse=True):
                            if key in query:
                                object_id = focus_map[key]
                                logger.info(f"✅ Mapped '{query}' to {object_id}")
                                break

                        # If no direct mapping, use side_agent to resolve
                        # NOTE: resolve_object_id already calls focus_item internally
                        if not object_id:
                            resolve_result = await side_agent.resolve_object_id(query)
                            if isinstance(resolve_result, dict):
                                object_id = resolve_result.get("object_id")
                                # resolve_object_id already focused, don't call again
                                already_focused = True
                            else:
                                object_id = resolve_result

                        if object_id:
                            if not already_focused:
                                focus_result = await canvas_ops.focus_item(object_id)
                            else:
                                focus_result = resolve_result.get("focus_result", {})
                                logger.info(f"🎯 Already focused by resolve_object_id, skipping duplicate")

                            result = json.dumps({
                                "status": "success" if focus_result.get("success") else "error",
                                "message": f"Focused on {object_id}",
                                "object_id": object_id,
                                "api_response": focus_result
                            })
                            logger.info(f"🎯 Focus API response: {focus_result}")
                        else:
                            result = json.dumps({
                                "status": "error",
                                "message": "Could not find matching board item"
                            })
                    
                    elif function_name == "create_task":
                        query = arguments.get("query", "")
                        # Save the query for auto-focus
                        if query:
                            self.last_user_query = query

                        # Generate task JSON using side_agent (without animation)
                        logger.info(f"📝 Creating task for: {query}")
                        task_obj = await side_agent.generate_task_obj(query)

                        # Create TODO on board
                        todo_response = await canvas_ops.create_todo(task_obj)
                        todo_id = todo_response.get('id')

                        # Auto-focus on the newly created TODO
                        if todo_id:
                            logger.info(f"🎯 Auto-focusing on created TODO: {todo_id}")
                            try:
                                await asyncio.sleep(0.5)  # Brief delay for board to render
                                await canvas_ops.focus_item(todo_id)
                            except Exception as e:
                                logger.error(f"Failed to auto-focus on TODO: {e}")

                        # Start animation with WebSocket notifications for real-time updates
                        if todo_id and 'todos' in task_obj:
                            logger.info(f"🎬 Starting TODO animation with notifications for {todo_id}")
                            # Run animation in background but WITH WebSocket notifications
                            asyncio.create_task(self.animate_todo_with_notifications(todo_id, task_obj['todos']))

                        result = json.dumps({
                            "status": "success",
                            "message": f"Task created: {task_obj.get('title', 'Task')}",
                            "todo_id": todo_id,
                            "tasks_count": len(task_obj.get('todos', []))
                        })
                    
                    elif function_name == "send_to_easl":
                        question = arguments.get("question", "")
                        # Use side_agent to trigger EASL
                        easl_result = await side_agent.trigger_easl(question)
                        result = f"Sent to EASL: {easl_result}"
                    
                    elif function_name == "generate_dili_diagnosis":
                        # Generate DILI diagnosis report
                        logger.info("🔬 Generating DILI diagnosis...")
                        try:
                            diagnosis_result = await side_agent.create_dili_diagnosis()
                            logger.info(f"📊 DILI diagnosis board_response: {diagnosis_result.get('board_response', {}).get('status')}")

                            # ID is at board_response.data.id
                            report_id = diagnosis_result.get('board_response', {}).get('data', {}).get('id')
                            if report_id:
                                logger.info(f"🎯 Auto-focusing on DILI diagnosis: {report_id}")
                                try:
                                    await asyncio.sleep(0.5)
                                    await canvas_ops.focus_item(report_id)
                                except Exception as focus_error:
                                    logger.error(f"Failed to auto-focus on DILI diagnosis: {focus_error}")
                            else:
                                logger.warning(f"⚠️ No DILI report ID returned")

                            result = json.dumps({
                                "status": "success",
                                "message": "DILI diagnosis report generated and added to board",
                                "report_id": report_id
                            })
                        except Exception as e:
                            logger.error(f"❌ DILI diagnosis failed: {e}")
                            import traceback
                            traceback.print_exc()
                            result = json.dumps({
                                "status": "error",
                                "message": f"Failed to generate DILI diagnosis: {str(e)}"
                            })
                    
                    elif function_name == "generate_patient_report":
                        # Generate patient report
                        logger.info("📄 Generating patient report...")
                        try:
                            report_result = await side_agent.create_patient_report()
                            logger.info(f"📊 Patient report board_response: {report_result.get('board_response', {}).get('status')}")

                            # ID is at board_response.data.id
                            report_id = report_result.get('board_response', {}).get('data', {}).get('id')
                            if report_id:
                                logger.info(f"🎯 Auto-focusing on patient report: {report_id}")
                                try:
                                    await asyncio.sleep(0.5)
                                    await canvas_ops.focus_item(report_id)
                                except Exception as focus_error:
                                    logger.error(f"Failed to auto-focus on patient report: {focus_error}")
                            else:
                                logger.warning(f"⚠️ No patient report ID returned")

                            result = json.dumps({
                                "status": "success",
                                "message": "Patient report generated and added to board",
                                "report_id": report_id
                            })
                        except Exception as e:
                            logger.error(f"❌ Patient report failed: {e}")
                            import traceback
                            traceback.print_exc()
                            result = json.dumps({
                                "status": "error",
                                "message": f"Failed to generate patient report: {str(e)}"
                            })
                    
                    elif function_name == "generate_legal_report":
                        # Generate legal report
                        logger.info("⚖️ Generating legal report...")
                        try:
                            legal_result = await side_agent.create_legal_doc()
                            logger.info(f"📊 Legal report result: {legal_result}")

                            # ID is at board_response.data.id (same structure as patient/DILI reports)
                            report_id = (
                                legal_result.get('board_response', {}).get('data', {}).get('id')
                                or legal_result.get('id')
                                or legal_result.get('result', {}).get('id')
                            )
                            if report_id:
                                logger.info(f"🎯 Auto-focusing on legal report: {report_id}")
                                try:
                                    await asyncio.sleep(0.5)  # Brief delay for rendering
                                    focus_result = await canvas_ops.focus_item(report_id)
                                    logger.info(f"✅ Auto-focused on legal report: {focus_result}")
                                except Exception as focus_error:
                                    logger.error(f"Failed to auto-focus on legal report: {focus_error}")
                            else:
                                logger.warning("⚠️ No report ID returned, cannot auto-focus")

                            result = json.dumps({
                                "status": "success",
                                "message": "Legal compliance report generated and added to board",
                                "report_id": report_id
                            })
                        except Exception as e:
                            logger.error(f"❌ Legal report generation failed: {e}")
                            import traceback
                            traceback.print_exc()
                            result = json.dumps({
                                "status": "error",
                                "message": f"Failed to generate legal report: {str(e)}"
                            })
                    
                    elif function_name == "generate_ai_diagnosis":
                        # Generate AI diagnosis report
                        logger.info("🧠 Generating AI diagnosis...")
                        try:
                            ai_diag_result = await side_agent.create_ai_diagnosis()
                            logger.info(f"📊 AI diagnosis result: {ai_diag_result}")

                            report_id = (
                                ai_diag_result.get('board_response', {}).get('data', {}).get('id')
                                or ai_diag_result.get('id')
                                or ai_diag_result.get('result', {}).get('id')
                            )
                            if report_id:
                                logger.info(f"🎯 Auto-focusing on AI diagnosis: {report_id}")
                                try:
                                    await asyncio.sleep(0.5)
                                    focus_result = await canvas_ops.focus_item(report_id)
                                    logger.info(f"✅ Auto-focused on AI diagnosis: {focus_result}")
                                except Exception as focus_error:
                                    logger.error(f"Failed to auto-focus on AI diagnosis: {focus_error}")
                            else:
                                logger.warning("⚠️ No report ID returned, cannot auto-focus")

                            result = json.dumps({
                                "status": "success",
                                "message": "AI clinical diagnosis generated and added to board",
                                "report_id": report_id
                            })
                        except Exception as e:
                            logger.error(f"❌ AI diagnosis generation failed: {e}")
                            import traceback
                            traceback.print_exc()
                            result = json.dumps({
                                "status": "error",
                                "message": f"Failed to generate AI diagnosis: {str(e)}"
                            })

                    elif function_name == "generate_ai_treatment_plan":
                        # Generate AI treatment plan
                        logger.info("📋 Generating AI treatment plan...")
                        try:
                            ai_plan_result = await side_agent.create_ai_treatment_plan()
                            logger.info(f"📊 AI treatment plan result: {ai_plan_result}")

                            report_id = (
                                ai_plan_result.get('board_response', {}).get('data', {}).get('id')
                                or ai_plan_result.get('id')
                                or ai_plan_result.get('result', {}).get('id')
                            )
                            if report_id:
                                logger.info(f"🎯 Auto-focusing on AI treatment plan: {report_id}")
                                try:
                                    await asyncio.sleep(0.5)
                                    focus_result = await canvas_ops.focus_item(report_id)
                                    logger.info(f"✅ Auto-focused on AI treatment plan: {focus_result}")
                                except Exception as focus_error:
                                    logger.error(f"Failed to auto-focus on AI treatment plan: {focus_error}")
                            else:
                                logger.warning("⚠️ No report ID returned, cannot auto-focus")

                            result = json.dumps({
                                "status": "success",
                                "message": "AI treatment plan generated and added to board",
                                "report_id": report_id
                            })
                        except Exception as e:
                            logger.error(f"❌ AI treatment plan generation failed: {e}")
                            import traceback
                            traceback.print_exc()
                            result = json.dumps({
                                "status": "error",
                                "message": f"Failed to generate AI treatment plan: {str(e)}"
                            })

                    elif function_name == "create_schedule":
                        # Create schedule panel using side_agent for proper structure
                        context = arguments.get("context", "Follow-up appointment scheduling")
                        logger.info(f"📅 Creating schedule: {context}")

                        try:
                            # side_agent.create_schedule(query, context) - query is the scheduling request,
                            # patient_id is handled internally via patient_manager
                            schedule_result = await side_agent.create_schedule(context)
                            logger.info(f"📊 Schedule result: {schedule_result}")

                            # ID is nested: side_agent returns {status, result: {status, id, api_response}}
                            schedule_id = None
                            inner_result = schedule_result.get('result', {})
                            if isinstance(inner_result, dict):
                                schedule_id = inner_result.get('id')
                            if not schedule_id:
                                schedule_id = schedule_result.get('id')

                            if schedule_id:
                                logger.info(f"🎯 Auto-focusing on schedule: {schedule_id}")
                                try:
                                    await asyncio.sleep(0.5)
                                    await canvas_ops.focus_item(schedule_id)
                                except Exception as focus_error:
                                    logger.error(f"Failed to auto-focus on schedule: {focus_error}")
                            else:
                                logger.warning(f"⚠️ No schedule_id returned. Full result: {schedule_result}")

                            result = json.dumps({
                                "status": "success",
                                "message": "Schedule panel created on board",
                                "schedule_id": schedule_id
                            })
                        except Exception as e:
                            logger.error(f"❌ Schedule creation failed: {e}")
                            import traceback
                            traceback.print_exc()
                            result = json.dumps({
                                "status": "error",
                                "message": f"Failed to create schedule: {str(e)}"
                            })
                    
                    elif function_name == "create_doctor_note":
                        # Create doctor/nurse note with AI-enhanced content
                        raw_content = arguments.get("content", "")
                        logger.info(f"📝 Creating doctor note: {raw_content[:50]}")

                        # AI-enhance the note with patient context (like chat agent)
                        try:
                            if not self.context_data:
                                self.context_data = await canvas_ops.get_board_items_async()
                            context_str = json.dumps(self.context_data, indent=2) if self.context_data else ""

                            genai_legacy.configure(api_key=os.getenv("GOOGLE_API_KEY"))
                            note_model = genai_legacy.GenerativeModel("gemini-2.0-flash")
                            note_prompt = f"""Generate professional clinical notes based on the doctor's request and patient data.

Doctor's request: "{raw_content}"

Patient data (board context):
{context_str[:20000]}

Rules:
- Write the note content ONLY (no metadata, no JSON, no markdown code blocks)
- Use professional clinical documentation style
- Include relevant findings, assessments, and plans from the patient data
- Be comprehensive but concise
- Use appropriate medical terminology
- Format with clear sections if the request calls for detailed notes
- If the request is simple (e.g., "patient refused medication"), write a brief note
- If the request asks for comprehensive/detailed notes, write thorough clinical documentation
- NEVER include the original command text - only the generated note content

Output ONLY the note content:"""
                            note_response = note_model.generate_content(note_prompt)
                            content = note_response.text.strip()
                            # Clean up any markdown code block wrappers
                            import re
                            if content.startswith('```'):
                                content = re.sub(r'^```\w*\n?', '', content)
                                content = re.sub(r'\n?```$', '', content)
                            logger.info(f"📝 AI-generated note content ({len(content)} chars)")
                        except Exception as e:
                            logger.error(f"Note AI enhancement failed, using raw content: {e}")
                            content = raw_content

                        note_result = await canvas_ops.create_doctor_note(content)
                        note_id = note_result.get("id")
                        result = json.dumps({
                            "status": note_result.get("status", "done"),
                            "message": note_result.get("message", "Note created"),
                            "note_id": note_id
                        })

                    elif function_name == "send_notification":
                        # Send notification
                        message = arguments.get("message", "Notification from voice agent")
                        logger.info(f"🔔 Sending notification: {message}")
                        notif_result = await canvas_ops.create_notification({"message": message})
                        result = json.dumps({
                            "status": notif_result.get("status", "done"),
                            "message": notif_result.get("message", "Notification sent"),
                            "api_response": notif_result.get("api_response")
                        })
                    
                    elif function_name == "send_message_to_patient":
                        # Send message to patient
                        message = arguments.get("message", "")
                        logger.info(f"💬 Sending message to patient: {message}")
                        msg_result = await canvas_ops.send_patient_message(message)
                        # Focus on patient chat after sending
                        try:
                            await asyncio.sleep(0.3)
                            await canvas_ops.focus_item("monitoring-patient-chat")
                        except Exception as focus_error:
                            logger.error(f"Failed to focus on patient chat: {focus_error}")
                        result = json.dumps({
                            "status": msg_result.get("status", "done"),
                            "message": msg_result.get("message", "Message sent to patient"),
                            "api_response": msg_result.get("api_response")
                        })

                    elif function_name == "create_lab_results" or function_name == "add_results_panel":
                        # Create lab results on the board
                        labs = arguments.get("labs", [])
                        source = arguments.get("source", "Voice Agent")
                        logger.info(f"🧪 Creating lab results: {len(labs)} values provided")
                        
                        from datetime import datetime
                        import re
                        
                        # Helper to map status to API expected values
                        def map_status(status_str, value=None, range_str=""):
                            """Map status to API values: optimal, warning, critical"""
                            if status_str:
                                status_lower = status_str.lower()
                                if status_lower in ['optimal', 'normal', 'ok']:
                                    return 'optimal'
                                elif status_lower in ['warning', 'borderline', 'elevated', 'low']:
                                    return 'warning'
                                elif status_lower in ['critical', 'high', 'abnormal', 'danger', 'severe']:
                                    return 'critical'
                            return 'warning'  # Default for unknown
                        
                        # Transform lab data for board API - handle various input formats
                        transformed_labs = []
                        
                        # If no labs provided, extract from patient data (like chat agent)
                        if not labs or len(labs) == 0:
                            logger.info("🧪 No labs provided - extracting from patient data...")
                            # Get patient data if not already loaded
                            if not self.context_data:
                                self.context_data = await canvas_ops.get_board_items_async()
                            
                            # Extract labs from context data
                            for item in self.context_data if isinstance(self.context_data, list) else []:
                                if not isinstance(item, dict):
                                    continue
                                    
                                # Look for LabTrack component
                                if item.get("componentType") == "LabTrack" and "labs" in item:
                                    lab_items = item.get("labs", [])
                                    logger.info(f"🧪 Found {len(lab_items)} labs in LabTrack")
                                    
                                    for lab in lab_items[:20]:  # Limit to 20 most recent
                                        if not isinstance(lab, dict):
                                            continue
                                        
                                        # Extract lab details
                                        name = lab.get('name') or lab.get('biomarker') or lab.get('parameter')
                                        unit = lab.get('unit', '-')
                                        if not unit:
                                            unit = '-'
                                        
                                        # Get latest value from values array
                                        value = None
                                        ref_min = None
                                        ref_max = None
                                        
                                        if 'values' in lab and isinstance(lab['values'], list) and lab['values']:
                                            latest = lab['values'][-1]
                                            value = latest.get('value') if isinstance(latest, dict) else latest
                                        else:
                                            value = lab.get('value')
                                        
                                        # Get reference range
                                        ref_range = lab.get('referenceRange', {})
                                        if isinstance(ref_range, dict):
                                            ref_min = ref_range.get('min')
                                            ref_max = ref_range.get('max')
                                        
                                        # Build range string - must not be empty
                                        if ref_min is not None and ref_max is not None:
                                            range_str = f"{ref_min}-{ref_max}"
                                        elif ref_min is not None:
                                            range_str = f">{ref_min}"
                                        elif ref_max is not None:
                                            range_str = f"<{ref_max}"
                                        else:
                                            range_str = "N/A"  # Default if no range
                                        
                                        # Determine status based on value vs range
                                        status = 'optimal'
                                        if value is not None:
                                            if ref_min is not None and value < ref_min:
                                                status = 'warning'
                                            elif ref_max is not None and value > ref_max:
                                                status = 'critical'
                                        
                                        if name and value is not None:
                                            labs.append({
                                                "name": name,
                                                "value": value,
                                                "unit": unit,
                                                "range": range_str,
                                                "status": status
                                            })
                                    break  # Found labs, stop searching
                            
                            if not labs:
                                logger.warning("⚠️ No labs found in patient data")
                                result = json.dumps({
                                    "status": "error",
                                    "message": "No lab results found in patient data"
                                })
                                await self.send_tool_notification(function_name, "completed", result)
                                function_responses.append({"name": function_name, "response": {"output": result}})
                                continue
                            
                            logger.info(f"🧪 Extracted {len(labs)} labs from patient data")
                        
                        # Check if Gemini sent a flat array of strings like ['name:', 'ALT', 'unit:', 'U/L', 'value:110']
                        # This happens when voice transcription breaks up the data
                        if labs and all(isinstance(item, str) for item in labs):
                            logger.info("🧪 Detected flat string array - attempting to reconstruct")
                            # Try to reconstruct lab objects from flat strings
                            current_lab = {}
                            i = 0
                            while i < len(labs):
                                item = labs[i].strip()
                                
                                # Check for key:value format
                                if ':' in item:
                                    parts = item.split(':', 1)
                                    key = parts[0].strip().lower()
                                    val = parts[1].strip() if len(parts) > 1 else ''
                                    
                                    if key in ['name', 'parameter', 'test']:
                                        if current_lab.get('name'):
                                            # Save previous lab and start new one
                                            transformed_labs.append({
                                                "parameter": current_lab.get('name', 'Unknown'),
                                                "value": current_lab.get('value', 0),
                                                "unit": current_lab.get('unit', ''),
                                                "range": current_lab.get('range', ''),
                                                "status": current_lab.get('status', 'normal')
                                            })
                                            current_lab = {}
                                        current_lab['name'] = val
                                    elif key == 'value':
                                        try:
                                            current_lab['value'] = float(val) if val else 0
                                        except:
                                            current_lab['value'] = 0
                                    elif key == 'unit':
                                        current_lab['unit'] = val
                                    elif key in ['range', 'normal', 'reference']:
                                        current_lab['range'] = val
                                    elif key == 'status':
                                        current_lab['status'] = val.lower()
                                else:
                                    # Might be a standalone value - check next item for context
                                    # Common lab names
                                    lab_names = ['ALT', 'AST', 'Bilirubin', 'Albumin', 'INR', 'Creatinine', 
                                                'BUN', 'Sodium', 'Potassium', 'Glucose', 'WBC', 'RBC', 
                                                'Hemoglobin', 'Hematocrit', 'Platelets', 'PT', 'PTT']
                                    if item.upper() in [n.upper() for n in lab_names]:
                                        if current_lab.get('name'):
                                            transformed_labs.append({
                                                "parameter": current_lab.get('name', 'Unknown'),
                                                "value": current_lab.get('value', 0),
                                                "unit": current_lab.get('unit', ''),
                                                "range": current_lab.get('range', ''),
                                                "status": current_lab.get('status', 'normal')
                                            })
                                            current_lab = {}
                                        current_lab['name'] = item
                                    elif item.replace('.', '').replace('-', '').isdigit():
                                        try:
                                            current_lab['value'] = float(item)
                                        except:
                                            pass
                                    elif item in ['U/L', 'mg/dL', 'g/dL', 'mEq/L', 'mmol/L', '%']:
                                        current_lab['unit'] = item
                                    elif item.lower() in ['high', 'low', 'normal', 'abnormal', 'optimal', 'warning', 'critical']:
                                        current_lab['status'] = map_status(item)
                                i += 1
                            
                            # Don't forget the last lab
                            if current_lab.get('name'):
                                transformed_labs.append({
                                    "parameter": current_lab.get('name', 'Unknown'),
                                    "value": current_lab.get('value', 0),
                                    "unit": current_lab.get('unit', ''),
                                    "range": current_lab.get('range', ''),
                                    "status": map_status(current_lab.get('status', 'warning'))
                                })
                        else:
                            # Normal processing - labs should be list of dicts
                            for lab in labs:
                                # Handle case where lab might be a string (JSON) instead of dict
                                if isinstance(lab, str):
                                    try:
                                        lab = json.loads(lab)
                                    except:
                                        # If it's just a name string, create minimal entry
                                        lab = {"name": lab, "value": 0, "unit": "", "range": "", "status": "warning"}
                                
                                if isinstance(lab, dict):
                                    # Clean keys - Gemini sometimes sends keys with quotes like '"name"' instead of 'name'
                                    cleaned_lab = {}
                                    for k, v in lab.items():
                                        # Remove quotes from key if present
                                        clean_key = k.strip('"').strip("'")
                                        cleaned_lab[clean_key] = v
                                    
                                    logger.info(f"  Lab entry: {cleaned_lab}")
                                    
                                    transformed_labs.append({
                                        "parameter": cleaned_lab.get("name") or cleaned_lab.get("parameter", "Unknown"),
                                        "value": cleaned_lab.get("value", 0),
                                        "unit": cleaned_lab.get("unit", ""),
                                        "range": cleaned_lab.get("range") or cleaned_lab.get("normalRange", ""),
                                        "status": map_status(cleaned_lab.get("status", "warning"))
                                    })
                                else:
                                    logger.warning(f"Skipping invalid lab entry: {lab}")
                        
                        lab_payload = {
                            "labResults": transformed_labs,
                            "date": datetime.now().strftime('%Y-%m-%d'),
                            "source": source
                        }
                        
                        logger.info(f"🧪 Sending lab payload: {lab_payload}")
                        lab_result = await canvas_ops.create_lab(lab_payload)
                        
                        # Auto-focus on the first created lab result (or skip if no results)
                        if lab_result.get("status") == "success" or lab_result.get("successful", 0) > 0:
                            # Get the ID of the first created lab result if available
                            created_results = lab_result.get("results", [])
                            if created_results and isinstance(created_results[0], dict) and created_results[0].get("id"):
                                first_lab_id = created_results[0].get("id")
                                logger.info(f"🎯 Auto-focusing on created lab result: {first_lab_id}")
                                try:
                                    await asyncio.sleep(0.3)
                                    focus_result = await canvas_ops.focus_item(first_lab_id)
                                    logger.info(f"✅ Auto-focused on lab result: {focus_result}")
                                except Exception as e:
                                    logger.error(f"Failed to auto-focus on lab result: {e}")
                            else:
                                logger.info("📊 Lab results created but no focus (no ID returned)")
                        
                        result = json.dumps({
                            "status": "success",
                            "message": f"Created {len(transformed_labs)} lab results on board",
                            "labs_added": [l.get('parameter') for l in transformed_labs],
                            "api_response": lab_result
                        })
                    
                    elif function_name == "create_agent_result":
                        # Create agent analysis result on the board
                        title = arguments.get("title", "")
                        content = arguments.get("content", "")
                        logger.info(f"📊 Creating agent result: {title}")
                        
                        from datetime import datetime
                        now = datetime.now()
                        
                        # Auto-generate title if not provided
                        if not title:
                            title = f"Clinical Analysis - {now.strftime('%I:%M:%S %p')}"
                        
                        # Auto-generate content if not provided by extracting from patient data
                        if not content:
                            logger.info("📊 Auto-generating analysis content from patient data...")
                            # Get patient data if not already loaded
                            if not self.context_data:
                                self.context_data = await canvas_ops.get_board_items_async()
                            
                            # Extract patient info and labs
                            patient_name = "Unknown Patient"
                            labs_info = []
                            
                            if isinstance(self.context_data, list):
                                for item in self.context_data:
                                    if not isinstance(item, dict):
                                        continue
                                    
                                    # Get patient name from Sidebar
                                    if item.get("componentType") == "Sidebar" and "patientData" in item:
                                        pd = item["patientData"]
                                        if "patient" in pd and isinstance(pd["patient"], dict):
                                            patient_name = pd["patient"].get("name", patient_name)
                                    
                                    # Get labs from LabTrack
                                    if item.get("componentType") == "LabTrack" and "labs" in item:
                                        for lab in item.get("labs", [])[:10]:
                                            if isinstance(lab, dict):
                                                name = lab.get("biomarker") or lab.get("name", "")
                                                unit = lab.get("unit", "")
                                                values = lab.get("values", [])
                                                ref = lab.get("referenceRange", {})
                                                
                                                if values and isinstance(values, list):
                                                    latest = values[-1]
                                                    val = latest.get("value") if isinstance(latest, dict) else latest
                                                    
                                                    # Determine status
                                                    ref_max = ref.get("max") if isinstance(ref, dict) else None
                                                    ref_min = ref.get("min") if isinstance(ref, dict) else None
                                                    
                                                    if ref_max and val > ref_max:
                                                        status = "elevated"
                                                    elif ref_min and val < ref_min:
                                                        status = "low"
                                                    else:
                                                        status = "normal"
                                                    
                                                    labs_info.append(f"- {name}: {val} {unit} ({status})")
                            
                            # Build formatted content matching the screenshot
                            labs_section = "\n".join(labs_info) if labs_info else "- No lab data available"
                            
                            content = f"""Clinical Analysis Summary

Patient: {patient_name}
Analysis Date: {now.strftime('%m/%d/%Y')}
Generated by: Voice Agent

Key Findings

1. Liver Function Tests
{labs_section}

2. Clinical Impression
- Evidence of hepatocellular injury based on elevated liver enzymes
- Signs of synthetic dysfunction if albumin/INR abnormal
- Consistent with decompensated liver disease

3. Recommendations
- Continue monitoring liver function tests
- Consider hepatology consultation
- Review medication compliance

---
This analysis was generated via Voice Agent at {now.isoformat()}"""
                        
                        # Match chat agent structure - use both content and markdown for compatibility
                        agent_payload = {
                            "title": title,
                            "content": content,      # For display
                            "markdown": content,     # For agentData
                            "agentName": "Voice Agent",
                            "timestamp": now.isoformat()
                        }
                        
                        agent_res = await canvas_ops.create_result(agent_payload)
                        
                        # Auto-focus on the newly created agent result
                        if agent_res and agent_res.get("id"):
                            logger.info(f"🎯 Auto-focusing on agent result: {agent_res.get('id')}")
                            try:
                                await asyncio.sleep(0.5)  # Brief delay to ensure it's rendered
                                focus_result = await canvas_ops.focus_item(agent_res.get("id"))
                                logger.info(f"✅ Auto-focused on agent result: {focus_result}")
                            except Exception as e:
                                logger.error(f"Failed to auto-focus on agent result: {e}")
                        
                        result = json.dumps({
                            "status": "success",
                            "message": f"Created agent analysis: {title}",
                            "api_response": agent_res
                        })

                    elif function_name == "stop_audio":
                        # User said "stop" - immediately clear all audio
                        logger.info("🛑 STOP AUDIO - User requested to stop speaking")
                        await self.stop_speaking()
                        result = json.dumps({
                            "status": "success",
                            "message": "Audio stopped"
                        })

                    else:
                        result = f"Unknown tool: {function_name}"
                    
                except Exception as tool_error:
                    logger.error(f"Tool {function_name} error: {tool_error}")
                    result = f"Error executing {function_name}: {str(tool_error)}"
                    
                    # Notify UI that tool failed
                    await self.send_tool_notification(function_name, "failed", result)
                
                function_responses.append(
                    types.FunctionResponse(
                        id=fc.id,
                        name=function_name,
                        response={"result": result}
                    )
                )
                
                # Notify UI that tool completed
                await self.send_tool_notification(function_name, "completed", result)

                logger.info(f"  ✅ Tool {function_name} completed")

            # Send responses back to Gemini - use correct Live API method
            logger.info(f"📤 Sending {len(function_responses)} function response(s) back to Gemini")
            await self.session.send(input={"function_responses": function_responses})
            logger.info("✅ Function responses sent - awaiting Gemini's reply")

        except Exception as e:
            logger.error(f"Tool execution error: {e}")
            traceback.print_exc()
    
    async def stop_speaking(self):
        """Stop current Gemini response and clear audio queue immediately"""
        logger.info("🛑 STOP - Clearing all audio immediately")
        self.should_stop = True

        # Clear audio queue immediately - aggressive clearing
        cleared_audio = 0
        for _ in range(1000):  # Safety limit
            try:
                self.audio_in_queue.get_nowait()
                cleared_audio += 1
            except asyncio.QueueEmpty:
                break

        # Also clear any pending output
        cleared_out = 0
        for _ in range(1000):  # Safety limit
            try:
                self.out_queue.get_nowait()
                cleared_out += 1
            except asyncio.QueueEmpty:
                break

        logger.info(f"✅ STOPPED - cleared {cleared_audio} audio + {cleared_out} out chunks")

        # Notify client to clear their audio queue too
        try:
            await self.websocket.send_json({
                "type": "stop_confirmed",
                "message": "Audio stopped",
                "cleared_chunks": cleared_audio + cleared_out
            })
        except Exception as e:
            logger.error(f"Failed to send stop confirmation: {e}")

        # Keep should_stop True for at least 1 second to ensure all queued audio is blocked
        await asyncio.sleep(1.0)
        self.should_stop = False
        logger.info("✅ Stop flag reset, ready for new audio")
    
    def _calculate_audio_energy(self, audio_bytes: bytes) -> float:
        """Calculate RMS energy of audio chunk for voice activity detection"""
        import struct
        try:
            # Assume 16-bit PCM audio
            samples = struct.unpack(f'{len(audio_bytes)//2}h', audio_bytes)
            if not samples:
                return 0.0
            # Calculate RMS energy
            sum_squares = sum(s * s for s in samples)
            rms = (sum_squares / len(samples)) ** 0.5
            return rms
        except:
            return 0.0

    async def listen_audio(self):
        """Receive audio from WebSocket and send to Gemini with VAD filtering"""
        logger.info("🎤 Listening to client audio...")

        # Voice Activity Detection settings
        VAD_THRESHOLD = 300  # RMS threshold for speech detection (lowered for better interruption)
        SILENCE_FRAMES_TO_SEND = 5  # Send a few silence frames after speech ends

        silence_count = 0
        speech_detected = False
        chunk_count = 0
        sent_count = 0

        try:
            while True:
                message = await self.websocket.receive()

                # Check for stop command
                if "text" in message:
                    try:
                        data = json.loads(message["text"])
                        if data.get("type") == "stop":
                            logger.info("🛑 STOP command received from client!")
                            await self.stop_speaking()
                            continue
                    except:
                        pass

                if "bytes" in message:
                    data = message["bytes"]
                    chunk_count += 1

                    # Calculate audio energy for VAD
                    energy = self._calculate_audio_energy(data)

                    if energy > VAD_THRESHOLD:
                        # Speech detected - send audio
                        speech_detected = True
                        silence_count = 0
                        sent_count += 1
                        # Reset audio suppression - user is speaking a new question
                        self._suppress_follow_up_audio = False
                        await self.out_queue.put({"data": data, "mime_type": "audio/pcm"})
                    elif speech_detected:
                        # Speech was detected recently - send trailing silence
                        silence_count += 1
                        if silence_count <= SILENCE_FRAMES_TO_SEND:
                            sent_count += 1
                            await self.out_queue.put({"data": data, "mime_type": "audio/pcm"})
                        else:
                            # End of speech - reset
                            speech_detected = False
                            silence_count = 0
                    # Else: silence before speech - skip

                    if chunk_count == 1:
                        logger.info(f"🎤 listen_audio: First audio chunk received ({len(data)} bytes)")
                    elif chunk_count % 100 == 0:
                        logger.info(f"🎤 VAD: {chunk_count} received, {sent_count} sent ({100*sent_count//chunk_count}% passed VAD)")

        except WebSocketDisconnect:
            logger.info("Client disconnected")
            raise asyncio.CancelledError()
        except Exception as e:
            logger.error(f"Error receiving audio: {e}")
            raise asyncio.CancelledError()
    
    async def send_audio_to_gemini(self):
        """Send audio from queue to Gemini"""
        try:
            logger.info("🎤 send_audio_to_gemini: Starting...")
            chunk_count = 0
            while True:
                audio_data = await self.out_queue.get()
                chunk_count += 1
                if chunk_count == 1:
                    logger.info("🎤 First audio chunk received from client, sending to Gemini...")
                elif chunk_count % 50 == 0:
                    logger.info(f"🎤 Sent {chunk_count} audio chunks to Gemini")
                await self.session.send(input=audio_data)
        except Exception as e:
            logger.error(f"Error sending to Gemini: {e}")
    
    async def receive_audio(self):
        """Receive audio and handle tool calls from Gemini Live"""
        logger.info("🔊 Starting response processing...")
        first_audio_logged = False
        turn_number = 0
        try:
            while True:
                turn = self.session.receive()
                turn_number += 1
                logger.info(f"🔄 === TURN {turn_number} START ===")

                response_count = 0
                audio_chunks = 0
                tool_call_after_audio = False

                # If previous turn had audio then a tool call, suppress this turn's audio
                suppress_this_turn = self._suppress_follow_up_audio
                if suppress_this_turn:
                    logger.info(f"🔇 Turn {turn_number}: Suppressing follow-up audio (previous turn already spoke)")
                    self._suppress_follow_up_audio = False

                async for response in turn:
                    response_count += 1

                    # Check for interruption
                    if hasattr(response, 'server_content') and response.server_content:
                        sc = response.server_content
                        if hasattr(sc, 'model_turn') and sc.model_turn:
                            if hasattr(sc.model_turn, 'parts'):
                                for part in sc.model_turn.parts:
                                    if hasattr(part, 'text') and part.text:
                                        logger.info(f"📝 Gemini text: '{part.text}'")

                        if sc.interrupted:
                            logger.info("🛑 User interrupted!")
                            await self.stop_speaking()
                            continue

                    # Check stop flag - if stopped, skip processing
                    if self.should_stop:
                        continue

                    # Handle audio data - stream immediately for low latency
                    if data := response.data:
                        if not self.should_stop:
                            # Suppress duplicate audio from tool follow-ups
                            if suppress_this_turn or tool_call_after_audio:
                                logger.info(f"🔇 Suppressing duplicate audio chunk ({len(data)} bytes)")
                                continue

                            # Debug first audio chunk to verify format
                            if not first_audio_logged:
                                logger.info(f"🎵 First audio chunk: {len(data)} bytes, type: {type(data)}")
                                duration_ms = (len(data) / 2 / 24000) * 1000
                                logger.info(f"🎵 Estimated duration: {duration_ms:.1f}ms at 24kHz")
                                first_audio_logged = True

                            self.audio_in_queue.put_nowait(data)
                            audio_chunks += 1

                    # Handle tool calls - await them to ensure proper execution
                    if hasattr(response, 'tool_call') and response.tool_call:
                        if audio_chunks > 0:
                            # Audio was already spoken in this turn BEFORE this tool call
                            # Any audio Gemini generates after this tool's response will be a repeat
                            tool_call_after_audio = True
                            self._suppress_follow_up_audio = True
                            logger.info(f"🔇 Audio already spoken ({audio_chunks} chunks), will suppress follow-up audio")
                        await self.handle_tool_call(response.tool_call)

                # Log turn summary
                if audio_chunks > 0:
                    logger.info(f"🔊 Turn {turn_number}: {audio_chunks} audio chunks sent to client")

                logger.info(f"🔄 === TURN {turn_number} END (responses: {response_count}, audio: {audio_chunks}, suppressed: {suppress_this_turn or tool_call_after_audio}) ===")
                        
        except Exception as e:
            logger.error(f"Error receiving audio: {e}")
            traceback.print_exc()
    
    async def play_audio(self):
        """Send audio from queue to WebSocket - stream as fast as possible"""
        logger.info("🔊 Streaming to client...")

        try:
            while True:
                # Check stop flag before waiting for audio
                if self.should_stop:
                    # Clear remaining audio
                    cleared = 0
                    while not self.audio_in_queue.empty():
                        try:
                            self.audio_in_queue.get_nowait()
                            cleared += 1
                        except asyncio.QueueEmpty:
                            break
                    if cleared > 0:
                        logger.info(f"🛑 Cleared {cleared} audio chunks (stopped)")
                    # Don't reset should_stop here - let stop_speaking() handle it
                    await asyncio.sleep(0.05)
                    continue

                # Get next audio chunk with short timeout to check stop flag frequently
                try:
                    bytestream = await asyncio.wait_for(self.audio_in_queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue  # Check stop flag again

                # Double-check stop flag after getting audio
                if self.should_stop:
                    continue

                # Send audio chunk immediately - let client handle buffering
                await self.websocket.send_bytes(bytestream)

        except Exception as e:
            logger.error(f"Error sending audio: {e}")
    
    async def run_with_session(self):
        """
        Run voice handler with a PRE-CONNECTED session.
        This is called when using the two-phase connection (session already connected).
        """
        logger.info(f"🎵 Starting voice session with pre-connected Gemini for patient {self.patient_id}")

        try:
            # CRITICAL: Ensure patient_manager points to the correct patient for this session.
            # canvas_ops and side_agent read patient_id from the global singleton.
            patient_manager.set_patient_id(self.patient_id, quiet=True)

            # Session is already connected - just notify UI and start tasks
            await self.send_status_to_ui("connected", "Voice agent ready (pre-connected)")

            # Load patient context
            logger.info(f"Loading patient context for patient {self.patient_id}...")
            self.context_data = await canvas_ops.get_board_items_async()

            # Create patient summary (for reference in this handler, system instruction was set by session manager)
            self.patient_summary = self._create_brief_summary()
            logger.info(f"📋 Patient summary loaded: {self.patient_summary[:200] if self.patient_summary else 'EMPTY'}")

            # Ensure queues are set
            if self.audio_in_queue is None:
                self.audio_in_queue = asyncio.Queue()
            if self.out_queue is None:
                self.out_queue = asyncio.Queue(maxsize=10)
            
            logger.info("🔗 Using pre-connected Gemini Live API session!")
            
            # Start concurrent tasks
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self.send_audio_to_gemini())
                tg.create_task(self.listen_audio())
                tg.create_task(self.receive_audio())
                tg.create_task(self.play_audio())
                
                # Keep alive until disconnect
                await asyncio.Future()
                
        except asyncio.CancelledError:
            logger.info("✅ Voice session ended (pre-connected)")
        except Exception as e:
            logger.error(f"❌ Voice session error: {type(e).__name__}: {e}")
            traceback.print_exc()
            try:
                await self.send_status_to_ui("error", f"Voice session error: {str(e)}")
            except:
                pass
        finally:
            logger.info("🧹 Cleanup completed (pre-connected session)")
    
    async def run(self):
        """Main run loop with concurrent tasks"""
        logger.info(f"🎵 Starting voice session for patient {self.patient_id}")

        # CRITICAL: Ensure patient_manager points to the correct patient
        patient_manager.set_patient_id(self.patient_id, quiet=True)

        # IMMEDIATELY tell browser we're starting - prevents browser timeout
        await self.send_status_to_ui("connecting", "Initializing voice agent...")

        # Check environment variables before attempting connection
        api_key = os.getenv('GOOGLE_API_KEY')
        
        if api_key:
            logger.info(f"🔑 Using API Key authentication: {api_key[:10]}...{api_key[-4:]}")
        else:
            logger.error("❌ GOOGLE_API_KEY not found in environment!")
            await self.send_status_to_ui("error", "Missing GOOGLE_API_KEY")
            return
        
        try:
            # Send status update to keep browser alive
            await self.send_status_to_ui("connecting", "Loading patient context...")
            
            # Load patient context using canvas_ops (agent-2.9 way) - use async version
            logger.info(f"Loading patient context for voice session...")
            self.context_data = await canvas_ops.get_board_items_async()

            # Create patient summary for system instruction BEFORE get_config()
            self.patient_summary = self._create_brief_summary()
            logger.info(f"📋 Patient summary for system instruction: {self.patient_summary[:200] if self.patient_summary else 'EMPTY'}")

            # Send another status update
            await self.send_status_to_ui("connecting", "Preparing configuration...")

            # Get the FULL config with system instructions, tools, and generation settings
            config = self.get_config()
            
            logger.info(f"✅ Voice session configured with full config including tools and system instruction")
            logger.info(f"   Tools: {len(config.get('tools', [{}])[0].get('function_declarations', []))} functions")
            logger.info(f"   System instruction length: {len(config.get('system_instruction', ''))} chars")
            logger.info(f"   Generation config: {config.get('generation_config', {})}")
            
            # Send status update - connecting to API
            await self.send_status_to_ui("connecting", "Connecting to Gemini Live API...")
            
            # Connect to Gemini Live API with increased timeout
            logger.info(f"🔌 Attempting to connect to Gemini Live API...")
            logger.info(f"   Model: {MODEL}")
            logger.info(f"   Auth: API Key")
            
            # Send status right before client init
            await self.send_status_to_ui("connecting", "Initializing Gemini client...")
            
            # Initialize client lazily (only when needed)
            client = self._get_client()
            
            # Send status right before connection
            await self.send_status_to_ui("connecting", "Establishing connection (this may take 30-60 seconds)...")
            
            # Create a heartbeat task to keep browser alive during connection
            async def send_heartbeat():
                """Send periodic status updates to keep browser WebSocket alive"""
                for i in range(60):  # Up to 60 seconds
                    await asyncio.sleep(1)
                    try:
                        await self.send_status_to_ui("connecting", f"Connecting... ({i+1}s)")
                    except:
                        break  # Connection established or failed
            
            # Start heartbeat and connection in parallel
            heartbeat_task = asyncio.create_task(send_heartbeat())
            
            try:
                # Connect directly - the websocket patch handles timeout (120s)
                logger.info(f"⏱️ Connecting to Gemini Live API...")
                logger.info(f"   Config: {config.get('response_modalities')} modalities")
                logger.info("   Calling client.aio.live.connect()...")
                
                async with (
                    client.aio.live.connect(
                        model=MODEL, 
                        config=config
                    ) as session,
                    asyncio.TaskGroup() as tg,
                ):
                    # Cancel heartbeat once connected
                    heartbeat_task.cancel()
                    
                    self.session = session
                    self.audio_in_queue = asyncio.Queue()
                    self.out_queue = asyncio.Queue(maxsize=10)
                    
                    logger.info("🔗 Connected to Gemini Live API successfully!")
                    
                    # Notify UI that voice is ready
                    await self.send_status_to_ui("connected", "Voice agent connected and ready")
                    
                    # Start concurrent tasks
                    tg.create_task(self.send_audio_to_gemini())
                    tg.create_task(self.listen_audio())
                    tg.create_task(self.receive_audio())
                    tg.create_task(self.play_audio())
                    
                    # Keep alive until disconnect
                    await asyncio.Future()
            except asyncio.CancelledError:
                # Heartbeat cancelled, this is normal
                pass
                
        except asyncio.CancelledError:
            logger.info("✅ Voice session ended")
        except (TimeoutError, asyncio.TimeoutError) as e:
            logger.error(f"❌ Voice session timeout: {e}")
            logger.error(f"   Possible causes: Network issues, firewall, model access")
            try:
                await self.send_status_to_ui("error", f"Connection timeout: {str(e)}")
            except:
                pass
        except Exception as e:
            logger.error(f"❌ Voice session error: {type(e).__name__}: {e}")
            traceback.print_exc()
            try:
                await self.send_status_to_ui("error", f"Voice session error: {str(e)}")
            except:
                pass
        finally:
            logger.info("🧹 Cleanup completed")
