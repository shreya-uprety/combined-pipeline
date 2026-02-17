# MedForce Backend API Documentation

**Base URL:** `https://combined-pipeline-481780815788.europe-west1.run.app`

**Protocol:** HTTPS for REST endpoints, WSS for WebSocket endpoints

---

## Table of Contents

1. [Health & Status](#1-health--status)
2. [Patient Management](#2-patient-management)
3. [Board Chat (Doctor's Board Agent)](#3-board-chat-doctors-board-agent)
4. [Voice Agent](#4-voice-agent)
5. [Canvas Operations (Board Actions)](#5-canvas-operations-board-actions)
6. [Report Generation](#6-report-generation)
7. [Pre-Consultation (Patient Chat)](#7-pre-consultation-patient-chat)
8. [Data Processing](#8-data-processing)
9. [Scheduling](#9-scheduling)
10. [Simulation](#10-simulation)
11. [Admin (Patient File Management)](#11-admin-patient-file-management)
12. [Utility](#12-utility)
13. [WebSocket Message Formats](#13-websocket-message-formats)

---

## 1. Health & Status

### `GET /`
Returns server status and a map of available endpoints.

**Response:**
```json
{
  "status": "MedForce Unified Server is Running",
  "features": ["chat", "voice", "canvas_operations", "simulation", "pre_consultation", "admin"],
  "endpoints": {
    "chat": "/send-chat",
    "voice_ws": "/ws/voice/{patient_id}",
    "chat_ws": "/ws/chat/{patient_id}",
    "canvas_focus": "/api/canvas/focus",
    "canvas_todo": "/api/canvas/create-todo",
    "generate_diagnosis": "/generate_diagnosis",
    "generate_report": "/generate_report",
    "pre_consult": "/chat",
    "simulation": "/ws/simulation",
    "transcriber": "/ws/transcriber",
    "admin": "/admin"
  }
}
```

---

### `GET /health`
Health check endpoint.

**Response:**
```json
{
  "status": "healthy",
  "service": "medforce-unified",
  "port": 8080
}
```

---

## 2. Patient Management

### `GET /patient/current`
Get the currently active patient ID.

**Response:**
```json
{
  "patient_id": "p0001"
}
```

---

### `POST /patient/switch`
Switch the active patient context. All subsequent operations will use this patient.

**Request Body:**
```json
{
  "patient_id": "p0002"
}
```

**Response:**
```json
{
  "status": "success",
  "patient_id": "p0002"
}
```

---

## 3. Board Chat (Doctor's Board Agent)

### `POST /send-chat`
Send a chat message to the board agent (doctor-facing AI assistant). Accepts full conversation history and returns the agent response.

**Request Body:** Array of message objects (conversation history)
```json
[
  {
    "patient_id": "p0001",
    "role": "user",
    "content": "What are the latest lab results for this patient?"
  }
]
```

**Response:**
```json
{
  "response": "The latest lab results show ALT at 245 U/L (elevated)...",
  "status": "success"
}
```

---

### `WSS /ws/chat/{patient_id}`
WebSocket endpoint for real-time board chat with RAG + tools.

**Connect:** `wss://combined-pipeline-481780815788.europe-west1.run.app/ws/chat/{patient_id}`

**Client sends (JSON):**
```json
{
  "type": "message",
  "content": "Show me the medication timeline"
}
```

**Server sends (JSON):** Streamed responses including text, tool calls, and status updates. See [WebSocket Message Formats](#13-websocket-message-formats).

---

## 4. Voice Agent

### `POST /api/voice/start/{patient_id}`
**Phase 1** of two-phase voice connection. Starts connecting to Gemini Live API in the background. Returns immediately with a session ID.

**Response:**
```json
{
  "session_id": "a1b2c3d4",
  "patient_id": "p0001",
  "status": "connecting",
  "poll_url": "/api/voice/status/a1b2c3d4",
  "websocket_url": "/ws/voice-session/a1b2c3d4",
  "message": "Connection started. Poll status endpoint until ready, then connect to WebSocket."
}
```

---

### `GET /api/voice/status/{session_id}`
**Phase 2** — Poll this endpoint to check if the voice session is ready.

**Response (connecting):**
```json
{
  "status": "connecting",
  "session_id": "a1b2c3d4",
  "patient_id": "p0001",
  "elapsed_seconds": 12.5
}
```

**Response (ready):**
```json
{
  "status": "ready",
  "session_id": "a1b2c3d4",
  "patient_id": "p0001",
  "connection_time_seconds": 45.2
}
```

**Error:** `404` if session not found.

---

### `WSS /ws/voice-session/{session_id}`
**Phase 3** — Connect WebSocket to a pre-connected voice session. Use the `session_id` returned from Phase 1 after status is `ready`.

**Connect:** `wss://combined-pipeline-481780815788.europe-west1.run.app/ws/voice-session/{session_id}`

**Client sends:**
- **Binary (bytes):** Raw PCM audio (16-bit, 16kHz or 24kHz)
- **JSON:** `{"type": "stop"}` to stop audio playback

**Server sends:**
- **Binary (bytes):** Audio response from Gemini (PCM, 24kHz)
- **JSON:** Status updates, tool call notifications. See [Voice WebSocket Messages](#voice-websocket-messages).

---

### `WSS /ws/voice/{patient_id}`
Direct voice connection (no pre-connect). Slower initial connection (~30-85 seconds), but simpler flow.

**Connect:** `wss://combined-pipeline-481780815788.europe-west1.run.app/ws/voice/{patient_id}`

Same binary/JSON message format as `/ws/voice-session/`.

---

### `DELETE /api/voice/session/{session_id}`
Close a voice session and free resources.

**Response:**
```json
{
  "status": "closed",
  "session_id": "a1b2c3d4"
}
```

---

## 5. Canvas Operations (Board Actions)

All canvas endpoints accept an optional `patient_id` field in the body to set context.

### `POST /api/canvas/focus`
Focus/navigate to a specific board item.

**Request Body (by ID):**
```json
{
  "patient_id": "p0001",
  "object_id": "lab-track-1"
}
```

**Request Body (by query — AI resolves the ID):**
```json
{
  "patient_id": "p0001",
  "query": "medication timeline"
}
```

**Response:**
```json
{
  "status": "success",
  "object_id": "medication-track-1",
  "data": { ... }
}
```

**Known Board Item IDs:**
| ID | Description |
|---|---|
| `sidebar-1` | Patient profile sidebar |
| `lab-track-1` | Lab timeline |
| `dashboard-item-lab-table` | Lab table view |
| `dashboard-item-lab-chart` | Lab chart view |
| `medication-track-1` | Medication timeline |
| `encounter-track-1` | Encounters |
| `risk-track-1` | Risk track |
| `key-events-track-1` | Key events |
| `adverse-event-analytics` | Adverse event panel |
| `differential-diagnosis` | Differential diagnosis |
| `referral-doctor-info` | Referral information |
| `easl-panel` | EASL guidelines panel |
| `raw-encounter-image-1` | Raw encounter report |
| `raw-lab-image-1` | Pathology report |
| `raw-lab-image-radiology-1` | Radiology report |
| `monitoring-patient-chat` | Patient chat panel |

---

### `POST /api/canvas/create-todo`
Create a TODO task on the board. The AI generates structured task data from the query.

**Request Body:**
```json
{
  "patient_id": "p0001",
  "query": "Order liver ultrasound and schedule hepatology consult"
}
```

**Response:**
```json
{
  "status": "success",
  "data": { ... }
}
```

---

### `POST /api/canvas/send-to-easl`
Send a clinical question to EASL (European Association for Study of the Liver) guidelines.

**Request Body:**
```json
{
  "patient_id": "p0001",
  "question": "What does EASL recommend for DILI management?"
}
```

**Response:**
```json
{
  "status": "success",
  "data": { ... }
}
```

---

### `POST /api/canvas/prepare-easl-query`
Prepare an EASL query by generating context and a refined question.

**Request Body:**
```json
{
  "patient_id": "p0001",
  "question": "treatment options for drug-induced liver injury"
}
```

**Response:**
```json
{
  "status": "success",
  "data": { ... }
}
```

---

### `POST /api/canvas/create-schedule`
Create a scheduling panel on the board using AI to generate structured data.

**Request Body:**
```json
{
  "patient_id": "p0001",
  "schedulingContext": "Follow-up appointment in 2 weeks for liver function tests",
  "context": ""
}
```

**Response:**
```json
{
  "status": "success",
  "data": { ... }
}
```

---

### `POST /api/canvas/send-notification`
Send a notification/alert to the care team.

**Request Body:**
```json
{
  "patient_id": "p0001",
  "message": "Critical lab values require immediate review",
  "type": "alert"
}
```

**Response:**
```json
{
  "status": "success",
  "data": { ... }
}
```

---

### `POST /api/canvas/create-lab-results`
Add a lab results panel to the board.

**Request Body:**
```json
{
  "patient_id": "p0001",
  "labResults": [
    {
      "name": "ALT",
      "value": 245,
      "unit": "U/L",
      "range": "7-56",
      "status": "high",
      "trend": "increasing"
    },
    {
      "name": "AST",
      "value": 189,
      "unit": "U/L",
      "range": "10-40",
      "status": "high",
      "trend": "stable"
    }
  ],
  "date": "2026-02-16",
  "source": "Lab Report"
}
```

Each lab result object:
| Field | Type | Required | Description |
|---|---|---|---|
| `name` or `parameter` | string | Yes | Lab test name (e.g., "ALT", "Bilirubin") |
| `value` | number | Yes | Test result value |
| `unit` | string | No | Unit of measurement (default: "-") |
| `range` or `normalRange` | string | No | Normal range as "min-max" (default: "0-100") |
| `status` | string | No | "normal", "high", "low", "abnormal" (default: "normal") |
| `trend` | string | No | "stable", "increasing", "decreasing" (default: "stable") |

**Response:**
```json
{
  "status": "success",
  "data": { ... }
}
```

---

### `POST /api/canvas/create-agent-result`
Create an AI analysis result card on the board.

**Request Body:**
```json
{
  "patient_id": "p0001",
  "title": "Clinical Analysis",
  "content": "## Key Findings\n- ALT elevated at 245 U/L\n- Suspected DILI",
  "agentName": "Clinical Agent"
}
```

**Response:**
```json
{
  "status": "success",
  "data": { ... }
}
```

---

### `GET /api/canvas/board-items/{patient_id}`
Get all board items/components for a patient.

**Response:**
```json
{
  "status": "success",
  "patient_id": "p0001",
  "items": [ ... ],
  "count": 15
}
```

---

## 6. Report Generation

All report endpoints accept an optional `patient_id` in the body.

### `POST /generate_diagnosis`
Generate a DILI (Drug-Induced Liver Injury) diagnosis report and add it to the board.

**Request Body:**
```json
{
  "patient_id": "p0001"
}
```

**Response:**
```json
{
  "status": "done",
  "data": {
    "board_response": { ... }
  }
}
```

---

### `POST /generate_report`
Generate a comprehensive patient summary report and add it to the board.

**Request Body:**
```json
{
  "patient_id": "p0001"
}
```

**Response:**
```json
{
  "status": "done",
  "data": {
    "board_response": { ... }
  }
}
```

---

### `POST /generate_legal`
Generate a legal compliance report and add it to the board.

**Request Body:**
```json
{
  "patient_id": "p0001"
}
```

**Response:**
```json
{
  "status": "done",
  "data": {
    "board_response": { ... }
  }
}
```

---

## 7. Pre-Consultation (Patient Chat)

The pre-consultation system simulates a clinic admin desk (Linda) that chats with patients before their appointment.

### `POST /chat`
Send a patient message to the pre-consultation agent.

**Request Body:**
```json
{
  "patient_id": "p0001",
  "patient_message": "I've been having stomach pain for 3 days",
  "patient_attachments": [
    {
      "filename": "lab_report.pdf",
      "content_base64": "data:application/pdf;base64,JVBERi0xLjQ..."
    }
  ],
  "patient_form": null
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `patient_id` | string | Yes | Patient identifier |
| `patient_message` | string | Yes | Patient's chat message |
| `patient_attachments` | array | No | Files as base64 (uploaded to GCS) |
| `patient_form` | object | No | Structured form data |

**Response:**
```json
{
  "patient_id": "p0001",
  "nurse_response": {
    "message": "I understand you've been experiencing stomach pain...",
    "actions": []
  },
  "status": "success"
}
```

---

### `GET /chat/{patient_id}`
Get full chat history for a patient.

**Response:**
```json
{
  "conversation": [
    {
      "sender": "admin",
      "message": "Hello, this is Linda the Hepatology Clinic admin desk. How can I help you today?"
    },
    {
      "sender": "patient",
      "message": "I've been having stomach pain"
    }
  ]
}
```

---

### `POST /chat/{patient_id}/reset`
Reset chat history to default initial greeting.

**Response:**
```json
{
  "status": "success",
  "message": "Chat history has been reset.",
  "current_state": {
    "conversation": [
      {
        "sender": "admin",
        "message": "Hello, this is Linda the Hepatology Clinic admin desk. How can I help you today?"
      }
    ]
  }
}
```

---

### `GET /patients`
Get list of all patients with their basic info.

**Response:** Array of patient basic info objects
```json
[
  {
    "patient_id": "p0001",
    "name": "John Smith",
    "age": 52,
    "chief_complaint": "Abdominal pain"
  }
]
```

---

### `POST /register`
Register a new patient.

**Request Body:**
```json
{
  "first_name": "John",
  "last_name": "Smith",
  "dob": "1974-03-15",
  "gender": "Male",
  "occupation": "Engineer",
  "marital_status": "Married",
  "phone": "+1234567890",
  "email": "john@example.com",
  "address": "123 Main St",
  "emergency_name": "Jane Smith",
  "emergency_relation": "Spouse",
  "emergency_phone": "+0987654321",
  "chief_complaint": "Persistent abdominal pain and fatigue",
  "medical_history": "Hypertension, Type 2 Diabetes",
  "allergies": "Penicillin"
}
```

| Field | Type | Required |
|---|---|---|
| `first_name` | string | Yes |
| `last_name` | string | Yes |
| `dob` | string | Yes |
| `gender` | string | Yes |
| `phone` | string | Yes |
| `email` | string | Yes |
| `chief_complaint` | string | Yes |
| `occupation` | string | No |
| `marital_status` | string | No |
| `address` | string | No |
| `emergency_name` | string | No |
| `emergency_relation` | string | No |
| `emergency_phone` | string | No |
| `medical_history` | string | No (default: "None") |
| `allergies` | string | No (default: "None") |

**Response:**
```json
{
  "patient_id": "PT-A1B2C3D4",
  "status": "Patient profile created successfully."
}
```

---

### `WSS /ws/pre-consult/{patient_id}`
WebSocket endpoint for real-time pre-consultation chat (Linda the admin).

**Connect:** `wss://combined-pipeline-481780815788.europe-west1.run.app/ws/pre-consult/{patient_id}`

---

## 8. Data Processing

### `GET /process/{patient_id}/preconsult`
Process raw pre-consultation data for a patient (parses uploaded files, generates structured data).

**Response:**
```json
{
  "status": "success",
  "message": "Pre-consultation data processed."
}
```

---

### `GET /process/{patient_id}/board`
Process and generate board/dashboard content for a patient.

**Response:**
```json
{
  "status": "success",
  "message": "Board objects have been processed."
}
```

---

### `GET /process/{patient_id}/board-update`
Process board object updates for a patient (incremental update).

**Response:**
```json
{
  "status": "success",
  "message": "Board objects have been processed."
}
```

---

### `GET /data/{patient_id}/{file_path}`
Get a JSON data file for a patient from GCS.

**Example:** `GET /data/p0001/basic_info.json`

**Response:** The JSON content of the requested file.

---

### `GET /image/{patient_id}/{file_path}`
Get an image file for a patient from GCS.

**Example:** `GET /image/p0001/lab_report.png`

**Response:**
```json
{
  "file": "lab_report.png",
  "data": "<base64 encoded bytes>"
}
```

---

## 9. Scheduling

### `GET /schedule/{clinician_id}`
Get the full schedule for a clinician.

**Clinician ID format:**
- `N001`, `N002`, ... = Nurses
- `D001`, `D002`, ... = Doctors

**Example:** `GET /schedule/D001`

**Response:** Schedule data as JSON (structure depends on CSV content).

---

### `POST /schedule/update`
Update a specific schedule slot.

**Request Body:**
```json
{
  "clinician_id": "D001",
  "date": "2026-02-17",
  "time": "09:00",
  "patient": "p0001",
  "status": "confirmed"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `clinician_id` | string | Yes | Starts with "N" (nurse) or "D" (doctor) |
| `date` | string | Yes | Date of the slot |
| `time` | string | Yes | Time of the slot |
| `patient` | string | No | Patient ID to assign |
| `status` | string | No | New status (e.g., "confirmed", "done", "break", "cancelled") |

**Response:**
```json
{
  "message": "Schedule updated successfully."
}
```

---

### `POST /schedule/switch`
Switch two schedule slots.

**Request Body:**
```json
{
  "clinician_id": "D001",
  "item1": {
    "patient": "p0001",
    "date": "2026-02-17",
    "time": "09:00"
  },
  "item2": {
    "patient": "p0002",
    "date": "2026-02-17",
    "time": "10:00"
  }
}
```

**Response:**
```json
{
  "message": "Schedule updated successfully."
}
```

---

### `GET /slots?doctor_type=General`
Get available (empty) appointment slots.

**Query Parameters:**
| Param | Type | Default | Description |
|---|---|---|---|
| `doctor_type` | string | "General" | Type of doctor to filter slots |

**Response:**
```json
{
  "available_slots": [
    {
      "date": "2026-02-17",
      "time": "14:00",
      "clinician_id": "D001"
    }
  ]
}
```

---

## 10. Simulation

### `WSS /ws/simulation`
WebSocket endpoint for text-based clinical simulation (AI patient + AI doctor interaction).

**Connect:** `wss://combined-pipeline-481780815788.europe-west1.run.app/ws/simulation`

**Client sends (first message must be start command):**
```json
{
  "type": "start",
  "patient_id": "P0001",
  "gender": "Male"
}
```

**Server sends:** Simulation messages (text exchanges, status updates).

---

### `WSS /ws/simulation/audio`
WebSocket endpoint for scripted audio simulation.

**Connect:** `wss://combined-pipeline-481780815788.europe-west1.run.app/ws/simulation/audio`

**Client sends (first message):**
```json
{
  "type": "start",
  "patient_id": "P0001",
  "script_file": "scenario_script.json"
}
```

**Server sends:** Audio data (binary) and status updates (JSON).

---

### `WSS /ws/transcriber`
AI Transcriber WebSocket. Receives live audio, performs speech-to-text, and generates AI clinical analysis in real-time.

**Connect:** `wss://combined-pipeline-481780815788.europe-west1.run.app/ws/transcriber`

**Client sends:**

1. **Start command (JSON):**
```json
{
  "type": "start",
  "patient_id": "P0001"
}
```

2. **Audio data (Binary):** Raw audio bytes for transcription.

3. **Stop command (JSON):**
```json
{
  "status": true
}
```

**Server sends:**
```json
{
  "type": "system",
  "message": "Transcriber initialized for P0001"
}
```
Plus transcription results, AI analysis updates, question suggestions, and education content as JSON.

---

## 11. Admin (Patient File Management)

### `GET /admin`
Serves the Admin UI HTML page.

**Response:** HTML page

---

### `POST /api/get-patient-file`
Retrieve a file from GCS for a patient (from `patient_profile/` bucket).

**Request Body:**
```json
{
  "pid": "P0001",
  "file_name": "patient_info.md"
}
```

**Response:** File content with appropriate Content-Type:
- `.json` files: JSON response
- `.md`, `.txt` files: text/markdown
- `.png`, `.jpg` files: image bytes
- Other: binary download

---

### `GET /api/admin/list-files/{pid}`
List all files for a patient.

**Example:** `GET /api/admin/list-files/P0001`

**Response:**
```json
{
  "files": [
    {
      "name": "patient_info.md",
      "full_path": "patient_profile/P0001/patient_info.md",
      "size": 1234,
      "updated": "2026-02-16T10:00:00.000Z"
    }
  ]
}
```

---

### `POST /api/admin/save-file`
Create or update a text-based file for a patient.

**Request Body:**
```json
{
  "pid": "P0001",
  "file_name": "patient_info.md",
  "content": "# Patient Profile\nName: John Smith\nAge: 52"
}
```

**Response:**
```json
{
  "message": "File saved successfully",
  "path": "patient_profile/P0001/patient_info.md"
}
```

---

### `DELETE /api/admin/delete-file?pid=P0001&file_name=notes.md`
Delete a specific file.

**Query Parameters:**
| Param | Type | Required |
|---|---|---|
| `pid` | string | Yes |
| `file_name` | string | Yes |

**Response:**
```json
{
  "message": "File deleted successfully"
}
```

---

### `GET /api/admin/list-patients`
List all patient folders.

**Response:**
```json
{
  "patients": ["P0001", "P0002", "P0003"]
}
```

---

### `POST /api/admin/create-patient`
Create a new patient folder with a default `patient_info.md` file.

**Request Body:**
```json
{
  "pid": "P0010"
}
```

**Response:**
```json
{
  "message": "Patient created",
  "pid": "P0010"
}
```

**Error (already exists):** `400`
```json
{
  "error": "Patient already exists"
}
```

---

### `DELETE /api/admin/delete-patient?pid=P0010`
Delete a patient folder and ALL files inside it.

**Query Parameters:**
| Param | Type | Required |
|---|---|---|
| `pid` | string | Yes |

**Response:**
```json
{
  "message": "Deleted 5 files for patient P0010"
}
```

---

## 12. Utility

### `GET /ws/sessions`
Get information about all active WebSocket sessions (chat + pre-consult).

**Response:**
```json
{
  "active_sessions": 2,
  "sessions": [ ... ]
}
```

---

### `GET /test-gemini-live`
Test endpoint to check Gemini Live API connection speed.

**Response:**
```json
{
  "status": "connected",
  "connect_time_seconds": 45.23
}
```

---

### `GET /ui/{file_path}`
Serve static UI/test files from the `ui/` directory.

**Example:** `GET /ui/test_page.html`

**Response:** HTML content of the requested file.

---

## 13. WebSocket Message Formats

### Voice WebSocket Messages

**Server -> Client JSON messages:**

| Type | Description | Fields |
|---|---|---|
| `status` | Connection/session status | `status`, `message`, `timestamp` |
| `tool_call` | Tool execution notification | `tool`, `status` ("executing"/"completed"/"failed"), `result`, `timestamp` |
| `todo_update` | TODO task animation update | `todo_id`, `task_id`, `index`, `status`, `timestamp` |
| `stop_confirmed` | Audio stop acknowledgment | `message`, `cleared_chunks` |

**Status message types:**
```json
{"type": "status", "status": "connecting", "message": "Initializing voice agent..."}
{"type": "status", "status": "connecting", "message": "Connecting... (15s)"}
{"type": "status", "status": "connected", "message": "Voice agent connected and ready"}
{"type": "status", "status": "error", "message": "Connection timeout: ..."}
```

**Tool call notifications:**
```json
{"type": "tool_call", "tool": "get_patient_data", "status": "executing", "timestamp": "..."}
{"type": "tool_call", "tool": "get_patient_data", "status": "completed", "result": "...", "timestamp": "..."}
```

**Client -> Server:**
- **Binary:** Raw PCM audio (16-bit, mono)
- **JSON:** `{"type": "stop"}` to stop audio playback

### Voice Agent Available Tools
The voice agent can execute these tools during conversation:

| Tool | Trigger | Description |
|---|---|---|
| `get_patient_data` | Any patient question | Retrieves patient data from the board |
| `focus_board_item` | "show me", "go to", "focus on" | Navigates board to an item |
| `create_task` | "create task", "add todo", "remind me" | Creates a TODO on the board |
| `send_to_easl` | "EASL", "guidelines" | Sends question to EASL guidelines |
| `generate_dili_diagnosis` | "DILI diagnosis", "RUCAM" | Generates DILI report |
| `generate_patient_report` | "patient report", "summary" | Generates patient report |
| `generate_legal_report` | "legal report", "compliance" | Generates legal report |
| `generate_ai_diagnosis` | "AI diagnosis" | Generates AI diagnostic report |
| `generate_ai_treatment_plan` | "treatment plan" | Generates AI treatment plan |
| `create_schedule` | "schedule", "appointment" | Creates scheduling panel |
| `send_notification` | "notify", "alert" | Sends alert to care team |
| `create_doctor_note` | "add a note", "clinical note" | Creates a doctor/nurse note |
| `send_message_to_patient` | "message the patient", "tell the patient" | Sends message via patient chat |
| `create_lab_results` | "add labs", "post labs" | Adds lab panel to board |
| `create_agent_result` | "create analysis" | Creates AI analysis card |
| `stop_audio` | "stop", "quiet", "enough" | Stops audio playback |

---

## Recommended Connection Flow for Voice

```
1. POST /api/voice/start/{patient_id}     -> get session_id
2. Poll GET /api/voice/status/{session_id} -> wait for "ready"
3. Connect WSS /ws/voice-session/{session_id}
4. Send binary audio, receive binary audio + JSON status
5. DELETE /api/voice/session/{session_id}  -> cleanup
```

---

## Error Responses

All endpoints return standard HTTP error codes:

| Code | Description |
|---|---|
| `400` | Bad request (missing required fields, invalid input) |
| `404` | Resource not found (patient, file, session) |
| `500` | Internal server error (agent failure, GCS error) |
| `503` | Service unavailable (agent not loaded) |

Error body format:
```json
{
  "detail": "Error description here"
}
```

---

## CORS

The server allows all origins (`*`), all methods, and all headers. No special CORS configuration needed from the frontend.
