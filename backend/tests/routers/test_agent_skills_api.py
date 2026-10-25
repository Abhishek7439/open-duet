"""Agent skills over HTTP, and what a turn sends when the person names one.
Real routes, runtime and database; a real child process plays the CLI."""

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete

from app.core.config import settings
from app.db.models import Agent
from app.routers.agent_skills import router as skills_router
from app.routers.agents import get_runtime as agents_runtime
from app.routers.agents import router as agents_router
from app.routers.threads import get_runtime as threads_runtime
from app.routers.threads import router as threads_router
from tests.routers.test_agents_api import client_for, code_of, make, thread_from  # noqa: F401
from tests.services.thread_harness import (  # noqa: F401
    cleanup,
    cli,
    finished,
    owner,
    rt,
    sessions,
)


@pytest.fixture(autouse=True)
def local_enabled(monkeypatch):
    monkeypatch.setattr(settings, "LOCAL_EXECUTION_ENABLED", True)
    monkeypatch.setattr(settings, "FRONTEND_ORIGINS", "http://localhost:3000")
    monkeypatch.setattr(settings, "LOCAL_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]")


@pytest.fixture(autouse=True)
async def agent_cleanup(sessions, owner):
    yield
    async with sessions() as db:
        await db.execute(delete(Agent).where(Agent.owner_id == owner))
        await db.commit()


def app_for(rt) -> FastAPI:
    app = FastAPI()
    for router in (agents_router, skills_router, threads_router):
        app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[agents_runtime] = lambda: rt
    app.dependency_overrides[threads_runtime] = lambda: rt
    return app


@pytest.fixture
async def api(rt, cli):
    async with client_for(app_for(rt)) as client:
        yield client


def url(agent, name=""):
    return f"/api/v1/agents/{agent['id']}/skills" + (f"/{name}" if name else "")


async def put(api, agent, name, **body):
    body.setdefault("description", "A skill")
    body.setdefault("body", "Do the thing.")
    return await api.put(url(agent, name), json=body)


ROUTES = [
    ("GET", "/api/v1/agents/1/skills", None),
    ("GET", "/api/v1/agents/1/skills/x", None),
    ("PUT", "/api/v1/agents/1/skills/x", {"description": "d", "body": "b"}),
    ("DELETE", "/api/v1/agents/1/skills/x", None),
    ("POST", "/api/v1/agents/1/skills/import", {"markdown": "x"}),
]


@pytest.mark.parametrize("method,path,body", ROUTES)
async def test_every_route_refuses_anyone_but_this_machine(rt, method, path, body):
    app = app_for(rt)
    for kwargs in (dict(client=("203.0.113.9", 50000)), dict(base_url="http://evil.example")):
        async with client_for(app, **kwargs) as remote:
            assert (await remote.request(method, path, json=body)).status_code == 403


async def test_skills_are_created_listed_read_replaced_and_deleted(api):
    agent = await make(api)
    assert (await api.get(url(agent))).json() == []
    created = await put(api, agent, "report", description="Weekly", body="Step 1")
    assert created.status_code == 200 and created.json()["enabled"] is True
    listing = (await api.get(url(agent))).json()
    assert [(s["name"], s["description"], s["enabled"], s["source"]) for s in listing] == [("report", "Weekly", True, "user")]
    assert listing[0]["bytes"] > 0 and listing[0]["updated_at"]
    read = (await api.get(url(agent, "report"))).json()
    assert read["body"] == "Step 1" and read["redacted"] == 0 and read["warnings"] == []
    await put(api, agent, "report", body="Step 2", enabled=False)
    assert (await api.get(url(agent, "report"))).json()["enabled"] is False
    assert (await api.delete(url(agent, "report"))).status_code == 204
    assert (await api.get(url(agent, "report"))).status_code == 404
    assert code_of(await api.get(url(agent, "report"))) == "skill_not_found"


async def test_errors_carry_codes(api):
    agent = await make(api)
    assert code_of(await put(api, agent, "Bad Name")) == "invalid_skill_name"
    assert code_of(await put(api, agent, "ok", body=" ")) == "invalid_skill"
    assert (await api.get("/api/v1/agents/999999/skills")).status_code == 404
    assert (await put(api, {"id": 999999}, "ok")).status_code == 404


async def test_secrets_are_reported_and_replaced(api):
    agent = await make(api)
    response = (await put(api, agent, "k", body="key sk-abcdefghijklmnopqrstuvwxyz")).json()
    assert response["redacted"] == 1 and "sk-abcdef" not in response["body"]


async def test_import_preview_saves_nothing(api):
    agent = await make(api)
    response = await api.post(url(agent, "import"), json={"markdown": "---\nname: Plan Trip\ndescription: Trips\n---\nBook it."})
    assert response.status_code == 200 and response.json()["name"] == "plan-trip"
    assert (await api.get(url(agent))).json() == []
    assert code_of(await api.post(url(agent, "import"), json={"markdown": "---\nname: x\n---\n"})) == "invalid_skill"


# --- using a skill in a message ------------------------------------------------------------------


@pytest.fixture
def seen(rt, monkeypatch):
    calls = []
    original = rt.adapters["claude"].run_turn

    async def spy(ctx, sink, gate):
        calls.append(ctx)
        return await original(ctx, sink, gate)

    monkeypatch.setattr(rt.adapters["claude"], "run_turn", spy)
    return calls


async def test_a_named_skill_goes_before_that_one_message_only(api, sessions, seen):
    agent = await make(api, memory_enabled=False)
    await put(api, agent, "chosen", body="CHOSEN BODY")
    await put(api, agent, "unchosen", body="UNCHOSEN BODY")
    thread = (await thread_from(api, agent)).json()
    response = await api.post(f"/api/v1/threads/{thread['id']}/turns", json={"prompt_text": "do it", "skills": ["chosen"]})
    assert response.status_code == 202, response.text
    await finished(sessions, thread["id"], 1)
    ctx = seen[-1]
    assert "CHOSEN BODY" in ctx.prompt and ctx.prompt.rstrip().endswith("do it")
    assert "UNCHOSEN BODY" not in ctx.prompt and "BODY" not in (ctx.instructions or "")
    assert ctx.approval_mode == agent["approval_mode"]
    events = (await api.get(f"/api/v1/threads/{thread['id']}/events")).json()["events"]
    used = [e for e in events if e["type"] == "skill.used"]
    assert [e["data"] for e in used] == [{"skills": ["chosen"]}]
    # The next message names nothing, so no skill text goes with it.
    await api.post(f"/api/v1/threads/{thread['id']}/turns", json={"prompt_text": "again"})
    await finished(sessions, thread["id"], 2)
    assert "CHOSEN BODY" not in seen[-1].prompt


async def test_an_unknown_or_disabled_skill_refuses_the_turn(api, seen):
    agent = await make(api)
    await put(api, agent, "off", enabled=False)
    thread = (await thread_from(api, agent)).json()
    for names in (["off"], ["nope"], ["../x"]):
        response = await api.post(f"/api/v1/threads/{thread['id']}/turns", json={"prompt_text": "x", "skills": names})
        assert response.status_code == 422 and code_of(response) == "skill_unavailable"
    too_many = await api.post(f"/api/v1/threads/{thread['id']}/turns", json={"prompt_text": "x", "skills": list("abcd")})
    assert too_many.status_code == 422
    assert seen == []
    # No turn was started by the refusals.
    assert (await api.get(f"/api/v1/threads/{thread['id']}")).json()["active_turn_id"] is None


async def test_a_thread_without_an_agent_refuses_skills(api):
    thread = (await api.post("/api/v1/threads", json={"provider": "claude"})).json()
    response = await api.post(f"/api/v1/threads/{thread['id']}/turns", json={"prompt_text": "x", "skills": ["a"]})
    assert code_of(response) == "skill_unavailable"


# --- agent files ------------------------------------------------------------------------------------


async def test_skills_travel_in_an_agent_file_and_arrive_turned_off(api):
    agent = await make(api)
    await put(api, agent, "carry", description="Carried", body="Body text")
    export = (await api.get(f"/api/v1/agents/{agent['id']}/export", params={"include_skills": True})).json()
    assert export["document"]["skills"] == [{"name": "carry", "description": "Carried", "body": "Body text"}]
    plain = (await api.get(f"/api/v1/agents/{agent['id']}/export")).json()
    assert "skills" not in plain["document"]
    imported = (await api.post("/api/v1/agents/import", json={"document": export["document"]})).json()
    copied = (await api.get(url(imported))).json()
    assert [(s["name"], s["enabled"], s["source"]) for s in copied] == [("carry", False, "imported")]
    assert (await api.get(url(imported, "carry"))).json()["body"] == "Body text"
    skipped = (await api.post("/api/v1/agents/import", json={"document": export["document"], "include_skills": False})).json()
    assert (await api.get(url(skipped))).json() == []


async def test_a_hand_edited_file_cannot_smuggle_skills(api):
    agent = await make(api)
    document = (await api.get(f"/api/v1/agents/{agent['id']}/export")).json()["document"]
    for bad in (
        [{"name": "../x", "description": "d", "body": "b"}],
        [{"name": "a", "description": "d", "body": "b"}, {"name": "a", "description": "d", "body": "b"}],
        [{"name": "a", "description": "d"}],
        "not a list",
        [{"name": f"s{i}", "description": "d", "body": "b"} for i in range(31)],
    ):
        response = await api.post("/api/v1/agents/import", json={"document": {**document, "skills": bad}})
        assert response.status_code == 422 and code_of(response) == "invalid_agent_file"
    # Secrets inside imported skills are replaced; the enabled flag in a file is ignored.
    ok = {**document, "skills": [{"name": "a", "description": "d", "body": "key sk-abcdefghijklmnopqrstuvwxyz", "enabled": True}]}
    imported = (await api.post("/api/v1/agents/import", json={"document": ok})).json()
    skill = (await api.get(url(imported, "a"))).json()
    assert skill["enabled"] is False and "sk-abcdef" not in skill["body"]


async def test_deleting_an_agent_removes_its_skills(api, tmp_path):
    agent = await make(api)
    await put(api, agent, "gone")
    folder = tmp_path / "agents" / str(agent["id"]) / "skills"
    assert folder.exists()
    assert (await api.delete(f"/api/v1/agents/{agent['id']}")).status_code == 204
    assert not folder.exists()


async def learn_source(api, sessions, agent, prompt="Review expenses in first.csv"):
    thread = (await thread_from(api, agent)).json()
    turn = (await api.post(f"/api/v1/threads/{thread['id']}/turns", json={"prompt_text": prompt})).json()
    await finished(sessions, thread["id"])
    return {"thread_id": thread["id"], "turn_id": turn["id"]}


async def test_reviewed_learn_save_second_fixture_and_revision_bound_undo(api, sessions, cli, seen):
    agent = await make(api, memory_enabled=False)
    cli.script({"type": "chat", "text": "First result: list the source row and policy rule."})
    source = await learn_source(api, sessions, agent)
    response = await api.post(url(agent, "learn"), json=source)
    assert response.status_code == 200, response.text
    draft = response.json()
    assert "First result:" in draft["body"] and "first.csv" in draft["body"]
    assert draft["thread_id"] == source["thread_id"] and draft["turn_id"] == source["turn_id"]
    assert (await api.get(url(agent))).json() == []  # preview is read-only
    body = {**source, "description": "Review expenses", "body": "Compare each supplied CSV row to the supplied policy; list row and rule.", "reviewed": False}
    assert code_of(await api.post(url(agent, "learn/expense-review"), json=body)) == "skill_review_required"
    body["reviewed"] = True
    saved = await api.post(url(agent, "learn/expense-review"), json=body)
    assert saved.status_code == 201, saved.text
    doc = saved.json()
    assert doc["enabled"] is False and doc["source"] == f"learned:{source['thread_id']}:{source['turn_id']}"
    assert code_of(await api.post(url(agent, "learn/expense-review"), json=body)) == "skill_exists"
    # Turning on is a separate explicit edit; invocation carries the reviewed
    # instructions to a second fixture, without claiming real-model quality.
    await put(api, agent, "expense-review", **{key: doc[key] for key in ("description", "body", "source")}, enabled=True)
    cli.script({"type": "chat", "text": "Second fixture result"})
    second = (await thread_from(api, agent)).json()
    result = await api.post(f"/api/v1/threads/{second['id']}/turns", json={"prompt_text": "Review second.csv using policy.md", "skills": ["expense-review"]})
    assert result.status_code == 202
    await finished(sessions, second["id"])
    assert body["body"] in seen[-1].prompt and "second.csv" in seen[-1].prompt
    stale = await api.post(url(agent, "expense-review/undo"), json={"revision": doc["revision"]})
    assert code_of(stale) == "skill_undo_stale"
    assert (await api.get(url(agent, "expense-review"))).json()["enabled"] is True
    # A second saved draft can be undone while it is still exactly the saved creation.
    fresh = (await api.post(url(agent, "learn/expense-review-draft"), json=body)).json()
    assert (await api.post(url(agent, "expense-review-draft/undo"), json={"revision": fresh["revision"]})).status_code == 204
    assert (await api.get(url(agent, "expense-review-draft"))).status_code == 404


async def test_learn_refuses_other_agent_owner_and_unsuccessful_origins(api, sessions, rt):
    from sqlalchemy import update
    from app.db.models import Thread, Turn

    agent = await make(api)
    other = await make(api)
    source = await learn_source(api, sessions, agent)
    assert code_of(await api.post(url(other, "learn"), json=source)) == "skill_source_not_found"
    async with sessions() as db:
        await db.execute(update(Thread).where(Thread.id == source["thread_id"]).values(owner_id="another-owner"))
        await db.commit()
    assert code_of(await api.post(url(agent, "learn"), json=source)) == "skill_source_not_found"
    async with sessions() as db:
        await db.execute(update(Thread).where(Thread.id == source["thread_id"]).values(owner_id=rt.owner_id))
        await db.commit()
    for values in ({"status": "failed"}, {"status": "cancelled"}, {"status": "interrupted"}, {"status": "running"}, {"status": "succeeded", "origin": "routine"}, {"origin": "outside"}, {"origin": "peer"}, {"origin": None}):
        async with sessions() as db:
            await db.execute(update(Turn).where(Turn.id == source["turn_id"]).values(**values))
            await db.commit()
        assert code_of(await api.post(url(agent, "learn"), json=source)) == "skill_source_unavailable"
    async with sessions() as db:
        await db.execute(update(Turn).where(Turn.id == source["turn_id"]).values(status="succeeded", origin="person"))
        await db.commit()


ROUTES.extend([
    ("POST", "/api/v1/agents/1/skills/learn", {"thread_id": 1, "turn_id": 1}),
    ("POST", "/api/v1/agents/1/skills/learn/x", {"thread_id": 1, "turn_id": 1, "description": "d", "body": "b", "reviewed": True}),
    ("POST", "/api/v1/agents/1/skills/x/undo", {"revision": "a" * 64}),
])
