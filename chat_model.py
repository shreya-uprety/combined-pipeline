import warnings
from google.genai.types import GenerateContentConfig
# Suppress deprecation warning for google.generativeai (agent-2.9 legacy code)
warnings.filterwarnings('ignore', category=FutureWarning, module='google.generativeai')
import google.generativeai as genai
import time
import json
import asyncio
import os
import logging
import threading
from dotenv import load_dotenv
import side_agent
import canvas_ops
load_dotenv()

logger = logging.getLogger("chat-model")

# Lazy initialization - configure only when needed
_genai_configured = False
_cached_model = None

def _ensure_genai_configured():
    global _genai_configured
    if not _genai_configured:
        genai.configure(api_key=os.getenv("GOOGLE_API_KEY"))
        _genai_configured = True

def _get_model():
    """Get or create cached model instance for faster responses"""
    global _cached_model
    _ensure_genai_configured()
    if _cached_model is None:
        with open("system_prompts/chat_model_system.md", "r", encoding="utf-8") as f:
            system_prompt = f.read()
        _cached_model = genai.GenerativeModel(
            "gemini-2.0-flash",  # Use faster model
            system_instruction=system_prompt
        )
    return _cached_model

MODEL = "gemini-2.0-flash"  # Faster model

# Topic to board item ID mapping
TOPIC_FOCUS_MAP = {
    # Encounters
    "encounter": "encounter-track-1",
    "visit": "encounter-track-1",
    "consultation": "encounter-track-1",
    "appointment": "encounter-track-1",
    "history": "encounter-track-1",

    # Labs
    "lab": "dashboard-item-lab-table",
    "alt": "dashboard-item-lab-table",
    "ast": "dashboard-item-lab-table",
    "bilirubin": "dashboard-item-lab-table",
    "albumin": "dashboard-item-lab-table",
    "inr": "dashboard-item-lab-table",
    "creatinine": "dashboard-item-lab-table",
    "hemoglobin": "dashboard-item-lab-table",
    "platelet": "dashboard-item-lab-table",
    "sodium": "dashboard-item-lab-table",
    "lft": "dashboard-item-lab-table",
    "liver function": "dashboard-item-lab-table",
    "blood test": "dashboard-item-lab-table",

    # Lab chart
    "chart": "dashboard-item-lab-chart",
    "graph": "dashboard-item-lab-chart",
    "trend": "dashboard-item-lab-chart",

    # Medications
    "medication": "medication-track-1",
    "drug": "medication-track-1",
    "medicine": "medication-track-1",
    "prescription": "medication-track-1",
    "lactulose": "medication-track-1",
    "furosemide": "medication-track-1",
    "propranolol": "medication-track-1",
    "sertraline": "medication-track-1",

    # Diagnosis
    "diagnosis": "differential-diagnosis",
    "differential": "differential-diagnosis",
    "dili": "differential-diagnosis",
    "liver injury": "differential-diagnosis",

    # Risk
    "risk": "risk-track-1",
    "safety": "risk-track-1",

    # Adverse events
    "adverse": "adverse-event-analytics",
    "causality": "adverse-event-analytics",
    "rucam": "adverse-event-analytics",

    # Key events
    "event": "key-events-track-1",
    "timeline": "key-events-track-1",
    "key event": "key-events-track-1",

    # Referral
    "referral": "referral-doctor-info",
    "referral letter": "referral-doctor-info",
    "referred": "referral-doctor-info",
    "referrer": "referral-doctor-info",
    "gp letter": "referral-doctor-info",
    "doctor letter": "referral-doctor-info",
    "referring doctor": "referral-doctor-info",

    # Reports / Raw EHR data
    "report": "raw-encounter-image-1",
    "reports": "raw-encounter-image-1",
    "clinical notes": "raw-encounter-image-1",
    "raw data": "raw-encounter-image-1",
    "encounter report": "raw-encounter-image-1",
    "radiology": "raw-lab-image-radiology-1",
    "radiology report": "raw-lab-image-radiology-1",
    "imaging": "raw-lab-image-radiology-1",
    "imaging report": "raw-lab-image-radiology-1",
    "x-ray": "raw-lab-image-radiology-1",
    "xray": "raw-lab-image-radiology-1",
    "ultrasound": "raw-lab-image-radiology-1",
    "chest x-ray": "raw-lab-image-radiology-1",
    "lab report": "raw-lab-image-1",
    "blood test report": "raw-lab-image-1",
    "pathology": "raw-lab-image-1",
    "pathology report": "raw-lab-image-1",
    "scan": "raw-lab-image-radiology-1",
    "ct scan": "raw-lab-image-radiology-1",
    "mri": "raw-lab-image-radiology-1",

    # Patient profile / overview
    "medical situation": "sidebar-1",
    "overview": "sidebar-1",
    "patient": "sidebar-1",
    "profile": "sidebar-1",

    # Clinical timeline
    "clinical timeline": "key-events-track-1",

    # Physical exam / encounters
    "physical exam": "encounter-track-1",
    "exam finding": "encounter-track-1",

    # EASL
    "easl": "iframe-item-easl-interface",
    "guideline": "iframe-item-easl-interface",
    "guidelines": "iframe-item-easl-interface",

    # Patient Chat
    "patient chat": "monitoring-patient-chat",
    "patient message": "monitoring-patient-chat",
}


def detect_focus_topic(query: str) -> str:
    """Detect which board item to focus based on query keywords.

    Uses priority tiers: specific clinical terms (labs, meds, diagnoses) are checked
    first so they win over generic terms like 'patient' which appear in most queries.
    """
    query_lower = query.lower()

    # Generic keywords that should only match if no specific topic matches
    generic_keywords = {"patient", "overview", "profile", "medical situation", "report", "reports"}

    # First pass: check specific (non-generic) keywords, longest first
    for keyword in sorted(TOPIC_FOCUS_MAP.keys(), key=len, reverse=True):
        if keyword in generic_keywords:
            continue
        if keyword in query_lower:
            return TOPIC_FOCUS_MAP[keyword]

    # Second pass: check generic keywords only if nothing specific matched
    for keyword in sorted(generic_keywords, key=len, reverse=True):
        if keyword in query_lower:
            return TOPIC_FOCUS_MAP[keyword]

    return None


async def get_answer(query: str, conversation_text: str = '', context: str = '', relevant_item_id: str = ''):
    """Get answer from Gemini - uses cached model and pre-loaded context"""
    if not context:
        # Only fetch if not provided (should be provided by chat_agent)
        context_raw = canvas_ops.get_board_items(quiet=True)
        context = json.dumps(context_raw, indent=2)

    # If we know which board section is most relevant, extract it and prioritize it
    relevant_hint = ""
    if relevant_item_id:
        try:
            context_data = json.loads(context) if isinstance(context, str) else context
            for item in context_data:
                if isinstance(item, dict) and item.get('id', '') == relevant_item_id:
                    relevant_hint = f"\n\nMOST RELEVANT SECTION for this query (board item '{relevant_item_id}'):\n{json.dumps(item, indent=2)[:8000]}\n\nUse the above section as the PRIMARY source for your answer.\n"
                    break
        except Exception:
            pass

    # Keep prompt concise for faster response
    prompt = f"""Answer the user query using the patient data from the board context.
Be helpful and informative. Use 1-3 sentences.

Query: {query}{relevant_hint}

Context (Board Data):
{context[:30000]}"""  # Increased context size to include sidebar data

    model = _get_model()
    response = model.generate_content(prompt)
    return response.text.strip()


async def chat_agent(chat_history: list[dict]) -> str:
    """
    Chat Agent - Optimized for speed.
    Takes chat history and returns agent response.
    """
    start_time = time.time()
    query = chat_history[-1].get('content', '').strip().strip('"').strip('\u201c').strip('\u201d').strip("'")
    logger.info(f"⏱️ chat_agent: START - Query: {query[:50]}...")
    
    # Fast tool routing (keyword-based, no API call)
    tool_res = side_agent.parse_tool(query)
    logger.info(f"⏱️ chat_agent: parse_tool in {time.time()-start_time:.2f}s")
    print("Tools use:", tool_res)

    # Get context once (uses cache)
    t0 = time.time()
    context_raw = canvas_ops.get_board_items(quiet=False)  # Show logs for debugging
    context = json.dumps(context_raw, indent=2)
    logger.info(f"⏱️ chat_agent: get_board_items in {time.time()-t0:.2f}s (context: {len(context)} chars)")

    tool = tool_res.get('tool')
    
    if tool == "get_easl_answer":
        result = await side_agent.trigger_easl(query)
        return "Query sent to EASL guidelines."
    
    elif tool == "generate_task":
        # Use generate_task_obj (no background processing/raw EHR posting)
        task_obj = await side_agent.generate_task_obj(query)
        todo_response = await canvas_ops.create_todo(task_obj)
        todo_id = todo_response.get('id')
        # Auto-focus on the newly created TODO
        if todo_id:
            try:
                await asyncio.sleep(0.5)
                await canvas_ops.focus_item(todo_id)
            except Exception as e:
                logger.error(f"Failed to auto-focus on TODO: {e}")
            # Start background animation for automatic status updates (like voice/side agents)
            if 'todos' in task_obj:
                asyncio.create_task(side_agent._animate_todo_tasks(todo_id, task_obj['todos']))
        return f"Task created: {task_obj.get('title', 'Task')}"
    
    elif tool == "navigate_canvas":
        try:
            result = await side_agent.resolve_object_id(query, context)
            # resolve_object_id returns {"object_id": str, "focus_result": dict} and already focuses
            if result and isinstance(result, dict):
                object_id_str = result.get("object_id", "")
            elif result and isinstance(result, str):
                object_id_str = result
            else:
                return "Could not identify the section to focus on."

            if not object_id_str:
                return "Could not identify the section to focus on."

            # Return friendly message based on object_id
            friendly_names = {
                "encounter-track-1": "encounters timeline",
                "dashboard-item-lab-table": "lab results table",
                "dashboard-item-lab-chart": "lab results chart",
                "lab-track-1": "lab timeline",
                "medication-track-1": "medication timeline",
                "differential-diagnosis": "differential diagnosis",
                "adverse-event-analytics": "adverse events analytics",
                "risk-track-1": "risk events timeline",
                "key-events-track-1": "key events timeline",
                "sidebar-1": "patient sidebar",
                "referral-doctor-info": "referral letter",
                "referral-letter-image": "referral letter",
                "raw-encounter-image-1": "encounter reports",
                "raw-encounter-image-2": "encounter reports",
                "raw-encounter-image-3": "encounter reports",
                "raw-lab-image-radiology-1": "radiology report",
                "raw-lab-image-radiology-2": "radiology report",
                "raw-lab-image-1": "lab report",
                "raw-lab-image-2": "lab report",
                "raw-lab-image-3": "lab report",
                "iframe-item-easl-interface": "EASL guidelines",
                "monitoring-patient-chat": "patient chat",
            }
            friendly_name = friendly_names.get(object_id_str, "the requested section")
            return f"Focused on {friendly_name}."
        except Exception as e:
            return f"Navigation failed: {str(e)}"
    
    elif tool == "create_schedule":
        result = await side_agent.create_schedule(query, context)
        return "Schedule created successfully."
    
    elif tool == "create_doctor_note":
        # Always use AI to generate proper clinical note content from the doctor's request
        import re
        content = query.strip().strip('"').strip('\u201c').strip('\u201d').strip("'")
        # Check for embedded quoted text (e.g., 'create a note "patient is stable"')
        # Only match quotes that are INSIDE a larger command, not wrapping the whole query
        quoted = re.search(r'\w\s+["\u201c](.+?)["\u201d]', content)
        if quoted:
            content = quoted.group(1)
        else:
            # Use AI to generate proper note content from the command + patient context
            try:
                _ensure_genai_configured()
                note_model = genai.GenerativeModel("gemini-2.0-flash")
                note_prompt = f"""Generate professional clinical notes based on the doctor's request and patient data.

Doctor's request: "{query}"

Patient data (board context):
{context[:20000]}

Rules:
- Write the note content ONLY (no metadata, no JSON, no markdown code blocks)
- Use professional clinical documentation style
- Include relevant findings, assessments, and plans from the patient data
- Be comprehensive but concise
- Use appropriate medical terminology
- Format with clear sections if the request calls for detailed notes
- If the request is simple (e.g., "patient refused medication"), write a brief note
- If the request asks for comprehensive/detailed notes, write thorough clinical documentation
- NEVER include the original command text - only the generated note content

Output ONLY the note content:"""
                note_response = note_model.generate_content(note_prompt)
                content = note_response.text.strip()
                # Clean up any markdown code block wrappers
                if content.startswith('```'):
                    content = re.sub(r'^```\w*\n?', '', content)
                    content = re.sub(r'\n?```$', '', content)
                logger.info(f"📝 AI-generated note content ({len(content)} chars)")
            except Exception as e:
                logger.error(f"Note generation failed, using query as content: {e}")
                # Fallback: strip command prefixes so raw command text isn't the note
                for prefix in [
                    'create comprehensive nursing notes ',
                    'create a doctor note ',
                    'create a nurse note ',
                    'create a clinical note ',
                    'create a note ',
                    'create note ',
                    'add a note ',
                    'add note ',
                    'write a note ',
                    'write note ',
                    'draft ',
                ]:
                    if content.lower().startswith(prefix):
                        content = content[len(prefix):]
                        break
        result = await canvas_ops.create_doctor_note(content)
        return "Note created successfully."

    elif tool == "send_message_to_patient":
        # Convert doctor's intent into a proper patient-facing message using AI
        import re
        message = query.strip().strip('"').strip('\u201c').strip('\u201d').strip("'")
        # Check for embedded quoted text (e.g., 'send message "please take your meds"')
        # Only match quotes that are INSIDE a larger command, not wrapping the whole query
        quoted = re.search(r'\w\s+["\u201c](.+?)["\u201d]', message)
        if quoted:
            message = quoted.group(1)
        else:
            # Use AI to convert doctor's command into a patient-friendly message
            try:
                _ensure_genai_configured()
                rewrite_model = genai.GenerativeModel("gemini-2.0-flash")
                rewrite_prompt = f"""Convert this doctor's instruction into a direct, professional message to the patient.
The doctor said: "{query}"

Patient context (use this to personalize the message):
{context[:10000]}

Rules:
- Write ONLY the message text that the patient will see
- Address the patient directly (use "you/your")
- Be professional, warm, and clear
- Use the patient context to include specific, relevant details (medications, conditions, etc.)
- Do NOT include any prefixes like "Dear patient" or sign-offs
- Do NOT include quotes around the message
- Keep it concise (2-4 sentences)

Example inputs and outputs:
- "ask the patient about his chest pain" → "How has your chest pain been? Could you describe any recent changes or episodes?"
- "tell the patient to take their medication" → "Please remember to take your medication as prescribed."
- "draft a compassionate message about his condition" → "We understand this is a difficult time. Your recent labs show improvement, and we want to support your recovery. Please continue taking your medications as prescribed and avoid alcohol."

Output ONLY the message:"""
                rewrite_response = rewrite_model.generate_content(rewrite_prompt)
                message = rewrite_response.text.strip().strip('"').strip("'")
                logger.info(f"💬 Rewrote message: {query[:50]}... → {message[:50]}...")
            except Exception as e:
                logger.error(f"Message rewrite failed, falling back to prefix strip: {e}")
                # Fallback: strip common prefixes
                for prefix in [
                    'send a message to the patient saying ',
                    'send a message to the patient ',
                    'send message to the patient saying ',
                    'send message to the patient ',
                    'message the patient saying ',
                    'message the patient ',
                    'tell the patient to ',
                    'tell the patient ',
                    'text the patient ',
                    'ask the patient about ',
                    'ask the patient ',
                    'ask patient ',
                    'chat with patient ',
                ]:
                    if message.lower().startswith(prefix):
                        message = message[len(prefix):]
                        break
        result = await canvas_ops.send_patient_message(message)
        # Focus on patient chat after sending
        try:
            await canvas_ops.focus_item("monitoring-patient-chat")
        except Exception:
            pass
        return "Message sent to patient."

    elif tool == "send_notification":
        result = await canvas_ops.create_notification({"message": query})
        return "Notification sent."
    
    elif tool == "create_lab_results":
        # Parse lab values from query using AI
        lab_data = await side_agent.parse_lab_values(query, context)
        if lab_data:
            result = await canvas_ops.create_lab(lab_data)
            return "Lab results posted to board."
        return "Could not parse lab values from the query. Please provide values like 'ALT 110, AST 150'."
    
    elif tool == "generate_diagnosis":
        result = await side_agent.create_dili_diagnosis()
        return "DILI diagnosis generated."
    
    elif tool == "generate_patient_report":
        result = await side_agent.create_patient_report()
        return "Patient report generated."
    
    elif tool == "generate_legal_report":
        result = await side_agent.create_legal_doc()
        return "Legal report generated."

    elif tool == "generate_ai_diagnosis":
        result = await side_agent.create_ai_diagnosis()
        return "AI diagnosis generated."

    elif tool == "generate_ai_treatment_plan":
        result = await side_agent.create_ai_treatment_plan()
        return "AI treatment plan generated."
    
    else:
        # General Q&A - pass context directly (no redundant fetch)
        conversation_text = ""
        if len(chat_history) > 1:
            conversation_text = "\n".join([
                f"{msg.get('role')}: {msg.get('content')}"
                for msg in chat_history[:-1]
            ])

        # Detect relevant section to prioritize in the answer and auto-focus
        focus_object_id = detect_focus_topic(query)

        # Get the answer with relevant section hint
        answer = await get_answer(query, conversation_text, context, relevant_item_id=focus_object_id or '')

        # Auto-focus on relevant section
        if focus_object_id:
            try:
                await canvas_ops.focus_item(focus_object_id)
                logger.info(f"🎯 Auto-focused on: {focus_object_id}")
            except Exception as e:
                logger.warning(f"⚠️ Auto-focus failed: {e}")

        return answer
