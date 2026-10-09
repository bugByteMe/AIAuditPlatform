// Frontend controller: chatstream responsibilities.
export function createChatStream(context) {
  let activeChatStream = null;
  let activePollTimer = null;
  let activeStreamWatchdog = null;
  let activeReconnectTimer = null;
  let activeStreamLastActivity = 0;
  let activeReconnectAttempts = 0;
  let fallbackPollingActive = false;
  let activeStreamGeneration = 0;
  const api = (...args) => context.api(...args);
  const authenticatedApiUrl = (...args) => context.authenticatedApiUrl(...args);
  const LIVE_CHAT_STATES = context.LIVE_CHAT_STATES;
  const TERMINAL_CHAT_STATES = context.TERMINAL_CHAT_STATES;
  const findSessionById = (...args) => context.findSessionById(...args);
  const mergeHistoricalEvents = (...args) => context.mergeHistoricalEvents(...args);
  const mergeSessionEvents = (...args) => context.mergeSessionEvents(...args);
  const state = context.state;
  const renderDynamic = (...args) => context.renderDynamic(...args);
  const safeCurrentWorkspace = (...args) => context.safeCurrentWorkspace(...args);
  const refreshCurrentUser = (...args) => context.refreshCurrentUser(...args);
  const refreshWorkspaceById = (...args) => context.refreshWorkspaceById(...args);

  function stopChatStream() {
    activeStreamGeneration += 1;
    if (activeChatStream) activeChatStream.close();
    activeChatStream = null;
    if (activePollTimer) window.clearTimeout(activePollTimer);
    activePollTimer = null;
    if (activeStreamWatchdog) window.clearInterval(activeStreamWatchdog);
    activeStreamWatchdog = null;
    if (activeReconnectTimer) window.clearTimeout(activeReconnectTimer);
    activeReconnectTimer = null;
    activeStreamLastActivity = 0;
    activeReconnectAttempts = 0;
    fallbackPollingActive = false;
  }


  function currentSessionObject() {
    const workspace = safeCurrentWorkspace();
    return workspace?.sessions?.[state.selectedSession] || workspace?.sessions?.[0] || null;
  }

  function chatPollInterval() {
    return Math.max(250, Number(state.runtimeConfig.chatPollIntervalMs) || 2_000);
  }

  function scheduleChatPoll(workspaceId, sessionId, generation, delay = chatPollInterval()) {
    if (generation !== activeStreamGeneration || !fallbackPollingActive || activePollTimer) return;
    activePollTimer = window.setTimeout(() => {
      activePollTimer = null;
      pollChatEvents(workspaceId, sessionId, generation);
    }, delay);
  }

  async function pollChatEvents(workspaceId, sessionId, generation, reschedule = true) {
    if (generation !== activeStreamGeneration) return;
    const after = state.chatLastEventIds[sessionId] || 0;
    try {
      const result = await api(`/api/workspaces/${encodeURIComponent(workspaceId)}/chat/events?${new URLSearchParams({ sessionId, after }).toString()}`);
      if (generation !== activeStreamGeneration) return;
      const session = findSessionById(state.workspaces, workspaceId, sessionId);
      if (!session) {
        stopChatStream();
        return;
      }
      const changed = mergeSessionEvents(session, result.events || [], state.chatLastEventIds);
      const serverStatus = result.sessionStatus || session.status;
      const statusChanged = serverStatus !== session.status;
      session.status = serverStatus;
      if (result.session) {
        session.resources = result.session.resources;
        session.queueAhead = result.session.queueAhead;
        session.workerId = result.session.workerId;
        session.container = result.session.container;
        state.resourceStatus = result.session.resources || state.resourceStatus;
      }
      if (changed || statusChanged || result.session) renderDynamic();
      if (TERMINAL_CHAT_STATES.has(serverStatus)) {
        stopChatStream();
        await Promise.all([refreshWorkspaceById(workspaceId), refreshCurrentUser()]);
        return;
      }
    } catch (error) {
      console.warn("Chat event reconciliation failed", error);
    }
    if (reschedule) scheduleChatPoll(workspaceId, sessionId, generation);
  }

  function markStreamHealthy(workspaceId, sessionId, generation) {
    if (generation !== activeStreamGeneration) return;
    activeStreamLastActivity = Date.now();
    activeReconnectAttempts = 0;
    fallbackPollingActive = false;
    if (activePollTimer) window.clearTimeout(activePollTimer);
    activePollTimer = null;
    if (activeReconnectTimer) window.clearTimeout(activeReconnectTimer);
    activeReconnectTimer = null;
  }

  function scheduleStreamReconnect(workspaceId, sessionId, generation) {
    if (generation !== activeStreamGeneration || activeReconnectTimer || activeChatStream) return;
    const base = Math.max(500, Number(state.runtimeConfig.sseRetryMs) || 2_000);
    const delay = Math.min(30_000, base * (2 ** Math.min(activeReconnectAttempts, 4)));
    activeReconnectAttempts += 1;
    activeReconnectTimer = window.setTimeout(() => {
      activeReconnectTimer = null;
      openChatStream(workspaceId, sessionId, generation);
    }, delay);
  }

  function activatePollingFallback(workspaceId, sessionId, generation) {
    if (generation !== activeStreamGeneration) return;
    fallbackPollingActive = true;
    if (activeChatStream) activeChatStream.close();
    activeChatStream = null;
    scheduleChatPoll(workspaceId, sessionId, generation, 0);
    scheduleStreamReconnect(workspaceId, sessionId, generation);
  }

  function openChatStream(workspaceId, sessionId, generation) {
    if (generation !== activeStreamGeneration || activeChatStream) return;
    const after = state.chatLastEventIds[sessionId] || 0;
    const url = authenticatedApiUrl(`/api/workspaces/${encodeURIComponent(workspaceId)}/chat/stream?${new URLSearchParams({ sessionId, after }).toString()}`);
    activeChatStream = new EventSource(url, { withCredentials: true });
    activeChatStream.onopen = () => {
      if (generation !== activeStreamGeneration) return;
      markStreamHealthy(workspaceId, sessionId, generation);
      pollChatEvents(workspaceId, sessionId, generation, false);
    };
    const handleEvent = (event) => {
      if (generation !== activeStreamGeneration) return;
      markStreamHealthy(workspaceId, sessionId, generation);
      let payload;
      try {
        payload = JSON.parse(event.data);
      } catch (error) {
        console.warn("Invalid chat event payload", error);
        return;
      }
      const session = findSessionById(state.workspaces, workspaceId, sessionId);
      if (mergeSessionEvents(session, [payload], state.chatLastEventIds)) renderDynamic();
      if (TERMINAL_CHAT_STATES.has(payload.type)) {
        stopChatStream();
        refreshWorkspaceById(workspaceId);
        refreshCurrentUser();
      }
    };
    activeChatStream.onmessage = handleEvent;
    ["user", "queued", "starting", "running", "stopping", "assistant", "command", "websearch", "tool", "usage", "progress", "error", "completed", "stopped", "failed"].forEach((type) => {
      activeChatStream.addEventListener(type, handleEvent);
    });
    activeChatStream.addEventListener("heartbeat", (event) => {
      if (generation !== activeStreamGeneration) return;
      markStreamHealthy(workspaceId, sessionId, generation);
      try {
        const heartbeat = JSON.parse(event.data);
        const session = findSessionById(state.workspaces, workspaceId, sessionId);
        if (session && heartbeat.sessionStatus && session.status !== heartbeat.sessionStatus) {
          session.status = heartbeat.sessionStatus;
        }
        if (session) {
          session.queueAhead = heartbeat.queueAhead;
          session.resources = heartbeat.resources || session.resources;
        }
        state.resourceStatus = heartbeat.resources || state.resourceStatus;
        renderDynamic();
        if (TERMINAL_CHAT_STATES.has(heartbeat.sessionStatus)) {
          stopChatStream();
          refreshWorkspaceById(workspaceId);
          refreshCurrentUser();
        }
      } catch (error) {
        console.warn("Invalid chat heartbeat payload", error);
      }
    });
    activeChatStream.onerror = () => {
      if (generation !== activeStreamGeneration) return;
      activatePollingFallback(workspaceId, sessionId, generation);
    };
  }

  function startChatStreamForSession(workspaceId, sessionId) {
    if (!workspaceId || !sessionId) return;
    stopChatStream();
    const generation = activeStreamGeneration;
    activeStreamLastActivity = Date.now();
    openChatStream(workspaceId, sessionId, generation);
    activeStreamWatchdog = window.setInterval(() => {
      if (generation !== activeStreamGeneration) return;
      if (!fallbackPollingActive && Date.now() - activeStreamLastActivity > 15_000) {
        activatePollingFallback(workspaceId, sessionId, generation);
      }
    }, 5_000);
  }

  async function loadLatestSessionHistory(workspaceId, session) {
    if (!workspaceId || !session || session.historyLoaded) return;
    session.historyLoading = true;
    session.historyLoadingKind = "initial";
    session.historyLoadError = false;
    renderDynamic();
    try {
      const result = await api(`/api/workspaces/${encodeURIComponent(workspaceId)}/chat/events?${new URLSearchParams({
        sessionId: session.id,
        latest: "true",
        limit: "200",
      }).toString()}`);
      const events = result.events || [];
      mergeSessionEvents(session, events, state.chatLastEventIds);
      session.historyBefore = events.length ? Math.min(...events.map((event) => Number(event.id) || 0)) : 0;
      session.historyHasMore = Boolean(result.hasMore && events.length);
      session.historyLoaded = true;
    } catch (error) {
      session.historyLoadError = true;
      throw error;
    } finally {
      session.historyLoading = false;
      session.historyLoadingKind = "";
      renderDynamic();
    }
  }

  async function loadOlderSessionHistory() {
    const workspace = safeCurrentWorkspace();
    const session = currentSessionObject();
    if (!workspace || !session?.historyLoaded || !session.historyHasMore || session.historyLoading) return;
    const before = Number(session.historyBefore || 0);
    if (before <= 1) {
      session.historyHasMore = false;
      return;
    }
    session.historyLoading = true;
    session.historyLoadingKind = "older";
    session.historyLoadError = false;
    renderDynamic();
    try {
      const result = await api(`/api/workspaces/${encodeURIComponent(workspace.id)}/chat/events?${new URLSearchParams({
        sessionId: session.id,
        before,
        limit: "200",
      }).toString()}`);
      const current = findSessionById(state.workspaces, workspace.id, session.id);
      if (!current) return;
      const events = result.events || [];
      if (events.length) current.historyBefore = Math.min(...events.map((event) => Number(event.id) || before));
      current.historyHasMore = Boolean(result.hasMore && events.length);
      if (mergeHistoricalEvents(current, events)) renderDynamic();
    } catch (error) {
      const current = findSessionById(state.workspaces, workspace.id, session.id);
      if (current) current.historyLoadError = true;
      console.warn("Older chat history load failed", error);
    } finally {
      const current = findSessionById(state.workspaces, workspace.id, session.id);
      if (current) {
        current.historyLoading = false;
        current.historyLoadingKind = "";
      }
      renderDynamic();
    }
  }

  async function maybeStartChatStream() {
    const workspace = safeCurrentWorkspace();
    const session = currentSessionObject();
    if (!workspace || !session) {
      stopChatStream();
      return;
    }
    try {
      await loadLatestSessionHistory(workspace.id, session);
      renderDynamic();
    } catch (error) {
      console.warn("Chat history load failed", error);
    }
    if (!LIVE_CHAT_STATES.has(session.status)) {
      stopChatStream();
      return;
    }
    startChatStreamForSession(workspace.id, session.id);
  }

  return { stopChatStream, currentSessionObject, chatPollInterval, scheduleChatPoll, pollChatEvents, markStreamHealthy, scheduleStreamReconnect, activatePollingFallback, openChatStream, startChatStreamForSession, loadLatestSessionHistory, loadOlderSessionHistory, maybeStartChatStream };
}
