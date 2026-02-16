# --- simulation_scenario.py ---

import asyncio
import json
import logging
import datetime
import os
import base64
import time
from fastapi import WebSocket
logger = logging.getLogger("medforce-backend-audio")

# Try to import mutagen for accurate audio duration
try:
    from mutagen import File as MutagenFile
    MUTAGEN_AVAILABLE = True
    logger.warning(f"MUTAGEN : Available")

except ImportError:
    MUTAGEN_AVAILABLE = False
    logger.warning(f"MUTAGEN : Not Available")



class TranscriptManager:
    """Thread-safe manager for the simulation history."""
    def __init__(self):
        self.history = []

    def log(self, speaker, text):
        entry = {
            "timestamp": datetime.datetime.now().strftime("%H:%M:%S"),
            "speaker": speaker,
            "text": text.strip()
        }
        self.history.append(entry)

class SimulationAudioManager:
    def __init__(self, websocket: WebSocket, patient_id: str, script_file: str = "scenario_script.json"):
        self.websocket = websocket
        self.patient_id = patient_id
        self.tm = TranscriptManager()
        self.running = False
        self.script_file = script_file

        # Load the linear script
        self.script_data = self._load_script()

    def _load_script(self):
        """Loads the conversation flow from a JSON file."""
        if not os.path.exists(self.script_file):
            logger.error(f"Script file {self.script_file} not found.")
            return []
        try:
            with open(self.script_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return sorted(data, key=lambda x: x.get('index', 0))
        except Exception as e:
            logger.error(f"Error loading script: {e}")
            return []

    def _get_audio_duration(self, file_path: str) -> float:
        """Returns duration in seconds."""
        if not os.path.exists(file_path):
            return 2.0 # Default fallback

        if MUTAGEN_AVAILABLE:
            try:
                audio = MutagenFile(file_path)
                if audio is not None and audio.info is not None:
                    return audio.info.length
            except Exception as e:
                logger.warning(f"Could not read duration for {file_path}: {e}")

        # Fallback: Estimate based on file size (assuming roughly 128kbps mp3)
        try:
            size_in_bytes = os.path.getsize(file_path)
            return size_in_bytes / 16000
        except:
            return 2.0

    async def _stream_audio_file(self, speaker: str, audio_path: str, text_content: str):
        """Reads an audio file and streams it to the WS in chunks."""
        if not audio_path or not os.path.exists(audio_path):
            logger.warning(f"Audio file not found: {audio_path}")
            await self.websocket.send_json({
                "type": "audio",
                "speaker": speaker,
                "text": text_content,
                "data": None,
                "isFinal": True
            })
            return

        chunk_size = 4096 * 4
        try:
            with open(audio_path, "rb") as f:
                while self.running:
                    data = f.read(chunk_size)
                    if not data:
                        break

                    encoded_data = base64.b64encode(data).decode('utf-8')

                    await self.websocket.send_json({
                        "type": "audio",
                        "speaker": speaker,
                        "data": encoded_data,
                        "text": ""
                    })

                    # Small delay to prevent flooding
                    await asyncio.sleep(0.02)
        except Exception as e:
            logger.error(f"Error streaming audio: {e}")

        # Send Final Text Packet
        await self.websocket.send_json({
            "type": "audio",
            "speaker": speaker,
            "data": None,
            "text": text_content,
            "isFinal": True
        })

    async def _send_scenario_update(self, folder: str, file_prefix: str, index: int, msg_type: str, data_key: str):
        """
        Helper to fetch a JSON dump file and send it via WebSocket.
        """
        file_path = f"scenario_dumps/{folder}/{file_prefix}{index}.json"

        if os.path.exists(file_path):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)

                await self.websocket.send_json({
                    "type": msg_type,
                    data_key: data
                })
            except Exception as e:
                logger.error(f"Failed to load/send update from {file_path}: {e}")

    async def run(self):
        self.running = True
        logger.info("▶ Starting Audio Simulation")

        await self.websocket.send_json({"type": "system", "message": "Initializing Audio Script..."})
        await asyncio.sleep(1)
        await self.websocket.send_json({"type": "system", "message": "Ready."})

        transcript_pool = []
        updates_to_process = [
            ("questions", "q", "questions", "questions"),
            ("diagnosis", "diag", "diagnosis", "diagnosis"),
        ]

        # Process all updates dynamically
        for folder, prefix, msg_type, key in updates_to_process:
            await self._send_scenario_update(folder, prefix, 0, msg_type, key)


        for item in self.script_data:
            if not self.running:
                break

            # --- 1. PREPARE AUDIO & TRANSCRIPT ---
            index = item.get("index")
            transcript = item.get("message", "")
            audio_path = item.get("audio_path", "")
            speaker = item.get("role", "SYSTEM")

            transcript_pool.append({
                "role": speaker,
                "message": transcript,
                "highlights": item.get("highlights")
            })

            self.tm.log(speaker, transcript)
            logger.info(f"[{index}] {speaker}: {transcript}")

            # --- 2. STREAM AUDIO ---
            audio_duration = self._get_audio_duration(audio_path)
            start_time = asyncio.get_event_loop().time()

            await self._stream_audio_file(speaker, audio_path, transcript)

            # --- 3. SMART SLEEP (Wait for audio to finish) ---
            end_time = asyncio.get_event_loop().time()
            elapsed_upload_time = end_time - start_time
            remaining_audio_time = audio_duration - elapsed_upload_time

            # Use buffer of 2.5s
            wait_time = max(remaining_audio_time, 0) + 2.5
            logger.info(f"WAIT TIME : {wait_time}")

            await asyncio.sleep(wait_time)

            print("After wait")

            # --- 4. SEND CHAT HISTORY ---
            await self.websocket.send_json({
                "type": "chat",
                "questions": transcript_pool
            })

            # --- 5. SEND CLINICAL UPDATES (Questions, Education, Diagnosis) ---
            if index % 2 == 0:
                update_index = max(0, int(index // 2))

                updates_to_process = [
                    ("questions", "q", "questions", "questions"),
                    ("education", "ed", "education", "data"),
                    ("diagnosis", "diag", "diagnosis", "diagnosis"),
                    ("analytics", "a", "analytics", "data")
                ]

                for folder, prefix, msg_type, key in updates_to_process:
                    await self._send_scenario_update(folder, prefix, update_index, msg_type, key)

            # --- 6. FINISH TURN ---
            await self.websocket.send_json({"type": "turn", "data": "finish cycle"})

        await asyncio.sleep(3)
        try:
            with open("scenario_dumps/checklist.json", "r", encoding="utf-8") as f:
                checklist = json.load(f)
            with open("scenario_dumps/report.json", "r", encoding="utf-8") as f:
                report = json.load(f)

            await self.websocket.send_json({"type": "checklist", "data": checklist})
            await self.websocket.send_json({"type": "report", "data": report})
        except:
            pass

        if self.running:
            await self.websocket.send_json({"type": "system", "message": "Session Complete."})
            await self.websocket.send_json({"type": "turn", "data": "end"})
            self.running = False
            logger.info("🛑 Audio Simulation Ended.")

    def stop(self):
        self.running = False
