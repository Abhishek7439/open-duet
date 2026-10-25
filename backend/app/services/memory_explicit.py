"""Literal private remember requests, without a model or background helper.

Only a whole, directly typed remember command is handled. External content,
rooms, delegated work and routines never acquire this write path.
"""
import re

from app.services import agent_memory, memory_entries, memory_journal

_REQUEST = re.compile(r"(?:please\s+)?remember\s+(?:that\s+)?([^\n]+?)[.!]?\s*", re.I)
_SUBJECT = re.compile(r"(my [\w '-]{1,80}?)\s+(?:is|are)\s+.+", re.I)

TRUTH_NOTE = (
    "Memory persistence: conversation history recall is separate from a saved private note. "
    "Never say a fact was saved or remembered durably unless a write actually succeeded. "
    "Background upkeep may be unavailable on this engine without a helper. "
    "For an explicit literal private write, the person can send a standalone "
    "'Remember that <one fact>' message. Personal facts must not be shared with a team "
    "without the person's explicit request."
)


def prepare(agent, thread, turn, *, internal: bool = False) -> str:
    """Return factual per-turn instructions; write only eligible literal requests."""
    if agent is None:
        return ""
    match = _REQUEST.fullmatch(turn.prompt_text.strip())
    if not agent.memory_enabled:
        if match and not internal and thread.kind == "chat" and thread.job_id is None and turn.origin == "person":
            return TRUTH_NOTE + " This request was not saved: private notes are switched off. Tell the person to enable Notes before requesting persistence."
        return ""
    if internal or not match or thread.kind != "chat" or thread.job_id is not None or turn.origin != "person":
        return TRUTH_NOTE
    fact = match.group(1).strip()
    # Keep commands bounded and literal. Never flatten pasted text into a fact.
    if not fact or len(fact) > memory_entries.ENTRY_MAX_CHARS or any(c in fact for c in ('`', '·', '~')):
        return TRUTH_NOTE + " This request was not saved: send one plain fact of at most 1,000 characters."
    clean, redacted = agent_memory.redact_secrets(fact)
    if redacted:
        return TRUTH_NOTE + " This request was not saved because it contains text that looks like a secret."
    try:
        before = agent_memory.read_memory(agent.id)
        if memory_entries.identity(clean) in memory_entries.identities(before.text):
            return TRUTH_NOTE + " Receipt: this exact fact is already in your private MEMORY.md; no new write was needed."
        day = memory_entries.today()
        lines = before.text.splitlines()
        subject = _SUBJECT.fullmatch(clean)
        # Supersede only a simple, matching first-person subject. Keep all other
        # entries untouched; free-form semantic corrections need review.
        if subject:
            key = subject.group(1).casefold()
            for entry in memory_entries.parse(before.text):
                previous = _SUBJECT.fullmatch(entry.body)
                if not entry.struck and previous and previous.group(1).casefold() == key and entry.end == entry.start + 1:
                    lines[entry.start] = f"{entry.prefix}~~{entry.body}~~ · superseded {day}"
        title, _ = agent_memory.redact_secrets(thread.title or "")
        source = memory_entries.source_label(title, thread.id)
        source += f" (thread {thread.id}, turn {turn.id})"
        lines.append(memory_entries.entry_line(clean, source=source, day=day))
        saved, _ = agent_memory.write_memory(agent.id, "\n".join(lines) + "\n", expected_hash=before.hash)
        row = memory_journal.record(agent.id, "MEMORY.md", actor="person", via="explicit", before=before.text, after=saved.text)
        if row is None:
            try:
                agent_memory.write_memory(agent.id, before.text, expected_hash=saved.hash)
            except (OSError, ValueError, agent_memory.MemoryConflict):
                return TRUTH_NOTE + (
                    " The note may have persisted, but its change could not be journaled and rollback failed. "
                    "Undo is unavailable for this change. Ask the person to check the Notes panel; "
                    "do not claim either a complete save or no persistence."
                )
            return TRUTH_NOTE + " This request was not saved: its change could not be journaled."
        return TRUTH_NOTE + (
            f" Receipt: the person's literal fact was saved in this agent's private MEMORY.md "
            f"from thread {thread.id}, turn {turn.id}. Journal change {row['id']} supports Undo "
            "in the Notes panel. No team memory was changed."
        )
    except (OSError, ValueError, agent_memory.MemoryConflict):
        return TRUTH_NOTE + " This request could not be saved. Report persistence unavailable; do not claim it was remembered."
