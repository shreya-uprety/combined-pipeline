"""
Tests for all MedForce endpoints — organized by router.
Uses mocked external dependencies so tests run fast and offline.
"""

import json
import pytest
from unittest.mock import MagicMock, AsyncMock, patch, PropertyMock


# ────────────────────────────── Health ──────────────────────────────


class TestHealth:
    """GET / and GET /health"""

    def test_root(self, test_client):
        resp = test_client.get("/")
        assert resp.status_code == 200
        data = resp.json()
        assert "status" in data
        assert "endpoints" in data

    def test_health(self, test_client):
        resp = test_client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert data["service"] == "medforce-unified"


# ────────────────────────────── Patient ─────────────────────────────


class TestPatient:
    """GET /patient/current, POST /patient/switch"""

    def test_get_current_patient(self, test_client):
        resp = test_client.get("/patient/current")
        assert resp.status_code == 200
        assert "patient_id" in resp.json()

    def test_switch_patient(self, test_client):
        resp = test_client.post(
            "/patient/switch",
            json={"patient_id": "p9999"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["patient_id"] == "p9999"

    def test_switch_patient_missing_id(self, test_client):
        """Empty patient_id should be rejected by Pydantic or the endpoint"""
        resp = test_client.post("/patient/switch", json={})
        assert resp.status_code == 422  # Pydantic validation error


# ────────────────────────────── Board Chat ──────────────────────────


class TestBoardChat:
    """POST /send-chat, WS /ws/chat/{patient_id}"""

    @patch("medforce.routers.board_chat.chat_model")
    def test_send_chat(self, mock_chat_model, test_client):
        mock_chat_model.chat_agent = AsyncMock(return_value="Test response from agent")
        resp = test_client.post(
            "/send-chat",
            json=[{"role": "user", "content": "Hello", "patient_id": "p0001"}],
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert "response" in data

    @patch("medforce.routers.board_chat.chat_model")
    def test_send_chat_empty_payload(self, mock_chat_model, test_client):
        mock_chat_model.chat_agent = AsyncMock(return_value="OK")
        resp = test_client.post("/send-chat", json=[])
        assert resp.status_code == 200

    @patch("medforce.routers.board_chat.websocket_chat_endpoint", new=None)
    def test_ws_chat_unavailable(self, test_client):
        from starlette.websockets import WebSocketDisconnect
        with pytest.raises(WebSocketDisconnect):
            with test_client.websocket_connect("/ws/chat/p0001") as ws:
                pass


# ────────────────────────────── Voice ───────────────────────────────


class TestVoice:
    """5 voice endpoints"""

    @patch("medforce.routers.voice.voice_session_manager")
    @patch("medforce.routers.voice.patient_manager")
    def test_start_voice_session(self, mock_pm, mock_vsm, test_client):
        mock_vsm.create_session = AsyncMock(return_value="sess-123")
        mock_pm.set_patient_id = MagicMock()
        resp = test_client.post("/api/voice/start/p0001")
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == "sess-123"
        assert data["status"] == "connecting"

    @patch("medforce.routers.voice.voice_session_manager", new=None)
    def test_start_voice_session_unavailable(self, test_client):
        resp = test_client.post("/api/voice/start/p0001")
        assert resp.status_code == 503

    @patch("medforce.routers.voice.voice_session_manager")
    def test_get_voice_status(self, mock_vsm, test_client):
        mock_vsm.get_status = MagicMock(return_value={
            "status": "ready",
            "session_id": "sess-123",
        })
        resp = test_client.get("/api/voice/status/sess-123")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ready"

    @patch("medforce.routers.voice.voice_session_manager")
    def test_get_voice_status_not_found(self, mock_vsm, test_client):
        mock_vsm.get_status = MagicMock(return_value={"status": "not_found"})
        resp = test_client.get("/api/voice/status/bad-id")
        assert resp.status_code == 404

    @patch("medforce.routers.voice.voice_session_manager")
    def test_close_voice_session(self, mock_vsm, test_client):
        mock_vsm.close_session = AsyncMock()
        resp = test_client.delete("/api/voice/session/sess-123")
        assert resp.status_code == 200
        assert resp.json()["status"] == "closed"


# ────────────────────────────── Canvas ──────────────────────────────


class TestCanvas:
    """9 canvas endpoints"""

    @patch("medforce.routers.canvas.patient_manager")
    @patch("medforce.routers.canvas.canvas_ops")
    @patch("medforce.routers.canvas.side_agent")
    def test_canvas_focus_with_object_id(self, mock_sa, mock_co, mock_pm, test_client):
        mock_co.focus_item = AsyncMock(return_value={"focused": True})
        resp = test_client.post(
            "/api/canvas/focus",
            json={"object_id": "obj-1", "patient_id": "p0001"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"

    @patch("medforce.routers.canvas.patient_manager")
    @patch("medforce.routers.canvas.canvas_ops")
    @patch("medforce.routers.canvas.side_agent")
    def test_canvas_focus_with_query(self, mock_sa, mock_co, mock_pm, test_client):
        mock_co.get_board_items = MagicMock(return_value=[])
        mock_sa.resolve_object_id = AsyncMock(return_value="obj-99")
        mock_co.focus_item = AsyncMock(return_value={"focused": True})
        resp = test_client.post(
            "/api/canvas/focus",
            json={"query": "show labs"},
        )
        assert resp.status_code == 200

    @patch("medforce.routers.canvas.patient_manager")
    @patch("medforce.routers.canvas.side_agent")
    def test_canvas_create_todo(self, mock_sa, mock_pm, test_client):
        mock_sa.generate_todo = AsyncMock(return_value={"created": True})
        resp = test_client.post(
            "/api/canvas/create-todo",
            json={"query": "Order CBC labs"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"

    @patch("medforce.routers.canvas.patient_manager")
    @patch("medforce.routers.canvas.side_agent")
    def test_canvas_send_to_easl(self, mock_sa, mock_pm, test_client):
        mock_sa.trigger_easl = AsyncMock(return_value={"sent": True})
        resp = test_client.post(
            "/api/canvas/send-to-easl",
            json={"question": "DILI criteria?"},
        )
        assert resp.status_code == 200

    @patch("medforce.routers.canvas.patient_manager")
    @patch("medforce.routers.canvas.side_agent")
    def test_canvas_prepare_easl_query(self, mock_sa, mock_pm, test_client):
        mock_sa.prepare_easl_query = AsyncMock(return_value={"query": "refined"})
        resp = test_client.post(
            "/api/canvas/prepare-easl-query",
            json={"question": "What about DILI?"},
        )
        assert resp.status_code == 200

    @patch("medforce.routers.canvas.patient_manager")
    @patch("medforce.routers.canvas.side_agent")
    def test_canvas_create_schedule(self, mock_sa, mock_pm, test_client):
        mock_sa.create_schedule = AsyncMock(return_value={"schedule": "created"})
        resp = test_client.post(
            "/api/canvas/create-schedule",
            json={"query": "Follow-up in 2 weeks"},
        )
        assert resp.status_code == 200

    @patch("medforce.routers.canvas.patient_manager")
    @patch("medforce.routers.canvas.canvas_ops")
    def test_canvas_send_notification(self, mock_co, mock_pm, test_client):
        mock_co.create_notification = AsyncMock(return_value={"notified": True})
        resp = test_client.post(
            "/api/canvas/send-notification",
            json={"message": "Alert", "patient_id": "p0001"},
        )
        assert resp.status_code == 200

    @patch("medforce.routers.canvas.patient_manager")
    @patch("medforce.routers.canvas.canvas_ops")
    def test_canvas_create_lab_results(self, mock_co, mock_pm, test_client):
        mock_pm.get_patient_id = MagicMock(return_value="p0001")
        mock_co.create_lab = AsyncMock(return_value={"lab": "created"})
        resp = test_client.post(
            "/api/canvas/create-lab-results",
            json={
                "labResults": [
                    {
                        "name": "ALT",
                        "value": 45,
                        "unit": "U/L",
                        "range": "7-56",
                        "status": "normal",
                    }
                ],
                "patient_id": "p0001",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"

    @patch("medforce.routers.canvas.patient_manager")
    @patch("medforce.routers.canvas.canvas_ops")
    def test_canvas_create_agent_result(self, mock_co, mock_pm, test_client):
        mock_co.create_result = AsyncMock(return_value={"result": "ok"})
        resp = test_client.post(
            "/api/canvas/create-agent-result",
            json={"title": "Analysis", "content": "Patient stable"},
        )
        assert resp.status_code == 200

    @patch("medforce.routers.canvas.patient_manager")
    @patch("medforce.routers.canvas.canvas_ops")
    def test_get_board_items(self, mock_co, mock_pm, test_client):
        mock_co.get_board_items = MagicMock(return_value=[{"id": "1", "type": "note"}])
        resp = test_client.get("/api/canvas/board-items/p0001")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["count"] == 1


# ────────────────────────────── Reports ─────────────────────────────


class TestReports:
    """POST /generate_diagnosis, /generate_report, /generate_legal"""

    @patch("medforce.routers.reports.patient_manager")
    @patch("medforce.routers.reports.side_agent")
    def test_generate_diagnosis(self, mock_sa, mock_pm, test_client):
        mock_sa.create_dili_diagnosis = AsyncMock(return_value={"diagnosis": "DILI"})
        resp = test_client.post("/generate_diagnosis", json={"patient_id": "p0001"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "done"

    @patch("medforce.routers.reports.patient_manager")
    @patch("medforce.routers.reports.side_agent")
    def test_generate_report(self, mock_sa, mock_pm, test_client):
        mock_sa.create_patient_report = AsyncMock(return_value={"report": "done"})
        resp = test_client.post("/generate_report", json={"patient_id": "p0001"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "done"

    @patch("medforce.routers.reports.patient_manager")
    @patch("medforce.routers.reports.side_agent")
    def test_generate_legal(self, mock_sa, mock_pm, test_client):
        mock_sa.create_legal_doc = AsyncMock(return_value={"legal": "done"})
        resp = test_client.post("/generate_legal", json={"patient_id": "p0001"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "done"


# ────────────────────────────── Pre-Consult ─────────────────────────


class TestPreConsult:
    """POST /chat, GET /chat/{id}, POST /chat/{id}/reset,
       GET /patients, POST /register, WS /ws/pre-consult/{id}"""

    @patch("medforce.routers.pre_consult.get_chat_agent")
    def test_post_chat(self, mock_get_agent, test_client):
        mock_agent = MagicMock()
        mock_agent.pre_consulte_agent = AsyncMock(
            return_value={"message": "How can I help?"}
        )
        mock_agent.gcs.create_file_from_string = MagicMock()
        mock_get_agent.return_value = mock_agent
        resp = test_client.post(
            "/chat",
            json={
                "patient_id": "p0001",
                "patient_message": "I have a headache",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["patient_id"] == "p0001"

    @patch("medforce.routers.pre_consult.get_chat_agent")
    def test_get_chat_history(self, mock_get_agent, test_client):
        mock_agent = MagicMock()
        mock_agent.gcs.read_file_as_string = MagicMock(
            return_value='{"conversation": [{"sender": "admin", "message": "Hello"}]}'
        )
        mock_get_agent.return_value = mock_agent
        resp = test_client.get("/chat/p0001")
        assert resp.status_code == 200
        assert "conversation" in resp.json()

    @patch("medforce.routers.pre_consult.get_chat_agent")
    def test_get_chat_history_not_found(self, mock_get_agent, test_client):
        mock_agent = MagicMock()
        mock_agent.gcs.read_file_as_string = MagicMock(return_value=None)
        mock_get_agent.return_value = mock_agent
        resp = test_client.get("/chat/p9999")
        assert resp.status_code == 404

    @patch("medforce.routers.pre_consult.get_chat_agent")
    def test_reset_chat_history(self, mock_get_agent, test_client):
        mock_agent = MagicMock()
        mock_agent.gcs.create_file_from_string = MagicMock()
        mock_get_agent.return_value = mock_agent
        resp = test_client.post("/chat/p0001/reset")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert "current_state" in data

    @patch("medforce.routers.pre_consult.get_chat_agent")
    def test_get_patients(self, mock_get_agent, test_client):
        mock_agent = MagicMock()
        mock_agent.gcs.list_files = MagicMock(return_value=["p0001/", "p0002/"])
        mock_agent.gcs.read_file_as_string = MagicMock(
            return_value='{"patient_id": "p0001", "name": "Test"}'
        )
        mock_get_agent.return_value = mock_agent
        resp = test_client.get("/patients")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    @patch("medforce.routers.pre_consult.get_gcs")
    def test_register_patient(self, mock_get_gcs, test_client):
        mock_gcs = MagicMock()
        mock_gcs.create_file_from_string = MagicMock()
        mock_get_gcs.return_value = mock_gcs
        resp = test_client.post(
            "/register",
            json={
                "first_name": "Jane",
                "last_name": "Doe",
                "dob": "1990-01-01",
                "gender": "Female",
                "phone": "555-0100",
                "email": "jane@example.com",
                "chief_complaint": "Abdominal pain",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["patient_id"].startswith("PT-")
        assert "success" in data["status"].lower()

    @patch("medforce.routers.pre_consult.websocket_pre_consult_endpoint", new=None)
    def test_ws_pre_consult_unavailable(self, test_client):
        from starlette.websockets import WebSocketDisconnect
        with pytest.raises(WebSocketDisconnect):
            with test_client.websocket_connect("/ws/pre-consult/p0001") as ws:
                pass


# ────────────────────────────── Data Processing ─────────────────────


class TestDataProcessing:
    """5 data-processing endpoints"""

    @patch("medforce.routers.data_processing.my_agents")
    def test_process_preconsult(self, mock_agents, test_client):
        mock_proc = MagicMock()
        mock_proc.process_raw_data = AsyncMock()
        mock_agents.RawDataProcessing = MagicMock(return_value=mock_proc)
        resp = test_client.get("/process/p0001/preconsult")
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"

    @patch("medforce.routers.data_processing.my_agents")
    def test_process_board(self, mock_agents, test_client):
        mock_proc = MagicMock()
        mock_proc.process_dashboard_content = AsyncMock()
        mock_agents.RawDataProcessing = MagicMock(return_value=mock_proc)
        resp = test_client.get("/process/p0001/board")
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"

    @patch("medforce.routers.data_processing.my_agents")
    def test_process_board_update(self, mock_agents, test_client):
        mock_proc = MagicMock()
        mock_proc.process_board_object = AsyncMock()
        mock_agents.RawDataProcessing = MagicMock(return_value=mock_proc)
        resp = test_client.get("/process/p0001/board-update")
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"

    @patch("medforce.routers.data_processing.get_chat_agent")
    def test_get_patient_data(self, mock_get_agent, test_client):
        mock_agent = MagicMock()
        mock_agent.gcs.read_file_as_string = MagicMock(
            return_value='{"test": "data"}'
        )
        mock_get_agent.return_value = mock_agent
        resp = test_client.get("/data/p0001/basic_info.json")
        assert resp.status_code == 200
        assert resp.json()["test"] == "data"

    @patch("medforce.routers.data_processing.get_chat_agent")
    def test_get_image(self, mock_get_agent, test_client):
        mock_agent = MagicMock()
        mock_agent.gcs.read_file_as_bytes = MagicMock(return_value="base64data==")
        mock_get_agent.return_value = mock_agent
        resp = test_client.get("/image/p0001/scan.png")
        assert resp.status_code == 200
        data = resp.json()
        assert data["file"] == "scan.png"


# ────────────────────────────── Scheduling ──────────────────────────


class TestScheduling:
    """GET /schedule/{id}, POST /schedule/update, POST /schedule/switch, GET /slots"""

    @patch("medforce.routers.scheduling.schedule_manager")
    @patch("medforce.routers.scheduling.get_gcs")
    def test_get_schedule_nurse(self, mock_get_gcs, mock_sm, test_client):
        mock_gcs = MagicMock()
        mock_get_gcs.return_value = mock_gcs
        mock_ops = MagicMock()
        mock_ops.get_all = MagicMock(return_value=[{"time": "09:00", "patient": "p0001"}])
        mock_sm.ScheduleCSVManager = MagicMock(return_value=mock_ops)
        resp = test_client.get("/schedule/N001")
        assert resp.status_code == 200

    @patch("medforce.routers.scheduling.schedule_manager")
    @patch("medforce.routers.scheduling.get_gcs")
    def test_get_schedule_doctor(self, mock_get_gcs, mock_sm, test_client):
        mock_gcs = MagicMock()
        mock_get_gcs.return_value = mock_gcs
        mock_ops = MagicMock()
        mock_ops.get_all = MagicMock(return_value=[])
        mock_sm.ScheduleCSVManager = MagicMock(return_value=mock_ops)
        resp = test_client.get("/schedule/D001")
        assert resp.status_code == 200

    def test_get_schedule_invalid_prefix(self, test_client):
        resp = test_client.get("/schedule/X001")
        assert resp.status_code == 400

    @patch("medforce.routers.scheduling.schedule_manager")
    @patch("medforce.routers.scheduling.get_gcs")
    def test_update_schedule(self, mock_get_gcs, mock_sm, test_client):
        mock_gcs = MagicMock()
        mock_get_gcs.return_value = mock_gcs
        mock_ops = MagicMock()
        mock_ops.update_slot = MagicMock(return_value=True)
        mock_sm.ScheduleCSVManager = MagicMock(return_value=mock_ops)
        resp = test_client.post(
            "/schedule/update",
            json={
                "clinician_id": "N001",
                "date": "2025-01-15",
                "time": "09:00",
                "status": "done",
            },
        )
        assert resp.status_code == 200

    @patch("medforce.routers.scheduling.schedule_manager")
    @patch("medforce.routers.scheduling.get_gcs")
    def test_switch_schedule(self, mock_get_gcs, mock_sm, test_client):
        mock_gcs = MagicMock()
        mock_get_gcs.return_value = mock_gcs
        mock_ops = MagicMock()
        mock_ops.update_slot = MagicMock(return_value=True)
        mock_sm.ScheduleCSVManager = MagicMock(return_value=mock_ops)
        resp = test_client.post(
            "/schedule/switch",
            json={
                "clinician_id": "N001",
                "item1": {"patient": "p0001", "date": "2025-01-15", "time": "09:00"},
                "item2": {"patient": "p0002", "date": "2025-01-15", "time": "10:00"},
            },
        )
        assert resp.status_code == 200

    @patch("medforce.routers.scheduling.schedule_manager")
    @patch("medforce.routers.scheduling.get_gcs")
    def test_get_slots(self, mock_get_gcs, mock_sm, test_client):
        mock_gcs = MagicMock()
        mock_get_gcs.return_value = mock_gcs
        mock_ops = MagicMock()
        mock_ops.get_empty_schedule = MagicMock(return_value=[
            {"date": "2025-01-16", "time": "14:00", "doctor": "Dr. Smith"}
        ])
        mock_sm.ScheduleCSVManager = MagicMock(return_value=mock_ops)
        resp = test_client.get("/slots")
        assert resp.status_code == 200
        assert "available_slots" in resp.json()


# ────────────────────────────── Simulation ──────────────────────────


class TestSimulation:
    """WS /ws/simulation, /ws/simulation/audio, /ws/transcriber"""

    @patch("medforce.routers.simulation.SimulationManager")
    def test_ws_simulation(self, mock_sim_cls, test_client):
        mock_mgr = MagicMock()
        mock_mgr.run = AsyncMock()
        mock_sim_cls.return_value = mock_mgr
        with test_client.websocket_connect("/ws/simulation") as ws:
            ws.send_json({"type": "start", "patient_id": "P0001", "gender": "Male"})

    @patch("medforce.routers.simulation.simulation_scenario")
    def test_ws_simulation_audio(self, mock_scenario, test_client):
        mock_mgr = MagicMock()
        mock_mgr.run = AsyncMock()
        mock_scenario.SimulationAudioManager = MagicMock(return_value=mock_mgr)
        with test_client.websocket_connect("/ws/simulation/audio") as ws:
            ws.send_json({"type": "start", "patient_id": "P0001"})

    @patch("medforce.routers.simulation.TranscriberEngine")
    @patch("medforce.routers.simulation.fetch_gcs_text_internal")
    def test_ws_transcriber(self, mock_fetch, mock_engine_cls, test_client):
        mock_fetch.return_value = "Patient info text"
        mock_engine = MagicMock()
        mock_engine.running = False
        mock_engine.stt_loop = MagicMock()
        mock_engine_cls.return_value = mock_engine
        # The transcriber first reads data/questions.json and writes output files
        # We need those files to exist for the test
        import os
        os.makedirs("data", exist_ok=True)
        os.makedirs("output", exist_ok=True)
        if not os.path.exists("data/questions.json"):
            with open("data/questions.json", "w") as f:
                json.dump([], f)

        with test_client.websocket_connect("/ws/transcriber") as ws:
            ws.send_json({"type": "start", "patient_id": "P0001"})
            resp = ws.receive_json()
            assert resp["type"] == "system"


# ────────────────────────────── Admin ───────────────────────────────


class TestAdmin:
    """8 admin endpoints"""

    def test_get_admin_ui(self, test_client):
        import os
        os.makedirs("ui", exist_ok=True)
        # Create a minimal admin UI file if needed
        if not os.path.exists("ui/admin_ui.html"):
            with open("ui/admin_ui.html", "w") as f:
                f.write("<html><body>Admin</body></html>")
        resp = test_client.get("/admin")
        assert resp.status_code == 200
        assert "Admin" in resp.text

    @patch("medforce.routers.admin.storage")
    def test_get_patient_file_json(self, mock_storage, test_client):
        mock_blob = MagicMock()
        mock_blob.exists.return_value = True
        mock_blob.download_as_text.return_value = '{"name": "John"}'
        mock_bucket = MagicMock()
        mock_bucket.blob.return_value = mock_blob
        mock_client = MagicMock()
        mock_client.bucket.return_value = mock_bucket
        mock_storage.Client.return_value = mock_client

        resp = test_client.post(
            "/api/get-patient-file",
            json={"pid": "p0001", "file_name": "patient_info.json"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "John"

    @patch("medforce.routers.admin.storage")
    def test_get_patient_file_not_found(self, mock_storage, test_client):
        mock_blob = MagicMock()
        mock_blob.exists.return_value = False
        mock_bucket = MagicMock()
        mock_bucket.blob.return_value = mock_blob
        mock_client = MagicMock()
        mock_client.bucket.return_value = mock_bucket
        mock_storage.Client.return_value = mock_client

        resp = test_client.post(
            "/api/get-patient-file",
            json={"pid": "p0001", "file_name": "missing.json"},
        )
        assert resp.status_code == 404

    @patch("medforce.routers.admin.storage")
    def test_list_patient_files(self, mock_storage, test_client):
        mock_blob = MagicMock()
        mock_blob.name = "patient_profile/p0001/info.json"
        mock_blob.size = 100
        mock_blob.updated = None
        mock_client = MagicMock()
        mock_client.list_blobs.return_value = [mock_blob]
        mock_storage.Client.return_value = mock_client

        resp = test_client.get("/api/admin/list-files/p0001")
        assert resp.status_code == 200
        assert "files" in resp.json()

    @patch("medforce.routers.admin.storage")
    def test_save_patient_file(self, mock_storage, test_client):
        mock_blob = MagicMock()
        mock_bucket = MagicMock()
        mock_bucket.blob.return_value = mock_blob
        mock_client = MagicMock()
        mock_client.bucket.return_value = mock_bucket
        mock_storage.Client.return_value = mock_client

        resp = test_client.post(
            "/api/admin/save-file",
            json={"pid": "p0001", "file_name": "notes.txt", "content": "Test notes"},
        )
        assert resp.status_code == 200
        mock_blob.upload_from_string.assert_called_once()

    @patch("medforce.routers.admin.storage")
    def test_delete_admin_file(self, mock_storage, test_client):
        mock_blob = MagicMock()
        mock_blob.exists.return_value = True
        mock_bucket = MagicMock()
        mock_bucket.blob.return_value = mock_blob
        mock_client = MagicMock()
        mock_client.bucket.return_value = mock_bucket
        mock_storage.Client.return_value = mock_client

        resp = test_client.delete("/api/admin/delete-file?pid=p0001&file_name=notes.txt")
        assert resp.status_code == 200
        mock_blob.delete.assert_called_once()

    @patch("medforce.routers.admin.storage")
    def test_list_admin_patients(self, mock_storage, test_client):
        mock_blobs = MagicMock()
        mock_blobs.__iter__ = MagicMock(return_value=iter([]))
        mock_blobs.prefixes = ["patient_profile/p0001/", "patient_profile/p0002/"]
        mock_client = MagicMock()
        mock_client.list_blobs.return_value = mock_blobs
        mock_storage.Client.return_value = mock_client

        resp = test_client.get("/api/admin/list-patients")
        assert resp.status_code == 200
        assert "patients" in resp.json()

    @patch("medforce.routers.admin.storage")
    def test_create_admin_patient(self, mock_storage, test_client):
        mock_blob = MagicMock()
        mock_blob.exists.return_value = False
        mock_bucket = MagicMock()
        mock_bucket.blob.return_value = mock_blob
        mock_client = MagicMock()
        mock_client.bucket.return_value = mock_bucket
        mock_storage.Client.return_value = mock_client

        resp = test_client.post(
            "/api/admin/create-patient",
            json={"pid": "p9999"},
        )
        assert resp.status_code == 200
        mock_blob.upload_from_string.assert_called_once()

    @patch("medforce.routers.admin.storage")
    def test_create_admin_patient_duplicate(self, mock_storage, test_client):
        mock_blob = MagicMock()
        mock_blob.exists.return_value = True
        mock_bucket = MagicMock()
        mock_bucket.blob.return_value = mock_blob
        mock_client = MagicMock()
        mock_client.bucket.return_value = mock_bucket
        mock_storage.Client.return_value = mock_client

        resp = test_client.post(
            "/api/admin/create-patient",
            json={"pid": "p0001"},
        )
        assert resp.status_code == 400

    @patch("medforce.routers.admin.storage")
    def test_delete_admin_patient(self, mock_storage, test_client):
        mock_blob = MagicMock()
        mock_bucket = MagicMock()
        mock_bucket.list_blobs.return_value = [mock_blob]
        mock_client = MagicMock()
        mock_client.bucket.return_value = mock_bucket
        mock_storage.Client.return_value = mock_client

        resp = test_client.delete("/api/admin/delete-patient?pid=p0001")
        assert resp.status_code == 200
        mock_bucket.delete_blobs.assert_called_once()

    @patch("medforce.routers.admin.storage")
    def test_delete_admin_patient_not_found(self, mock_storage, test_client):
        mock_bucket = MagicMock()
        mock_bucket.list_blobs.return_value = []
        mock_client = MagicMock()
        mock_client.bucket.return_value = mock_bucket
        mock_storage.Client.return_value = mock_client

        resp = test_client.delete("/api/admin/delete-patient?pid=p9999")
        assert resp.status_code == 404


# ────────────────────────────── Utility ─────────────────────────────


class TestUtility:
    """GET /ws/sessions, /test-gemini-live, /ui/{file_path}"""

    def test_ws_sessions_no_agent(self, test_client):
        resp = test_client.get("/ws/sessions")
        assert resp.status_code == 200
        # When agent not available, should still return gracefully
        data = resp.json()
        assert "sessions" in data or "error" in data

    @patch("medforce.routers.utility.get_websocket_agent")
    def test_ws_sessions_with_agent(self, mock_get_agent, test_client):
        mock_agent = MagicMock()
        mock_agent.get_active_sessions.return_value = [
            {"id": "s1", "patient_id": "p0001"}
        ]
        mock_get_agent.return_value = mock_agent
        resp = test_client.get("/ws/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert data["active_sessions"] == 1

    def test_serve_ui_file(self, test_client):
        import os
        os.makedirs("ui", exist_ok=True)
        with open("ui/test_page.html", "w") as f:
            f.write("<html><body>Test</body></html>")
        resp = test_client.get("/ui/test_page.html")
        assert resp.status_code == 200
        assert "Test" in resp.text

    def test_serve_ui_file_not_found(self, test_client):
        resp = test_client.get("/ui/nonexistent.html")
        assert resp.status_code in (404, 500)
