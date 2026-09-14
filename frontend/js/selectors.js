import { state } from "./state.js";

export function workspaceList() {
  return state.workspaces;
}

export function currentWorkspace() {
  return workspaceList()[state.selectedWorkspace] || workspaceList()[0] || null;
}

export function currentSession() {
  const workspace = currentWorkspace();
  if (!workspace) return null;
  return workspace.sessions[state.selectedSession] || workspace.sessions[0] || null;
}
