"""
Side Agent - Combined version with animated TODOs, error handling, and async background processing
Provides tool routing, canvas operations, and report generation for the clinic-sim-pipeline
"""

from google.genai.types import GenerateContentConfig
import google.generativeai as genai
import time
import json
import asyncio
import os
import random
import threading
import httpx
from dotenv import load_dotenv
import requests
import aiohttp
from medforce import settings as config
from medforce.infrastructure import canvas_ops
load_dotenv()
from medforce.agents import helper_model
from medforce.managers.patient_state import patient_manager

# Configuration
BASE_URL = patient_manager.get_base_url()
print("#### side_agent.py CANVAS_URL:", BASE_URL)
print("#### Current Patient ID:", patient_manager.get_patient_id())

# Lazy initialization - configure only when needed
_genai_configured = False
_cached_models = {}

def _ensure_genai_configured():
    global _genai_configured
    if not _genai_configured:
        genai.configure(api_key=os.getenv("GOOGLE_API_KEY"))
        _genai_configured = True

def _get_model(system_prompt_file: str = None):
    """Get or create cached model instance"""
    global _cached_models
    _ensure_genai_configured()
    
    cache_key = system_prompt_file or "default"
    if cache_key not in _cached_models:
        system_prompt = ""
        if system_prompt_file:
            try:
                with open(system_prompt_file, "r", encoding="utf-8") as f:
                    system_prompt = f.read()
            except:
                pass
        _cached_models[cache_key] = genai.GenerativeModel(
            "gemini-2.0-flash",  # Faster model
            system_instruction=system_prompt if system_prompt else None
        )
    return _cached_models[cache_key]

MODEL = "gemini-2.0-flash"  # Faster model

# ============================================================================
# TOOL PARSING - Route user queries to appropriate tools
# ============================================================================

def parse_tool(query):
    """Parse user query and route to appropriate tool - with fast keyword matching first"""
    q_lower = query.lower().strip()

    import re

    # STEP 1: Detect QUESTIONS - these should almost always be general Q&A, not tool calls
    # Questions start with interrogative words or are phrased as requests for information
    is_question = bool(re.match(
        r'^(what|which|who|when|where|why|how|describe|explain|list|tell me|give me|walk me|summarize|'
        r'can you tell|could you|is there|are there|does|do |did |has |have |was |were )',
        q_lower
    )) or q_lower.endswith('?')

    # STEP 2: If it's a question, only match explicit tool triggers (not ambiguous keywords)
    # This prevents "List medications and compliance issues" from triggering legal report

    # EASL - questions about guidelines ARE tool calls (send to EASL)
    if any(kw in q_lower for kw in ['easl', 'clinical guideline']):
        return {"query": query, "tool": "get_easl_answer"}
    # Only trigger guideline/recommendation if it's clearly asking EASL (not just mentioning them)
    if not is_question and any(kw in q_lower for kw in ['guideline', 'recommendation']):
        return {"query": query, "tool": "get_easl_answer"}

    # Navigation - explicit navigation commands
    if any(kw in q_lower for kw in ['navigate', 'go to', 'show me', 'focus on', 'zoom to']):
        return {"query": query, "tool": "navigate_canvas"}

    # Task creation - explicit create/add commands
    # Also match "create a ... task" patterns (e.g. "create a lab follow up task")
    if any(kw in q_lower for kw in ['create task', 'add task', 'todo', 'to-do', 'reminder']):
        return {"query": query, "tool": "generate_task"}
    if re.search(r'(create|add)\s+.{0,30}\btask\b', q_lower):
        return {"query": query, "tool": "generate_task"}

    # send_message_to_patient MUST be checked before schedule/notification
    # because message content may contain words like "follow up", "alert", etc.
    if any(kw in q_lower for kw in ['message patient', 'message the patient', 'message to the patient', 'send a message', 'send message to patient', 'tell the patient', 'text the patient', 'chat with patient', 'ask the patient', 'ask patient', 'draft a message', 'draft message', 'send it to']):
        return {"query": query, "tool": "send_message_to_patient"}
    # Detect "ask/tell {name} about/to/..." patterns (e.g. "ask arthur about his chest pain")
    # Excludes "tell me" and "ask me" which are questions, not messages
    if re.match(r'(ask|tell)\s+(?!me\b|us\b)\w+\s+(about|to|if|how|when|whether|that)\b', q_lower):
        return {"query": query, "tool": "send_message_to_patient"}
    # Detect "draft/send/write ... message" patterns (e.g. "draft a compassionate message to Arthur")
    if re.search(r'(draft|send|write).{0,50}message', q_lower):
        return {"query": query, "tool": "send_message_to_patient"}

    # Doctor notes - explicit create/add commands
    if any(kw in q_lower for kw in ['doctor note', 'add note', 'add a note', 'create note', 'create a note', 'write note', 'write a note', 'clinical note', 'nurse note', 'nursing note', 'admission note', 'admission assessment']):
        return {"query": query, "tool": "create_doctor_note"}
    # Detect "create/write/draft ... notes" patterns (e.g. "create comprehensive nursing notes")
    if re.search(r'(create|write|draft|add).{0,30}note', q_lower):
        return {"query": query, "tool": "create_doctor_note"}

    # If it's a question, skip action-oriented tools and go to general Q&A
    # This prevents "What follow-up should Arthur have?" from creating a schedule
    # or "List medications and compliance issues" from generating a legal report
    if is_question:
        return {"query": query, "tool": "general"}

    # STEP 3: Action-oriented commands (only reached if NOT a question)

    # Schedule - requires explicit scheduling intent
    if any(kw in q_lower for kw in ['schedule', 'book appointment', 'create appointment']):
        return {"query": query, "tool": "create_schedule"}
    # "follow-up" / "follow up" only triggers schedule when it's a command, not a question
    if any(kw in q_lower for kw in ['follow-up', 'follow up']) and any(kw in q_lower for kw in ['schedule', 'book', 'create', 'arrange', 'set up']):
        return {"query": query, "tool": "create_schedule"}

    # Notifications - requires explicit send/notify intent
    if any(kw in q_lower for kw in ['send notification', 'send alert', 'notify the', 'notify care']):
        return {"query": query, "tool": "send_notification"}

    # ONLY create labs when user explicitly says "add" or "create" or "post"
    if any(kw in q_lower for kw in ['add lab', 'create lab', 'post lab', 'put lab']):
        return {"query": query, "tool": "create_lab_results"}

    # AI tools - require explicit "generate" or "create" or "ai" prefix
    if any(kw in q_lower for kw in ['ai diagnosis', 'ai diagnostic', 'generate ai diagnosis', 'clinical diagnosis', 'medforce diagnosis']):
        return {"query": query, "tool": "generate_ai_diagnosis"}
    if any(kw in q_lower for kw in ['ai treatment', 'ai plan', 'generate treatment', 'medforce treatment', 'ai treatment plan']):
        return {"query": query, "tool": "generate_ai_treatment_plan"}

    # DILI diagnosis - only explicit generation commands
    if any(kw in q_lower for kw in ['generate diagnosis', 'create diagnosis', 'dili diagnosis', 'liver injury diagnosis']):
        return {"query": query, "tool": "generate_diagnosis"}

    # Reports - only explicit generation commands
    if any(kw in q_lower for kw in ['generate patient report', 'create patient report', 'patient report', 'summary report', 'generate report']):
        return {"query": query, "tool": "generate_patient_report"}

    # Legal - only explicit legal report generation (NOT just mentioning "compliance" or "legal")
    if any(kw in q_lower for kw in ['generate legal', 'create legal', 'legal report', 'compliance report']):
        return {"query": query, "tool": "generate_legal_report"}

    # Default to general Q&A for most queries (no tool needed)
    return {"query": query, "tool": "general"}


# ============================================================================
# NAVIGATION - Focus on board items
# ============================================================================

def _get_item_description(item_id: str, component_type: str, item: dict) -> str:
    """Generate a human-readable description for a board item based on its ID and metadata."""
    item_id_lower = item_id.lower()

    # Referral items
    if 'referral-doctor-info' in item_id_lower or 'referral-info' in item_id_lower:
        provider = item.get('provider', '')
        return f"Referral letter{f' from {provider}' if provider else ''}"
    if 'referral-letter' in item_id_lower:
        return "Referral letter image / scanned referral document"

    # Raw EHR data items — disambiguate by ID pattern
    if item_id_lower.startswith('raw-lab-image-radiology') or 'imaging-report' in item_id_lower:
        return "Radiology/imaging report (X-ray, ultrasound, CT, MRI)"
    if item_id_lower.startswith('raw-lab-image'):
        return "Lab report (original blood test / laboratory report document)"
    if item_id_lower.startswith('raw-encounter-image') or item_id_lower.startswith('raw-encounter-report'):
        return "Encounter report (original clinical notes document)"
    if item_id_lower.startswith('raw-ice-lab-data'):
        return "Raw lab data from encounter"

    # Single encounter documents
    if 'single-encounter' in item_id_lower:
        enc = item.get('encounter', {})
        meta = enc.get('meta', {})
        visit_type = meta.get('visit_type', '')
        date = meta.get('date_time', '')[:10] if meta.get('date_time') else ''
        provider_name = meta.get('provider', {}).get('name', '') if isinstance(meta.get('provider'), dict) else ''
        parts = [p for p in [visit_type, date, provider_name] if p]
        return f"Encounter document ({', '.join(parts)})" if parts else "Encounter document"

    # Named components
    desc_map = {
        'adverse-event-analytics': 'Adverse event analysis (RUCAM / CTCAE causality assessment)',
        'differential-diagnosis': 'Differential diagnosis panel',
        'encounter-track': 'Encounter timeline',
        'lab-track': 'Lab results timeline',
        'medication-track': 'Medication timeline',
        'key-events-track': 'Key clinical events timeline',
        'risk-track': 'Risk score timeline',
        'dashboard-item-lab-table': 'Lab results table',
        'dashboard-item-lab-chart': 'Lab results chart / graph',
        'sidebar-1': 'Patient sidebar (demographics, problem list, medications)',
        'iframe-item-easl': 'EASL clinical guidelines interface',
        'chronomed': 'ChronoMed DILI assessment timeline',
    }
    for pattern, desc in desc_map.items():
        if pattern in item_id_lower:
            return desc

    # Fallback for componentType-based identification
    comp_map = {
        'RawClinicalNote': 'Clinical note / referral document',
        'Sidebar': 'Patient profile sidebar',
        'DifferentialDiagnosis': 'Differential diagnosis',
        'AdverseEventAnalytics': 'Adverse event analysis',
    }
    if component_type in comp_map:
        return comp_map[component_type]

    return ""


async def resolve_object_id(query: str, context: str = ""):
    """Resolve user query to a board object ID and focus on it"""
    # Get board items using canvas_ops (has proper error handling and cache fallback)
    try:
        data = canvas_ops.get_board_items(quiet=True)  # Use quiet mode to reduce log noise

        board_items = []
        for item in data:
            if not isinstance(item, dict):
                continue  # Skip invalid items

            item_type = item.get('item_type', item.get('type', ''))
            item_id = item.get('object_id', item.get('id', ''))
            component_type = item.get('componentType', '')

            # Build a human-readable description to help the parser identify items
            description = _get_item_description(item_id, component_type, item)

            if item_type == 'content':
                item_content = item.get('content', {})
                entry = {
                    "object_id": item_id,
                    "item_type": item_type,
                    "title": item_content.get('title', ''),
                    "component": item_content.get('component', component_type),
                }
            else:
                entry = {
                    "object_id": item_id,
                    "componentType": component_type,
                    "title": item.get('title', ''),
                }

            if description:
                entry["description"] = description

            # Include extra identifying fields when available
            if item.get('visitType'):
                entry["visitType"] = item['visitType']
            if item.get('dataSource'):
                entry["dataSource"] = item['dataSource']
            if item.get('studyType'):
                entry["studyType"] = item['studyType']
            if item.get('date'):
                entry["date"] = item['date']
            if item.get('provider'):
                entry["provider"] = item['provider']

            board_items.append(entry)
    except Exception as e:
        print(f"❌ Error processing board items: {e}")
        board_items = []

    RESPONSE_SCHEMA = {
        "type": "OBJECT",
        "properties": {
            "objectId": {"type": "STRING", "description": "Resolved object ID."}
        },
        "required": ["objectId"]
    }

    model = _get_model("system_prompts/objectid_parser.md")
    prompt = f"User query : '{query}'\n\nBoard items: {json.dumps(board_items[:30])}"  # Limit items for speed
    
    response = model.generate_content(
        prompt,
        generation_config=genai.GenerationConfig(
            response_mime_type="application/json",
            response_schema=RESPONSE_SCHEMA,
            temperature=0.1,
        )
    )

    result = json.loads(response.text)
    object_id = result.get('objectId')
    print(f"🎯 ObjectID Resolved: {object_id}")
    
    # Focus on the item
    focus_result = await canvas_ops.focus_item(object_id)
    return {"object_id": object_id, "focus_result": focus_result}


# ============================================================================
# EASL - Send clinical questions to EASL with animated TODO workflow
# ============================================================================

async def prepare_easl_query(question: str):
    """
    Prepare an EASL query by generating context and refined question.
    Returns the prepared data for frontend to use - does NOT send to board.
    
    Frontend can then:
    1. Display the prepared context/question to user
    2. Call board's EASL iframe API directly
    3. Or call /api/canvas/send-to-easl to let backend handle it
    """
    print("📝 Preparing EASL query (no send)...")
    
    try:
        # Load prompts
        with open("system_prompts/context_agent.md", "r", encoding="utf-8") as f:
            SYSTEM_PROMPT_CONTEXT = f.read()
        with open("system_prompts/question_gen.md", "r", encoding="utf-8") as f:
            SYSTEM_PROMPT_QUESTION = f.read()
        
        # Load EHR data
        ehr_data = await helper_model.load_ehr()
        
        # Generate clinical context
        model = genai.GenerativeModel(MODEL, system_instruction=SYSTEM_PROMPT_CONTEXT)
        prompt = f"Please generate context for: Question: {question}\n\nRaw data: {ehr_data}"
        resp = model.generate_content(prompt)
        context_result = resp.text.replace("```markdown", " ").replace("```", "")
        
        # Generate refined question
        model = genai.GenerativeModel(MODEL, system_instruction=SYSTEM_PROMPT_QUESTION)
        prompt = f"Please generate proper question: Question: {question}\n\nRaw data: {ehr_data}"
        resp = model.generate_content(prompt)
        refined_question = resp.text.replace("```markdown", " ").replace("```", "")
        
        # Build the full query for EASL
        full_query = f"Context: {context_result}\n\nQuestion: {refined_question}"
        
        # Get patient info for frontend
        patient_id = patient_manager.get_patient_id()
        base_url = patient_manager.get_base_url()
        
        return {
            "status": "prepared",
            "original_question": question,
            "generated_context": context_result,
            "refined_question": refined_question,
            "full_query": full_query,
            "patient_id": patient_id,
            "board_easl_endpoint": f"{base_url}/api/board/{patient_id}/easl",
            "board_easl_payload": {
                "patientId": patient_id,
                "query": full_query
            },
            "usage_instructions": {
                "option_1": "Frontend can POST to board_easl_endpoint with board_easl_payload",
                "option_2": "Frontend can call /api/canvas/send-to-easl to let backend send (with TODO animation)",
                "option_3": "Frontend can display context/question to user for review before sending"
            }
        }
        
    except Exception as e:
        print(f"❌ Error preparing EASL query: {e}")
        return {
            "status": "error",
            "message": str(e),
            "original_question": question
        }


async def trigger_easl(question):
    """Send clinical question to EASL - directly sends to iframe without TODO animation"""
    print("🚀 Starting EASL workflow (direct send, no TODO)...")
    
    try:
        # Load prompts
        with open("system_prompts/context_agent.md", "r", encoding="utf-8") as f:
            SYSTEM_PROMPT_CONTEXT = f.read()
        with open("system_prompts/question_gen.md", "r", encoding="utf-8") as f:
            SYSTEM_PROMPT_QUESTION = f.read()
        
        # Load EHR data
        ehr_data = await helper_model.load_ehr()
        
        # Generate clinical context
        print("📝 Generating clinical context...")
        model = genai.GenerativeModel(MODEL, system_instruction=SYSTEM_PROMPT_CONTEXT)
        prompt = f"Please generate context for: Question: {question}\n\nRaw data: {ehr_data}"
        resp = model.generate_content(prompt)
        context_result = resp.text.replace("```markdown", " ").replace("```", "")
        
        with open(f"{config.output_dir}/context.md", "w", encoding="utf-8") as f:
            f.write(context_result)
        
        # Generate refined question
        print("📝 Generating refined question...")
        model = genai.GenerativeModel(MODEL, system_instruction=SYSTEM_PROMPT_QUESTION)
        prompt = f"Please generate proper question: Question: {question}\n\nRaw data: {ehr_data}"
        resp = model.generate_content(prompt)
        q_gen_result = resp.text.replace("```markdown", " ").replace("```", "")
        
        with open(f"{config.output_dir}/question.md", "w", encoding="utf-8") as f:
            f.write(q_gen_result)
        
        # Send to EASL iframe directly
        full_question = f"Context: {context_result}\n\nQuestion: {q_gen_result}"
        print("📤 Sending query to EASL iframe...")
        easl_result = await canvas_ops.initiate_easl_iframe(full_question)
        
        # Focus on EASL iframe
        await canvas_ops.focus_item("iframe-item-easl-interface")
        
        print(f"✅ EASL query sent successfully")
        
        return {
            "status": "success",
            "message": "EASL query sent - check the EASL panel on the board",
            "question": question,
            "easl_result": easl_result
        }
        
    except Exception as e:
        print(f"❌ EASL workflow error: {e}")
        return {
            "status": "error",
            "message": str(e),
            "question": question
        }


# NOTE: _animate_easl_todo is deprecated - EASL now sends directly without TODO animation
# Keeping the function commented out for reference if needed later
# async def _animate_easl_todo(todo_id: str, question: str):
#     """Background task to animate TODO and process EASL query with 2-second delays"""
#     ... (deprecated)



async def _do_nothing_placeholder():
    """Placeholder to maintain code structure"""
    pass


# ============================================================================
# TASK WORKFLOW - Generate and execute tasks with background processing
# ============================================================================

async def generate_task_workflow(query: str):
    """Generate a task workflow and process it in background"""
    with open("system_prompts/task_generator.md", "r", encoding="utf-8") as f:
        SYSTEM_PROMPT = f.read()

    RESPONSE_SCHEMA = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "description": {"type": "string"},
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "text": {"type": "string"},
                        "status": {"type": "string", "enum": ["pending", "executing", "finished"]},
                        "agent": {"type": "string"},
                        "subTodos": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "text": {"type": "string"},
                                    "status": {"type": "string", "enum": ["pending", "executing", "finished"]}
                                },
                                "required": ["text", "status"]
                            }
                        }
                    },
                    "required": ["id", "text", "status", "agent", "subTodos"]
                }
            }
        },
        "required": ["title", "description", "todos"]
    }

    ehr_data = await load_ehr()
    prompt = f"User request:\n{query}\n\nPatient data: {ehr_data}\n\nGenerate the task workflow JSON."

    model = genai.GenerativeModel(MODEL, system_instruction=SYSTEM_PROMPT)
    resp = model.generate_content(
        prompt,
        generation_config=genai.GenerationConfig(
            response_mime_type="application/json",
            response_schema=RESPONSE_SCHEMA,
            temperature=0.7,
        )
    )
    
    todo_json = json.loads(resp.text)
    with open(f"{config.output_dir}/generate_task_workflow.json", "w", encoding="utf-8") as f:
        json.dump(todo_json, f, ensure_ascii=False, indent=4)

    # Create TODO on board
    task_res = await canvas_ops.create_todo(todo_json)
    
    # Start background processing (non-blocking)
    asyncio.create_task(_process_task_workflow(todo_json, task_res))
    
    return {
        "status": "processing",
        "message": "Task workflow created - processing in background",
        "todo_id": task_res.get('id'),
        "workflow": todo_json
    }


async def _process_task_workflow(todo_json: dict, todo_obj: dict):
    """Background task to animate and process the workflow"""
    try:
        todo_id = todo_obj.get("id")
        
        for task_idx, task in enumerate(todo_json.get('todos', [])):
            task_id = task.get('id')
            
            # Mark task as executing (include task_id and empty index)
            await canvas_ops.update_todo({
                "id": todo_id, 
                "task_id": task_id,
                "index": "",
                "status": "executing"
            })
            
            # Process subtodos (include task_id with string index)
            for subtodo_idx, subtodo in enumerate(task.get('subTodos', [])):
                await asyncio.sleep(random.uniform(0.3, 0.8))
                await canvas_ops.update_todo({
                    "id": todo_id, 
                    "task_id": task_id,
                    "index": str(subtodo_idx),
                    "status": "finished"
                })
            
            # Mark task as finished
            await asyncio.sleep(random.uniform(0.2, 0.5))
            await canvas_ops.update_todo({
                "id": todo_id, 
                "task_id": task_id,
                "index": "",
                "status": "finished"
            })
        
        # Generate response and post to board
        response_data = await generate_response(todo_json)
        agent_result = {
            'content': response_data.get('answer', ''),
            'title': todo_json.get('title', 'Analysis Result').replace("To Do", "Result"),
            'zone': "raw-ehr-data-zone"
        }
        
        await canvas_ops.create_result(agent_result)
        print(f"✅ Task workflow completed: {todo_id}")
        
    except Exception as e:
        print(f"❌ Task workflow error: {e}")


async def generate_todo(query: str):
    """Generate a simple TODO (without background processing)"""
    with open("system_prompts/task_generator.md", "r", encoding="utf-8") as f:
        SYSTEM_PROMPT = f.read()

    ehr_data = await load_ehr()
    prompt = f"User request:\n{query}\n\nPatient data: {ehr_data}\n\nGenerate the task workflow JSON."

    model = genai.GenerativeModel(MODEL, system_instruction=SYSTEM_PROMPT)
    resp = model.generate_content(
        prompt,
        generation_config=genai.GenerationConfig(
            response_mime_type="application/json",
            temperature=0.7,
        )
    )
    
    result = json.loads(resp.text)
    with open(f"{config.output_dir}/generate_todo.json", "w", encoding="utf-8") as f:
        json.dump(result, f, indent=4)

    response = await canvas_ops.create_todo(result)
    
    # Start background animation for automatic status updates
    todo_id = response.get('id')
    if todo_id and 'todos' in result:
        print(f"🎬 Starting TODO animation for {todo_id} with {len(result['todos'])} tasks")
        asyncio.create_task(_animate_todo_tasks(todo_id, result['todos']))
    else:
        print(f"⚠️ TODO animation skipped - todo_id: {todo_id}, has todos: {'todos' in result}")
    
    return response


async def _animate_todo_tasks(todo_id: str, tasks: list):
    """Background task to automatically update TODO task statuses with 2-second delays
    Uses numeric index (0, 1, 2...) for tasks and "parent.child" format for subtodos
    """
    try:
        print(f"🎭 Animation started for TODO {todo_id}")
        for task_idx, task in enumerate(tasks):
            task_id = task.get('id', f'task-{task_idx}')
            
            # Step 1: Mark parent task as executing (include task_id and empty index for parent)
            print(f"⏳ Task {task_idx} ({task_id}): pending → executing")
            await asyncio.sleep(2)
            await canvas_ops.update_todo({
                "id": todo_id, 
                "task_id": task_id,
                "index": "",  # Empty string for parent task
                "status": "executing"
            })
            
            # Step 2: Animate all subtodos if they exist
            subtodos = task.get('subTodos', [])
            if subtodos:
                print(f"  📋 Processing {len(subtodos)} subtodos for task {task_idx}")
                for subtodo_idx, subtodo in enumerate(subtodos):
                    # Mark subtodo as executing (use string index)
                    print(f"    ⏳ Subtodo {task_idx}.{subtodo_idx}: pending → executing")
                    await asyncio.sleep(2)
                    await canvas_ops.update_todo({
                        "id": todo_id, 
                        "task_id": task_id,
                        "index": str(subtodo_idx),  # String index for subtodo
                        "status": "executing"
                    })
                    
                    # Mark subtodo as finished
                    print(f"    ✅ Subtodo {task_idx}.{subtodo_idx}: executing → finished")
                    await asyncio.sleep(2)
                    await canvas_ops.update_todo({
                        "id": todo_id, 
                        "task_id": task_id,
                        "index": str(subtodo_idx),  # String index for subtodo
                        "status": "finished"
                    })
            else:
                # No subtodos, just wait
                await asyncio.sleep(2)
            
            # Step 3: Mark parent task as finished
            print(f"✅ Task {task_idx} ({task_id}): executing → finished")
            await canvas_ops.update_todo({
                "id": todo_id, 
                "task_id": task_id,
                "index": "",  # Empty string for parent task
                "status": "finished"
            })
            await asyncio.sleep(1)
        
        print(f"✅ TODO {todo_id} animation completed")
    except Exception as e:
        print(f"⚠️ TODO animation error: {e}")


# ============================================================================
# EHR DATA LOADING
# ============================================================================

async def load_ehr():
    """Load EHR data from board items"""
    print("📊 Loading EHR data...")
    try:
        data = canvas_ops.get_board_items()
        with open(f"{config.output_dir}/ehr_data.json", "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
        return data
    except Exception as e:
        print(f"❌ Error loading EHR: {e}")
        return []


async def generate_response(todo_obj):
    """Generate clinical response for a TODO"""
    with open("system_prompts/clinical_agent.md", "r", encoding="utf-8") as f:
        SYSTEM_PROMPT = f.read()
    
    model = genai.GenerativeModel(MODEL, system_instruction=SYSTEM_PROMPT)
    ehr_data = await load_ehr()
    
    prompt = f"""Please execute this todo: {todo_obj}

This is patient encounter data: {ehr_data}"""

    resp = model.generate_content(prompt)
    
    with open(f"{config.output_dir}/generate_response.md", "w", encoding="utf-8") as f:
        f.write(resp.text)

    return {"answer": resp.text.replace("```markdown", " ").replace("```", "")}


# ============================================================================
# REPORT GENERATION - DILI, Patient, Legal with animated workflows
# ============================================================================

async def create_dili_diagnosis():
    """Generate DILI diagnosis and post to board directly"""
    print("🔬 Generating DILI diagnosis...")
    
    try:
        # Load EHR data and generate diagnosis
        ehr_data = await load_ehr()
        result = await generate_dili_diagnosis()
        print("✅ DILI diagnosis generated successfully")
        
        # Post to board
        board_response = canvas_ops.create_diagnosis(result)
        print(f"✅ DILI diagnosis posted to board")
        
        return {"generated": result, "board_response": board_response}
        
    except Exception as e:
        print(f"❌ DILI diagnosis error: {e}")
        return {"generated": None, "board_response": {"status": "error", "message": str(e)}}


async def generate_dili_diagnosis():
    """Generate DILI diagnosis JSON"""
    with open("system_prompts/dili_diagnosis_prompt.md", "r", encoding="utf-8") as f:
        SYSTEM_PROMPT = f.read()

    model = genai.GenerativeModel(MODEL, system_instruction=SYSTEM_PROMPT)
    ehr_data = await load_ehr()

    prompt = f"Generate DILI diagnosis based on patient data.\n\nPatient data: {ehr_data}"

    gen_config = genai.GenerationConfig(
        response_mime_type="application/json",
        temperature=0.7,
    )
    # Run synchronous generate_content in thread to avoid blocking event loop
    resp = await asyncio.to_thread(model.generate_content, prompt, generation_config=gen_config)

    result = json.loads(resp.text)
    # AI sometimes returns a list instead of dict - handle gracefully
    if isinstance(result, list):
        print(f"⚠️ DILI diagnosis returned list ({len(result)} items), using first element")
        result = result[0] if result else {}
    with open(f"{config.output_dir}/generate_dili_diagnosis.json", "w", encoding="utf-8") as f:
        json.dump(result, f, indent=4)

    return result


async def create_patient_report():
    """Generate patient report and post to board directly"""
    print("📄 Generating patient report...")
    
    try:
        # Generate the report
        result = await generate_patient_report()
        print("✅ Patient report generated successfully")
        
        # Post to board
        board_response = await canvas_ops.create_report(result)
        print(f"✅ Patient report posted to board")
        
        return {"generated": result, "board_response": board_response}
        
    except Exception as e:
        print(f"❌ Patient report error: {e}")
        return {"generated": None, "board_response": {"status": "error", "message": str(e)}}
        return {"generated": None, "board_response": {"status": "error", "message": str(e)}}


async def generate_patient_report():
    """Generate patient report JSON"""
    with open("system_prompts/patient_report_prompt.md", "r", encoding="utf-8") as f:
        SYSTEM_PROMPT = f.read()

    model = genai.GenerativeModel(MODEL, system_instruction=SYSTEM_PROMPT)
    ehr_data = await load_ehr()

    prompt = f"Generate patient report based on patient data.\n\nPatient data: {ehr_data}"

    gen_config = genai.GenerationConfig(
        response_mime_type="application/json",
        temperature=0.7,
    )
    # Run synchronous generate_content in thread to avoid blocking event loop
    resp = await asyncio.to_thread(model.generate_content, prompt, generation_config=gen_config)

    result = json.loads(resp.text)
    # AI sometimes returns a list instead of dict - handle gracefully
    if isinstance(result, list):
        print(f"⚠️ Patient report returned list ({len(result)} items), using first element")
        result = result[0] if result else {}
    with open(f"{config.output_dir}/generate_patient_report.json", "w", encoding="utf-8") as f:
        json.dump(result, f, indent=4)

    return result


async def create_legal_doc():
    """Generate legal compliance report and post to board directly"""
    print("⚖️ Generating legal compliance report...")
    
    try:
        # Generate the report
        result = await generate_legal_report()
        print("✅ Legal report generated successfully")
        
        # Post to board using dedicated legal-compliance endpoint
        board_response = await canvas_ops.create_legal(result)
        print(f"✅ Legal report posted to board")
        
        return {"generated": result, "board_response": board_response}
        
    except Exception as e:
        print(f"❌ Legal report error: {e}")
        return {"generated": None, "board_response": {"status": "error", "message": str(e)}}


async def generate_legal_report():
    """Generate legal compliance report JSON"""
    with open("system_prompts/legal_report_prompt.md", "r", encoding="utf-8") as f:
        SYSTEM_PROMPT = f.read()

    model = genai.GenerativeModel(MODEL, system_instruction=SYSTEM_PROMPT)
    ehr_data = await load_ehr()

    prompt = f"Generate a legal compliance report based on patient data.\n\nPatient data: {ehr_data}"

    gen_config = genai.GenerationConfig(
        response_mime_type="application/json",
        temperature=0.7,
    )
    # Run synchronous generate_content in thread to avoid blocking event loop
    resp = await asyncio.to_thread(model.generate_content, prompt, generation_config=gen_config)

    result = json.loads(resp.text)
    # AI sometimes returns a list instead of dict - handle gracefully
    if isinstance(result, list):
        print(f"⚠️ Legal report returned list ({len(result)} items), using first element")
        result = result[0] if result else {}
    with open(f"{config.output_dir}/generate_legal_report.json", "w", encoding="utf-8") as f:
        json.dump(result, f, indent=4)

    return result


# ============================================================================
# AI DIAGNOSIS & AI TREATMENT PLAN
# ============================================================================

async def generate_ai_diagnosis():
    """Generate AI clinical diagnosis JSON"""
    with open("system_prompts/ai_diagnosis_prompt.md", "r", encoding="utf-8") as f:
        SYSTEM_PROMPT = f.read()

    model = genai.GenerativeModel(MODEL, system_instruction=SYSTEM_PROMPT)
    ehr_data = await load_ehr()

    prompt = f"Generate an AI clinical diagnosis based on patient data.\n\nPatient data: {ehr_data}"

    gen_config = genai.GenerationConfig(
        response_mime_type="application/json",
        temperature=0.7,
    )
    # Run synchronous generate_content in thread to avoid blocking event loop
    resp = await asyncio.to_thread(model.generate_content, prompt, generation_config=gen_config)

    result = json.loads(resp.text)
    # AI sometimes returns a list instead of dict - handle gracefully
    if isinstance(result, list):
        print(f"⚠️ AI diagnosis returned list ({len(result)} items), using first element")
        result = result[0] if result else {}
    with open(f"{config.output_dir}/generate_ai_diagnosis.json", "w", encoding="utf-8") as f:
        json.dump(result, f, indent=4)

    return result


async def create_ai_diagnosis():
    """Generate AI diagnosis and post to board directly"""
    print("🧠 Generating AI diagnosis...")

    try:
        result = await generate_ai_diagnosis()
        print("✅ AI diagnosis generated successfully")

        # Post to board
        board_response = await canvas_ops.create_ai_diagnosis(result)
        print(f"✅ AI diagnosis posted to board")

        return {"generated": result, "board_response": board_response}

    except Exception as e:
        print(f"❌ AI diagnosis error: {e}")
        return {"generated": None, "board_response": {"status": "error", "message": str(e)}}


async def generate_ai_treatment_plan():
    """Generate AI treatment plan JSON"""
    with open("system_prompts/ai_treatment_plan_prompt.md", "r", encoding="utf-8") as f:
        SYSTEM_PROMPT = f.read()

    model = genai.GenerativeModel(MODEL, system_instruction=SYSTEM_PROMPT)
    ehr_data = await load_ehr()

    prompt = f"Generate an AI treatment plan based on patient data.\n\nPatient data: {ehr_data}"

    gen_config = genai.GenerationConfig(
        response_mime_type="application/json",
        temperature=0.7,
    )
    # Run synchronous generate_content in thread to avoid blocking event loop
    resp = await asyncio.to_thread(model.generate_content, prompt, generation_config=gen_config)

    result = json.loads(resp.text)
    # AI sometimes returns a list instead of dict - handle gracefully
    if isinstance(result, list):
        print(f"⚠️ AI treatment plan returned list ({len(result)} items), using first element")
        result = result[0] if result else {}
    with open(f"{config.output_dir}/generate_ai_treatment_plan.json", "w", encoding="utf-8") as f:
        json.dump(result, f, indent=4)

    return result


async def create_ai_treatment_plan():
    """Generate AI treatment plan and post to board directly"""
    print("📋 Generating AI treatment plan...")

    try:
        result = await generate_ai_treatment_plan()
        print("✅ AI treatment plan generated successfully")

        # Post to board
        board_response = await canvas_ops.create_ai_treatment_plan(result)
        print(f"✅ AI treatment plan posted to board")

        return {"generated": result, "board_response": board_response}

    except Exception as e:
        print(f"❌ AI treatment plan error: {e}")
        return {"generated": None, "board_response": {"status": "error", "message": str(e)}}


# ============================================================================
# NOTIFICATION & SCHEDULE - Board operations
# ============================================================================

async def send_notification(message: str, notification_type: str = "info"):
    """Send notification to board"""
    try:
        result = await canvas_ops.create_notification({
            "message": message,
            "type": notification_type
        })
        return {"status": "success", "result": result}
    except Exception as e:
        return {"status": "error", "message": str(e)}


async def create_schedule(query: str, context: str = ""):
    """Create schedule panel on board with structured scheduling context"""
    try:
        # Generate structured scheduling context using AI
        _ensure_genai_configured()
        
        SCHEDULE_SCHEMA = {
            "type": "OBJECT",
            "properties": {
                "title": {"type": "STRING"},
                "currentStatus": {"type": "STRING"},
                "schedulingContext": {
                    "type": "OBJECT",
                    "properties": {
                        "nextAvailableSlot": {
                            "type": "OBJECT",
                            "properties": {
                                "date": {"type": "STRING"},
                                "provider": {"type": "STRING"},
                                "clinicType": {"type": "STRING"},
                                "location": {"type": "STRING"},
                                "wait_time": {"type": "STRING"}
                            }
                        },
                        "outstandingInvestigations": {
                            "type": "ARRAY",
                            "items": {
                                "type": "OBJECT",
                                "properties": {
                                    "id": {"type": "STRING"},
                                    "name": {"type": "STRING"},
                                    "status": {"type": "STRING"},
                                    "priority": {"type": "STRING"},
                                    "notes": {"type": "STRING"}
                                }
                            }
                        },
                        "bookingAction": {
                            "type": "OBJECT",
                            "properties": {
                                "status": {"type": "STRING"},
                                "lastUpdated": {"type": "STRING"},
                                "actionsTaken": {"type": "ARRAY", "items": {"type": "STRING"}},
                                "correspondencePreview": {"type": "STRING"}
                            }
                        }
                    }
                }
            },
            "required": ["title", "schedulingContext"]
        }
        
        SYSTEM_PROMPT = """Generate a structured scheduling panel with appointment details, outstanding investigations, and booking actions.
Include realistic dates (format: YYYY-MM-DDTHH:mm:ss), provider names, clinic types, investigation details, and correspondence.
Ensure all dates are in the future and wait times are realistic."""
        
        model = genai.GenerativeModel(MODEL, system_instruction=SYSTEM_PROMPT)
        ehr_data = await load_ehr()
        
        prompt = f"""Create a scheduling panel for this request: {query}

Patient context: {context if context else ehr_data}

Generate complete scheduling information including next available appointment slot, outstanding investigations, and booking confirmation."""
        
        response = model.generate_content(
            prompt,
            generation_config=genai.GenerationConfig(
                response_mime_type="application/json",
                response_schema=SCHEDULE_SCHEMA,
                temperature=0.7,
            )
        )
        
        schedule_data = json.loads(response.text)
        
        # Debug output
        print(f"📊 Generated schedule data: {json.dumps(schedule_data, indent=2)[:500]}...")
        
        # Add optional fields
        schedule_data["zone"] = "task-management-zone"
        schedule_data["width"] = 600
        
        # Ensure patientId is set
        schedule_data["patientId"] = patient_manager.get_patient_id()
        
        result = await canvas_ops.create_schedule(schedule_data)
        return {"status": "success", "result": result}
    except Exception as e:
        print(f"❌ Error creating schedule: {e}")
        return {"status": "error", "message": str(e)}


# ============================================================================
# LAB RESULTS - Parse and create lab results from natural language
# ============================================================================

async def parse_lab_values(query: str, context: str = ""):
    """
    Parse natural language lab values into structured lab results.
    Example: "ALT is 150, AST is 200, bilirubin 3.5" -> structured lab payload
    """
    print(f"🧪 Parsing lab values from: {query[:100]}...")
    
    _ensure_genai_configured()
    
    LAB_RESPONSE_SCHEMA = {
        "type": "OBJECT",
        "properties": {
            "labResults": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "parameter": {"type": "STRING", "description": "Lab parameter name (e.g., ALT, AST, Bilirubin)"},
                        "value": {"type": "STRING", "description": "String value of the lab result"},
                        "unit": {"type": "STRING", "description": "Unit of measurement (e.g., U/L, mg/dL, g/dL)"},
                        "status": {"type": "STRING", "description": "Status: 'optimal', 'warning', or 'critical'"},
                        "range": {
                            "type": "OBJECT",
                            "properties": {
                                "min": {"type": "NUMBER", "description": "Minimum normal value"},
                                "max": {"type": "NUMBER", "description": "Maximum normal value"},
                                "warningMin": {"type": "NUMBER", "description": "Warning threshold minimum"},
                                "warningMax": {"type": "NUMBER", "description": "Warning threshold maximum"}
                            },
                            "required": ["min", "max", "warningMin", "warningMax"]
                        },
                        "trend": {"type": "STRING", "description": "Trend: 'stable', 'increasing', 'decreasing', or 'unknown'"}
                    },
                    "required": ["parameter", "value", "unit", "status", "range", "trend"]
                }
            },
            "date": {"type": "STRING", "description": "Date in YYYY-MM-DD format, use today if not specified"},
            "source": {"type": "STRING", "description": "Source of the lab results"},
            "patientId": {"type": "STRING", "description": "Patient identifier"}
        },
        "required": ["labResults", "date", "source", "patientId"]
    }
    
    SYSTEM_PROMPT = """You are a clinical lab results parser. Extract lab values from natural language into structured data.

Common lab parameters and their normal ranges:
- ALT (Alanine Aminotransferase): 7-56 U/L
- AST (Aspartate Aminotransferase): 10-40 U/L
- ALP (Alkaline Phosphatase): 44-147 U/L
- GGT (Gamma-glutamyl Transferase): 0-45 U/L
- Total Bilirubin: 0.2-1.2 mg/dL
- Direct Bilirubin: 0.0-0.3 mg/dL
- Albumin: 3.4-5.4 g/dL
- Total Protein: 6.0-8.3 g/dL
- INR (International Normalized Ratio): 0.9-1.1 (no unit)
- PT (Prothrombin Time): 11-13.5 seconds
- Creatinine: 0.7-1.3 mg/dL
- BUN (Blood Urea Nitrogen): 7-20 mg/dL
- Hemoglobin: 12-16 g/dL (female), 14-18 g/dL (male)
- Platelet Count: 150-400 x10^9/L
- WBC (White Blood Cells): 4.5-11.0 x10^9/L
- Methotrexate Level: 0-0.5 umol/L

For range object, use:
- min/max: Normal range boundaries
- warningMin/warningMax: Warning thresholds (typically same as min/max or slightly wider)

Determine status:
- 'optimal' if value is within normal range
- 'warning' if value is outside normal range but not critical
- 'critical' if value is severely abnormal (>2x upper limit or <0.5x lower limit)

Trend should be:
- 'stable' if no prior data or value appears stable
- 'increasing' if value trending up
- 'decreasing' if value trending down
- 'unknown' if cannot determine

Value must be a string. Use patientId from context if available."""

    model = genai.GenerativeModel(MODEL, system_instruction=SYSTEM_PROMPT)
    
    # Get today's date for default
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    patient_id = patient_manager.get_patient_id()
    
    prompt = f"""Parse these lab values into structured data:

User input: {query}

Additional context: {context if context else 'None provided'}

Today's date: {today}
Patient ID: {patient_id}
Source: Chat input"""

    try:
        response = model.generate_content(
            prompt,
            generation_config=genai.GenerationConfig(
                response_mime_type="application/json",
                response_schema=LAB_RESPONSE_SCHEMA,
                temperature=0.1,
            )
        )
        
        lab_data = json.loads(response.text)
        
        # Save for debugging
        with open(f"{config.output_dir}/parsed_lab_values.json", "w", encoding="utf-8") as f:
            json.dump(lab_data, f, indent=4)
        
        print(f"✅ Parsed {len(lab_data.get('labResults', []))} lab results")
        return lab_data
        
    except Exception as e:
        print(f"❌ Error parsing lab values: {e}")
        return {
            "labResults": [],
            "date": today,
            "source": "Chat input (parse error)",
            "error": str(e)
        }


# ============================================================================
# EASL DIAGNOSIS (separate from DILI)
# ============================================================================

async def generate_easl_diagnosis(ehr_data=None):
    """Generate EASL-specific diagnosis assessment"""
    with open("system_prompts/easl_diagnose.md", "r", encoding="utf-8") as f:
        SYSTEM_PROMPT = f.read()

    if not ehr_data:
        ehr_data = await load_ehr()

    model = genai.GenerativeModel(MODEL, system_instruction=SYSTEM_PROMPT)
    
    prompt = f"Please generate EASL diagnosis assessment.\n\nPatient encounter data: {ehr_data}"

    resp = model.generate_content(prompt)
    
    with open(f"{config.output_dir}/generate_easl_diagnosis.md", "w", encoding="utf-8") as f:
        f.write(resp.text)

    try:
        result_json = json.loads(resp.text.replace("```json", "").replace("```", "").strip())
        with open(f"{config.output_dir}/generate_easl_diagnosis.json", "w", encoding="utf-8") as f:
            json.dump(result_json, f, indent=4)
        return result_json
    except:
        return {"answer": resp.text.replace("```markdown", " ").replace("```", "")}


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def start_background_agent_processing(action_data, todo_obj):
    """Start background processing in separate thread (for sync contexts)"""
    threading.Thread(
        target=lambda: asyncio.run(_handle_agent_processing(action_data, todo_obj)),
        daemon=True
    ).start()
    print("🔄 Background processing started")


async def _handle_agent_processing(action_data, todo_obj):
    """Handle agent processing in background"""
    try:
        response_result = await generate_response(action_data)
        
        patient_id = patient_manager.get_patient_id()
        url = BASE_URL + "/api/canvas-ops"
        payload = {
            "boardId": patient_id,
            "objectId": action_data.get('objectId'),
            "operation": "agent_answer",
            "agent_answer": response_result.get('answer')
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    print("✅ Agent answer sent successfully")
                else:
                    print(f"⚠️ Agent answer returned {response.status}")
                    
    except Exception as e:
        print(f"❌ Background processing error: {e}")


async def generate_task_obj(query):
    """Generate task object without creating on board"""
    with open("system_prompts/task_generator.md", "r", encoding="utf-8") as f:
        SYSTEM_PROMPT = f.read()

    ehr_data = await load_ehr()
    prompt = f"User request: {query}\n\nPatient data: {ehr_data}"

    model = genai.GenerativeModel(MODEL, system_instruction=SYSTEM_PROMPT)
    resp = model.generate_content(
        prompt,
        generation_config=genai.GenerationConfig(
            response_mime_type="application/json",
            temperature=0.7,
        )
    )

    result = json.loads(resp.text)
    with open(f"{config.output_dir}/generate_task_obj.json", "w", encoding="utf-8") as f:
        json.dump(result, f, indent=4)

    return result
