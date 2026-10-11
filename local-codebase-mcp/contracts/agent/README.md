# Optional Agent contracts

These schemas preserve the existing optional component interfaces. Core RC2
registers the original 15 MCP tools; Agent is OFF by default. Schema validation
does not establish model availability, reading isolation, or remote authorization.

`contract-bundle.json` is consumed by `local_agents.contracts`. `cases.json` and
`verification-scope.json` support offline contract tests. No API key, account
identity, private configuration or runtime report is distributed here.

From `local-codebase-mcp`, after installing `requirements-dev.txt`:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/agent_contract -q
```

This checks interface structure and validation only. VMware, custom OAuth and
automatic project reading remain deferred; successful contract tests do not
enable these capabilities or prove a sandbox.
