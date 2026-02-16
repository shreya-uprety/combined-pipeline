# Board Agents Server

A streamlined FastAPI server providing chat and voice agents for canvas/board operations in a clinical simulation environment.

## Features

- **Chat Agent**: Text-based interaction with RAG (Retrieval-Augmented Generation) for patient context
- **Voice Agent**: Real-time voice communication using Gemini Live API
- **Canvas Operations**: Focus, navigate, create TODOs, generate reports on the clinical board

## Project Structure

```
board agents/
├── server.py                  # Main FastAPI server
├── canvas_ops.py              # Canvas/board operations (focus, todos, etc.)
├── canvas_tools.py            # Additional canvas manipulation tools
├── chat_model.py              # Chat agent implementation
├── chat_agent.py              # General chat agent with RAG
├── side_agent.py              # Tool routing and canvas operations
├── helper_model.py            # Helper functions for LLM responses
├── patient_manager.py         # Patient ID management
├── config.py                  # Configuration
├── websocket_agent.py         # WebSocket for real-time chat
├── voice_websocket_handler.py # Voice WebSocket for Gemini Live
├── voice_session_manager.py   # Voice session management
├── requirements.txt           # Python dependencies
├── system_prompts/            # LLM system prompts
├── response_schema/           # JSON response schemas
├── ui/                        # Test UI files
│   ├── integrated-test-agent.html  # Full test interface
│   └── chat-voice.html             # Voice chat test
└── output/                    # Generated output files
```

## Setup

1. **Create virtual environment**:
   ```bash
   python -m venv venv
   ```

2. **Activate virtual environment**:
   ```bash
   # Windows
   .\venv\Scripts\Activate.ps1
   
   # Linux/Mac
   source venv/bin/activate
   ```

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment variables** (create `.env` file):
   ```env
   GOOGLE_API_KEY=your_google_api_key
   CANVAS_URL=https://iso-clinic-v3-481780815788.europe-west1.run.app
   DEFAULT_PATIENT_ID=p0001
   ```

## Running the Server

```bash
python server.py
```

The server starts on `http://localhost:8080`

## API Endpoints

### Basic
- `GET /` - Server status and available endpoints
- `GET /health` - Health check

### Patient Management
- `GET /patient/current` - Get current patient ID
- `POST /patient/switch` - Switch patient (body: `{"patient_id": "p0001"}`)

### Chat
- `POST /send-chat` - Send chat message (body: list of chat messages)
- `WS /ws/chat/{patient_id}` - WebSocket for real-time chat

### Voice
- `WS /ws/voice/{patient_id}` - WebSocket for voice communication
- `POST /api/voice/start/{patient_id}` - Start voice session (two-phase)
- `GET /api/voice/status/{session_id}` - Check voice session status
- `WS /ws/voice-session/{session_id}` - Connect to pre-established session

### Canvas Operations
- `POST /api/canvas/focus` - Focus on board item
- `POST /api/canvas/create-todo` - Create TODO task
- `POST /api/canvas/send-to-easl` - Send question to EASL
- `POST /api/canvas/create-schedule` - Create schedule
- `POST /api/canvas/send-notification` - Send notification
- `POST /api/canvas/create-lab-results` - Add lab results
- `POST /api/canvas/create-agent-result` - Add agent analysis
- `GET /api/canvas/board-items/{patient_id}` - Get board items

### Report Generation
- `POST /generate_diagnosis` - Generate DILI diagnosis
- `POST /generate_report` - Generate patient report
- `POST /generate_legal` - Generate legal document

### Test UI
- `GET /ui/integrated-test-agent.html` - Integrated test interface
- `GET /ui/chat-voice.html` - Voice chat test

## Testing

Open the test UI in browser:
- http://localhost:8080/ui/integrated-test-agent.html
- http://localhost:8080/ui/chat-voice.html

## Deployment

This is a standalone service that can be deployed independently. It communicates with the frontend board via API.

## Dependencies

- FastAPI + Uvicorn (web server)
- google-genai (Gemini API for voice)
- google-generativeai (Gemini API for chat)
- websockets (real-time communication)
- httpx, aiohttp (HTTP clients)
