export const LIVE_CHAT_STATES = new Set(["queued", "starting", "running", "stopping"]);
export const TERMINAL_CHAT_STATES = new Set(["completed", "stopped", "failed"]);

export function findSessionById(workspaces, workspaceId, sessionId) {
  const workspace = workspaces.find((item) => item.id === workspaceId);
  return workspace?.sessions?.find((session) => session.id === sessionId) || null;
}

export function sessionEventCursor(session) {
  return Math.max(0, ...(session?.events || []).map((event) => Number(event[4]) || 0));
}

export function initializeEventCursors(workspaces, cursors) {
  workspaces.forEach((workspace) => {
    (workspace.sessions || []).forEach((session) => {
      cursors[session.id] = Math.max(Number(cursors[session.id]) || 0, sessionEventCursor(session));
    });
  });
}

export function mergeSessionEvents(session, events, cursors) {
  if (!session || !events.length) return false;
  session.events = session.events || [];
  let cursor = Number(cursors[session.id]) || sessionEventCursor(session);
  let changed = false;
  [...events]
    .sort((left, right) => Number(left.id) - Number(right.id))
    .forEach((event) => {
      const eventId = Number(event.id) || 0;
      if (eventId <= cursor) return;
      const toolCallId = event.toolCallId || "";
      const existingToolIndex =
        toolCallId && ["command", "tool"].includes(event.type)
          ? session.events.findIndex((item) => item[0] === event.type && item[3] === (event.runId || "") && item[6] === toolCallId)
          : -1;
      const tuple = [event.type, event.message, event.message, event.runId || "", eventId, event.status || "", toolCallId];
      if (existingToolIndex >= 0) session.events[existingToolIndex] = tuple;
      else session.events.push(tuple);
      cursor = eventId;
      if (LIVE_CHAT_STATES.has(event.type) || TERMINAL_CHAT_STATES.has(event.type)) session.status = event.type;
      session.updated = event.time || session.updated;
      changed = true;
    });
  cursors[session.id] = cursor;
  return changed;
}
