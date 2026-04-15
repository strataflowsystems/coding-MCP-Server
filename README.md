# coding-MCP-Server

A local MCP (Model Context Protocol) server that gives AI coding agents (Gemma 4, Qwen3-coder) full autonomous control over a Windows development environment — filesystem, git, Docker, npm, SSH, databases, secrets, and deployment.

Runs on `http://localhost:3001/mcp` and is consumed by [Void IDE](https://voideditor.com) via MCP and by the included `agent.py` / `orchestrate.py` loop scripts.

---

## Architecture

```
Void IDE  ──────────────────────────────────────────────────────┐
                                                                 │ MCP (SSE, port 3001)
agent.py / orchestrate.py ──── Ollama (local LLMs) ────────────►│
                                                                 ▼
                                                     server.py (FastMCP)
                                                         │
                              ┌──────────────────────────┼──────────────────────────┐
                              │                          │                          │
                        Filesystem               SSH / servers              Secrets
                        Git / npm / Docker       (ops-vm, droplet)         (Infisical)
                        Postgres / SQLite        ssh_run / ssh_copy         infisical_*
```

---

## Files

| File | Purpose |
|---|---|
| `server.py` | FastMCP server — 75+ tools across all categories |
| `agent.py` | Single-model agent loop — connects Ollama to MCP |
| `orchestrate.py` | Multi-model orchestrator — planner → worker → reviewer |
| `Modelfile` | Ollama Modelfile for `gemma4-coder` (26B, autonomous coding agent) |
| `Modelfile.qwen3` | Ollama Modelfile for `qwen3-coder-agent` (30B, complex reasoning) |
| `requirements.txt` | Python dependencies |

---

## Tool Categories (server.py)

### Filesystem
`read_file`, `write_file`, `read_file_range`, `replace_in_file`, `list_dir`, `tree`, `search_files`, `get_file_outline`, `count_file_lines`, `diff_files`, `get_project_context`

### Shell Execution
`run_cmd`, `run_powershell`, `launch_app`

### Git
`git_status`, `git_diff`, `git_add`, `git_commit`, `git_push`, `git_pull`, `git_log`, `git_checkout`, `git_create_branch`, `git_clone`

### Node / npm
`npm_install`, `npm_run`, `npm_build`, `npx`

### Python
`pip_install`, `run_python`, `create_venv`

### Code Quality
`lint_file`, `format_file`, `type_check`, `run_tests`

### Docker
`docker_build`, `docker_run`, `docker_stop`, `docker_remove`, `docker_logs`, `docker_compose_up`, `docker_compose_down`, `docker_ps`

### HTTP / Network
`http_request`, `check_port`, `download_file`

### SSH
`ssh_run` — Run commands on remote servers. Supports local key files, Infisical-fetched keys, or password auth.
`ssh_copy` — SCP upload/download with the same auth options.

**ops VM shortcut** (via `~/.ssh/config`):
```bash
ssh ops-vm "command"   # no flags needed — key, user, host all pre-configured
```

**Batching** — always combine multiple checks into one call to minimise connections:
```bash
ssh ops-vm "ps aux | grep next; ss -tlnp; ls ~/services/alncom-workflow/releases | tail -3"
```

### Infisical Secrets
`infisical_status`, `infisical_list_secrets`, `infisical_search_secrets`, `infisical_get_secret`, `infisical_export_env`

- Self-hosted at `https://secrets.strataflowsystems.com`
- Auth via machine identity token in `.env` — never requires manual login
- `infisical_list_secrets` recursively walks all subfolders (secrets are often not in root `/`)
- Projects: **Codex**, **OERATIONS_VM**, **Synapse**

### Structured Data
`read_json`, `write_json`, `set_json_key`, `read_yaml`, `set_yaml_key`

### Database
`sqlite_query`, `sqlite_schema`, `postgres_query`, `postgres_schema`

### Agent Knowledge & Memory
`get_agent_config` — loads `C:\ai-workspace\agent-config.json` (infrastructure inventory, server details, Infisical project IDs, coding preferences, repo paths)
`update_agent_config` — agents can update their own knowledge base
`memory_save` / `memory_get` — cross-session persistent memory in `C:\ai-workspace\agent-memory.json`
`list_tools` — full tool registry (prevents hallucinated tool names)
`get_tools_for_task` — focused tool list by category: `git`, `npm`, `docker`, `ssh`, `secrets`, `servers`, etc.

### Safety
`_validate_command` — rejects shell metacharacter injection and paths outside sandbox
Output truncated at 40,000 chars to prevent context overflow

---

## Models

### `gemma4-coder` (default in Void)
- Base: `gemma4:26b`
- Temperature: 0.2, ctx: 32768
- Tuned for: tool-first behaviour, no narration before acting, infrastructure awareness

### `qwen3-coder-agent`
- Base: `qwen3-coder:30b`
- Temperature: 0.2, ctx: 32768, thinking suppressed via `/no_think`
- Tuned for: complex multi-file code changes, explicit permission to act without confirmation

Both models are told:
- Call `list_tools()` first in every new conversation
- Call `get_agent_config()` before any infrastructure/server/secrets task
- SSH to ops VM: `ssh ops-vm "cmd"` — key and user pre-configured
- Batch all SSH checks into one call (semicolons)
- Never search Infisical for secrets already named in `agent-config.json`

---

## agent.py

Single-model agent loop with full MCP tool support.

```bash
python agent.py                          # interactive, gemma4-coder
python agent.py --model qwen3-coder-agent
python agent.py --output result.json     # write structured result for orchestrator
python agent.py --context prev.json      # chain from previous agent output
```

Features:
- SSE parsing for MCP responses
- Qwen3 thinking suppression (`/no_think` prefix, `<think>` block stripping)
- XML fallback tool call parser for models that don't use native tool format
- Nudge loop — re-prompts model if it narrates instead of acting
- Max 40 turns, max 3 nudges per session

---

## orchestrate.py

Multi-model task coordinator.

```bash
python orchestrate.py "refactor the auth module"
python orchestrate.py --plan-only "migrate database schema"   # just show the plan
python orchestrate.py --no-review "quick fix"
python orchestrate.py --resume <run-id>                       # resume interrupted task
```

Pipeline:
1. **Planner** (`qwen2.5:14b`) — decomposes task into JSON subtask list with dependencies
2. **Worker** (`gemma4-coder`) — executes each subtask, chains context between steps
3. **Coder** (`qwen3-coder-agent`) — handles complex code-heavy subtasks
4. **Reviewer** (`gemma4-coder`) — final verification pass

Run artifacts saved to `C:\ai-workspace\.orchestrator-runs\`.

---

## Desktop Launcher

`C:\Users\lauri\Desktop\Gemma Coding Agent.bat`

1. Starts Ollama if not running
2. Starts MCP server if not running
3. Menu: Gemma4 / Qwen3 / Orchestrate mode

---

## Infrastructure Knowledge (agent-config.json)

Location: `C:\ai-workspace\agent-config.json`

Loaded automatically by `get_agent_config()`. Contains:
- All repo local paths (19 repos)
- Infrastructure: DigitalOcean droplet (`app.strataflowsystems.com`), ops VM (`192.168.5.68`), IQGeo droplet
- Ops VM services: Next.js :3000, Directus primary :8055, Directus secondary :8056, Postgres :5432
- Ops VM SSH: alias `ops-vm`, key `~/.ssh/operations_vm_ed25519`
- Infisical: domain, org ID, all 3 project IDs and known secret paths
- Coding preferences per project (Next.js 15 App Router, custom CSS only, pnpm monorepo, etc.)
- Agent model assignments

---

## Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Create .env with Infisical machine identity token
echo "INFISICAL_TOKEN=<token>" > .env

# Build custom models
ollama create gemma4-coder -f Modelfile
ollama create qwen3-coder-agent -f Modelfile.qwen3

# Start server
python server.py

# Void IDE: add MCP server at http://localhost:3001/mcp
```

### Requirements
- Python 3.11+
- Ollama with `gemma4:26b` and `qwen3-coder:30b` pulled
- Git, Node/npm, Docker (for respective tool groups)
- Infisical CLI (`scoop install infisical`)
- ripgrep (`scoop install ripgrep`) — optional, improves search_files
