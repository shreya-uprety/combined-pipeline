import json
import logging
from datetime import datetime
from fastapi import APIRouter, HTTPException

router = APIRouter()
logger = logging.getLogger("medforce-server")

# Lazy imports
try:
    from medforce.infrastructure import canvas_ops
    from medforce.agents import side_agent
    from medforce.managers.patient_state import patient_manager
except Exception:
    canvas_ops = None
    side_agent = None
    patient_manager = None


@router.post("/api/canvas/focus")
async def canvas_focus(payload: dict):
    """Focus on a board item"""
    try:
        if patient_manager and payload.get('patient_id'):
            patient_manager.set_patient_id(payload['patient_id'])

        object_id = payload.get('object_id') or payload.get('objectId')
        if not object_id and payload.get('query'):
            context = json.dumps(canvas_ops.get_board_items())
            object_id = await side_agent.resolve_object_id(payload['query'], context)

        if object_id:
            result = await canvas_ops.focus_item(object_id)
            return {"status": "success", "object_id": object_id, "data": result}
        return {"status": "error", "message": "Could not resolve object_id"}
    except Exception as e:
        logger.error(f"Error focusing board item: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/canvas/create-todo")
async def canvas_create_todo(payload: dict):
    """Create a TODO task on the board"""
    try:
        if patient_manager and payload.get('patient_id'):
            patient_manager.set_patient_id(payload['patient_id'])

        query = payload.get('query') or payload.get('description')
        if query:
            result = await side_agent.generate_todo(query)
            return {"status": "success", "data": result}
        return {"status": "error", "message": "Query/description required"}
    except Exception as e:
        logger.error(f"Error creating TODO: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/canvas/send-to-easl")
async def canvas_send_to_easl(payload: dict):
    """Send a clinical question to EASL"""
    try:
        if patient_manager and payload.get('patient_id'):
            patient_manager.set_patient_id(payload['patient_id'])

        question = payload.get('question') or payload.get('query')
        if question:
            result = await side_agent.trigger_easl(question)
            return {"status": "success", "data": result}
        return {"status": "error", "message": "Question required"}
    except Exception as e:
        logger.error(f"Error sending to EASL: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/canvas/prepare-easl-query")
async def canvas_prepare_easl_query(payload: dict):
    """Prepare an EASL query by generating context and refined question."""
    try:
        if patient_manager and payload.get('patient_id'):
            patient_manager.set_patient_id(payload['patient_id'])

        question = payload.get('question') or payload.get('query')
        if not question:
            return {"status": "error", "message": "Question required"}

        result = await side_agent.prepare_easl_query(question)
        return {"status": "success", "data": result}
    except Exception as e:
        logger.error(f"Error preparing EASL query: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/canvas/create-schedule")
async def canvas_create_schedule(payload: dict):
    """Create a schedule on the board using AI to generate structured data"""
    try:
        if patient_manager and payload.get('patient_id'):
            patient_manager.set_patient_id(payload['patient_id'])

        query = payload.get('schedulingContext', payload.get('query', 'Create a follow-up appointment schedule'))
        context = payload.get('context', '')
        result = await side_agent.create_schedule(query, context)
        return {"status": "success", "data": result}
    except Exception as e:
        logger.error(f"Error creating schedule: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/canvas/send-notification")
async def canvas_send_notification(payload: dict):
    """Send a notification"""
    try:
        if patient_manager and payload.get('patient_id'):
            patient_manager.set_patient_id(payload['patient_id'])

        result = await canvas_ops.create_notification(payload)
        return {"status": "success", "data": result}
    except Exception as e:
        logger.error(f"Error sending notification: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/canvas/create-lab-results")
async def canvas_create_lab_results(payload: dict):
    """Create lab results on the board"""
    try:
        if patient_manager and payload.get('patient_id'):
            patient_manager.set_patient_id(payload['patient_id'])

        # Transform lab results to board API format with range object
        raw_labs = payload.get('labResults', [])
        transformed_labs = []

        for lab in raw_labs:
            range_str = lab.get('range') or lab.get('normalRange', '0-100')
            try:
                if isinstance(range_str, str) and '-' in range_str:
                    range_parts = range_str.split('-')
                    min_val = float(range_parts[0].strip())
                    max_val = float(range_parts[1].strip())
                else:
                    min_val, max_val = 0, 100
            except Exception as e:
                print(f"Error parsing range '{range_str}' for {lab.get('name') or lab.get('parameter')}: {e}")
                min_val, max_val = 0, 100

            value = lab.get('value')
            value_str = str(value) if value is not None else "0"

            unit = lab.get('unit', '')
            if unit is None or unit == '':
                unit = '-'

            status = lab.get('status', 'normal').lower()
            if status in ['high', 'low', 'abnormal']:
                try:
                    val_float = float(value)
                    if status == 'high':
                        status = 'critical' if val_float > (max_val * 2) else 'warning'
                    elif status == 'low':
                        status = 'critical' if val_float < (min_val * 0.5) else 'warning'
                except:
                    status = 'warning'
            elif status == 'normal':
                status = 'optimal'

            transformed_labs.append({
                "parameter": lab.get('name') or lab.get('parameter'),
                "value": value_str,
                "unit": str(unit),
                "status": status,
                "range": {
                    "min": min_val,
                    "max": max_val,
                    "warningMin": min_val,
                    "warningMax": max_val
                },
                "trend": lab.get('trend', 'stable')
            })

        lab_payload = {
            "labResults": transformed_labs,
            "date": payload.get('date', datetime.now().strftime('%Y-%m-%d')),
            "source": payload.get('source', 'Agent Generated'),
            "patientId": patient_manager.get_patient_id()
        }

        result = await canvas_ops.create_lab(lab_payload)
        return {"status": "success", "data": result}
    except Exception as e:
        logger.error(f"Error creating lab results: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/canvas/create-agent-result")
async def canvas_create_agent_result(payload: dict):
    """Create an agent analysis result on the board"""
    try:
        if patient_manager and payload.get('patient_id'):
            patient_manager.set_patient_id(payload['patient_id'])

        agent_payload = {
            "title": payload.get('title', 'Agent Analysis Result'),
            "content": payload.get('content') or payload.get('markdown', ''),
            "agentName": payload.get('agentName', 'Clinical Agent'),
            "timestamp": datetime.now().isoformat()
        }

        result = await canvas_ops.create_result(agent_payload)
        return {"status": "success", "data": result}
    except Exception as e:
        logger.error(f"Error creating agent result: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/canvas/board-items/{patient_id}")
async def get_board_items_api(patient_id: str):
    """Get all board items for a patient"""
    try:
        if patient_manager:
            patient_manager.set_patient_id(patient_id, quiet=True)

        items = canvas_ops.get_board_items(quiet=True)
        return {"status": "success", "patient_id": patient_id, "items": items, "count": len(items)}
    except Exception as e:
        logger.error(f"Error getting board items: {e}")
        raise HTTPException(status_code=500, detail=str(e))
