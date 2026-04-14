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
TIMEOUT       = 120                  # seconds per Ollama call

SYSTEM_PROMPT = textwrap.dedent("""\
    You are an autonomous coding agent with access to a set of tools that let
    you read, search, and edit files; run shell commands; manage git; run builds
    and tests; and query databases.

    Rules:
    - ALWAYS use tools — never describe what you would do, just do it.
    - Start every task with tree() or get_project_context() to orient yourself.
    - Use search_files() before reading files — never guess file paths.
    - Use replace_in_file() for edits, not write_file() (which overwrites).
    - Use read_file_range() on large files — call count_file_lines() first.
    - After making changes, run the relevant tests or build to verify.
    - When the task is fully done, say DONE and summarise what you changed.
    - If you are stuck or blocked, say BLOCKED and explain why.
""")


# ─── MCP client ──────────────────────────────────────────────────────────────

def _mcp_post(payload: dict) -> dict:
    r = httpx.post(MCP_URL, json=payload, timeout=60)
    r.raise_for_status()
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

    for turn in range(MAX_TURNS):
        print(f"\n[turn {turn + 1}/{MAX_TURNS}] Calling Gemma...")
        msg = chat(messages, tools)
        messages.append(msg)

        tool_calls = msg.get("tool_calls", [])

        if not tool_calls:
            # Gemma finished — no more tool calls
            content = msg.get("content", "")
            print(f"\n[agent] Final response:\n{content}")
            return content

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

            print(f"  → {name}({_fmt_args(args)})")
            result = call_tool(name, args)
            truncated = result[:500] + "…" if len(result) > 500 else result
            print(f"    ← {truncated}")

            messages.append({
                "role": "tool",
                "content": result,
            })

    return "[agent] MAX_TURNS reached — stopping."


def _fmt_args(args: dict) -> str:
    parts = []
    for k, v in args.items():
        v_str = repr(v) if not isinstance(v, str) else f'"{v[:60]}{"…" if len(v) > 60 else ""}"'
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

        for turn in range(MAX_TURNS):
            msg = chat(messages, tools)
            messages.append(msg)
            tool_calls = msg.get("tool_calls", [])

            if not tool_calls:
                print(f"\nGemma: {msg.get('content', '')}\n")
                break

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
                print(f"  [result] {result[:300]}{'…' if len(result) > 300 else ''}")
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
