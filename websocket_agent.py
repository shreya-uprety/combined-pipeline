"""
WebSocket-Based Live Agent for Real-Time Frontend Interaction
================================================================

This module provides WebSocket support for real-time streaming of agent responses.
Integrates with the existing PreConsulteAgent for live chat functionality.

Features:
- Real-time bidirectional communication
- Streaming responses
- Session management
- Background task support
- Connection state tracking

Author: AI Developer Assistant
Date: January 28, 2026
Version: 2.0 - Lazy initialization for Cloud Run compatibility
"""

import asyncio
import json
import logging
import base64
import io
from typing import Dict, Optional, Any, Set
from datetime import datetime
from fastapi import WebSocket, WebSocketDisconnect
from enum import Enum
import uuid
import os
from google import genai

# Configure logging first (before any usage)
logger = logging.getLogger("websocket-agent")

# Gemini Live API configuration
GEMINI_LIVE_MODEL = "gemini-live-2.5-flash-preview-native-audio-09-2025"
VOICE_ENABLED = True  # Gemini Live handles audio natively

# Import existing agents (PreConsulteAgent removed - not needed for board agents)
from chat_agent import ChatAgent


class MessageType(str, Enum):
    """WebSocket message types."""
    TEXT = "text"
    FORM = "form"
    SLOTS = "slots"
    ATTACHMENT = "attachment"
    ERROR = "error"
    STATUS = "status"
    TYPING = "typing"
    TOOL_CALL = "tool_call"
    STREAM_START = "stream_start"
    STREAM_CHUNK = "stream_chunk"
    STREAM_END = "stream_end"
    # Voice communication types
    AUDIO_START = "audio_start"
    AUDIO_CHUNK = "audio_chunk"
    AUDIO_END = "audio_end"
    AUDIO_RESPONSE = "audio_response"
    TRANSCRIPTION = "transcription"


class ConnectionState(str, Enum):
    """WebSocket connection states."""
    CONNECTING = "connecting"
    CONNECTED = "connected"
    PROCESSING = "processing"
    IDLE = "idle"
    DISCONNECTING = "disconnecting"
    DISCONNECTED = "disconnected"


class WebSocketSession:
    """
    Manages a single WebSocket connection session.
    Tracks state, metadata, and provides helper methods.
    """
    
    def __init__(self, websocket: WebSocket, session_id: str, patient_id: str):
        """
        Initialize WebSocket session.
        
        Args:
            websocket: FastAPI WebSocket connection
            session_id: Unique session identifier
            patient_id: Patient identifier
        """
        self.websocket = websocket
        self.session_id = session_id
        self.patient_id = patient_id
        self.state = ConnectionState.CONNECTING
        self.connected_at = datetime.now()
        self.last_activity = datetime.now()
        self.message_count = 0
        
    async def send_json(self, data: Dict[str, Any]):
        """
        Send JSON data through WebSocket.
        
        Args:
            data: Data to send
        """
        try:
            await self.websocket.send_json(data)
            self.last_activity = datetime.now()
            logger.debug(f"Sent message to session {self.session_id}")
        except Exception as e:
            logger.error(f"Error sending to session {self.session_id}: {e}")
            raise
    
    async def send_text(self, text: str, msg_type: MessageType = MessageType.TEXT):
        """
        Send text message through WebSocket.
        
        Args:
            text: Text to send
            msg_type: Message type
        """
        await self.send_json({
            "type": msg_type.value,
            "content": text,
            "timestamp": datetime.now().isoformat(),
            "session_id": self.session_id
        })
    
    async def send_typing_indicator(self, is_typing: bool = True):
        """
        Send typing indicator status.
        
        Args:
            is_typing: Whether agent is typing
        """
        await self.send_json({
            "type": MessageType.TYPING.value,
            "is_typing": is_typing,
            "timestamp": datetime.now().isoformat()
        })
    
    async def send_error(self, error_message: str, error_code: Optional[str] = None):
        """
        Send error message.
        
        Args:
            error_message: Error description
            error_code: Optional error code
        """
        await self.send_json({
            "type": MessageType.ERROR.value,
            "error": error_message,
            "error_code": error_code,
            "timestamp": datetime.now().isoformat()
        })
    
    def update_state(self, new_state: ConnectionState):
        """Update connection state."""
        old_state = self.state
        self.state = new_state
        logger.info(f"Session {self.session_id} state: {old_state} -> {new_state}")
    
    def get_session_info(self) -> Dict[str, Any]:
        """Get session information."""
        return {
            "session_id": self.session_id,
            "patient_id": self.patient_id,
            "state": self.state.value,
            "connected_at": self.connected_at.isoformat(),
            "last_activity": self.last_activity.isoformat(),
            "message_count": self.message_count,
            "duration_seconds": (datetime.now() - self.connected_at).total_seconds()
        }


class WebSocketConnectionManager:
    """
    Manages multiple WebSocket connections.
    Handles connection pooling, broadcasting, and session lifecycle.
    """
    
    def __init__(self):
        """Initialize connection manager."""
        self.active_sessions: Dict[str, WebSocketSession] = {}
        self.patient_to_sessions: Dict[str, Set[str]] = {}
        
    async def connect(self, websocket: WebSocket, patient_id: str) -> WebSocketSession:
        """
        Accept new WebSocket connection and create session.
        
        Args:
            websocket: WebSocket connection
            patient_id: Patient identifier
            
        Returns:
            Created session
        """
        await websocket.accept()
        
        session_id = str(uuid.uuid4())
        session = WebSocketSession(websocket, session_id, patient_id)
        
        self.active_sessions[session_id] = session
        
        # Track patient to session mapping
        if patient_id not in self.patient_to_sessions:
            self.patient_to_sessions[patient_id] = set()
        self.patient_to_sessions[patient_id].add(session_id)
        
        session.update_state(ConnectionState.CONNECTED)
        
        logger.info(f"New WebSocket connection: {session_id} (Patient: {patient_id})")
        logger.info(f"Active sessions: {len(self.active_sessions)}")
        
        return session
    
    def disconnect(self, session_id: str):
        """
        Disconnect and remove session.
        
        Args:
            session_id: Session to disconnect
        """
        if session_id in self.active_sessions:
            session = self.active_sessions[session_id]
            session.update_state(ConnectionState.DISCONNECTED)
            
            # Remove from patient mapping
            patient_id = session.patient_id
            if patient_id in self.patient_to_sessions:
                self.patient_to_sessions[patient_id].discard(session_id)
                if not self.patient_to_sessions[patient_id]:
                    del self.patient_to_sessions[patient_id]
            
            # Remove session
            del self.active_sessions[session_id]
            
            logger.info(f"Disconnected session: {session_id}")
            logger.info(f"Active sessions: {len(self.active_sessions)}")
    
    def get_session(self, session_id: str) -> Optional[WebSocketSession]:
        """Get session by ID."""
        return self.active_sessions.get(session_id)
    
    def get_patient_sessions(self, patient_id: str) -> list[WebSocketSession]:
        """
        Get all active sessions for a patient.
        
        Args:
            patient_id: Patient identifier
            
        Returns:
            List of active sessions
        """
        session_ids = self.patient_to_sessions.get(patient_id, set())
        return [self.active_sessions[sid] for sid in session_ids if sid in self.active_sessions]
    
    async def broadcast_to_patient(self, patient_id: str, message: Dict[str, Any]):
        """
        Broadcast message to all sessions for a patient.
        
        Args:
            patient_id: Patient identifier
            message: Message to broadcast
        """
        sessions = self.get_patient_sessions(patient_id)
        for session in sessions:
            try:
                await session.send_json(message)
            except Exception as e:
                logger.error(f"Broadcast error to session {session.session_id}: {e}")
    
    def get_all_sessions_info(self) -> list[Dict[str, Any]]:
        """Get information about all active sessions."""
        return [session.get_session_info() for session in self.active_sessions.values()]


class WebSocketLiveAgent:
    """
    Main WebSocket live agent that handles real-time communication.
    Integrates with ChatAgent for board operations.
    """
    
    def __init__(self):
        """Initialize WebSocket live agent."""
        self.connection_manager = WebSocketConnectionManager()
        self.pre_consult_agent = None  # Pre-consult agent removed - not needed for board agents
        self.gemini_client = None
        
        # Chat agents are the primary interface for board agents
        # Cache chat agents per patient for session persistence
        self.chat_agents: Dict[str, ChatAgent] = {}
        
        # Gemini Live sessions cache (per WebSocket session)
        self.gemini_live_sessions: Dict[str, Any] = {}
        
        # Try to initialize Gemini client for Live API
        try:
            api_key = os.getenv("GOOGLE_API_KEY")
            if api_key:
                self.gemini_client = genai.Client(api_key=api_key)
                logger.info("✅ Gemini client initialized in WebSocketLiveAgent")
            else:
                logger.warning("⚠️ GOOGLE_API_KEY not found")
        except Exception as e:
            logger.warning(f"⚠️ Gemini client initialization failed: {e}")
    
    def get_or_create_chat_agent(self, patient_id: str, use_tools: bool = True) -> ChatAgent:
        """
        Get existing or create new chat agent for patient.
        
        Args:
            patient_id: Patient identifier
            use_tools: Whether to enable tools
            
        Returns:
            ChatAgent instance
        """
        if patient_id not in self.chat_agents:
            self.chat_agents[patient_id] = ChatAgent(
                patient_id=patient_id,
                use_tools=use_tools
            )
            logger.info(f"Created new chat agent for patient {patient_id}")
        
        return self.chat_agents[patient_id]
    
    async def _create_gemini_live_session(self, session_id: str, patient_id: str):
        """
        Create Gemini Live API session for real-time audio processing.
        
        Args:
            session_id: WebSocket session ID
            patient_id: Patient identifier
        """
        try:
            # Load system prompt from file
            try:
                with open("system_prompts/system_prompt.md", "r", encoding="utf-8") as f:
                    base_prompt = f.read()
                
                # Add patient-specific context
                system_instruction = f"""{base_prompt}

--- PATIENT-SPECIFIC CONTEXT ---
Current Patient ID: {patient_id}
Board URL: https://iso-clinic-v3-481780815788.europe-west1.run.app/board/{patient_id}

CRITICAL INSTRUCTIONS:
- You are currently helping with patient ID: {patient_id}
- This patient ID never changes during this conversation
- When using tools, ALWAYS use patient_id: {patient_id}
- NEVER ask for patient ID - you already know it is {patient_id}
- All data queries should reference patient {patient_id}
"""
            except Exception as e:
                logger.error(f"Failed to load system prompt: {e}")
                # Fallback to basic prompt
                system_instruction = f"""You are MedForce Agent — a real-time conversational AI assistant.
Current Patient ID: {patient_id}
Board URL: https://iso-clinic-v3-481780815788.europe-west1.run.app/board/{patient_id}
Assist the clinician with patient care. Communicate only in English. Be concise.
"""
            
            # Gemini Live configuration
            config = {
                "response_modalities": ["AUDIO"],
                "system_instruction": system_instruction,
                "speech_config": {
                    "voice_config": {
                        "prebuilt_voice_config": {
                            "voice_name": "Charon"  # Professional voice
                        }
                    },
                    "language_code": "en-US"
                },
                "realtime_input_config": {
                    "automatic_activity_detection": {
                        "disabled": False,
                        "start_of_speech_sensitivity": "START_SENSITIVITY_LOW",
                        "end_of_speech_sensitivity": "END_SENSITIVITY_LOW",
                        "prefix_padding_ms": 200,
                        "silence_duration_ms": 2000  # 2 seconds of silence
                    }
                }
            }
            
            # Create Gemini Live session
            live_session = self.gemini_client.aio.live.connect(
                model=GEMINI_LIVE_MODEL,
                config=config
            )
            
            self.gemini_live_sessions[session_id] = {
                "session": live_session,
                "patient_id": patient_id,
                "audio_queue": asyncio.Queue()
            }
            
            logger.info(f"Created Gemini Live session for {session_id}")
            return live_session
            
        except Exception as e:
            logger.error(f"Failed to create Gemini Live session: {e}")
            raise
    
    async def handle_connection(self, websocket: WebSocket, patient_id: str, agent_type: str = "pre_consult"):
        """
        Handle WebSocket connection lifecycle.
        
        Args:
            websocket: WebSocket connection
            patient_id: Patient identifier
            agent_type: Type of agent to use ("pre_consult" or "chat")
        """
        session = await self.connection_manager.connect(websocket, patient_id)
        
        try:
            # Send welcome message
            await session.send_json({
                "type": MessageType.STATUS.value,
                "status": "connected",
                "message": "Connected to live agent",
                "session_id": session.session_id,
                "agent_type": agent_type
            })
            
            # Main message loop
            while True:
                # Receive message from client
                data = await websocket.receive_json()
                
                session.message_count += 1
                session.last_activity = datetime.now()
                session.update_state(ConnectionState.PROCESSING)
                
                # Process message based on agent type
                if agent_type == "pre_consult":
                    await self._handle_pre_consult_message(session, data)
                elif agent_type == "chat":
                    await self._handle_chat_message(session, data)
                else:
                    await session.send_error(f"Unknown agent type: {agent_type}")
                
                session.update_state(ConnectionState.IDLE)
                
        except WebSocketDisconnect:
            logger.info(f"Client disconnected: {session.session_id}")
        except Exception as e:
            logger.error(f"WebSocket error in session {session.session_id}: {e}")
            try:
                await session.send_error(f"Internal error: {str(e)}")
            except:
                pass
        finally:
            self.connection_manager.disconnect(session.session_id)
    
    async def _handle_pre_consult_message(self, session: WebSocketSession, data: Dict[str, Any]):
        """
        Handle message for PreConsulteAgent (Linda the admin).
        NOTE: This functionality has been removed for board agents.
        Redirect to chat agent instead.
        
        Args:
            session: WebSocket session
            data: Message data
        """
        # Pre-consult agent removed - redirect to chat agent
        await self._handle_chat_message(session, data)
    
    async def _handle_chat_message(self, session: WebSocketSession, data: Dict[str, Any]):
        """
        Handle message for ChatAgent (general Q&A with RAG + tools).
        
        Args:
            session: WebSocket session
            data: Message data
        """
        try:
            # Check if this is a voice message
            if data.get("type") == MessageType.AUDIO_CHUNK.value:
                await self._handle_voice_message(session, data)
                return
            
            # Get or create chat agent for this patient
            chat_agent = self.get_or_create_chat_agent(session.patient_id)
            
            user_message = data.get("message", "")
            stream_response = data.get("stream", True)  # Default to streaming
            voice_response = data.get("voice_response", False)  # Whether to return audio
            
            if stream_response:
                # Stream response
                await session.send_json({
                    "type": MessageType.STREAM_START.value,
                    "timestamp": datetime.now().isoformat()
                })
                
                await session.send_typing_indicator(True)
                
                full_response = ""
                async for chunk in chat_agent.chat_stream(user_message):
                    await session.send_json({
                        "type": MessageType.STREAM_CHUNK.value,
                        "content": chunk,
                        "timestamp": datetime.now().isoformat()
                    })
                    full_response += chunk
                
                await session.send_typing_indicator(False)
                
                await session.send_json({
                    "type": MessageType.STREAM_END.value,
                    "timestamp": datetime.now().isoformat()
                })
                
                # Note: For voice responses, use audio_chunk message type instead
                # Voice is handled natively through Gemini Live API

            else:
                # Non-streaming response
                await session.send_typing_indicator(True)
                
                response = await chat_agent.chat(user_message)
                
                await session.send_typing_indicator(False)
                
                await session.send_json({
                    "type": MessageType.TEXT.value,
                    "content": response,
                    "timestamp": datetime.now().isoformat()
                })
                
                # Note: For voice responses, use audio_chunk message type instead
                # Voice is handled natively through Gemini Live API

            
            logger.info(f"Chat response sent to session {session.session_id}")
            
        except Exception as e:
            logger.error(f"Error handling chat message: {e}")
            await session.send_error(f"Failed to process chat: {str(e)}")
    
    async def _handle_voice_message(self, session: WebSocketSession, data: Dict[str, Any]):
        """
        Handle voice/audio message from client using Gemini Live API.
        
        Args:
            session: WebSocket session
            data: Message data with audio
        """
        try:
            # Get audio data (base64 encoded)
            audio_base64 = data.get("audio", "")
            audio_bytes = base64.b64decode(audio_base64)
            
            # Send processing status
            await session.send_json({
                "type": MessageType.STATUS.value,
                "status": "processing",
                "message": "Processing audio..."
            })
            
            # Get or create Gemini Live session for this patient
            gemini_session = await self._get_or_create_live_session(session.patient_id)
            
            # Send audio to Gemini Live
            await gemini_session.send(input=audio_bytes)
            
            # Receive and process responses
            full_text = ""
            audio_chunks = []
            
            async for response in gemini_session.receive():
                # Handle server content (text/audio from Gemini)
                if response.server_content:
                    # Check for model turn (includes both text and audio)
                    if response.server_content.model_turn:
                        for part in response.server_content.model_turn.parts:
                            # Handle text responses
                            if hasattr(part, 'text') and part.text:
                                full_text += part.text
                                # Stream text chunks back to client
                                await session.send_json({
                                    "type": MessageType.STREAM_CHUNK.value,
                                    "content": part.text,
                                    "timestamp": datetime.now().isoformat()
                                })
                            
                            # Handle inline audio responses
                            if hasattr(part, 'inline_data') and part.inline_data:
                                audio_data = part.inline_data.data
                                audio_chunks.append(audio_data)
                                # Stream audio chunks back to client
                                audio_base64 = base64.b64encode(audio_data).decode('utf-8')
                                await session.send_json({
                                    "type": MessageType.AUDIO_RESPONSE.value,
                                    "audio": audio_base64,
                                    "format": "pcm",
                                    "timestamp": datetime.now().isoformat()
                                })
                    
                    # Handle tool calls
                    if response.server_content.turn_complete:
                        # Check if there are any tool calls to execute
                        for part in response.server_content.model_turn.parts:
                            if hasattr(part, 'function_call') and part.function_call:
                                # Execute tool call in background
                                asyncio.create_task(
                                    self._handle_tool_call(gemini_session, part.function_call, session)
                                )
                        break  # Turn is complete
            
            # Send completion status
            await session.send_json({
                "type": MessageType.STATUS.value,
                "status": "complete",
                "message": "Processing complete"
            })
            
            logger.info(f"Voice message processed for session {session.session_id}")
            
        except Exception as e:
            logger.error(f"Error handling voice message with Gemini Live: {e}")
            await session.send_error(f"Voice processing failed: {str(e)}")
    
    async def _get_or_create_live_session(self, patient_id: str):
        """
        Get or create a Gemini Live session for a patient.
        
        Args:
            patient_id: Patient identifier
            
        Returns:
            Gemini Live session
        """
        if patient_id not in self.gemini_live_sessions:
            # Get patient context for system instruction
            patient_data = await self.gcs_bucket_manager.get_patient_data(patient_id)
            
            # Create comprehensive system instruction
            system_instruction = f"""You are a helpful medical assistant for patient {patient_id}.

Patient Information:
{json.dumps(patient_data.get('basic_info', {}), indent=2)}

Your capabilities:
- Answer questions about the patient's medical history
- Explain lab results and medications
- Provide general medical information
- Schedule appointments or consultations

Always be professional, empathetic, and accurate. If you need to access specific data,
use the available tools to retrieve it."""
            
            # Create new Gemini Live session
            session = await self._create_gemini_live_session(system_instruction)
            self.gemini_live_sessions[patient_id] = session
        
        return self.gemini_live_sessions[patient_id]
    
    async def _handle_tool_call(self, gemini_session, function_call, websocket_session: WebSocketSession):
        """
        Handle tool/function calls from Gemini Live.
        
        Args:
            gemini_session: Gemini Live session
            function_call: Function call from Gemini
            websocket_session: WebSocket session for notifications
        """
        try:
            function_name = function_call.name
            function_args = dict(function_call.args)
            
            # Notify client that tool is being executed
            await websocket_session.send_json({
                "type": MessageType.STATUS.value,
                "status": "tool_execution",
                "message": f"Executing {function_name}..."
            })
            
            # Execute the tool using ChatAgent's tool execution
            chat_agent = self.get_or_create_chat_agent(websocket_session.patient_id)
            result = await chat_agent._execute_tool_call(function_name, function_args)
            
            # Send result back to Gemini Live session
            await gemini_session.send(
                tool_response={
                    "function_responses": [{
                        "id": function_call.id,
                        "name": function_name,
                        "response": {"result": result}
                    }]
                }
            )
            
            logger.info(f"Tool {function_name} executed successfully")
            
        except Exception as e:
            logger.error(f"Error executing tool {function_name}: {e}")
            # Send error back to Gemini
            await gemini_session.send(
                tool_response={
                    "function_responses": [{
                        "id": function_call.id,
                        "name": function_name,
                        "response": {"error": str(e)}
                    }]
                }
            )
    
    async def broadcast_to_patient(self, patient_id: str, message: str, msg_type: MessageType = MessageType.TEXT):
        """
        Broadcast message to all sessions for a patient.
        Useful for notifications or updates.
        
        Args:
            patient_id: Patient identifier
            message: Message to broadcast
            msg_type: Message type
        """
        await self.connection_manager.broadcast_to_patient(
            patient_id,
            {
                "type": msg_type.value,
                "content": message,
                "timestamp": datetime.now().isoformat(),
                "broadcast": True
            }
        )
    
    def get_active_sessions(self) -> list[Dict[str, Any]]:
        """Get information about all active sessions."""
        return self.connection_manager.get_all_sessions_info()


# Global instance for FastAPI integration - LAZY INITIALIZATION (DO NOT instantiate here!)
# This prevents crashes when credentials are missing at import time
websocket_agent = None

def get_websocket_agent():
    """
    Get or create the global WebSocket agent instance.
    Lazy initialization prevents import-time crashes.
    """
    global websocket_agent
    if websocket_agent is None:
        try:
            logger.info("Initializing WebSocketLiveAgent...")
            websocket_agent = WebSocketLiveAgent()
            logger.info("✅ WebSocketLiveAgent instance created successfully")
        except Exception as e:
            logger.error(f"❌ Failed to create WebSocketLiveAgent: {e}")
            import traceback
            traceback.print_exc()
            # Return None - caller must handle this
            return None
    return websocket_agent


# FastAPI WebSocket endpoint handlers
async def websocket_pre_consult_endpoint(websocket: WebSocket, patient_id: str):
    """
    WebSocket endpoint for pre-consultation chat (Linda the admin).
    
    Usage in FastAPI:
        @app.websocket("/ws/pre-consult/{patient_id}")
        async def websocket_pre_consult(websocket: WebSocket, patient_id: str):
            await websocket_pre_consult_endpoint(websocket, patient_id)
    
    Args:
        websocket: WebSocket connection
        patient_id: Patient identifier
    """
    agent = get_websocket_agent()
    if agent is None:
        await websocket.close(code=1011, reason="WebSocket agent initialization failed")
        return
    await agent.handle_connection(websocket, patient_id, agent_type="pre_consult")


async def websocket_chat_endpoint(websocket: WebSocket, patient_id: str):
    """
    WebSocket endpoint for general chat with RAG + tools.
    
    Usage in FastAPI:
        @app.websocket("/ws/chat/{patient_id}")
        async def websocket_chat(websocket: WebSocket, patient_id: str):
            await websocket_chat_endpoint(websocket, patient_id)
    
    Args:
        websocket: WebSocket connection
        patient_id: Patient identifier
    """
    agent = get_websocket_agent()
    if agent is None:
        await websocket.close(code=1011, reason="WebSocket agent initialization failed")
        return
    await agent.handle_connection(websocket, patient_id, agent_type="chat")


# Example client-side JavaScript for reference
EXAMPLE_CLIENT_CODE = """
// Example WebSocket client code (JavaScript)

// Connect to pre-consultation agent
const wsPreConsult = new WebSocket('ws://localhost:8000/ws/pre-consult/P0001');

wsPreConsult.onopen = () => {
    console.log('Connected to pre-consult agent');
};

wsPreConsult.onmessage = (event) => {
    const data = JSON.parse(event.data);
    console.log('Received:', data);
    
    switch(data.type) {
        case 'text':
            displayMessage(data.content);
            break;
        case 'form':
            showForm(data.data.form_request);
            break;
        case 'slots':
            showSlots(data.data.available_slots);
            break;
        case 'typing':
            showTypingIndicator(data.is_typing);
            break;
        case 'error':
            displayError(data.error);
            break;
        case 'stream_chunk':
            appendStreamChunk(data.content);
            break;
    }
};

// Send message
function sendMessage(message) {
    wsPreConsult.send(JSON.stringify({
        message: message,
        attachments: [],
        form_data: {}
    }));
}

// Connect to general chat agent
const wsChat = new WebSocket('ws://localhost:8000/ws/chat/P0001');

wsChat.onmessage = (event) => {
    const data = JSON.parse(event.data);
    
    if (data.type === 'stream_chunk') {
        // Append chunk to message display
        appendToLastMessage(data.content);
    }
};

// Send chat message with streaming
function sendChatMessage(message) {
    wsChat.send(JSON.stringify({
        message: message,
        stream: true  // Enable streaming
    }));
}
"""


if __name__ == "__main__":
    # Print example usage
    print("WebSocket Live Agent Module")
    print("=" * 60)
    print("\nTo integrate with FastAPI server, add these endpoints:")
    print("""
from websocket_agent import websocket_pre_consult_endpoint, websocket_chat_endpoint

@app.websocket("/ws/pre-consult/{patient_id}")
async def websocket_pre_consult(websocket: WebSocket, patient_id: str):
    await websocket_pre_consult_endpoint(websocket, patient_id)

@app.websocket("/ws/chat/{patient_id}")
async def websocket_chat(websocket: WebSocket, patient_id: str):
    await websocket_chat_endpoint(websocket, patient_id)
    """)
    print("\nExample client code saved in EXAMPLE_CLIENT_CODE variable")
