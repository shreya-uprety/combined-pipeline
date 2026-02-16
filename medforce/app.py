"""
MedForce Unified Server — Application Factory
"""

import os
import time
import asyncio
import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# ── 1. Apply patches FIRST (before any google.genai imports) ──
from medforce.patches import apply_all
apply_all()

# ── 2. Configure logging ──
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("medforce-server")

_startup_time = time.time()
logger.info("Server initialization started...")

# ── 3. Create FastAPI app ──
app = FastAPI(title="MedForce Unified Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 4. Register routers ──
from medforce.routers import (
    health,
    patient,
    board_chat,
    voice,
    canvas,
    reports,
    pre_consult,
    data_processing,
    scheduling,
    simulation,
    admin,
    utility,
)

app.include_router(health.router)
app.include_router(patient.router)
app.include_router(board_chat.router)
app.include_router(voice.router)
app.include_router(canvas.router)
app.include_router(reports.router)
app.include_router(pre_consult.router)
app.include_router(data_processing.router)
app.include_router(scheduling.router)
app.include_router(simulation.router)
app.include_router(admin.router)
app.include_router(utility.router)

# ── 5. Startup event ──
@app.on_event("startup")
async def startup_event():
    """Log startup information and pre-warm models"""
    port = os.environ.get("PORT", "8080")
    logger.info("=" * 60)
    logger.info("MedForce Unified Server Starting")
    logger.info(f"Listening on port: {port}")
    logger.info(f"Total init time: {time.time() - _startup_time:.2f}s")
    logger.info("=" * 60)

    # Start voice session cleanup task
    try:
        from medforce.agents.voice_session import voice_session_manager
        if voice_session_manager:
            voice_session_manager.start_cleanup_task()
    except Exception:
        pass

    # Pre-warm models
    logger.info("Pre-warming Gemini models...")
    try:
        try:
            from medforce.agents import board_chat_model as chat_model
            await asyncio.get_event_loop().run_in_executor(
                None, chat_model._get_model
            )
            logger.info("  Chat model warmed up")
        except Exception:
            pass

        try:
            from medforce.agents import side_agent
            side_agent._get_model("prompt_tool_call.txt")
            logger.info("  Side agent model warmed up")
        except Exception:
            pass

        logger.info("Model pre-warming complete!")
    except Exception as e:
        logger.warning(f"Model pre-warming failed (will warm on first request): {e}")
