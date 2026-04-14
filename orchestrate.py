"""
Multi-Model Orchestrator
Claude calls this to coordinate local models on heavy coding tasks.

Each subtask runs as an isolated agent loop with a fresh context window,
dispatched to the right model based on task type. Results are passed
between steps as structured context so no single model holds everything.

Model assignments:
    planner  = qwen2.5:14b         Fast decomposition, cheap
    worker   = gemma4-coder        Tool use, git, simple edits, search
    coder    = qwen3-coder-agent   Complex refactoring, architecture, hard bugs
    reviewer = gemma4-coder        Verify output, run tests

Usage (Claude calls this via Bash):
    python orchestrate.py --task "refactor auth module in C:/ai-workspace/myapp"
    python orchestrate.py --task "..." --plan-only       # just show the plan
    python orchestrate.py --task "..." --model coder     # skip planner, use one model
    python orchestrate.py --resume <run-id>              # resume a failed run
"""

import argparse
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

# ─── Config ──────────────────────────────────────────────────────────────────

AGENT      = Path(__file__).parent / "agent.py"
RUNS_DIR   = Path(r"C:\ai-workspace\.orchestrator-runs")
RUNS_DIR.mkdir(parents=True, exist_ok=True)

MODELS = {
    "planner":  "qwen2.5:14b",
    "worker":   "gemma4-coder",
    "coder":    "qwen3-coder-agent",
    "reviewer": "gemma4-coder",
}

PLANNER_PROMPT = """\
You are a task planner. Break the following coding task into focused subtasks.
Each subtask must be self-contained and executable by a single AI agent.

For each subtask output a JSON array like:
[
  {{
    "id": 1,
    "description": "clear one-sentence task",
    "model": "worker|coder",
    "depends_on": []
  }},
  ...
]

Model guide:
- worker  = fast tool use, file search, orientation, git operations, simple edits
- coder   = complex logic, refactoring, architecture, writing new modules, hard bugs

Rules:
- Keep subtasks small and focused — one concern each
- Start with an orientation subtask (model: worker) to map the codebase
- Only use coder for subtasks that genuinely need deep reasoning
- Max 8 subtasks. Merge anything that can be done together.
- Output ONLY the JSON array, no other text.

Task: {task}
"""


# ─── Run agent subprocess ────────────────────────────────────────────────────

def run_agent(model: str, prompt: str, context: str = "", run_id: str = "") -> dict:
    """Invoke agent.py for a single subtask and return structured result."""
    out_file = RUNS_DIR / f"{run_id or 'tmp'}-{model.replace(':', '_')}-{uuid.uuid4().hex[:6]}.json"
    cmd = [sys.executable, str(AGENT), prompt, "--model", model, "--output", str(out_file)]
    if context:
        cmd += ["--context", context[:4000]]  # cap context passed between agents

    print(f"\n  [dispatch] {model}")
    print(f"  [task]     {prompt[:120]}{'...' if len(prompt) > 120 else ''}")

    try:
        proc = subprocess.run(cmd, capture_output=False, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout", "result": ""}

    if out_file.exists():
        data = json.loads(out_file.read_text(encoding="utf-8"))
        return {"ok": True, "result": data.get("result", ""), "model": model}

    return {"ok": proc.returncode == 0, "result": "", "error": "no output file"}


# ─── Planner ─────────────────────────────────────────────────────────────────

def plan_task(task: str, run_id: str) -> list[dict]:
    """Use qwen2.5:14b to decompose the task into subtasks."""
    print(f"\n[orchestrator] Planning task with {MODELS['planner']}...")
    out_file = RUNS_DIR / f"{run_id}-plan.json"
    result = run_agent(MODELS["planner"], PLANNER_PROMPT.format(task=task), run_id=run_id)

    raw = result.get("result", "")
    # Extract JSON array from response
    try:
        start = raw.index("[")
        end   = raw.rindex("]") + 1
        plan  = json.loads(raw[start:end])
        print(f"[orchestrator] Plan: {len(plan)} subtasks")
        for step in plan:
            print(f"  {step['id']}. [{step['model']}] {step['description']}")
        out_file.write_text(json.dumps(plan, indent=2), encoding="utf-8")
        return plan
    except Exception as e:
        print(f"[orchestrator] Could not parse plan: {e}\nRaw: {raw[:500]}")
        # Fallback: single coder task
        return [{"id": 1, "description": task, "model": "coder", "depends_on": []}]


# ─── Executor ────────────────────────────────────────────────────────────────

def execute_plan(plan: list[dict], run_id: str) -> dict:
    """Execute subtasks in dependency order, passing context between steps."""
    results = {}   # step_id -> result string
    failed  = []

    # Simple topological execution — process in order, respect depends_on
    remaining = list(plan)
    passes = 0
    while remaining and passes < len(plan) + 1:
        passes += 1
        for step in list(remaining):
            deps = step.get("depends_on", [])
            if any(d not in results for d in deps):
                continue  # dependency not ready yet

            # Build context from dependency results
            context = ""
            for dep_id in deps:
                if dep_id in results:
                    context += f"Step {dep_id} result:\n{results[dep_id]}\n\n"

            model_key = step.get("model", "worker")
            model     = MODELS.get(model_key, MODELS["coder"])
            result    = run_agent(model, step["description"], context=context, run_id=run_id)

            step_id = step["id"]
            results[step_id] = result.get("result", "")
            if not result.get("ok"):
                failed.append(step_id)
                print(f"  [FAILED] step {step_id}")
            else:
                print(f"  [DONE]   step {step_id}")

            remaining.remove(step)

    return {"results": results, "failed": failed}


# ─── Review ──────────────────────────────────────────────────────────────────

def review(task: str, results: dict, run_id: str) -> str:
    """Quick review pass to verify the work and run tests if relevant."""
    summary = "\n\n".join(
        f"Step {sid}:\n{res[:800]}" for sid, res in results.items()
    )
    review_prompt = (
        f"Review the following completed work for the task: {task}\n\n"
        f"What was done:\n{summary}\n\n"
        "Run any relevant tests. Check for obvious errors. "
        "Report PASS or FAIL with a brief summary."
    )
    print(f"\n[orchestrator] Reviewing with {MODELS['reviewer']}...")
    result = run_agent(MODELS["reviewer"], review_prompt, run_id=run_id)
    return result.get("result", "")


# ─── Save run state ───────────────────────────────────────────────────────────

def save_run(run_id: str, task: str, plan: list, execution: dict, review_result: str):
    state = {
        "run_id":    run_id,
        "task":      task,
        "timestamp": datetime.utcnow().isoformat(),
        "plan":      plan,
        "results":   {str(k): v for k, v in execution["results"].items()},
        "failed":    execution["failed"],
        "review":    review_result,
        "status":    "failed" if execution["failed"] else "done",
    }
    path = RUNS_DIR / f"{run_id}-summary.json"
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(f"\n[orchestrator] Run saved: {path}")
    return state


# ─── Entry point ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Multi-model coding orchestrator")
    parser.add_argument("--task",      "-t", help="High-level task description")
    parser.add_argument("--plan-only", "-p", action="store_true", help="Show plan without executing")
    parser.add_argument("--no-review", action="store_true", help="Skip review step")
    parser.add_argument("--model",     "-m", help="Skip planner, run single model (worker|coder|planner)")
    parser.add_argument("--resume",    "-r", help="Resume a previous run ID")
    args = parser.parse_args()

    if not args.task and not args.resume:
        parser.print_help()
        sys.exit(1)

    run_id = args.resume or f"run-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"
    task   = args.task or ""

    print(f"\n{'='*52}")
    print(f"  Orchestrator — Run {run_id}")
    print(f"  Task: {task[:80]}")
    print(f"{'='*52}")

    # Resume: load existing plan
    if args.resume:
        plan_file = RUNS_DIR / f"{run_id}-plan.json"
        if not plan_file.exists():
            print(f"[error] No plan found for run {run_id}")
            sys.exit(1)
        plan = json.loads(plan_file.read_text())
        print(f"[orchestrator] Resuming with {len(plan)} subtasks")
    elif args.model:
        # Skip planner — run as single model
        model  = MODELS.get(args.model, args.model)
        result = run_agent(model, task, run_id=run_id)
        print(f"\n[result]\n{result.get('result', '')}")
        return
    else:
        plan = plan_task(task, run_id)

    if args.plan_only:
        return

    execution     = execute_plan(plan, run_id)
    review_result = "" if args.no_review else review(task, execution["results"], run_id)
    state         = save_run(run_id, task, plan, execution, review_result)

    print(f"\n{'='*52}")
    print(f"  Status : {state['status'].upper()}")
    if execution["failed"]:
        print(f"  Failed : steps {execution['failed']}")
    print(f"  Review : {review_result[:300]}")
    print(f"{'='*52}\n")


if __name__ == "__main__":
    main()
