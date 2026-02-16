import os
from fastapi import APIRouter

router = APIRouter()


@router.get("/")
async def root():
    return {
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


@router.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "service": "medforce-unified",
        "port": os.environ.get("PORT", 8080)
    }
