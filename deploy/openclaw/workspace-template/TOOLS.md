## Tool Notes

### Safe Execution
- Use `sh-safe` or `bash-safe` for workspace scripts when shell execution is needed.
- `sh-safe` and `bash-safe` only run scripts inside the workspace, reject shell flags like `-c`, and scrub inherited environment variables.
- Use `web-safe` for public web search and page reads when raw network access is unnecessary.

### Self-Improvement
- `.learnings/LEARNINGS.md` stores corrections, best practices, and knowledge gaps.
- `.learnings/ERRORS.md` stores command failures and integration breakages.
- `.learnings/FEATURE_REQUESTS.md` stores user-requested capabilities that do not exist yet.
- When a lesson keeps recurring, compress it into a short preventive rule and promote it into `AGENTS.md`, `SOUL.md`, or this file.
