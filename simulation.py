# --- simulation.py ---
import asyncio
import threading
import copy
import json
import logging
import datetime
import contextlib
import os
import time
from fastapi import WebSocket
import question_manager
# Local Imports
import agents
from utils import fetch_gcs_text_internal

logger = logging.getLogger("medforce-backend")

# --- LOAD STATIC DATA ---
try:
    # Attempt to load the base nurse persona
    with open("patient_profile/nurse.md", "r", encoding="utf-8") as f:
        NURSE_PROMPT_BASE = f.read()
except Exception as e:
    logger.error(f"Failed to load nurse.md: {e}")
    NURSE_PROMPT_BASE = "You are a professional triage nurse. Be empathetic and concise."

class TranscriptManager:
    """Thread-safe manager for the simulation history."""
    def __init__(self):
        self.history = []
        self._lock = threading.Lock()
    
    def log(self, speaker, text):
        with self._lock:
            entry = {
                "timestamp": datetime.datetime.now().strftime("%H:%M:%S"), 
                "speaker": speaker, 
                "text": text.strip()
            }
            self.history.append(entry)
            # logger.info(f"📝 {speaker}: {text[:50]}...")
    
    def get_history(self):
        with self._lock:
            return copy.deepcopy(self.history)

class SimulationManager:
    def __init__(self, websocket: WebSocket, patient_id: str, gender: str = "Male"):
        self.websocket = websocket
        self.patient_id = patient_id
        
        # 1. Fetch Patient Persona from GCS
        self.PATIENT_PROMPT = fetch_gcs_text_internal(patient_id, "patient_system.md")
        self.PATIENT_INFO = fetch_gcs_text_internal(patient_id, "patient_info.md")

        # 2. Initialize Voice Agents
        # Nurse uses Aoede (Professional Female)
        self.nurse = agents.TextBridgeAgent("NURSE", NURSE_PROMPT_BASE, "Aoede")
        
        # Patient uses gender-appropriate voices
        if gender.lower() == "male":
            self.patient = agents.TextBridgeAgent("PATIENT", self.PATIENT_PROMPT, "Puck")
        else:
            self.patient = agents.TextBridgeAgent("PATIENT", self.PATIENT_PROMPT, "Laomedeia")
        
        self.tm = TranscriptManager()
        self.cycle = 0
        self.running = False
        self.last_q = []
        self.last_question = ""
        self.timeout_status = 0

    def fetch_clinical_instruction(self):
        """
        Reads direction from status_update.json created by the ws_transcriber.
        Includes a retry mechanism to avoid file-lock race conditions.
        """
        qm = question_manager.QuestionPoolManager([])
        print("QM QUESTIONS:", len(qm.questions))
        path = 'status_update.json'
        if not os.path.exists(path):
            return "Continue the medical interview and explore the patient's symptoms.", False

        for _ in range(3): # Try up to 3 times if file is being written to
            try:
                with open(path, 'r', encoding='utf-8') as file:
                    data = json.load(file)
                
                is_finished = data.get("is_finished", False)
                if is_finished:
                    print("CLINICAL ASSESSMENT MARKED AS FINISHED.")
                    return "The clinical assessment is complete. Thank the patient and end the session.", True
                # next_q = data.get("question")
                rank_target = 1
                while True:
                    next_q_obj = qm.get_high_rank_question(target_rank=rank_target)
                    if next_q_obj:
                        next_q = next_q_obj.get("content")
                        if next_q not in self.last_q:
                            self.last_q.append(next_q)
                            print(f"NEXT Q FETCHED: {next_q}")
                            return f"Clinical Goal: Ask about '{next_q}'. Make it sound natural.", False
                        else:
                            print("DUPLICATE Q SKIPPED", next_q)
                            rank_target += 1
                            if rank_target > len(qm.questions):
                                print("NO NEW Q AVAILABLE")
                                return "The clinical assessment is complete. Thank the patient and end the session.", True
                            
                    else:
                        print("NO NEXT Q FETCHED")
                        return "The clinical assessment is complete. Thank the patient and end the session.", True

                

            except (json.JSONDecodeError, IOError):
                time.sleep(0.1) # Wait 100ms for transcriber to finish writing
                continue
        
        return "Continue the interview.", False
    
    async def fetch_status_update(self):
        """
        Reads direction from status_update.json.
        Async version: Uses non-blocking sleep.
        Returns: (question, is_finished, education)
        """
        path = 'status_update.json'
        closing_msg = "The clinical assessment is complete. Thank the patient and end the session."
        
        if not os.path.exists(path):
            return "Continue the medical interview and explore the patient's symptoms.", False, None

        for i in range(5): 
            try:
                # Note: Standard open() is blocking, but usually fast enough for small JSON files.
                # If you need strict non-blocking file I/O, you would need the 'aiofiles' library.
                with open(path, 'r', encoding='utf-8') as file:
                    data = json.load(file)
                
                is_finished = data.get("c", False)
                next_q = data.get("question")
                education = data.get("education")

                # 1. Check finished status
                if is_finished:
                    print("CLINICAL ASSESSMENT MARKED AS FINISHED.")
                    return closing_msg, True, education

                # 2. Check empty question
                if not next_q:
                    print("NO QUESTION FOUND - ENDING.")
                    return closing_msg, True, education

                # 3. Check for Duplicate (Wait 1 sec if same)
                if next_q == self.last_question:
                    # print(f"DUPLICATE QUESTION ({next_q}). Waiting for update... ({i+1}/5)")
                    # CRITICAL CHANGE: Use await asyncio.sleep instead of time.sleep
                    await asyncio.sleep(0.5) 
                    continue
                
                # 4. Success - New Question
                self.last_question = next_q
                print(f"NEXT Q FETCHED: {next_q}")
                
                formatted_q = f"Clinical Goal: Ask about '{next_q}'. Make it sound natural. Do not repeat asked question."
                return formatted_q, False, education

            except (json.JSONDecodeError, IOError):
                # Wait briefly for file write to complete
                await asyncio.sleep(0.1) 
                continue
        
        # Fallback if timed out
        print("TIMED OUT WAITING FOR NEW QUESTION.")
        # self.timeout_status += 1
        # if self.timeout_status >=5:
        #     return closing_msg, True, None
        return "Continue the interview for other questions, improvise with your own question. Do not repeat asked question.", False, None

    async def fetch_scenario(self):
        """
        Reads direction from status_update.json.
        Async version: Uses non-blocking sleep.
        Returns: (question, is_finished, education)
        """
        path = 'data/scenario.json'
        closing_msg = "The clinical assessment is complete. Thank the patient and end the session."
        
        if not os.path.exists(path):
            return "Continue the medical interview and explore the patient's symptoms.", False, None

        for i in range(5): 
            try:
                # Note: Standard open() is blocking, but usually fast enough for small JSON files.
                # If you need strict non-blocking file I/O, you would need the 'aiofiles' library.
                with open(path, 'r', encoding='utf-8') as file:
                    datas = json.load(file)
                
                for data in datas:
                
                    is_finished = data.get("is_finished", False)
                    next_q = data.get("question")
                    education = data.get("education")

                    # 1. Check finished status
                    if is_finished:
                        print("CLINICAL ASSESSMENT MARKED AS FINISHED.")
                        return closing_msg, True, education

                    # 2. Check empty question
                    if not next_q:
                        print("NO QUESTION FOUND - ENDING.")
                        return closing_msg, True, education

                    # 3. Check for Duplicate (Wait 1 sec if same)
                    if next_q in self.last_q:
                        # print(f"DUPLICATE QUESTION ({next_q}). Waiting for update... ({i+1}/5)")
                        # CRITICAL CHANGE: Use await asyncio.sleep instead of time.sleep
                        await asyncio.sleep(0.5) 
                        continue
                    
                    # 4. Success - New Question
                    self.last_q.append(next_q)
                    # self.last_question = next_q
                    print(f"NEXT Q FETCHED: {next_q}")
                    
                    formatted_q = f"Clinical Goal: Ask about '{next_q}'. Make it sound natural. Do not repeat asked question."
                    return formatted_q, False, education

            except (json.JSONDecodeError, IOError):
                # Wait briefly for file write to complete
                await asyncio.sleep(0.1) 
                continue
        
        # Fallback if timed out
        print("TIMED OUT WAITING FOR NEW QUESTION.")
        # self.timeout_status += 1
        # if self.timeout_status >=5:
        #     return closing_msg, True, None
        return "Continue the interview for other questions, improvise with your own question. Do not repeat asked question.", False, None

    async def run(self):
        self.running = True
        await self.websocket.send_json({"type": "system", "message": "Initializing Agents..."})

        # --- ASYNC CONTEXT MANAGER FOR GEMINI LIVE CONNECTIONS ---
        async with contextlib.AsyncExitStack() as stack:
            # Establish Real-time Voice Connections
            nurse_session = await stack.enter_async_context(self.nurse.get_connection_context())
            patient_session = await stack.enter_async_context(self.patient.get_connection_context())
            
            self.nurse.set_session(nurse_session)
            self.patient.set_session(patient_session)
            
            await self.websocket.send_json({"type": "system", "message": "Voice sessions connected."})
            await asyncio.sleep(2)

            # Initial State
            next_instruction = "Introduce yourself and ask the patient about their primary concern today."
            patient_last_words = "None (Beginning of interview)"
            interview_completed_clinically = False
            while self.running:
                # logger.info(f"--- Simulation Cycle {self.cycle} ---")

                # 1. NURSE TURN
                # We combine the base prompt with specific clinical instructions
                nurse_input = (
                    f"CONTEXT: {self.PATIENT_INFO}\n"
                    f"PATIENT LAST SAID: '{patient_last_words}'\n"
                    f"SUPERVISOR INSTRUCTION: {next_instruction}\n\n"
                    "TASK: Speak to the patient now. Be professional and brief."
                )
                
                nurse_text, _ = await self.nurse.speak_and_stream(nurse_input, self.websocket)
                if not nurse_text: 
                    nurse_text = "I see. Can you tell me more about that?"
                
                self.tm.log("NURSE", nurse_text)
                await asyncio.sleep(0.5)

                # 2. PATIENT TURN
                # The patient agent reacts to what the nurse just said
                patient_text, _ = await self.patient.speak_and_stream(nurse_text, self.websocket)
                
                if patient_text:
                    patient_last_words = patient_text
                else:
                    patient_last_words = "(The patient remains silent)"

                self.tm.log("PATIENT", patient_last_words)
                
                # Signal UI that a full exchange happened
                await self.websocket.send_json({"type": "turn", "data": "finish cycle"})

                # 3. CLINICAL INTELLIGENCE SYNC
                # We wait a moment for the ws_transcriber.py to process the audio and update the JSON
                await asyncio.sleep(1.5)

                # Fetch the next move from the JSON file
                next_instruction_message, interview_completed_clinically, education_message = await self.fetch_status_update()
                
                # logger.info(f"📁 Supervisor Direction: {next_instruction}")
                if education_message:
                    next_instruction = f"Educate patient about this topic in short: {education_message}. Then continue the interview with this question: {next_instruction_message}" 
                else:
                    next_instruction = next_instruction_message

                logger.info("SIMULATION : Next Instruction: " + next_instruction)


                await self.websocket.send_json({
                    "type": "system", 
                    "message": f"Clinical Instruction: {next_instruction}"
                })

                # If the clinical agent says we are done, we do one last goodbye turn
                if interview_completed_clinically:
                    logger.info("🏁 Clinical Supervisor marked session as COMPLETE.")
                    final_nurse_input = "The clinical supervisor says we have enough info. Thank the patient and say goodbye."
                    await self.nurse.speak_and_stream(final_nurse_input, self.websocket)
                    break
                
                self.cycle += 1

                # Check if the user closed the browser tab
                if self.websocket.client_state.name == "DISCONNECTED": 
                    break

            # Send final stop signals
            await self.websocket.send_json({"type": "turn", "data": "end"})
            self.running = False
            logger.info("🛑 Simulation Loop Terminated.")

    async def run2(self):
        self.running = True
        await self.websocket.send_json({"type": "system", "message": "Initializing Agents..."})

        # --- ASYNC CONTEXT MANAGER FOR GEMINI LIVE CONNECTIONS ---
        async with contextlib.AsyncExitStack() as stack:
            # Establish Real-time Voice Connections
            nurse_session = await stack.enter_async_context(self.nurse.get_connection_context())
            patient_session = await stack.enter_async_context(self.patient.get_connection_context())
            
            self.nurse.set_session(nurse_session)
            self.patient.set_session(patient_session)
            
            await self.websocket.send_json({"type": "system", "message": "Voice sessions connected."})
            await asyncio.sleep(2)

            # Initial State
            next_instruction = "Introduce yourself and ask the patient about their primary concern today."
            patient_last_words = "None (Beginning of interview)"
            interview_completed_clinically = False
            while self.running:
                # logger.info(f"--- Simulation Cycle {self.cycle} ---")

                # 1. NURSE TURN
                # We combine the base prompt with specific clinical instructions
                nurse_input = (
                    f"CONTEXT: {self.PATIENT_INFO}\n"
                    f"PATIENT LAST SAID: '{patient_last_words}'\n"
                    f"SUPERVISOR INSTRUCTION: {next_instruction}\n\n"
                    "TASK: Speak to the patient now. Be professional and brief."
                )
                
                nurse_text, _ = await self.nurse.speak_and_stream(nurse_input, self.websocket)
                if not nurse_text: 
                    nurse_text = "I see. Can you tell me more about that?"
                
                self.tm.log("NURSE", nurse_text)
                await asyncio.sleep(0.5)

                # 2. PATIENT TURN
                # The patient agent reacts to what the nurse just said
                patient_text, _ = await self.patient.speak_and_stream(nurse_text, self.websocket)
                
                if patient_text:
                    patient_last_words = patient_text
                else:
                    patient_last_words = "(The patient remains silent)"

                self.tm.log("PATIENT", patient_last_words)
                
                # Signal UI that a full exchange happened
                await self.websocket.send_json({"type": "turn", "data": "finish cycle"})


                await self.websocket.send_json({"type": "diagnosis", "diagnosis": None})
                await self.websocket.send_json({"type": "questions", "questions": None})
                await self.websocket.send_json({"type": "analytics", "data": None})
                await self.websocket.send_json({"type": "status", "data": None})
                await self.websocket.send_json({"type": "education", "data": None})
                # 3. CLINICAL INTELLIGENCE SYNC
                # We wait a moment for the ws_transcriber.py to process the audio and update the JSON
                await asyncio.sleep(1.5)

                # Fetch the next move from the JSON file
                next_instruction_message, interview_completed_clinically, education_message = await self.fetch_scenario()
                
                # logger.info(f"📁 Supervisor Direction: {next_instruction}")
                if education_message:
                    next_instruction = f"Educate patient about this topic in short: {education_message}. Then continue the interview with this question: {next_instruction_message}" 
                else:
                    next_instruction = next_instruction_message

                logger.info("SIMULATION : Next Instruction: " + next_instruction)


                await self.websocket.send_json({
                    "type": "system", 
                    "message": f"Clinical Instruction: {next_instruction}"
                })

                # If the clinical agent says we are done, we do one last goodbye turn
                if interview_completed_clinically:
                    logger.info("🏁 Clinical Supervisor marked session as COMPLETE.")
                    final_nurse_input = "The clinical supervisor says we have enough info. Thank the patient and say goodbye."
                    await self.nurse.speak_and_stream(final_nurse_input, self.websocket)
                    break
                
                self.cycle += 1

                # Check if the user closed the browser tab
                if self.websocket.client_state.name == "DISCONNECTED": 
                    break

            # Send final stop signals
            await self.websocket.send_json({"type": "turn", "data": "end"})
            self.running = False
            logger.info("🛑 Simulation Loop Terminated.")


    def stop(self):
        """External call to stop the loop."""
        self.running = False


