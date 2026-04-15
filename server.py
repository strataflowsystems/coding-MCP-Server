"""
OpenHands Host MCP Server
Exposes Windows shell execution tools to OpenHands running in Docker.
Listens on 0.0.0.0:3001 — Docker reaches it at http://host.docker.internal:3001/mcp
"""

import difflib
import json
import os
import re
import subprocess
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# Load .env from the server directory (never committed to git)
_ENV_FILE = Path(__file__).parent / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

mcp = FastMCP(
    "host-shell",
    host="0.0.0.0",
    port=3001,
    stateless_http=True,
)

# ─── Output helpers ──────────────────────────────────────────────────────────

MAX_OUTPUT = 40_000  # ~10K tokens — prevents context window overflow

def _ok(data: str, truncated: bool = False) -> dict:
    raw = str(data)
    if len(raw) > MAX_OUTPUT:
        return {"ok": True, "data": raw[:MAX_OUTPUT], "truncated": True, "error": None}
    return {"ok": True, "data": raw, "truncated": truncated, "error": None}

def _err(msg: str) -> dict:
    return {"ok": False, "data": None, "truncated": False, "error": str(msg)}


# ─── Shell helpers ────────────────────────────────────────────────────────────

def _run(cmd: str, shell_type: str = "powershell", cwd: str | None = None, timeout: int = 60) -> dict:
    if cwd and not os.path.isdir(cwd):
        return {"success": False, "error": f"Directory not found: {cwd}", "stdout": "", "stderr": ""}

    if shell_type == "cmd":
        args = ["cmd", "/c", cmd]
    else:
        args = ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd]

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            cwd=cwd or os.path.expanduser("~"),
            timeout=timeout,
        )
        return {
            "success": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"Timed out after {timeout}s", "stdout": "", "stderr": ""}
    except Exception as e:
        return {"success": False, "error": str(e), "stdout": "", "stderr": ""}


def _run_direct(args: list[str], cwd: str | None = None, timeout: int = 60) -> dict:
    """Run a process directly (no shell wrapper). Used for git, npm, etc."""
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            cwd=cwd or os.path.expanduser("~"),
            timeout=timeout,
        )
        return {
            "success": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"Timed out after {timeout}s", "stdout": "", "stderr": ""}
    except FileNotFoundError as e:
        return {"success": False, "error": f"Command not found: {args[0]}", "stdout": "", "stderr": ""}
    except Exception as e:
        return {"success": False, "error": str(e), "stdout": "", "stderr": ""}


def _shell_ok(result: dict) -> dict:
    combined = ""
    if result.get("stdout"):
        combined += result["stdout"]
    if result.get("stderr"):
        combined += ("\n" if combined else "") + result["stderr"]
    if result.get("error"):
        return _err(result["error"])
    if not result.get("success"):
        return _err(combined or f"exit code {result.get('returncode', '?')}")
    return _ok(combined or "OK")


# ═══════════════════════════════════════════════════════════════
# PHASE 1 — Shell Tools (upgraded return format)
# ═══════════════════════════════════════════════════════════════

@mcp.tool()
def run_powershell(command: str, cwd: str = "", timeout: int = 60) -> dict:
    """Run a PowerShell command on the Windows host. Returns structured result."""
    return _shell_ok(_run(command, shell_type="powershell", cwd=cwd or None, timeout=timeout))


@mcp.tool()
def run_cmd(command: str, cwd: str = "", timeout: int = 60) -> dict:
    """Run a Windows CMD command on the host. Returns structured result."""
    return _shell_ok(_run(command, shell_type="cmd", cwd=cwd or None, timeout=timeout))


@mcp.tool()
def read_file(path: str) -> dict:
    """Read a file from the Windows host filesystem."""
    try:
        content = Path(path).read_text(encoding="utf-8", errors="replace")
        return _ok(content)
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def write_file(path: str, content: str) -> dict:
    """Write content to a file on the Windows host filesystem. Creates parent dirs."""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return _ok(f"Written {len(content)} chars to {path}")
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def list_dir(path: str = "") -> dict:
    """List directory contents on the Windows host."""
    try:
        p = Path(path) if path else Path.home()
        entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        lines = []
        for e in entries:
            prefix = "[DIR] " if e.is_dir() else "      "
            lines.append(f"{prefix}{e.name}")
        return _ok("\n".join(lines) or "(empty)")
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def launch_app(app: str, path: str = "") -> dict:
    """Launch any Windows application or open a file/folder in Explorer.
    Examples: launch_app('explorer'), launch_app('explorer', 'C:/Users/lauri/Desktop'),
    launch_app('notepad', 'C:/file.txt'), launch_app('code', 'C:/myproject').
    Use this whenever the user asks to open an app, folder, or file."""
    clean_path = path.replace("/", "\\").strip() if path else ""
    if clean_path:
        ps_cmd = f'Start-Process "{app}" -ArgumentList \'"{clean_path}"\''
    else:
        ps_cmd = f'Start-Process "{app}"'
    result = _run(ps_cmd, shell_type="powershell")
    if result.get("success") or result.get("returncode", 1) == 0:
        label = f"{app} {clean_path}".strip()
        return _ok(f"SUCCESS. {label} is now open. Task complete. Do not retry.")
    return _shell_ok(result)


@mcp.tool()
def get_env(var: str = "") -> dict:
    """Get a Windows environment variable. Leave var empty to list all."""
    if var:
        return _ok(os.environ.get(var, f"(not set: {var})"))
    return _ok("\n".join(f"{k}={v}" for k, v in sorted(os.environ.items())))


# ═══════════════════════════════════════════════════════════════
# PHASE 1 — Code Search & Precise Edit
# ═══════════════════════════════════════════════════════════════

@mcp.tool()
def search_files(pattern: str, path: str, file_glob: str = "*") -> dict:
    """Search file contents with regex. Returns file:line:match format. Uses ripgrep if available, falls back to findstr."""
    try:
        # Try ripgrep first (much faster)
        result = _run_direct(
            ["rg", "--line-number", "--no-heading", "--with-filename",
             "-g", file_glob, pattern, path],
            timeout=30,
        )
        if result["success"] or result.get("returncode", 1) == 1:
            # rg returns 1 for no matches (not an error)
            out = result["stdout"] or "(no matches)"
            return _ok(out)
        # rg not found — fall back to PowerShell Select-String
        ps_cmd = f'Get-ChildItem -Path "{path}" -Recurse -Filter "{file_glob}" | Select-String -Pattern "{pattern}" | Select-Object -First 500 | Format-List'
        result2 = _run(ps_cmd, shell_type="powershell", timeout=30)
        return _shell_ok(result2)
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def read_file_range(path: str, start_line: int, end_line: int) -> dict:
    """Read a specific line range from a file. Lines are 1-indexed. Use count_file_lines first to know valid range."""
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        total = len(lines)
        if start_line < 1 or start_line > total:
            return _err(f"start_line {start_line} out of range (file has {total} lines)")
        chunk = lines[start_line - 1:end_line]
        return _ok("".join(chunk))
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def replace_in_file(path: str, old_str: str, new_str: str) -> dict:
    """Replace an exact string in a file. Fails if old_str not found or matches multiple locations. Prefer this over write_file for targeted edits."""
    try:
        content = Path(path).read_text(encoding="utf-8", errors="replace")
        count = content.count(old_str)
        if count == 0:
            return _err(f"String not found in {path}")
        if count > 1:
            return _err(f"String matches {count} locations in {path} — provide more surrounding context to make it unique")
        Path(path).write_text(content.replace(old_str, new_str, 1), encoding="utf-8")
        return _ok(f"Replaced 1 occurrence in {path}")
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def tree(path: str, max_depth: int = 3) -> dict:
    """Return an indented directory tree. Skips node_modules, .git, __pycache__, .next, dist."""
    SKIP = {"node_modules", ".git", "__pycache__", ".next", "dist", ".venv", "venv", ".mypy_cache"}

    def _walk(p: Path, depth: int, prefix: str) -> list[str]:
        if depth > max_depth:
            return []
        lines = []
        try:
            entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        except PermissionError:
            return [f"{prefix}[permission denied]"]
        for i, e in enumerate(entries):
            if e.name in SKIP:
                continue
            connector = "└── " if i == len(entries) - 1 else "├── "
            lines.append(f"{prefix}{connector}{e.name}{'/' if e.is_dir() else ''}")
            if e.is_dir():
                extension = "    " if i == len(entries) - 1 else "│   "
                lines.extend(_walk(e, depth + 1, prefix + extension))
        return lines

    try:
        p = Path(path)
        lines = [str(p)] + _walk(p, 1, "")
        return _ok("\n".join(lines))
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def get_project_context(path: str) -> dict:
    """Auto-detect project framework, entry points, scripts, and key dependencies from package.json, pyproject.toml, or requirements.txt."""
    try:
        p = Path(path)
        result = {}

        pkg_json = p / "package.json"
        if pkg_json.exists():
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
            result["language"] = "JavaScript/TypeScript"
            result["name"] = data.get("name", "")
            result["version"] = data.get("version", "")
            result["scripts"] = data.get("scripts", {})
            deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
            # Detect framework
            if "next" in deps:
                result["framework"] = "Next.js"
            elif "react" in deps:
                result["framework"] = "React"
            elif "express" in deps:
                result["framework"] = "Express"
            elif "fastify" in deps:
                result["framework"] = "Fastify"
            else:
                result["framework"] = "Node.js"
            result["key_dependencies"] = list(deps.keys())[:20]
            return _ok(json.dumps(result, indent=2))

        pyproject = p / "pyproject.toml"
        req_txt = p / "requirements.txt"
        if pyproject.exists() or req_txt.exists():
            result["language"] = "Python"
            if req_txt.exists():
                reqs = req_txt.read_text(encoding="utf-8").splitlines()
                result["dependencies"] = [r for r in reqs if r and not r.startswith("#")][:20]
            if pyproject.exists():
                content = pyproject.read_text(encoding="utf-8")
                if "fastapi" in content.lower():
                    result["framework"] = "FastAPI"
                elif "flask" in content.lower():
                    result["framework"] = "Flask"
                elif "django" in content.lower():
                    result["framework"] = "Django"
            return _ok(json.dumps(result, indent=2))

        return _err(f"No recognisable project manifest found in {path}")
    except Exception as e:
        return _err(str(e))


# ═══════════════════════════════════════════════════════════════
# PHASE 8 — Code Intelligence
# ═══════════════════════════════════════════════════════════════

# Language-specific patterns for function/class extraction
_OUTLINE_PATTERNS = {
    ".py": [
        (r"^(class)\s+(\w+)", "class"),
        (r"^(    def|def)\s+(\w+)", "function"),
    ],
    ".ts": [
        (r"^export\s+(default\s+)?(class)\s+(\w+)", "class"),
        (r"^(class)\s+(\w+)", "class"),
        (r"^export\s+(async\s+)?(function)\s+(\w+)", "function"),
        (r"^(async\s+)?(function)\s+(\w+)", "function"),
        (r"^export\s+(const|let)\s+(\w+)\s*=\s*(async\s*)?\(", "const-fn"),
        (r"^\s{2}(async\s+)?(\w+)\s*\(", "method"),
    ],
    ".tsx": [
        (r"^export\s+(default\s+)?(function)\s+(\w+)", "component"),
        (r"^(function)\s+(\w+)", "function"),
        (r"^export\s+(const)\s+(\w+)\s*=", "const"),
    ],
    ".js": [
        (r"^(class)\s+(\w+)", "class"),
        (r"^(function)\s+(\w+)", "function"),
        (r"^(const|let|var)\s+(\w+)\s*=\s*(async\s*)?\(", "const-fn"),
    ],
}


@mcp.tool()
def get_file_outline(path: str) -> dict:
    """Extract all function/class definitions with line numbers. Much faster than reading the whole file. Use this first, then read_file_range for specific functions."""
    try:
        ext = Path(path).suffix.lower()
        patterns = _OUTLINE_PATTERNS.get(ext, [
            (r"^(class|function|def)\s+(\w+)", "definition"),
        ])

        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
        results = []
        for i, line in enumerate(lines, 1):
            for pat, kind in patterns:
                m = re.match(pat, line)
                if m:
                    # Extract name from last named group
                    name = m.group(m.lastindex) if m.lastindex else line.strip()[:40]
                    results.append({"line": i, "type": kind, "name": name, "text": line.rstrip()[:80]})
                    break

        if not results:
            return _ok(f"(no definitions found in {path} — {len(lines)} lines total)")
        lines_out = [f"L{r['line']:>5}  {r['type']:<12} {r['name']}" for r in results]
        return _ok(f"{path} ({len(lines)} lines)\n" + "\n".join(lines_out))
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def diff_files(path_a: str, path_b: str) -> dict:
    """Return a unified diff between two files."""
    try:
        lines_a = Path(path_a).read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        lines_b = Path(path_b).read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        diff = list(difflib.unified_diff(lines_a, lines_b, fromfile=path_a, tofile=path_b))
        if not diff:
            return _ok("(files are identical)")
        return _ok("".join(diff))
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def count_file_lines(path: str) -> dict:
    """Count the number of lines in a file. Use before read_file_range to know the valid range."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            count = sum(1 for _ in f)
        size_kb = Path(path).stat().st_size // 1024
        return _ok(f"{count} lines ({size_kb} KB)")
    except Exception as e:
        return _err(str(e))


# ═══════════════════════════════════════════════════════════════
# PHASE 2 — Git Tools
# ═══════════════════════════════════════════════════════════════

@mcp.tool()
def git_status(path: str) -> dict:
    """Show working tree status (git status --short)."""
    return _shell_ok(_run_direct(["git", "status", "--short"], cwd=path))


@mcp.tool()
def git_diff(path: str, staged: bool = False) -> dict:
    """Show unstaged changes (or staged if staged=True)."""
    args = ["git", "diff"]
    if staged:
        args.append("--staged")
    return _shell_ok(_run_direct(args, cwd=path))


@mcp.tool()
def git_log(path: str, n: int = 10) -> dict:
    """Show recent commits (git log --oneline)."""
    return _shell_ok(_run_direct(["git", "log", "--oneline", f"-{n}"], cwd=path))


@mcp.tool()
def git_add(path: str, files: list[str]) -> dict:
    """Stage files for commit. Pass ['.'] to stage all."""
    return _shell_ok(_run_direct(["git", "add"] + files, cwd=path))


@mcp.tool()
def git_commit(path: str, message: str) -> dict:
    """Commit staged changes."""
    return _shell_ok(_run_direct(["git", "commit", "-m", message], cwd=path))


@mcp.tool()
def git_push(path: str, remote: str = "origin", branch: str = "") -> dict:
    """Push commits to remote. Warning: destructive on shared branches."""
    args = ["git", "push", remote]
    if branch:
        args.append(branch)
    result = _shell_ok(_run_direct(args, cwd=path))
    if result.get("ok"):
        result["warning"] = "Pushed to remote — ensure this was intentional on shared branches"
    return result


@mcp.tool()
def git_pull(path: str) -> dict:
    """Pull latest changes from remote."""
    return _shell_ok(_run_direct(["git", "pull"], cwd=path))


@mcp.tool()
def git_checkout(path: str, branch: str) -> dict:
    """Switch to a branch (git checkout / git switch)."""
    return _shell_ok(_run_direct(["git", "checkout", branch], cwd=path))


@mcp.tool()
def git_create_branch(path: str, name: str) -> dict:
    """Create and switch to a new branch."""
    return _shell_ok(_run_direct(["git", "checkout", "-b", name], cwd=path))


@mcp.tool()
def git_clone(url: str, dest: str) -> dict:
    """Clone a git repository."""
    return _shell_ok(_run_direct(["git", "clone", url, dest], timeout=120))


# ═══════════════════════════════════════════════════════════════
# PHASE 5 — Network & Validation
# ═══════════════════════════════════════════════════════════════

@mcp.tool()
def http_request(url: str, method: str = "GET", headers: dict | None = None, body: str = "", timeout: int = 10) -> dict:
    """Make an HTTP request. Returns status code, headers summary, and body."""
    try:
        import urllib.request
        import urllib.error
        req = urllib.request.Request(
            url,
            method=method.upper(),
            headers=headers or {},
            data=body.encode() if body else None,
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            resp_body = resp.read(MAX_OUTPUT).decode("utf-8", errors="replace")
            return _ok(f"HTTP {status}\n\n{resp_body}")
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def check_port(host: str, port: int, timeout: int = 3) -> dict:
    """Check if a TCP port is open. Useful for verifying a service started."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return _ok(f"{host}:{port} is open")
    except (ConnectionRefusedError, OSError) as e:
        return _err(f"{host}:{port} is not reachable: {e}")


@mcp.tool()
def download_file(url: str, dest_path: str) -> dict:
    """Download a file from a URL to a local path."""
    try:
        import urllib.request
        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(url, dest_path)
        size_kb = Path(dest_path).stat().st_size // 1024
        return _ok(f"Downloaded to {dest_path} ({size_kb} KB)")
    except Exception as e:
        return _err(str(e))


# ═══════════════════════════════════════════════════════════════
# PHASE 9 — Structured Data Tools
# ═══════════════════════════════════════════════════════════════

def _get_nested(data: dict, key_path: str):
    """Navigate dot-path like 'scripts.build' into nested dict."""
    keys = key_path.lstrip(".").split(".")
    for k in keys:
        if isinstance(data, dict) and k in data:
            data = data[k]
        else:
            return None
    return data


def _set_nested(data: dict, key_path: str, value) -> dict:
    """Set a value at dot-path like 'scripts.build' in a nested dict."""
    keys = key_path.lstrip(".").split(".")
    d = data
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    d[keys[-1]] = value
    return data


@mcp.tool()
def read_json(path: str, key_path: str = "") -> dict:
    """Parse a JSON file. Optionally filter to a sub-key using dot-path (e.g. 'scripts' or 'dependencies.react')."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if key_path:
            data = _get_nested(data, key_path)
            if data is None:
                return _err(f"Key path '{key_path}' not found in {path}")
        return _ok(json.dumps(data, indent=2))
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def write_json(path: str, data: str) -> dict:
    """Write a JSON string to a file with 2-space indent formatting. Validates JSON before writing."""
    try:
        parsed = json.loads(data)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(parsed, indent=2), encoding="utf-8")
        return _ok(f"Written to {path}")
    except json.JSONDecodeError as e:
        return _err(f"Invalid JSON: {e}")
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def set_json_key(path: str, key_path: str, value: str) -> dict:
    """Set a key in a JSON file by dot-path (e.g. 'scripts.build'). Value is JSON-parsed. All other keys preserved."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        try:
            parsed_value = json.loads(value)
        except json.JSONDecodeError:
            parsed_value = value  # treat as plain string if not valid JSON
        _set_nested(data, key_path, parsed_value)
        Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")
        return _ok(f"Set {key_path} in {path}")
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def read_yaml(path: str, key_path: str = "") -> dict:
    """Parse a YAML file and return as formatted JSON string. Optionally filter by dot-path key."""
    try:
        import yaml
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if key_path:
            data = _get_nested(data, key_path)
            if data is None:
                return _err(f"Key path '{key_path}' not found in {path}")
        return _ok(json.dumps(data, indent=2, default=str))
    except ImportError:
        return _err("pyyaml not installed — run: pip install pyyaml")
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def set_yaml_key(path: str, key_path: str, value: str) -> dict:
    """Set a key in a YAML file by dot-path. All other keys preserved. Value is JSON-parsed."""
    try:
        import yaml
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        try:
            parsed_value = json.loads(value)
        except json.JSONDecodeError:
            parsed_value = value
        _set_nested(data, key_path, parsed_value)
        Path(path).write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")
        return _ok(f"Set {key_path} in {path}")
    except ImportError:
        return _err("pyyaml not installed — run: pip install pyyaml")
    except Exception as e:
        return _err(str(e))


# ═══════════════════════════════════════════════════════════════
# PHASE 3 — Runtime & Quality Tools
# ═══════════════════════════════════════════════════════════════

@mcp.tool()
def npm_install(path: str) -> dict:
    """Run npm install in a directory."""
    return _shell_ok(_run_direct(["npm", "install"], cwd=path, timeout=180))


@mcp.tool()
def npm_run(path: str, script: str) -> dict:
    """Run an npm script (e.g. 'build', 'dev', 'lint')."""
    return _shell_ok(_run_direct(["npm", "run", script], cwd=path, timeout=300))


@mcp.tool()
def npx(path: str, command: str) -> dict:
    """Run an npx command in a directory."""
    return _shell_ok(_run(f"npx {command}", shell_type="cmd", cwd=path, timeout=120))


@mcp.tool()
def pip_install(packages: list[str], cwd: str = "") -> dict:
    """Install Python packages with pip."""
    return _shell_ok(_run_direct(
        ["pip", "install"] + packages,
        cwd=cwd or None,
        timeout=120,
    ))


@mcp.tool()
def run_python(script_path: str, args: list[str] | None = None, cwd: str = "") -> dict:
    """Run a Python script."""
    cmd = ["python", script_path] + (args or [])
    return _shell_ok(_run_direct(cmd, cwd=cwd or str(Path(script_path).parent), timeout=120))


@mcp.tool()
def run_tests(path: str, pattern: str = "") -> dict:
    """Run tests. Auto-detects pytest (Python) or npm test (Node). Returns pass/fail and output."""
    try:
        p = Path(path)
        # Detect project type
        if (p / "package.json").exists():
            args = ["npm", "test", "--", "--watchAll=false"]
            if pattern:
                args += ["--testPathPattern", pattern]
            return _shell_ok(_run_direct(args, cwd=path, timeout=300))
        else:
            args = ["python", "-m", "pytest", "-v"]
            if pattern:
                args += ["-k", pattern]
            return _shell_ok(_run_direct(args, cwd=path, timeout=300))
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def lint_file(path: str) -> dict:
    """Lint a file. Auto-detects ESLint (.ts/.js/.tsx) or flake8 (.py)."""
    ext = Path(path).suffix.lower()
    if ext in {".ts", ".tsx", ".js", ".jsx"}:
        return _shell_ok(_run_direct(["npx", "eslint", path], timeout=30))
    elif ext == ".py":
        return _shell_ok(_run_direct(["python", "-m", "flake8", path], timeout=30))
    return _err(f"No linter configured for {ext} files")


@mcp.tool()
def format_file(path: str) -> dict:
    """Format a file in-place. Uses Prettier (.ts/.js/.tsx/.json) or Black (.py)."""
    ext = Path(path).suffix.lower()
    if ext in {".ts", ".tsx", ".js", ".jsx", ".json", ".css"}:
        return _shell_ok(_run_direct(["npx", "prettier", "--write", path], timeout=30))
    elif ext == ".py":
        return _shell_ok(_run_direct(["python", "-m", "black", path], timeout=30))
    return _err(f"No formatter configured for {ext} files")


# ═══════════════════════════════════════════════════════════════
# PHASE 4 — Docker Tools
# ═══════════════════════════════════════════════════════════════

@mcp.tool()
def docker_ps() -> dict:
    """List running Docker containers."""
    return _shell_ok(_run_direct(["docker", "ps", "--format", "table {{.ID}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}\t{{.Names}}"]))


@mcp.tool()
def docker_build(path: str, tag: str) -> dict:
    """Build a Docker image from a Dockerfile."""
    return _shell_ok(_run_direct(["docker", "build", "-t", tag, "."], cwd=path, timeout=600))


@mcp.tool()
def docker_run(image: str, ports: dict | None = None, env: dict | None = None, name: str = "", detach: bool = True) -> dict:
    """Run a Docker container. ports: {'3000': '3000'}, env: {'KEY': 'val'}."""
    args = ["docker", "run"]
    if detach:
        args.append("-d")
    if name:
        args += ["--name", name]
    for host_port, container_port in (ports or {}).items():
        args += ["-p", f"{host_port}:{container_port}"]
    for k, v in (env or {}).items():
        args += ["-e", f"{k}={v}"]
    args.append(image)
    return _shell_ok(_run_direct(args, timeout=30))


@mcp.tool()
def docker_stop(container: str) -> dict:
    """Stop a running container."""
    return _shell_ok(_run_direct(["docker", "stop", container], timeout=30))


@mcp.tool()
def docker_remove(container: str, force: bool = False) -> dict:
    """Remove a container."""
    args = ["docker", "rm"]
    if force:
        args.append("-f")
    args.append(container)
    result = _shell_ok(_run_direct(args, timeout=30))
    if result.get("ok") and force:
        result["warning"] = "Force-removed container — ensure this was intentional"
    return result


@mcp.tool()
def docker_logs(container: str, tail: int = 100) -> dict:
    """Get container logs."""
    return _shell_ok(_run_direct(["docker", "logs", "--tail", str(tail), container], timeout=15))


@mcp.tool()
def docker_compose_up(path: str, detach: bool = True) -> dict:
    """Start services defined in docker-compose.yml."""
    args = ["docker", "compose", "up"]
    if detach:
        args.append("-d")
    return _shell_ok(_run_direct(args, cwd=path, timeout=300))


@mcp.tool()
def docker_compose_down(path: str) -> dict:
    """Stop and remove services defined in docker-compose.yml."""
    return _shell_ok(_run_direct(["docker", "compose", "down"], cwd=path, timeout=60))


# ═══════════════════════════════════════════════════════════════
# PHASE 10 — Database Tools
# ═══════════════════════════════════════════════════════════════

_WRITE_KEYWORDS = re.compile(
    r"^\s*(INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE|REPLACE)\b",
    re.IGNORECASE,
)


@mcp.tool()
def sqlite_query(db_path: str, query: str, allow_write: bool = False) -> dict:
    """Run a SQL query against a SQLite database. Read-only by default — set allow_write=True for INSERT/UPDATE/DELETE."""
    import sqlite3
    try:
        if not allow_write and _WRITE_KEYWORDS.match(query):
            return _err("Write queries blocked by default. Pass allow_write=True to enable INSERT/UPDATE/DELETE/etc.")
        conn = sqlite3.connect(db_path)
        cursor = conn.execute(query)
        if cursor.description:
            cols = [d[0] for d in cursor.description]
            rows = cursor.fetchmany(500)
            conn.close()
            return _ok(json.dumps({"columns": cols, "rows": rows, "row_count": len(rows)}, indent=2, default=str))
        conn.commit()
        conn.close()
        return _ok(f"Query executed. Rows affected: {cursor.rowcount}")
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def sqlite_schema(db_path: str) -> dict:
    """Return all table definitions in a SQLite database."""
    import sqlite3
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        conn.close()
        if not rows:
            return _ok("(no tables found)")
        return _ok("\n\n".join(f"-- {name}\n{sql}" for name, sql in rows if sql))
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def postgres_query(connection_env_var: str, query: str) -> dict:
    """Run a read-only SQL query against Postgres. Pass the name of an env var holding the connection string (never the raw string)."""
    try:
        import psycopg
    except ImportError:
        return _err("psycopg not installed — run: pip install 'psycopg[binary]'")
    try:
        if _WRITE_KEYWORDS.match(query):
            return _err("Write queries not permitted via postgres_query. Use a dedicated migration tool.")
        conn_str = os.environ.get(connection_env_var)
        if not conn_str:
            return _err(f"Environment variable '{connection_env_var}' is not set")
        with psycopg.connect(conn_str) as conn:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute(query)
                if cur.description:
                    cols = [d[0] for d in cur.description]
                    rows = cur.fetchmany(500)
                    return _ok(json.dumps({"columns": cols, "rows": rows, "row_count": len(rows)}, indent=2, default=str))
                return _ok("(no rows returned)")
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def postgres_schema(connection_env_var: str) -> dict:
    """Return table names and column definitions from Postgres information_schema."""
    try:
        import psycopg
    except ImportError:
        return _err("psycopg not installed — run: pip install 'psycopg[binary]'")
    try:
        conn_str = os.environ.get(connection_env_var)
        if not conn_str:
            return _err(f"Environment variable '{connection_env_var}' is not set")
        query = """
            SELECT table_name, column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'public'
            ORDER BY table_name, ordinal_position
        """
        with psycopg.connect(conn_str) as conn:
            with conn.cursor() as cur:
                cur.execute(query)
                rows = cur.fetchall()
        if not rows:
            return _ok("(no tables in public schema)")
        current_table = None
        lines = []
        for table, col, dtype, nullable in rows:
            if table != current_table:
                if current_table:
                    lines.append("")
                lines.append(f"TABLE {table}:")
                current_table = table
            null_str = "NULL" if nullable == "YES" else "NOT NULL"
            lines.append(f"  {col:<30} {dtype:<20} {null_str}")
        return _ok("\n".join(lines))
    except Exception as e:
        return _err(str(e))


# ═══════════════════════════════════════════════════════════════
# Self-awareness — lets the model query its actual capabilities
# ═══════════════════════════════════════════════════════════════

_TOOL_REGISTRY: dict[str, str] = {
    # Shell
    "run_powershell":        "Run a PowerShell command on the Windows host",
    "run_cmd":               "Run a Windows CMD command on the host",
    "launch_app":            "Launch any Windows app or open a folder in Explorer",
    # Filesystem
    "read_file":             "Read an entire file",
    "read_file_range":       "Read a specific line range from a file",
    "write_file":            "Write/overwrite a file (use replace_in_file for edits)",
    "replace_in_file":       "Replace an exact string in a file — safer than write_file",
    "list_dir":              "List directory contents",
    "tree":                  "Show indented directory tree (skips node_modules/.git)",
    "count_file_lines":      "Count lines in a file before using read_file_range",
    "get_file_outline":      "Extract all function/class definitions with line numbers",
    "diff_files":            "Unified diff between two files",
    # Search
    "search_files":          "Search file contents with regex (uses ripgrep)",
    # Project
    "get_project_context":   "Detect framework, scripts, dependencies from project manifest",
    "get_env":               "Read environment variables",
    # Git
    "git_status":            "git status",
    "git_diff":              "git diff (staged or unstaged)",
    "git_log":               "git log --oneline",
    "git_add":               "git add <files>",
    "git_commit":            "git commit -m",
    "git_push":              "git push",
    "git_pull":              "git pull",
    "git_checkout":          "git checkout / switch branch",
    "git_create_branch":     "git checkout -b",
    "git_clone":             "git clone",
    # npm / Node
    "npm_install":           "npm install",
    "npm_run":               "npm run <script>",
    "npx":                   "npx <command>",
    # Python
    "pip_install":           "pip install packages",
    "run_python":            "Run a Python script",
    # Quality
    "run_tests":             "Run tests (npm test / pytest — auto-detected)",
    "lint_file":             "Lint a file (eslint / flake8 — auto-detected)",
    "format_file":           "Format a file (prettier / black — auto-detected)",
    # Docker
    "docker_ps":             "List running containers",
    "docker_build":          "docker build",
    "docker_run":            "docker run",
    "docker_stop":           "docker stop",
    "docker_remove":         "docker rm",
    "docker_logs":           "docker logs",
    "docker_compose_up":     "docker-compose up",
    "docker_compose_down":   "docker-compose down",
    # Network
    "http_request":          "HTTP GET/POST/etc to any URL",
    "check_port":            "TCP connect test — verify a service is up",
    "download_file":         "Download a file from a URL",
    # Structured data
    "read_json":             "Parse a JSON file, optionally filter by key path",
    "write_json":            "Write formatted JSON to a file",
    "set_json_key":          "Set a key in a JSON file by dot-path",
    "read_yaml":             "Parse a YAML file and return as JSON",
    "set_yaml_key":          "Set a key in a YAML file by dot-path",
    # Databases
    "sqlite_query":          "Run a read-only SQL query on a SQLite database",
    "sqlite_schema":         "Show all table definitions in a SQLite database",
    "postgres_query":        "Run a read-only SQL query on Postgres",
    "postgres_schema":       "Show table/column definitions from Postgres",
    # Safety
    "check_command_safety":  "Validate a shell command before running it",
    "sandbox_info":          "Show sandbox root and allowed executables",
    # Task state
    "task_create":           "Create a tracked task with optional step list",
    "task_update":           "Update a step status within a task",
    "task_complete":         "Mark a task as done/failed/cancelled",
    "task_get":              "Get full state of a task by ID",
    "task_list":             "List all tasks with status and progress",
    "task_checkpoint":       "Save a recovery blob to a task",
    "task_add_note":         "Append a free-text note to a task log",
    # Routing
    "get_tools_for_task":    "Get a focused list of tools for a task type (git/npm/docker/etc)",
    "list_tools":            "List all available tools with descriptions (you are reading this now)",
    # Infisical
    "infisical_status":        "Check Infisical CLI login status — run first to confirm auth",
    "infisical_list_secrets":  "List secret names in a project/environment (no values by default)",
    "infisical_get_secret":    "Get a specific secret value by name from Infisical",
    "infisical_search_secrets":"Search secrets by name pattern — returns names only, not values",
    "infisical_export_env":    "Export all secrets as .env / JSON / YAML for a project/environment",
}


@mcp.tool()
def list_tools(group: str = "") -> dict:
    """List all tools available on this MCP server with short descriptions.
    Optionally pass a group name to filter (same groups as get_tools_for_task).
    Call this when asked what tools you have — never guess from training data."""
    if group and group in _TOOL_GROUPS:
        names = _TOOL_GROUPS[group]
        result = {name: _TOOL_REGISTRY.get(name, "") for name in names}
    else:
        result = _TOOL_REGISTRY
    lines = [f"  {name:<28} {desc}" for name, desc in result.items()]
    return _ok(f"{len(result)} tools:\n" + "\n".join(lines))


# ═══════════════════════════════════════════════════════════════
# Tool routing — helps the model focus on the right subset
# ═══════════════════════════════════════════════════════════════

_TOOL_GROUPS: dict[str, list[str]] = {
    "filesystem": [
        "read_file", "read_file_range", "write_file", "replace_in_file",
        "list_dir", "tree", "count_file_lines", "get_file_outline", "diff_files",
    ],
    "search": [
        "search_files", "get_file_outline", "count_file_lines",
    ],
    "git": [
        "git_status", "git_diff", "git_log", "git_add", "git_commit",
        "git_push", "git_pull", "git_checkout", "git_create_branch", "git_clone",
    ],
    "npm": [
        "npm_install", "npm_run", "npx", "run_tests", "lint_file", "format_file",
    ],
    "python": [
        "pip_install", "run_python", "run_tests", "lint_file", "format_file",
    ],
    "docker": [
        "docker_ps", "docker_build", "docker_run", "docker_stop",
        "docker_remove", "docker_logs", "docker_compose_up", "docker_compose_down",
    ],
    "data": [
        "read_json", "write_json", "set_json_key", "read_yaml", "set_yaml_key",
        "sqlite_query", "sqlite_schema", "postgres_query", "postgres_schema",
    ],
    "network": [
        "http_request", "check_port", "download_file",
    ],
    "shell": [
        "run_powershell", "run_cmd", "get_env", "check_command_safety",
    ],
    "project": [
        "get_project_context", "tree", "search_files", "git_status",
    ],
    "tasks": [
        "task_create", "task_update", "task_complete", "task_get",
        "task_list", "task_checkpoint", "task_add_note",
    ],
    "secrets": [
        "infisical_status", "infisical_list_secrets",
        "infisical_get_secret", "infisical_search_secrets", "infisical_export_env",
    ],
    "servers": [
        "infisical_status", "infisical_search_secrets", "infisical_get_secret",
        "infisical_export_env", "run_powershell", "run_cmd", "check_port", "http_request",
    ],
}


@mcp.tool()
def get_tools_for_task(task_type: str) -> dict:
    """Return the most relevant tool names for a given task type.
    Valid types: filesystem, search, git, npm, python, docker, data, network, shell, project, tasks.
    Call this first if unsure which tools to use — it keeps context usage low."""
    task_type = task_type.lower().strip()
    if task_type not in _TOOL_GROUPS:
        available = ", ".join(sorted(_TOOL_GROUPS.keys()))
        return _err(f"Unknown task type '{task_type}'. Available: {available}")
    tools = _TOOL_GROUPS[task_type]
    return _ok(json.dumps({"task_type": task_type, "tools": tools}, indent=2))


# ═══════════════════════════════════════════════════════════════
# PHASE 6 — Safety Hardening
# ═══════════════════════════════════════════════════════════════

SANDBOX_ROOT = r"C:\ai-workspace"

ALLOWED_EXECUTABLES = {
    "npm", "npx", "node", "git", "docker", "docker-compose",
    "python", "python3", "pip", "pip3", "rg", "grep",
    "powershell", "cmd", "where", "echo",
}

# Shell metacharacters that could chain or redirect commands unsafely
_DANGEROUS_PATTERNS = re.compile(
    r"(?:"
    r"\bstart\s+/b\b"         # background process spawning
    r"|&&|\|\||[;&]"          # command chaining
    r"|\$\(|`"                # command substitution
    r"|>>\s*\S|2>&1"          # output redirection (allow simple > via write_file instead)
    r"|Remove-Item\s+-Recurse\s+-Force"   # PowerShell recursive delete
    r"|rm\s+-rf"              # Unix recursive delete
    r"|format\s+[A-Za-z]:"   # disk format
    r"|reg\s+(add|delete)"    # registry modification
    r"|net\s+(user|localgroup)" # user account changes
    r")",
    re.IGNORECASE,
)

_DANGEROUS_GIT_REFS = re.compile(r"\b(main|master|HEAD)\b", re.IGNORECASE)


def _validate_shell_command(command: str) -> str | None:
    """Return an error string if the command looks unsafe, None if OK."""
    if _DANGEROUS_PATTERNS.search(command):
        return f"Command blocked: contains disallowed pattern. Use targeted tools instead."
    return None


def _warn_if_outside_sandbox(path: str) -> str | None:
    """Return a warning string if path is outside SANDBOX_ROOT."""
    try:
        resolved = str(Path(path).resolve())
        sandbox = str(Path(SANDBOX_ROOT).resolve())
        if not resolved.lower().startswith(sandbox.lower()):
            return f"[WARNING] Path '{path}' is outside sandbox root '{SANDBOX_ROOT}'"
    except Exception:
        pass
    return None


@mcp.tool()
def check_command_safety(command: str) -> dict:
    """Validate whether a shell command would be allowed. Use before run_powershell/run_cmd."""
    err = _validate_shell_command(command)
    if err:
        return _err(err)
    return _ok("Command appears safe.")


@mcp.tool()
def sandbox_info() -> dict:
    """Return the current sandbox configuration — allowed executables and root path."""
    info = {
        "sandbox_root": SANDBOX_ROOT,
        "sandbox_exists": os.path.isdir(SANDBOX_ROOT),
        "allowed_executables": sorted(ALLOWED_EXECUTABLES),
        "note": "run_powershell/run_cmd do NOT auto-enforce the sandbox. Use check_command_safety first.",
    }
    return _ok(json.dumps(info, indent=2))


# ═══════════════════════════════════════════════════════════════
# PHASE 7 — Task State
# ═══════════════════════════════════════════════════════════════

import uuid
import datetime

TASK_STATE_DIR = os.path.join(SANDBOX_ROOT, ".task-state")


def _task_path(task_id: str) -> Path:
    return Path(TASK_STATE_DIR) / f"{task_id}.json"


def _load_task(task_id: str) -> dict | None:
    p = _task_path(task_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_task(task: dict) -> None:
    p = _task_path(task["task_id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(task, indent=2, default=str), encoding="utf-8")


@mcp.tool()
def task_create(description: str, steps: list[str] | None = None) -> dict:
    """Create a new task with optional step list. Returns task_id for tracking."""
    task_id = str(uuid.uuid4())[:8]
    now = datetime.datetime.utcnow().isoformat()
    task = {
        "task_id": task_id,
        "description": description,
        "status": "in_progress",
        "created_at": now,
        "updated_at": now,
        "steps": [{"name": s, "status": "pending"} for s in (steps or [])],
        "checkpoints": [],
        "notes": [],
    }
    _save_task(task)
    return _ok(json.dumps({"task_id": task_id, "message": f"Task created: {description}"}, indent=2))


@mcp.tool()
def task_update(task_id: str, step: str, status: str, note: str = "") -> dict:
    """Update a step's status in a task. Status: pending | in_progress | done | failed."""
    task = _load_task(task_id)
    if not task:
        return _err(f"Task '{task_id}' not found")
    valid_statuses = {"pending", "in_progress", "done", "failed", "skipped"}
    if status not in valid_statuses:
        return _err(f"Invalid status '{status}'. Must be one of: {', '.join(valid_statuses)}")
    updated = False
    for s in task["steps"]:
        if s["name"] == step:
            s["status"] = status
            if note:
                s["note"] = note
            updated = True
            break
    if not updated:
        # Add new step if not found
        task["steps"].append({"name": step, "status": status, "note": note})
    task["updated_at"] = datetime.datetime.utcnow().isoformat()
    # Auto-complete task if all steps are done/failed/skipped
    if task["steps"] and all(s["status"] in {"done", "failed", "skipped"} for s in task["steps"]):
        failed = any(s["status"] == "failed" for s in task["steps"])
        task["status"] = "failed" if failed else "done"
    _save_task(task)
    return _ok(f"Step '{step}' updated to '{status}'")


@mcp.tool()
def task_complete(task_id: str, status: str = "done", summary: str = "") -> dict:
    """Mark a task as done or failed. Status: done | failed | cancelled."""
    task = _load_task(task_id)
    if not task:
        return _err(f"Task '{task_id}' not found")
    task["status"] = status
    task["updated_at"] = datetime.datetime.utcnow().isoformat()
    if summary:
        task["summary"] = summary
    _save_task(task)
    return _ok(f"Task '{task_id}' marked as '{status}'")


@mcp.tool()
def task_get(task_id: str) -> dict:
    """Get full state of a task by ID."""
    task = _load_task(task_id)
    if not task:
        return _err(f"Task '{task_id}' not found")
    return _ok(json.dumps(task, indent=2))


@mcp.tool()
def task_list(status_filter: str = "") -> dict:
    """List all tasks with their current status. Optional status_filter: in_progress | done | failed."""
    state_dir = Path(TASK_STATE_DIR)
    if not state_dir.exists():
        return _ok("(no tasks yet)")
    tasks = []
    for f in sorted(state_dir.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            t = json.loads(f.read_text(encoding="utf-8"))
            if status_filter and t.get("status") != status_filter:
                continue
            done = sum(1 for s in t.get("steps", []) if s["status"] == "done")
            total = len(t.get("steps", []))
            tasks.append({
                "task_id": t["task_id"],
                "status": t["status"],
                "description": t["description"],
                "progress": f"{done}/{total} steps" if total else "no steps",
                "updated_at": t.get("updated_at", ""),
            })
        except Exception:
            continue
    if not tasks:
        return _ok("(no matching tasks)")
    return _ok(json.dumps(tasks, indent=2))


@mcp.tool()
def task_checkpoint(task_id: str, label: str, data: dict) -> dict:
    """Save a checkpoint blob to a task. Use to preserve state mid-task for recovery."""
    task = _load_task(task_id)
    if not task:
        return _err(f"Task '{task_id}' not found")
    checkpoint = {
        "label": label,
        "saved_at": datetime.datetime.utcnow().isoformat(),
        "data": data,
    }
    task.setdefault("checkpoints", []).append(checkpoint)
    task["updated_at"] = datetime.datetime.utcnow().isoformat()
    _save_task(task)
    return _ok(f"Checkpoint '{label}' saved to task '{task_id}'")


@mcp.tool()
def task_add_note(task_id: str, note: str) -> dict:
    """Append a free-text note to a task's log."""
    task = _load_task(task_id)
    if not task:
        return _err(f"Task '{task_id}' not found")
    task.setdefault("notes", []).append({
        "text": note,
        "at": datetime.datetime.utcnow().isoformat(),
    })
    task["updated_at"] = datetime.datetime.utcnow().isoformat()
    _save_task(task)
    return _ok(f"Note added to task '{task_id}'")


# ═══════════════════════════════════════════════════════════════
# Infisical — secrets management
# ═══════════════════════════════════════════════════════════════

def _infisical(*args, extra_env: dict | None = None) -> dict:
    """Run the infisical CLI and return structured result."""
    cmd = ["infisical"] + list(args)
    env = os.environ.copy()
    # Inject machine identity token if available
    token = os.environ.get("INFISICAL_TOKEN")
    if token:
        env["INFISICAL_TOKEN"] = token
    if extra_env:
        env.update(extra_env)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, env=env
        )
        combined = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
        if result.returncode != 0:
            return _err(combined.strip() or f"infisical exit {result.returncode}")
        return _ok(combined.strip())
    except FileNotFoundError:
        return _err("infisical CLI not found — install from https://infisical.com/docs/cli/overview")
    except subprocess.TimeoutExpired:
        return _err("infisical CLI timed out after 30s")
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def infisical_status() -> dict:
    """Check Infisical auth status. Always call this before any other infisical tool.
    Authentication is pre-configured via machine identity token — do NOT attempt
    to login, use Client ID/Secret, or call infisical login. Just use the tools."""
    token = os.environ.get("INFISICAL_TOKEN", "")
    if not token:
        return _err("INFISICAL_TOKEN not set in .env — add it to C:\\Users\\lauri\\openhands-host-mcp\\.env")
    # Verify token works by doing a lightweight API call
    result = _infisical("--version")
    return _ok(
        f"Auth: READY (machine identity token loaded, {len(token)} chars)\n"
        f"CLI: {result.get('data', '').strip()}\n"
        f"Usage: call infisical_list_secrets(project_id, environment) to fetch secrets.\n"
        f"Do NOT attempt manual login or use Client ID/Secret — token auth is automatic."
    )


@mcp.tool()
def infisical_list_secrets(
    project_id: str,
    environment: str = "dev",
    path: str = "/",
) -> dict:
    """List all secret NAMES (no values) in an Infisical project/environment.
    environment: dev | staging | prod (or custom slug).
    Call this before infisical_get_secret to find the exact secret name.
    project_id: find yours at app.infisical.com → Project Settings → Project ID."""
    result = _infisical(
        "secrets",
        "--projectId", project_id,
        "--env", environment,
        "--path", path,
        "-o", "json",
    )
    if not result.get("ok"):
        return result
    try:
        secrets = json.loads(result["data"])
        names = [s.get("secretKey", "") for s in secrets]
        return _ok("\n".join(names) if names else "(no secrets found)")
    except Exception:
        return result  # return raw if JSON parse fails


@mcp.tool()
def infisical_get_secret(
    secret_name: str,
    project_id: str,
    environment: str = "dev",
    path: str = "/",
) -> dict:
    """Retrieve a specific secret value from Infisical by name.
    Use infisical_list_secrets or infisical_search_secrets first to find the exact name.
    Returns the secret value — handle with care, do not log unnecessarily."""
    result = _infisical(
        "secrets", "get", secret_name,
        "--projectId", project_id,
        "--env", environment,
        "--path", path,
        "-o", "json",
    )
    if not result.get("ok"):
        return result
    try:
        data = json.loads(result["data"])
        if isinstance(data, list) and data:
            return _ok(data[0].get("secretValue", ""))
        return _ok(str(data))
    except Exception:
        return result


@mcp.tool()
def infisical_search_secrets(
    pattern: str,
    project_id: str,
    environment: str = "dev",
    path: str = "/",
) -> dict:
    """Search for secrets whose names match a pattern (case-insensitive substring).
    Returns matching secret NAMES only — call infisical_get_secret for the value.
    Examples: search 'ssh' to find SSH keys, 'db' for database passwords,
    'vm-prod' for a specific server's credentials."""
    result = _infisical(
        "secrets",
        "--projectId", project_id,
        "--env", environment,
        "--path", path,
        "-o", "json",
    )
    if not result.get("ok"):
        return result
    try:
        secrets = json.loads(result["data"])
        pattern_lower = pattern.lower()
        matches = [s["secretKey"] for s in secrets if pattern_lower in s.get("secretKey", "").lower()]
        if not matches:
            return _ok(f"No secrets matching '{pattern}' in {environment}{path}")
        return _ok("\n".join(matches))
    except Exception:
        return result


@mcp.tool()
def infisical_export_env(
    project_id: str,
    environment: str = "dev",
    path: str = "/",
    format: str = "dotenv",
) -> dict:
    """Export all secrets as a formatted block for use in scripts or server config.
    format options: dotenv | json | csv
    Useful when connecting to a VM/server that needs multiple credentials at once."""
    return _infisical(
        "export",
        "--projectId", project_id,
        "-e", environment,
        "--path", path,
        "-f", format,
    )


# ═══════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    tool_count = len([k for k, v in globals().items() if callable(v) and hasattr(v, "_mcp_tool")])
    print("Starting OpenHands Host MCP Server")
    print("  Local:  http://localhost:3001/mcp")
    print("  Docker: http://host.docker.internal:3001/mcp")
    mcp.run(transport="streamable-http")
