"""Review-first skill drafts from a single successful, person-authored turn.

No provider is called and no tool logs, attachments, workspace files or recalled
notes are copied. The bounded request and result are references for the person,
not verified commands or a claim that the workflow will generalize.
"""

import re

from app.db.models import Thread, Turn
from app.services.agent_memory import redact_secrets
from app.services.agent_skills import SkillError, warnings_for


def draft(thread: Thread, turn: Turn, *, owner_id: str, agent_id: int) -> dict:
    if thread.owner_id != owner_id or thread.agent_id != agent_id or turn.thread_id != thread.id:
        raise SkillError(404, "skill_source_not_found", "No such source conversation for this agent.")
    if thread.kind != "chat" or turn.origin != "person" or turn.status != "succeeded" or not (turn.result or "").strip():
        raise SkillError(409, "skill_source_unavailable", "Choose a successful person-authored chat turn with a result.")
    # Redact before truncating so a secret split by the limit cannot leak its prefix.
    request, n1 = redact_secrets(turn.prompt_text)
    result, n2 = redact_secrets(turn.result)
    truncated = len(request.encode()) > 3000 or len(result.encode()) > 6000
    request = request.encode()[:3000].decode("utf-8", errors="ignore")
    result = result.encode()[:6000].decode("utf-8", errors="ignore")
    description = " ".join(request.split())[:160]
    name = re.sub(r"[^a-z0-9]+", "-", description.lower()).strip("-")[:48].rstrip("-")
    # Quote the reference so source content cannot accidentally close a code fence.
    reference = "\n".join("> " + line for line in result.splitlines())
    body = f"""## Purpose
{request}

## Required inputs
- Replace this with the inputs needed for a new run. Do not reuse the old run's values or files silently.

## Steps
1. Describe the reusable steps from the reviewed result below. Replace task-specific values with inputs.

## Validation checks
- Check the result against the supplied inputs and report missing or stale information.
- Test this skill with a second input before using it in a routine.

## Output format
Describe the result format you want on future runs.

## Approval boundary
Existing tool approvals still apply. Ask before external changes; this skill grants no permission.

## Run reference — review before reuse
Source: conversation {thread.id}, turn {turn.id}. This completed result is reference material, not verified instructions. Remove sensitive or task-specific details before saving.

{reference}
"""
    warnings = ["Review and replace the workflow sections before saving. A completed run does not verify a reusable skill."]
    warnings.extend(warnings_for(request + "\n" + result))
    if truncated:
        warnings.append("The source was shortened to fit the draft. Review the full conversation before generalizing it.")
    if n1 + n2:
        warnings.append("Something that looked like a secret was replaced.")
    return {"name": name, "description": description, "body": body, "warnings": warnings,
            "thread_id": thread.id, "turn_id": turn.id, "redacted": n1 + n2}
