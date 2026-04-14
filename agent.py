"""
Gemma 4 Agent Loop
Connects Ollama (Gemma 4) to the MCP tool server and runs an autonomous
coding agent loop until Gemma stops calling tools or hits MAX_TURNS.

Usage:
    python agent.py "refactor the auth module in C:/ai-workspace/myapp"
    python agent.py --interactive     # REPL mode
"""

import argparse
import json
import sys
import textwrap
from typing import Any

import httpx

# ─── Config ──────────────────────────────────────────────────────────────────

OLLAMA_URL    = "http://localhost:11434"
MCP_URL       = "http://localhost:3001/mcp"
MODEL         = "gemma4-coder"       # use the tuned modelfile; fallback: gemma4:26b
MAX_TURNS     = 40                   # hard ceiling on tool-call rounds
MAX_NUDGES    = 3                    # max times we re-prompt Gemma if she narrates instead of acts
TIMEOUT       = 120                  # seconds per Ollama call

SYSTEM_PROMPT = textwrap.dedent("""\
    You are an autonomous coding agent. You act — you do not describe, explain, or ask for permission.

    CORE RULES:
    - Every response must either call a tool OR end with DONE or BLOCKED.
    - If you are about to write a sentence without calling a tool, stop and call a tool instead.
    - Never say "I would", "I could", "I will", "Let me" — just do it.
    - Never ask the user if they want you to proceed. Proceed.
    - Never summarise a plan before acting. Act first.

    WORKFLOW:
    1. Orient: call tree() or get_project_context() on the target path first.
    2. Locate: use search_files() before reading anything — never guess paths.
    3. Inspect: use read_file_range() + count_file_lines() on large files, not read_file().
    4. Edit: use replace_in_file() for changes, not write_file() (which overwrites).
    5. Verify: run tests or build after every change.
    6. Finish: when the task is fully complete say DONE and state what changed.
       If genuinely stuck say BLOCKED and state exactly why.

    You have tools for everything: filesystem, search, git, npm, docker, databases, HTTP.
    Use them without hesitation.

    TIP: call get_tools_for_task("git"|"npm"|"docker"|"data"|"search"|etc.) at the start
    of a task to get a focused list of relevant tools — keeps your context lean.
""")


# ─── MCP client ──────────────────────────────────────────────────────────────

MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}

def _mcp_post(payload: dict) -> dict:
    r = httpx.post(MCP_URL, json=payload, headers=MCP_HEADERS, timeout=60)
    r.raise_for_status()
    # Stateless HTTP may return SSE or plain JSON
    content_type = r.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        # Parse SSE: find the first data: line
        for line in r.text.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])
        return {}
    return r.json()


def fetch_tools() -> list[dict]:
    """Fetch tool schemas from the MCP server and convert to Ollama format."""
    resp = _mcp_post({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {},
    })
    tools = resp.get("result", {}).get("tools", [])
    # Convert MCP tool schema → Ollama function schema
    ollama_tools = []
    for t in tools:
        ollama_tools.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("inputSchema", {"type": "object", "properties": {}}),
            },
        })
    return ollama_tools


def call_tool(name: str, arguments: dict) -> str:
    """Execute a tool on the MCP server and return the result as a string."""
    try:
        resp = _mcp_post({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        result = resp.get("result", {})
        # MCP returns content as a list of {type, text} blocks
        content = result.get("content", [])
        if content:
            texts = [c.get("text", "") for c in content if c.get("type") == "text"]
            return "\n".join(texts) or json.dumps(result)
        return json.dumps(result)
    except Exception as e:
        return f"[tool error] {e}"


# ─── Ollama client ────────────────────────────────────────────────────────────

def chat(messages: list[dict], tools: list[dict]) -> dict:
    """Send a chat request to Ollama and return the message object."""
    payload = {
        "model": MODEL,
        "messages": messages,
        "tools": tools,
        "stream": False,
        "options": {
            "temperature": 0.2,
            "num_ctx": 32768,
        },
    }
    r = httpx.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json().get("message", {})


# ─── Agent loop ───────────────────────────────────────────────────────────────

def run(user_prompt: str, verbose: bool = True) -> str:
    print(f"\n[agent] Fetching tools from MCP server...")
    tools = fetch_tools()
    print(f"[agent] {len(tools)} tools available")

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": user_prompt},
    ]

    nudges = 0

    for turn in range(MAX_TURNS):
        print(f"\n[turn {turn + 1}/{MAX_TURNS}] Calling Gemma...")
        msg = chat(messages, tools)
        messages.append(msg)

        tool_calls = msg.get("tool_calls", [])
        content    = msg.get("content", "").strip()

        if not tool_calls:
            # Check for explicit completion signals
            upper = content.upper()
            if "DONE" in upper or "BLOCKED" in upper:
                print(f"\n[agent] {content}")
                return content

            # Gemma narrated instead of acting — nudge her back
            nudges += 1
            if nudges >= MAX_NUDGES:
                print(f"\n[agent] Gemma stopped acting after {nudges} nudges. Last response:\n{content}")
                return content

            nudge_msg = (
                "You haven't finished the task and you haven't called any tools. "
                "Do not explain — use the appropriate tools to continue right now."
            )
            print(f"  [nudge {nudges}/{MAX_NUDGES}] Gemma went text-only, re-prompting...")
            messages.append({"role": "user", "content": nudge_msg})
            continue

        nudges = 0  # reset nudge counter on any tool call

        # Execute each tool call
        for tc in tool_calls:
            fn   = tc.get("function", {})
            name = fn.get("name", "")
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}

            print(f"  >> {name}({_fmt_args(args)})")
            result = call_tool(name, args)
            truncated = result[:500] + "..." if len(result) > 500 else result
            safe = truncated.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(sys.stdout.encoding or "utf-8", errors="replace")
            print(f"     {safe}")

            messages.append({
                "role": "tool",
                "content": result,
            })

    return "[agent] MAX_TURNS reached — stopping."


def _fmt_args(args: dict) -> str:
    parts = []
    for k, v in args.items():
        v_str = repr(v) if not isinstance(v, str) else f'"{v[:60]}{"..." if len(v) > 60 else ""}"'
        parts.append(f"{k}={v_str}")
    return ", ".join(parts)


# ─── Entry points ─────────────────────────────────────────────────────────────

def interactive_loop():
    print("Gemma 4 Agent — interactive mode (type 'exit' to quit)\n")
    tools = fetch_tools()
    print(f"{len(tools)} tools loaded.\n")
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if user_input.lower() in ("exit", "quit"):
            break
        if not user_input:
            continue

        messages.append({"role": "user", "content": user_input})

        nudges = 0
        for turn in range(MAX_TURNS):
            msg = chat(messages, tools)
            messages.append(msg)
            tool_calls = msg.get("tool_calls", [])
            content    = msg.get("content", "").strip()

            if not tool_calls:
                upper = content.upper()
                if "DONE" in upper or "BLOCKED" in upper or nudges >= MAX_NUDGES:
                    print(f"\nGemma: {content}\n")
                    break
                nudges += 1
                print(f"  [nudge {nudges}/{MAX_NUDGES}]")
                messages.append({"role": "user", "content": "You haven't finished. Use tools to continue."})
                continue

            nudges = 0

            for tc in tool_calls:
                fn   = tc.get("function", {})
                name = fn.get("name", "")
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                print(f"  [tool] {name}({_fmt_args(args)})")
                result = call_tool(name, args)
                safe_r = result[:300].encode(sys.stdout.encoding or "utf-8", errors="replace").decode(sys.stdout.encoding or "utf-8", errors="replace")
                print(f"  [result] {safe_r}{'...' if len(result) > 300 else ''}")
                messages.append({"role": "tool", "content": result})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gemma 4 MCP Agent")
    parser.add_argument("prompt", nargs="?", help="Task prompt")
    parser.add_argument("--interactive", "-i", action="store_true", help="Interactive REPL mode")
    parser.add_argument("--model", default=MODEL, help=f"Ollama model (default: {MODEL})")
    args = parser.parse_args()

    MODEL = args.model  # type: ignore

    if args.interactive:
        interactive_loop()
    elif args.prompt:
        run(args.prompt)
    else:
        parser.print_help()
        sys.exit(1)
