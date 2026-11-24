class CodexCommandMixin:
  def codex_command_args(self, run: dict) -> list[str]:
    base = ["codex", "--ask-for-approval", "never", "--sandbox", "danger-full-access", "exec"]
    prompt = self.codex_prompt(run)
    if run.get("codexFork"):
      return [
        *base,
        "fork",
        "--json",
        "--skip-git-repo-check",
        "-m",
        run["model"],
        run["codexSessionId"],
        prompt,
      ]
    if run.get("codexResume"):
      base.extend(["resume", "--json", "--skip-git-repo-check", "-m", run["model"]])
      if run.get("codexSessionId"):
        base.append(run["codexSessionId"])
      else:
        base.append("--last")
      base.append(prompt)
      return base
    return [
      *base,
      "--json",
      "--skip-git-repo-check",
      "-C",
      "/workspace",
      "-m",
      run["model"],
      prompt,
    ]

