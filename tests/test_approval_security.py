"""Exercise every HTTP gate using fictional decisions and no broker calls."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
import re
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from aurel2.dashboard import app as dashboard
from aurel2.dashboard.security import csrf_token
from aurel2.live.approval_security import approval_headers
from aurel2.live.pending import PendingManager, DecisionUrgency

PASSWORD = "fixture-only-approver-" * 3
TOKEN = "fixture-only-publisher-" * 3
DECISION_ID = "a" * 48


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AUREL2_DASHBOARD_USER", "approver")
    monkeypatch.setenv("AUREL2_DASHBOARD_PASSWORD", PASSWORD)
    monkeypatch.setenv("AUREL2_DASHBOARD_READ_USER", "reader")
    monkeypatch.setenv("AUREL2_DASHBOARD_READ_PASSWORD", "fixture-reader-" * 4)
    monkeypatch.setenv("AUREL2_APPROVAL_API_TOKEN", TOKEN)
    monkeypatch.setenv("AUREL2_DASHBOARD_ORIGIN", "http://testserver")
    monkeypatch.setattr(dashboard, "MODE_DATA_DIR", tmp_path)
    monkeypatch.setattr(dashboard, "DATA_DIR", tmp_path)
    pending = {"id": DECISION_ID, "created_at": datetime.now().isoformat(),
               "status": "pending", "action": "hold", "symbol": "FIXTURE",
               "reasoning": '<script>alert("fixture")</script>', "confidence": .5,
               "urgency": "non_routine"}
    (tmp_path / "pending_decisions.json").write_text(json.dumps({"decisions": {DECISION_ID: pending}}))
    with patch.object(dashboard, "get_broker_data", new=AsyncMock(side_effect=AssertionError("No broker calls"))):
        yield TestClient(dashboard.app)


def test_every_registered_route_denies_without_credentials(client):
    for route in dashboard.app.routes:
        for method in route.methods:
            response = client.request(method, route.path.replace("{decision_id}", DECISION_ID))
            assert response.status_code == 401, (method, route.path, response.text)


def test_missing_configuration_fails_closed(client, monkeypatch):
    monkeypatch.delenv("AUREL2_DASHBOARD_PASSWORD")
    assert client.get("/api/status", auth=("approver", PASSWORD)).status_code == 401


def test_reader_cannot_approve_and_publisher_is_scoped(client):
    assert client.get("/api/status", auth=("reader", "fixture-reader-" * 4)).status_code == 200
    url = f"/api/decision/{DECISION_ID}/approve"
    assert client.post(url, auth=("reader", "fixture-reader-" * 4)).status_code == 403
    publisher = {"Authorization": f"Bearer {TOKEN}"}
    assert client.post(url, headers=publisher).status_code == 403
    assert client.get("/api/positions", headers=publisher).status_code == 403
    assert client.get(f"/decision/{DECISION_ID}", headers={**publisher, "Accept": "application/json"}).json()["id"] == DECISION_ID
    assert client.post(f"/decision/{DECISION_ID}", headers=publisher).status_code == 201


def test_browser_form_csrf_expiry_and_single_use(client):
    client.auth = ("approver", PASSWORD)
    page = client.get(f"/decision/{DECISION_ID}")
    assert '<script>alert("fixture")</script>' not in page.text
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text)[1]
    url = f"/api/decision/{DECISION_ID}/approve"
    assert client.post(url).status_code == 403
    assert client.post(url, data={"csrf_token": token}, headers={"Origin": "https://hostile.invalid"}).status_code == 403
    assert client.post(url, headers={"X-CSRF-Token": csrf_token(DECISION_ID, 1)}).status_code == 403
    assert client.post(url, data={"csrf_token": token}, headers={"Origin": "http://testserver"}).status_code == 200
    assert client.post(url, data={"csrf_token": token}).status_code == 409
    assert client.post(url.replace("approve", "reject"), data={"csrf_token": token}).status_code == 409


def test_expired_decision_cannot_be_approved(client):
    path = dashboard.MODE_DATA_DIR / "pending_decisions.json"
    data = json.loads(path.read_text())
    data["decisions"][DECISION_ID]["created_at"] = (datetime.now() - timedelta(hours=2)).isoformat()
    path.write_text(json.dumps(data))
    result = client.post(f"/api/decision/{DECISION_ID}/approve", auth=("approver", PASSWORD), headers={"X-CSRF-Token": csrf_token(DECISION_ID)})
    assert result.status_code == 410
    assert json.loads(path.read_text())["decisions"][DECISION_ID]["status"] == "pending"


def test_concurrent_replies_have_one_winner(client):
    def reply(action):
        return client.post(f"/api/decision/{DECISION_ID}/{action}", auth=("approver", PASSWORD), headers={"X-CSRF-Token": csrf_token(DECISION_ID)}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reply, ["approve", "reject"])) == [200, 409]


def test_transport_credentials_and_https(monkeypatch):
    monkeypatch.delenv("AUREL2_APPROVAL_API_TOKEN", raising=False)
    with pytest.raises(ValueError): approval_headers("https://example.com/decision")
    monkeypatch.setenv("AUREL2_APPROVAL_API_TOKEN", TOKEN)
    for url in ["http://example.com/decision", "https://user:pass@example.com/decision", "https://example.com/?token=value"]:
        with pytest.raises(ValueError): approval_headers(url)
    assert approval_headers("https://example.com/decision")["Authorization"] == f"Bearer {TOKEN}"


def test_random_ids_and_remote_status_emitted_only_once(tmp_path):
    manager = PendingManager(str(tmp_path / "pending.json"))
    decision = manager.create_decision(DecisionUrgency.NON_ROUTINE, "hold", "FIXTURE", "fixture", .5)
    assert re.fullmatch(r"[a-f0-9]{48}", decision.id)
    async def check():
        with patch.object(manager, "poll_approval_status", new=AsyncMock(return_value="approved")):
            assert len(await manager.check_all_pending()) == 1
            assert await manager.check_all_pending() == []
    asyncio.run(check())


def test_stale_daemon_save_does_not_erase_dashboard_reply(client):
    manager = PendingManager(str(dashboard.MODE_DATA_DIR / "pending_decisions.json"))
    client.post(f"/api/decision/{DECISION_ID}/reject", auth=("approver", PASSWORD), headers={"X-CSRF-Token": csrf_token(DECISION_ID)})
    manager._save()
    manager._load()
    assert manager.decisions[DECISION_ID].status == "rejected"


def test_served_http_browser_approval_flow(client):
    """Run the actual ASGI server on loopback against only the temporary fixture."""
    import socket
    import threading
    import time
    import httpx
    import uvicorn

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(dashboard.app, log_level="error", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(.01)
        assert server.started
        address = f"http://127.0.0.1:{listener.getsockname()[1]}"
        manager = PendingManager(str(dashboard.MODE_DATA_DIR / "pending_decisions.json"))
        decision = manager.decisions[DECISION_ID]
        decision.approval_url = f"{address}/decision/{DECISION_ID}"
        assert asyncio.run(manager.post_to_approval_endpoint(decision))
        assert asyncio.run(manager.poll_approval_status(decision)) is None
        with httpx.Client(base_url=address, trust_env=False) as browser:
            assert browser.get(f"/decision/{DECISION_ID}").status_code == 401
            browser.auth = ("approver", PASSWORD)
            response = browser.get(f"/decision/{DECISION_ID}")
            assert response.status_code == 200
            token = re.search(r'name="csrf_token" value="([^"]+)"', response.text)[1]
            result = browser.post(f"/api/decision/{DECISION_ID}/approve", data={"csrf_token": token})
            assert result.status_code == 200
            assert result.json()["decision"]["status"] == "approved"
            assert asyncio.run(manager.poll_approval_status(decision)) == "approved"
            assert browser.post(f"/api/decision/{DECISION_ID}/approve", data={"csrf_token": token}).status_code == 409
            assert 'name="csrf_token"' not in browser.get(f"/decision/{DECISION_ID}").text
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        assert not thread.is_alive()


def test_local_reply_wins_over_in_flight_remote_poll(client):
    manager = PendingManager(str(dashboard.MODE_DATA_DIR / "pending_decisions.json"))

    async def remote_reply(decision):
        assert client.post(f"/api/decision/{DECISION_ID}/reject", auth=("approver", PASSWORD),
                           headers={"X-CSRF-Token": csrf_token(DECISION_ID)}).status_code == 200
        return "approved"

    async def check():
        with patch.object(manager, "poll_approval_status", side_effect=remote_reply):
            replies = await manager.check_all_pending()
            assert len(replies) == 1 and replies[0][1] == "rejected"
            assert replies[0][0].status == "rejected"
            assert await manager.check_all_pending() == []
        manager._load()
        assert manager.decisions[DECISION_ID].status == "rejected"

    asyncio.run(check())


def test_local_reply_wins_over_stale_timeout(client):
    manager = PendingManager(str(dashboard.MODE_DATA_DIR / "pending_decisions.json"))

    def timeout_after_reply(decision):
        assert client.post(f"/api/decision/{DECISION_ID}/reject", auth=("approver", PASSWORD),
                           headers={"X-CSRF-Token": csrf_token(DECISION_ID)}).status_code == 200
        return True

    with patch("aurel2.live.pending.PendingDecision.is_timed_out", timeout_after_reply):
        replies = asyncio.run(manager.check_all_pending())
    assert len(replies) == 1 and replies[0][1] == "rejected"
    manager._load()
    assert manager.decisions[DECISION_ID].status == "rejected"
