"""An agent's saved skills (docs/SKILLS.md): list, read, write, delete and
preview an import. Text only; nothing here runs a skill. Same local access
check as the agents router.

Errors carry `{"detail": {"error_code": "...", "message": "..."}}`."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.db.models import Thread, Turn
from app.services import skill_learning
from app.routers.agents import _agent_or_404, get_runtime
from app.services import agent_skills as skills
from app.services.local_access_policy import require_local_access
from app.services.thread_runtime import ThreadRuntime

router = APIRouter(
    prefix="/agents/{agent_id}/skills",
    tags=["Agent skills"],
    dependencies=[Depends(require_local_access)],
)


class SkillSummary(BaseModel):
    name: str
    description: str
    enabled: bool
    source: str
    bytes: int
    updated_at: datetime | None


class SkillResponse(BaseModel):
    name: str
    description: str
    body: str
    enabled: bool
    source: str
    revision: str = ""
    redacted: int = 0
    warnings: list[str] = []


class SkillWriteRequest(BaseModel):
    description: str = Field(max_length=2000)
    body: str = Field(max_length=200_000)
    enabled: bool = True
    source: str = Field(default="user", max_length=200)


class SkillLearnRequest(BaseModel):
    thread_id: int = Field(gt=0)
    turn_id: int = Field(gt=0)


class SkillLearnSaveRequest(SkillLearnRequest):
    description: str = Field(max_length=300)
    body: str = Field(max_length=200_000)
    reviewed: bool


class SkillLearnPreviewResponse(BaseModel):
    name: str
    description: str
    body: str
    warnings: list[str]
    thread_id: int
    turn_id: int
    redacted: int


class SkillUndoRequest(BaseModel):
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")


class SkillImportRequest(BaseModel):
    markdown: str = Field(max_length=200_000)


class SkillPreviewResponse(BaseModel):
    name: str
    description: str
    body: str
    warnings: list[str]


def _http(exc: skills.SkillError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail={"error_code": exc.code, "message": exc.message})


def _response(skill: skills.Skill) -> SkillResponse:
    return SkillResponse(
        name=skill.name,
        description=skill.description,
        body=skill.body,
        enabled=skill.enabled,
        source=skill.source,
        revision=skill.revision,
        redacted=skill.redacted,
        warnings=skill.warnings,
    )


@router.get("", response_model=list[SkillSummary])
async def list_skills(agent_id: int, rt: ThreadRuntime = Depends(get_runtime)):
    await _agent_or_404(rt, agent_id)
    try:
        return [
            SkillSummary(
                name=s.name, description=s.description, enabled=s.enabled, source=s.source,
                bytes=s.bytes, updated_at=s.updated_at,
            )
            for s in skills.list_skills(agent_id)
        ]
    except skills.SkillError as exc:
        raise _http(exc) from None


@router.post("/import", response_model=SkillPreviewResponse)
async def preview_import(agent_id: int, payload: SkillImportRequest, rt: ThreadRuntime = Depends(get_runtime)):
    """What a SKILL.md text would save. Saves nothing; the person reviews it,
    then saves it with PUT (turned off until reviewed)."""
    await _agent_or_404(rt, agent_id)
    try:
        return SkillPreviewResponse(**skills.preview(payload.markdown))
    except skills.SkillError as exc:
        raise _http(exc) from None


async def _learn_preview(rt: ThreadRuntime, agent_id: int, payload: SkillLearnRequest) -> dict:
    await _agent_or_404(rt, agent_id)
    async with rt.session() as db:
        thread = await db.get(Thread, payload.thread_id)
        turn = await db.get(Turn, payload.turn_id)
        if thread is None or turn is None:
            raise _http(skills.SkillError(404, "skill_source_not_found", "No such source conversation for this agent."))
        try:
            return skill_learning.draft(thread, turn, owner_id=rt.owner_id, agent_id=agent_id)
        except skills.SkillError as exc:
            raise _http(exc) from None


@router.post("/learn", response_model=SkillLearnPreviewResponse)
async def preview_learn(agent_id: int, payload: SkillLearnRequest, rt: ThreadRuntime = Depends(get_runtime)):
    """Prepare a bounded, redacted editable draft; persist and execute nothing."""
    return await _learn_preview(rt, agent_id, payload)


@router.post("/learn/{name}", response_model=SkillResponse, status_code=201)
async def save_learned(
    agent_id: int, name: str, payload: SkillLearnSaveRequest, rt: ThreadRuntime = Depends(get_runtime)
):
    await _learn_preview(rt, agent_id, payload)
    if not payload.reviewed:
        raise _http(skills.SkillError(422, "skill_review_required", "Review the instructions before saving."))
    try:
        return _response(skills.write_skill(
            agent_id, name, payload.description, payload.body, enabled=False,
            source=f"learned:{payload.thread_id}:{payload.turn_id}", create_only=True,
        ))
    except skills.SkillError as exc:
        raise _http(exc) from None


@router.post("/{name}/undo", status_code=204)
async def undo_learned(
    agent_id: int, name: str, payload: SkillUndoRequest, rt: ThreadRuntime = Depends(get_runtime)
):
    """Remove only the unchanged learned creation named by the saved receipt."""
    await _agent_or_404(rt, agent_id)
    try:
        skills.delete_skill(agent_id, name, expected_revision=payload.revision)
    except skills.SkillError as exc:
        raise _http(exc) from None


@router.get("/{name}", response_model=SkillResponse)
async def read_skill(agent_id: int, name: str, rt: ThreadRuntime = Depends(get_runtime)):
    await _agent_or_404(rt, agent_id)
    try:
        skills.check_name(name)
        return _response(skills.read_skill(agent_id, name))
    except skills.SkillError as exc:
        raise _http(exc) from None


@router.put("/{name}", response_model=SkillResponse)
async def write_skill(
    agent_id: int, name: str, payload: SkillWriteRequest, rt: ThreadRuntime = Depends(get_runtime)
):
    await _agent_or_404(rt, agent_id)
    try:
        return _response(
            skills.write_skill(
                agent_id, name, payload.description, payload.body, enabled=payload.enabled, source=payload.source
            )
        )
    except skills.SkillError as exc:
        raise _http(exc) from None


@router.delete("/{name}", status_code=204)
async def delete_skill(agent_id: int, name: str, rt: ThreadRuntime = Depends(get_runtime)):
    await _agent_or_404(rt, agent_id)
    try:
        skills.delete_skill(agent_id, name)
    except skills.SkillError as exc:
        raise _http(exc) from None
