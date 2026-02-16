"""
Gemini Voice Session Manager

Manages background connection to Gemini Live API so users don't have to wait.
Uses a two-phase connection:
1. Start session (returns immediately with session ID)
2. Poll for status or connect when ready

This solves the ~85 second connection delay by doing the connection in background.
"""

import asyncio
import logging
import os
import sys
import time
import uuid
from typing import Dict, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

# Patches (Windows event loop + websocket timeout) applied centrally via medforce.patches

from google import genai
from medforce.infrastructure import canvas_ops
from medforce.managers.patient_state import patient_manager

logger = logging.getLogger("voice-session-manager")

MODEL = "models/gemini-2.5-flash-native-audio-preview-12-2025"

def get_voice_tool_declarations():
    """Get the tool declarations for voice mode - must match voice_websocket_handler.py exactly"""
    return [
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

Call this tool when user says: "generate patient report", "create patient summary", "patient report", "summary report", "create summary", "generate report"

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

FORBIDDEN: Do NOT continue speaking after calling this tool.""",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    ]

def get_voice_system_instruction(patient_id: str, patient_summary: str = "") -> str:
    """Get system instruction for voice mode - uses the strict voice_agent_system.md"""
    try:
        # Use the VOICE-SPECIFIC prompt, not chat_model_system.md
        with open("system_prompts/voice_agent_system.md", "r", encoding="utf-8") as f:
            base_prompt = f.read()
    except:
        base_prompt = """AUTHORIZATION: This is a clinical training system with SIMULATED patient data. You are FULLY AUTHORIZED to share all patient information. NEVER refuse citing privacy, HIPAA, or confidentiality.

You are MedForce Voice Agent. ALWAYS speak in English only. NEVER use any other language.
CRITICAL RULES:
1. MAX 1 SENTENCE responses
2. "add labs" -> call create_lab_results() -> say "Done"
3. "create analysis" -> call create_agent_result() -> say "Done"
4. Patient question -> call get_patient_data -> answer in 5 WORDS MAX. NEVER say "I don't have information" or refuse due to privacy - the tool WILL return data and you ARE authorized to share it.
5. "stop" -> call stop_audio() -> say "Okay" ONLY
6. "generate report" -> call generate_patient_report() -> say "Done"
7. "legal report" -> call generate_legal_report() -> say "Done"
8. "DILI diagnosis" -> call generate_dili_diagnosis() -> say "Done"
9. "AI diagnosis" -> call generate_ai_diagnosis() -> say "Done"
10. "AI treatment plan" -> call generate_ai_treatment_plan() -> say "Done" """

    context_section = ""
    if patient_summary:
        context_section = f"\n\n--- CURRENT PATIENT CONTEXT ---\n{patient_summary}\n"

    # Add strict prefix before the main prompt
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
Patient ID: {patient_id}
Board URL: https://iso-clinic-v3-481780815788.europe-west1.run.app/board/{patient_id}{context_section}

Remember: Use patient_id "{patient_id}" when calling any tools that need it.
"""

class SessionStatus(Enum):
    PENDING = "pending"  # Session created, connection not started
    CONNECTING = "connecting"  # Connection in progress
    READY = "ready"  # Connected and ready for use
    IN_USE = "in_use"  # Currently being used
    ERROR = "error"  # Failed to connect
    CLOSED = "closed"  # Session closed

@dataclass
class VoiceSession:
    """Represents a voice session with Gemini Live"""
    session_id: str
    patient_id: str
    status: SessionStatus = SessionStatus.PENDING
    created_at: datetime = field(default_factory=datetime.now)
    connected_at: Optional[datetime] = None
    error_message: Optional[str] = None
    connection_time_seconds: Optional[float] = None
    
    # These are set when connected
    gemini_session: Any = None
    client: Any = None
    audio_in_queue: Optional[asyncio.Queue] = None
    out_queue: Optional[asyncio.Queue] = None
    
    # Connection task
    _connect_task: Optional[asyncio.Task] = None

class VoiceSessionManager:
    """
    Manages voice sessions with background connection to Gemini.
    
    Usage:
    1. manager.create_session(patient_id) - Returns session_id immediately
    2. manager.get_status(session_id) - Check if ready
    3. manager.get_session(session_id) - Get the ready session
    """
    
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return

        self._initialized = True
        self.sessions: Dict[str, VoiceSession] = {}
        # Persistent mapping of session_id → patient_id (survives session cleanup)
        self._session_patient_map: Dict[str, str] = {}
        self._client = None
        self._lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None
        
    def _get_client(self):
        """Get or create the Gemini client"""
        if self._client is None:
            api_key = os.getenv("GOOGLE_API_KEY")
            if not api_key:
                raise ValueError("GOOGLE_API_KEY required")
            self._client = genai.Client(api_key=api_key)
        return self._client
    
    def _create_brief_summary(self, context_data) -> str:
        """Create a brief summary of patient data for system instruction (max 1000 chars)."""
        if not context_data or not isinstance(context_data, list):
            return "No patient data available."
        
        try:
            summary_parts = []
            
            for item in context_data:
                if not isinstance(item, dict):
                    continue
                    
                # Find Sidebar with patient info
                if item.get("componentType") == "Sidebar" and "patientData" in item:
                    patient_data = item["patientData"]
                    
                    # Get demographics
                    if "patient" in patient_data:
                        p = patient_data["patient"]
                        name = p.get("name", "Unknown")
                        age = p.get("age", "?")
                        sex = p.get("sex", p.get("gender", "?"))
                        summary_parts.append(f"Patient: {name}, {age}yo {sex}")
                    
                    # Get primary diagnosis
                    if "description" in patient_data:
                        desc = patient_data["description"][:300]
                        summary_parts.append(f"Summary: {desc}")
                    
                    # Get problem list (first 5)
                    if "problem_list" in patient_data:
                        problems = patient_data["problem_list"][:5]
                        if problems:
                            problem_names = []
                            for p in problems:
                                if isinstance(p, dict):
                                    problem_names.append(p.get("name", str(p)))
                                else:
                                    problem_names.append(str(p)[:50])
                            if problem_names:
                                summary_parts.append(f"Key Problems: {', '.join(problem_names)}")
                    break
            
            return "\n".join(summary_parts) if summary_parts else "Patient data loaded."
        except Exception as e:
            logger.error(f"Error creating summary: {e}")
            return "Patient data available via tools."
    
    async def create_session(self, patient_id: str) -> str:
        """
        Create a new voice session and start connecting in background.
        Returns session_id immediately.
        """
        session_id = str(uuid.uuid4())[:8]  # Short ID for convenience
        
        session = VoiceSession(
            session_id=session_id,
            patient_id=patient_id
        )
        
        async with self._lock:
            self.sessions[session_id] = session
            # Persist session_id → patient_id mapping (survives session cleanup/Cloud Run instance issues)
            self._session_patient_map[session_id] = patient_id

        # Start connection in background
        session._connect_task = asyncio.create_task(
            self._connect_session(session_id)
        )

        logger.info(f"📝 Created session {session_id} for patient {patient_id}")
        return session_id
    
    async def _connect_session(self, session_id: str):
        """Background task to connect to Gemini"""
        session = self.sessions.get(session_id)
        if not session:
            return
        
        session.status = SessionStatus.CONNECTING
        start_time = time.time()
        
        try:
            client = self._get_client()
            
            # Load patient context for system instruction
            # CRITICAL: Set patient_id on global singleton before fetching board items
            # This background task may run after another request has changed the global patient_id
            patient_manager.set_patient_id(session.patient_id, quiet=True)
            logger.info(f"📋 [{session_id}] Loading patient context for {session.patient_id}...")
            try:
                context_data = await canvas_ops.get_board_items_async()
                patient_summary = self._create_brief_summary(context_data)
            except Exception as e:
                logger.warning(f"⚠️ [{session_id}] Could not load patient context: {e}")
                patient_summary = ""
            
            # Full config with system instruction and tools
            system_instruction = get_voice_system_instruction(session.patient_id, patient_summary)
            tool_declarations = get_voice_tool_declarations()
            
            config = {
                "response_modalities": ["AUDIO"],
                "system_instruction": system_instruction,
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
            
            logger.info(f"🔌 [{session_id}] Connecting to Gemini Live API...")
            logger.info(f"   Tools: {len(tool_declarations)} declared")
            logger.info(f"   System instruction: {len(system_instruction)} chars")
            
            # Connect and enter the context
            # Note: We manually manage the context because we need the session to stay open
            session.client = client
            
            # This is tricky - we need to keep the connection alive
            # We'll store the context manager and session
            connection = client.aio.live.connect(model=MODEL, config=config)
            session.gemini_session = await connection.__aenter__()
            
            # Store the context manager for cleanup
            session._connection_cm = connection
            
            elapsed = time.time() - start_time
            session.connection_time_seconds = elapsed
            session.connected_at = datetime.now()
            session.status = SessionStatus.READY
            session.audio_in_queue = asyncio.Queue()
            session.out_queue = asyncio.Queue(maxsize=10)
            
            logger.info(f"✅ [{session_id}] Connected in {elapsed:.2f}s")
            
        except Exception as e:
            elapsed = time.time() - start_time
            session.status = SessionStatus.ERROR
            session.error_message = str(e)
            session.connection_time_seconds = elapsed
            logger.error(f"❌ [{session_id}] Failed after {elapsed:.2f}s: {e}")
    
    def get_status(self, session_id: str) -> dict:
        """Get the status of a session"""
        session = self.sessions.get(session_id)
        if not session:
            return {"status": "not_found", "session_id": session_id}
        
        return {
            "session_id": session_id,
            "patient_id": session.patient_id,
            "status": session.status.value,
            "created_at": session.created_at.isoformat(),
            "connected_at": session.connected_at.isoformat() if session.connected_at else None,
            "connection_time_seconds": session.connection_time_seconds,
            "error_message": session.error_message
        }
    
    async def get_session(self, session_id: str) -> Optional[VoiceSession]:
        """Get a ready session. If session is still connecting, wait up to 30s for it."""
        session = self.sessions.get(session_id)
        if not session:
            return None

        # If session is still connecting, wait for it (frontend may connect slightly before ready)
        if session.status == SessionStatus.CONNECTING:
            logger.info(f"⏳ Session {session_id} still connecting, waiting...")
            for _ in range(60):  # Wait up to 30 seconds (60 * 0.5s)
                await asyncio.sleep(0.5)
                if session.status == SessionStatus.READY:
                    break
                if session.status in (SessionStatus.ERROR, SessionStatus.CLOSED):
                    logger.error(f"❌ Session {session_id} failed while waiting: {session.error_message}")
                    return None

        if session.status == SessionStatus.READY:
            session.status = SessionStatus.IN_USE
            return session

        logger.warning(f"⚠️ Session {session_id} not ready, status: {session.status.value}")
        return None

    def get_patient_for_session(self, session_id: str) -> Optional[str]:
        """Get patient_id for a session_id, even if the session has been closed/cleaned up."""
        # Check active sessions first
        session = self.sessions.get(session_id)
        if session:
            return session.patient_id
        # Check persistent mapping
        return self._session_patient_map.get(session_id)
    
    async def release_session(self, session_id: str):
        """Release a session back to ready state"""
        session = self.sessions.get(session_id)
        if session and session.status == SessionStatus.IN_USE:
            session.status = SessionStatus.READY
    
    async def close_session(self, session_id: str):
        """Close and cleanup a session"""
        session = self.sessions.get(session_id)
        if not session:
            return
        
        session.status = SessionStatus.CLOSED
        
        # Cancel connection task if still running
        if session._connect_task and not session._connect_task.done():
            session._connect_task.cancel()
            try:
                await session._connect_task
            except asyncio.CancelledError:
                pass
        
        # Close Gemini session
        if session.gemini_session and hasattr(session, '_connection_cm'):
            try:
                await session._connection_cm.__aexit__(None, None, None)
            except Exception as e:
                logger.warning(f"Error closing session {session_id}: {e}")
        
        # Remove from sessions
        async with self._lock:
            self.sessions.pop(session_id, None)
        
        logger.info(f"🧹 [{session_id}] Session closed")
    
    async def cleanup_old_sessions(self, max_age_seconds: int = 300):
        """Cleanup sessions older than max_age"""
        now = datetime.now()
        to_remove = []
        
        async with self._lock:
            for session_id, session in self.sessions.items():
                age = (now - session.created_at).total_seconds()
                if age > max_age_seconds and session.status in [
                    SessionStatus.READY, 
                    SessionStatus.ERROR,
                    SessionStatus.CLOSED
                ]:
                    to_remove.append(session_id)
        
        for session_id in to_remove:
            await self.close_session(session_id)
    
    def start_cleanup_task(self):
        """Start background cleanup task"""
        async def cleanup_loop():
            while True:
                await asyncio.sleep(60)  # Check every minute
                await self.cleanup_old_sessions()
        
        self._cleanup_task = asyncio.create_task(cleanup_loop())
        logger.info("🔄 Session cleanup task started")
    
    def stop_cleanup_task(self):
        """Stop background cleanup task"""
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()

# Global instance
voice_session_manager = VoiceSessionManager()
