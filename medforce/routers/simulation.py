import json
import asyncio
import logging
import threading
import traceback
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()
logger = logging.getLogger("medforce-server")

# Lazy imports
try:
    from medforce.simulation.manager import SimulationManager
except Exception:
    SimulationManager = None

try:
    from medforce.simulation import scenario as simulation_scenario
except Exception:
    simulation_scenario = None

try:
    from medforce.simulation.transcriber import TranscriberEngine
    from medforce.infrastructure.gcs import fetch_gcs_text_internal
except Exception:
    TranscriberEngine = None
    fetch_gcs_text_internal = None


@router.websocket("/ws/simulation")
async def websocket_simulation(websocket: WebSocket):
    """WebSocket endpoint for text-based simulation."""
    await websocket.accept()

    manager = None
    try:
        data = await websocket.receive_json()

        if isinstance(data, dict) and data.get("type") == "start":
            patient_id = data.get("patient_id", "P0001")
            gender = data.get("gender", "Male")

            manager = SimulationManager(websocket, patient_id, gender)
            await manager.run()

    except WebSocketDisconnect:
        logger.info("Client disconnected")
        if manager:
            manager.running = False
            if hasattr(manager, 'logic_thread'):
                manager.logic_thread.stop()
    except Exception as e:
        traceback.print_exc()
        logger.error(f"WebSocket Error: {e}")
        if manager:
            manager.running = False


@router.websocket("/ws/simulation/audio")
async def websocket_simulation_audio(websocket: WebSocket):
    """WebSocket endpoint for scripted/audio-only simulation."""
    await websocket.accept()

    manager = None
    try:
        data = await websocket.receive_json()

        if isinstance(data, dict) and data.get("type") == "start":
            patient_id = data.get("patient_id", "P0001")
            script_file = data.get("script_file", "scenario_script.json")

            logger.info(f"Starting Audio Simulation for {patient_id} using {script_file}")

            manager = simulation_scenario.SimulationAudioManager(websocket, patient_id, script_file="scenario_dumps/transcript.json")
            await manager.run()

    except WebSocketDisconnect:
        logger.info("Audio Simulation Client disconnected")
        if manager:
            manager.stop()
    except Exception as e:
        traceback.print_exc()
        logger.error(f"Audio Simulation WebSocket Error: {e}")
        if manager:
            manager.stop()


@router.websocket("/ws/transcriber")
async def websocket_transcriber(websocket: WebSocket):
    """
    Main entry point for the AI Transcriber.
    Receives configuration (JSON) to start, raw audio (Bytes) to process,
    and pushes AI updates (JSON) back to the frontend.
    """
    with open("data/questions.json", "r") as file:
        questions = json.load(file)
    with open("output/question_pool.json", "w") as file:
        json.dump(questions, file, indent=4)
    with open("output/education_pool.json", "w") as file:
        json.dump([], file, indent=4)

    await websocket.accept()

    main_loop = asyncio.get_running_loop()
    engine = None

    logger.info("Frontend connected to /ws/transcriber")

    try:
        while True:
            message = await websocket.receive()

            # Handle JSON commands
            if "text" in message:
                try:
                    data = json.loads(message["text"])

                    # Manual Stop Signal {"status": True}
                    if data.get("status") is True:
                        logger.info("Frontend requested End of Consultation.")
                        if engine:
                            engine.finish_consultation()
                        else:
                            logger.warning("Frontend sent stop signal, but engine is not running.")

                    # Start Signal
                    elif data.get("type") == "start":
                        patient_id = data.get("patient_id", "P0001")
                        logger.info(f"Starting Transcriber Engine for {patient_id}")

                        patient_info = fetch_gcs_text_internal(patient_id, "patient_info.md")

                        engine = TranscriberEngine(
                            patient_id=patient_id,
                            patient_info=patient_info,
                            websocket=websocket,
                            loop=main_loop
                        )

                        stt_thread = threading.Thread(
                            target=engine.stt_loop,
                            daemon=True,
                            name=f"STT_{patient_id}"
                        )
                        stt_thread.start()

                        await websocket.send_json({
                            "type": "system",
                            "message": f"Transcriber initialized for {patient_id}"
                        })

                except json.JSONDecodeError:
                    logger.error("Received invalid JSON from frontend")

            # Handle binary audio data
            elif "bytes" in message:
                if engine and engine.running:
                    engine.add_audio(message["bytes"])

    except WebSocketDisconnect:
        logger.info("Frontend disconnected from /ws/transcriber")
    except Exception as e:
        logger.error(f"Transcriber WebSocket Error: {e}")
        traceback.print_exc()
    finally:
        if engine:
            logger.info("Stopping Transcriber Engine...")
            engine.stop()
