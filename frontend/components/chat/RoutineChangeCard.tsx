"use client";

import { useEffect, useState } from "react";
import { useSession } from "@/components/AuthGate";
import { ApiError } from "@/lib/api";
import { collaboration } from "@/lib/collaboration";
import type { TranscriptItem } from "@/lib/transcript";

const LABEL: Record<string, string> = { create: "Saved", update: "Updated", pause: "Paused", resume: "Resumed", delete: "Deleted" };

export function RoutineChangeCard({ item }: { item: Extract<TranscriptItem, { kind: "routine_change" }> }) {
  const { session } = useSession();
  const admin = session?.scopes.includes("admin");
  const [receipt, setReceipt] = useState<{ state: string; undo_available: boolean; undo_reason: string | null } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!admin) return;
    let live = true;
    const refresh = () => collaboration.setupReceipt(item.requestId).then(result => {
      if (live) setReceipt(result);
    }, () => { if (live) setReceipt({ state: "unavailable", undo_available: false, undo_reason: "Could not check Undo availability." }); });
    void refresh();
    const timer = setInterval(() => { if (!document.hidden) void refresh(); }, 5000);
    window.addEventListener("focus", refresh);
    return () => { live = false; clearInterval(timer); window.removeEventListener("focus", refresh); };
  }, [admin, item.requestId]);
  const undo = async () => {
    setBusy(true); setError(null);
    try {
      await collaboration.undoSetup(item.requestId);
      setReceipt({ state: "undone", undo_available: false, undo_reason: null });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not undo this routine change.");
      setReceipt(null);
    } finally { setBusy(false); }
  };
  return <article className="space-y-2 rounded-field border border-line p-3 text-sm">
    <p><strong>{LABEL[item.action] ?? "Changed"} routine: {item.name || `Routine ${item.routineId}`}</strong></p>
    {item.action !== "delete" && <p className="text-xs text-muted">{item.paused ? "Saved paused. Resume separately when ready." : "Schedule active. This receipt does not mean a run completed."}</p>}
    <p className="text-xs text-muted">{item.note}</p>
    {item.action !== "delete" && <button type="button" className="text-xs underline" onClick={() => window.dispatchEvent(new CustomEvent("open-dots:open-routines"))}>Review routines</button>}
    {admin && receipt?.undo_available && <button type="button" disabled={busy} className="ml-3 text-xs underline" onClick={() => void undo()}>{busy ? "Undoing…" : "Undo"}</button>}
    {receipt?.state === "undone" ? <p role="status" className="text-xs text-muted">Undone. Restored routines stay paused.</p> : receipt?.undo_reason && <p className="text-xs text-muted">{receipt.undo_reason}</p>}
    {error && <p role="alert" className="text-xs text-danger">{error}</p>}
  </article>;
}
