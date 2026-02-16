import asyncio
import threading
import json
import audioop
import queue
import logging
import os
import time
import wave
import tempfile
from google.cloud import speech
from google import genai
from google.genai import types
from datetime import datetime
import traceback

# Local Imports
from medforce.agents import simulation_agents as agents
from medforce.managers import diagnosis as diagnosis_manager
from medforce.managers import questions as question_manager
from medforce.managers import education as education_manager
from medforce.infrastructure.gcs import GCSManager

logger = logging.getLogger("medforce-backend")
TRANSCRIPT_FILE = "simulation_transcript.txt"

# --- NEW AGENT CLASS ---

# --- LOGIC THREAD ---
class TranscriberLogicThread(threading.Thread):
    def __init__(self, patient_id, patient_info, dm, qm, main_loop, websocket, transcript_memory, run_status, audio_provider_callback):
        super().__init__()
        self.patient_id = patient_id
        self.patient_info = patient_info
        self.dm = dm
        self.qm = qm
        self.main_loop = main_loop
        self.websocket = websocket
        self.running = run_status
        self.daemon = True
        self.status = False
        self.transcript_memory = transcript_memory

        # Callback to get full audio from Engine
        self.get_full_audio = audio_provider_callback

        # Logic Components
        self.qc = agents.QuestionCheck()
        self.em = education_manager.EducationPoolManager()
        self.last_line_count = 0
        self.ready_event = threading.Event()
        self.gcs = GCSManager()
        
        # Chat State
        self.transcript_structure = []
        self.analytics_pool = {}
        self.check_count = 0
        self.consultation_start = time.perf_counter()


    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        # Initialize Agents
        self.transcriber_agent = agents.ConsultationTranscriber() # <--- NEW AGENT
        self.hepa_agent = agents.DiagnosisHepato()
        self.gen_agent = agents.DiagnosisGeneral()
        self.consolidate_agent = agents.DiagnosisConsolidate()
        self.merger_agent = agents.QuestionMerger()
        self.supervisor = agents.InterviewSupervisor()
        # self.transcript_parser = agents.TranscribeStructureAgent()
        self.q_enrich = agents.QuestionEnrichmentAgent()
        self.analytics_agent = agents.ConsultationAnalyticAgent()
        self.education_agent = agents.PatientEducationAgent()
        self.ranker = agents.QuestionRanker()

        self.checklist_agent = agents.ClinicalChecklistAgent()
        self.report_agent = agents.ComprehensiveReportAgent()
        self.q_dedup = agents.QuestionIntegrationGatekeeper()

        # Clear transcript file
        with open(TRANSCRIPT_FILE, "w", encoding="utf-8") as f:
            f.write("")

        logger.info(f"🩺 [Logic Thread] Monitoring {TRANSCRIPT_FILE}...")
        loop.run_until_complete(self.start_logic())

    async def start_logic(self):
        """Pre-analysis before allowing STT to process audio."""
        await self.run_initial_analysis()
        logger.info("🔔 [Logic Thread] Initial analysis complete. Signaling STT to start...")
        self.ready_event.set() 
        await self._logic_loop()

    async def run_initial_analysis(self):
        await self._push_to_ui({"type": "status", "data": {"end": False, "state": "initiate"}})
        q_list = [i.get('content','') for i in self.qm.questions]

        initial_instruction = "Initial file review and patient history analysis."
        h_coro = self.hepa_agent.get_hepa_diagnosis(initial_instruction, self.patient_info,q_list)
        g_coro = self.gen_agent.get_gen_diagnosis(initial_instruction, self.patient_info,q_list)
        
        hepa_res, gen_res = await asyncio.gather(h_coro, g_coro)
        consolidated = await self.consolidate_agent.consolidate_diagnosis(self.dm.diagnoses, hepa_res + gen_res)
        self.dm.diagnoses = consolidated

        await self._push_to_ui({
            "type": "diagnosis",
            "diagnosis": self.dm.get_diagnoses(),
            "source": "initial_analysis"
        })

        ranked_questions = await self.merger_agent.process_question("", consolidated, self.qm.get_questions_basic())
        self.qm.add_questions(ranked_questions)

        enriched_q = await self.q_enrich.enrich_questions(self.qm.get_questions_basic())
        self.qm.update_enriched_questions(enriched_q)
        
        await self._push_to_ui({"type": "questions", "questions": self.qm.questions, "source": "initial_analysis"})
        with open('status_update.json', 'w', encoding='utf-8') as f:
            json.dump({
                "is_finished": False,
                "question": self.qm.get_high_rank_question().get("content") if self.qm.get_high_rank_question() else None,
                "education":  ""
            }, f, indent=4)

    async def _push_to_ui(self, payload):
        if self.websocket and self.main_loop:
            try:
                asyncio.run_coroutine_threadsafe(self.websocket.send_json(payload), self.main_loop)
            except Exception as e:
                logger.error(f"UI Push Error: {e}")

    async def _process_full_audio(self):
        """
        Retrieves FULL piled-up audio from engine, converts to WAV, and sends to Gemini.
        Returns the structured transcript of the WHOLE conversation.
        """
        # 1. Get Raw Bytes (Full History)
        raw_audio_data = self.get_full_audio()
        if not raw_audio_data or len(raw_audio_data) < 1000: # Ignore tiny chunks
            return []

        temp_wav_name = None

        try:
            # 2. Create the file and WRITE to it, then CLOSE it immediately.
            # We use delete=False so it persists after closing, allowing us to read it again for upload.
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_wav:
                temp_wav_name = temp_wav.name
                
                # Use the wave library to write the frames to the file object
                with wave.open(temp_wav, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2) # 2 bytes = 16 bit
                    wf.setframerate(16000)
                    wf.writeframes(raw_audio_data)
            
            # AT THIS POINT: The 'with' block is done. 
            # Both 'wave' and 'tempfile' have closed their handles. 
            # The file exists on disk and is not locked.

            # 3. Send to Gemini Agent
            logger.info(f"🎧 [ConsultationTranscriber] Processing full audio: {len(raw_audio_data)} bytes...")
            
            # The upload function will open/read/close the file internally
            full_transcript = await self.transcriber_agent.transcribe_audio(temp_wav_name)
            
            logger.info(f"📝 [ConsultationTranscriber] Full Transcript Items: {len(full_transcript)}")
            return full_transcript

        except Exception as e:
            logger.error(f"Audio Processing Error: {e}")
            return []
            
        finally:
            # 4. Cleanup
            # Now safe to remove because we are outside the 'with' block 
            # and we are sure the file is closed.
            if temp_wav_name and os.path.exists(temp_wav_name):
                try:
                    os.remove(temp_wav_name)
                except Exception as cleanup_error:
                    logger.warning(f"⚠️ Could not remove temp file {temp_wav_name}: {cleanup_error}")

    async def _check_logic(self, raw_stt_text):
        """Main AI Reasoning Branch."""
        try:
            total_start = time.perf_counter()
            
            # --- NEW STEP: Process FULL Audio with Gemini ---
            # We fetch the high-quality diarized transcript for the ENTIRE audio history
            full_structured_transcript = await self._process_full_audio()
            
            # Since audio is piled up, the result represents the WHOLE conversation.
            # We OVERWRITE the old structure with the new, refined one.
            if full_structured_transcript:
                self.transcript_structure = full_structured_transcript

            # Convert structured transcript to text string for other agents
            full_clean_transcript_text = "\n".join([f"{item['role']}: {item['message']}" for item in self.transcript_structure])
            
            # Use Gemini text if available, else fallback to Google STT (Trigger) text
            text_for_analysis = raw_stt_text

            # 1. Parallel Tasks Execution
            parallel_start = time.perf_counter()
            q_list = [i.get('content','') for i in self.qm.questions]

            # Use text_for_analysis for agents
            edu_task = self.education_agent.generate_education(self.transcript_structure, self.em.pool)
            analytics_task = self.analytics_agent.analyze_consultation(self.transcript_structure)
            
            h_task = self.hepa_agent.get_hepa_diagnosis(text_for_analysis, self.patient_info, q_list)
            g_task = self.gen_agent.get_gen_diagnosis(text_for_analysis, self.patient_info, q_list)
            q_check_task = self.qc.check_question(text_for_analysis, self.qm.get_unanswered_questions())
            status_task = self.supervisor.check_completion(text_for_analysis, self.dm.diagnoses)

            (edu_res, analytics_res, h_res, g_res, answered_qs, status_res) = await asyncio.gather(
                edu_task, analytics_task, h_task, g_task, q_check_task, status_task
            )
            
            parallel_duration = time.perf_counter() - parallel_start
            logger.info(f"⏱️ [Parallel Tasks] Completed in {parallel_duration:.2f}s")


            with open('diagnosis_result.json', 'w', encoding='utf-8') as f:
                json.dump({
                    "general_diagnosis": g_res,
                    "hepato_diagnosis": h_res
                }, f, indent=4)

            # 2. Sequential Processing Start
            processing_start = time.perf_counter()

            # Update Chat (Full Replacement)
            await self._push_to_ui({"type": "chat", "data": self.transcript_structure})

            # Update Questions State
            for aq in answered_qs:
                self.qm.update_status(aq['qid'], "asked")
                self.qm.update_answer(aq['qid'], aq['answer'])
            

            consolidated_task = self.consolidate_agent.consolidate_diagnosis(self.dm.get_diagnoses_basic(), h_res + g_res)

            generated_questions = [i.get('followup_question') for i in h_res] + [i.get('followup_question') for i in g_res]
            filtered_q_task = self.q_dedup.filter_new_questions(generated_questions,[i.get('content','') for i in self.qm.questions])
            (consolidated, filtered_q) = await asyncio.gather(
                consolidated_task, filtered_q_task
            )
            # Consolidate Diagnosis
            with open('diagnosis_consolidate.json', 'w', encoding='utf-8') as f:
                json.dump(consolidated, f, indent=4)

            self.dm.diagnoses = consolidated
            self.qm.add_from_strings(filtered_q)

            ranked_questions = await self.ranker.rank_questions(text_for_analysis, self.qm.get_questions_basic())
            with open('output/ranked_questions.json', 'w', encoding='utf-8') as f:
                json.dump(ranked_questions, f, indent=4)
            
            self.qm.add_questions(ranked_questions.get('ranked',[]))
            
            enriched_q = await self.q_enrich.enrich_questions(self.qm.get_questions_basic())
            self.qm.update_enriched_questions(enriched_q)

            # Handle Education
            self.em.add_new_points(edu_res)
            next_ed = self.em.pick_and_mark_asked()

            self.analytics_pool = analytics_res

            # Final UI Push
            check_diagnosis = []
            diag_list = self.dm.get_diagnoses()
            for d in diag_list:
                check_diagnosis.append({
                    "headline" : d.get("headline"),
                    "rank" : d.get("rank"),
                    "severity" : d.get("severity")
                })
            print("Diagnosis rank :", check_diagnosis)
            await self._push_to_ui({"type": "diagnosis", "diagnosis": diag_list})
            await self._push_to_ui({"type": "questions", "questions": self.qm.questions})
            await self._push_to_ui({"type": "analytics", "data": analytics_res})
            await self._push_to_ui({"type": "status", "data": status_res})
            await self._push_to_ui({"type": "education", "data": self.em.pool})


            with open('master_question.json', 'w', encoding='utf-8') as f:
                json.dump(self.qm.questions, f, indent=4)
            
            # Status Update
            hr_q = self.qm.get_high_rank_question()

            if not self.status:
                self.status = status_res.get("end", False)

            if self.status:
                self.running = False
            
            if not hr_q:
                self.status = True

            logger.info(f"🤖 [AI Agent] Check status - count: {self.check_count}")

            if self.check_count < 15:
                update_object = {
                        "is_finished": self.status,
                        "question": hr_q.get("content") if hr_q else None,
                        "education": next_ed.get("content", "") if next_ed else ""
                    }
            else:
                self.status = True
                update_object = {
                        "is_finished": True,
                        "question": "",
                        "education": ""
                    }
            logger.info(f"Status Update : {self.status}")
            
            with open('status_update.json', 'w', encoding='utf-8') as f:
                json.dump(update_object, f, indent=4)

            processing_duration = time.perf_counter() - processing_start
            total_duration = time.perf_counter() - total_start

            logger.info(f"⏱️ [Processing] UI & Logic updates took {processing_duration:.2f}s")
            self.check_count += 1
        except Exception as e:
            logger.error(f"Check logic error: {e}")
            traceback.print_exc()

    def _upload_to_gcs(self, checklist_data, report_data):
        """Uploads all consultation data to GCS under patient_data/{patient_id}/"""
        prefix = f"patient_data/{self.patient_id}"
        try:
            self.gcs.write_file(f"{prefix}/transcript.json", self.transcript_structure)
            self.gcs.write_file(f"{prefix}/diagnosis.json", self.dm.get_diagnoses())
            self.gcs.write_file(f"{prefix}/questions.json", self.qm.questions)
            self.gcs.write_file(f"{prefix}/education.json", self.em.pool)
            self.gcs.write_file(f"{prefix}/analytics.json", self.analytics_pool)
            self.gcs.write_file(f"{prefix}/checklist.json", checklist_data)
            self.gcs.write_file(f"{prefix}/report.json", report_data)
            logger.info(f"Uploaded consultation data to GCS: {prefix}/")
        except Exception as e:
            logger.error(f"GCS upload error: {e}")

    async def _final_wrap(self):
        logger.info("🛑 [Finalization] Consultation complete. Generating final outputs...")
        check_result = await self.checklist_agent.generate_checklist(
            transcript = self.transcript_structure,
            diagnosis = self.dm.get_diagnoses(),
            question_list = self.qm.questions,
            analytics = self.analytics_pool,
            education_list = self.em.pool
        )

        await self._push_to_ui({"type": "checklist", "data": check_result})


        report_result = await self.report_agent.generate_report(
            transcript=self.transcript_structure,
            question_list=self.qm.questions,
            diagnosis_list=self.dm.get_diagnoses(),
            education_list=self.em.pool,
            analytics=self.analytics_pool
        )

        await self._push_to_ui({"type": "report", "data": report_result})

        self._upload_to_gcs(check_result, report_result)
        logger.info("🛑 [Finalization] Finished")

    async def _logic_loop(self):
        last_processed_text = ""
        while self.running:
            try:
                # Trigger logic based on Google STT activity (Voice Activity Detection)
                lines = self.transcript_memory
                full_text = " ".join(lines).strip()

                text_has_grown = len(full_text) > len(last_processed_text) + 20
                sentence_was_finalized = self.last_line_count < len(lines)
                
                logger.info(f"🤖 LOGIC LOOP] transcript lines: {len(full_text)},  last_line_count: {len(lines)}")
                
                

                if text_has_grown:
                    logger.info(f"🤖 [AI Agent] Analyzing updated transcript...")
                    
                    # Pass the rough Google STT text for logging/triggering
                    # _check_logic will pull the Full Audio from the Engine
                    await self._check_logic(full_text)
                    
                    self.last_line_count = len(lines)
                    last_processed_text = full_text
                    await asyncio.sleep(5) # Cooldown
                else:
                    await asyncio.sleep(1)
                    print("Waiting for new transcript lines...")

                if self.status:
                    logger.info(f"✅ [Logic Thread] Start Wrap Up.")

                    await self._final_wrap()
                    self.running = False
                    logger.info(f"✅ [Logic Thread] Consultation complete. Exiting.")
                    break
                
            except Exception as e:
                logger.error(f"❌ [Logic Thread] Error: {e}")
                traceback.print_exc()
                await asyncio.sleep(2)

    def trigger_manual_finish(self):
        """Called externally to force the consultation to end."""
        logger.info("🛑 [Logic Thread] Received MANUAL END signal from Frontend.")
        self.status = True

    def stop(self):
        self.running = False

class TranscriberEngine:
    def __init__(self, patient_id, patient_info, websocket, loop):
        self.websocket = websocket
        self.patient_id = patient_id
        self.patient_info = patient_info
        self.main_loop = loop
        self.running = True
        
        # Audio Config
        self.AUDIO_DELAY_SEC = 0.2
        self.SIMULATION_RATE = 24000
        self.TRANSCRIBER_RATE = 16000
        self.resample_state = None
        self.audio_queue = queue.Queue()       
        self.transcript_memory = []
        self.is_sentence_final = True

        # NEW: Audio Accumulation Buffer (Piled Up)
        self.raw_audio_buffer = bytearray()
        self.buffer_lock = threading.Lock()

        # Initialize Logic Thread
        self.logic_thread = TranscriberLogicThread(
            self.patient_id,
            self.patient_info,
            diagnosis_manager.DiagnosisManager(),
            question_manager.QuestionPoolManager([]),
            self.main_loop,
            self.websocket,
            self.transcript_memory,
            self.running,
            self.get_audio_buffer_copy # <--- Pass the callback
        )
        self.logic_thread.start()

    def add_audio(self, audio_bytes):
        """Receives raw bytes from server.py WebSocket."""
        try:
            # Resample from 24k (Simulation) to 16k (Google STT / Agent)
            converted, self.resample_state = audioop.ratecv(
                audio_bytes, 2, 1, self.SIMULATION_RATE, self.TRANSCRIBER_RATE, self.resample_state
            )
            
            # 1. Put into STT Queue (for Google Streaming Trigger)
            release_time = time.time() + self.AUDIO_DELAY_SEC
            self.audio_queue.put((release_time, converted))

            # 2. Accumulate in Buffer (Piled Up)
            with self.buffer_lock:
                self.raw_audio_buffer.extend(converted)

        except Exception as e:
            logger.error(f"Resampling Error: {e}")

    def get_audio_buffer_copy(self):
        """
        Callback used by Logic Thread to retrieve the FULL piled-up audio.
        Does NOT clear the buffer.
        """
        with self.buffer_lock:
            if len(self.raw_audio_buffer) == 0:
                return None
            # Return a COPY of the bytes
            return bytes(self.raw_audio_buffer)

    def stt_loop(self):
        """Google STT Streaming (Used as VAD/Trigger)."""
        logger.info("⏳ [Engine] Waiting for initial analysis...")
        self.logic_thread.ready_event.wait()

        client = speech.SpeechClient()
        config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=self.TRANSCRIBER_RATE,
            language_code="en-US",
            enable_automatic_punctuation=True,
            model="latest_long",
        )
        streaming_config = speech.StreamingRecognitionConfig(config=config, interim_results=True)

        def request_generator():
            while self.running:
                try:
                    item = self.audio_queue.get(timeout=1.0)
                    if item is None: return
                    
                    release_time, chunk = item
                    now = time.time()
                    if now < release_time:
                        time.sleep(release_time - now)
                    
                    yield speech.StreamingRecognizeRequest(audio_content=chunk)
                except queue.Empty:
                    continue

        logger.info(f"🎙️ [STT Loop] Google Stream started...")
        retries_count = 0
        while self.running:
            try:
                responses = client.streaming_recognize(streaming_config, request_generator())
                
                for response in responses:
                    if not self.running: break
                    if not response.results: continue
                    
                    result = response.results[0]
                    transcript = result.alternatives[0].transcript

                    if not result.is_final:
                        if self.is_sentence_final:
                            self.transcript_memory.append(transcript)
                            self.is_sentence_final = False
                        else:
                            if self.transcript_memory:
                                self.transcript_memory[-1] = transcript
                    else:
                        if self.is_sentence_final:
                            self.transcript_memory.append(transcript)
                        else:
                            self.transcript_memory[-1] = transcript

                        self.is_sentence_final = True
                        print(f"\n✅ [FINAL SENTENCE]: {transcript}")

                        # Push live STT transcript to UI immediately
                        try:
                            live_payload = {
                                "type": "stt_live",
                                "transcript": list(self.transcript_memory)
                            }
                            asyncio.run_coroutine_threadsafe(
                                self.websocket.send_json(live_payload),
                                self.main_loop
                            )
                        except Exception:
                            pass

            except Exception as e:
                if self.running:
                    logger.warning(f"🎙️ [STT Restarting] {retries_count}: {e}")
                    retries_count += 1
                    if retries_count >= 20:
                        self.running = False
                        break
                time.sleep(0.1)

    def finish_consultation(self):
        """Passes the manual finish signal to the logic thread."""
        if self.logic_thread and self.logic_thread.is_alive():
            self.logic_thread.trigger_manual_finish()
            self.running = False

    def stop(self):
        self.running = False
        self.logic_thread.stop()
        self.audio_queue.put(None)