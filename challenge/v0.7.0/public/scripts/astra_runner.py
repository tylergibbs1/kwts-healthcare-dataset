"""Isolated Codex Astra proposal transport; separate, unmatched provider row."""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time

from build_challenge import ROOT, fingerprint, load_json, private_path, write_json
from episode_session import EpisodeSession, decode_proposal, proposal_schema, proposal_content
from legacy_v1_2 import utc_now
from run_repeated import case_orders, durable_row, per_repetition_results, validate_freeze
from validate_dataset import read_jsonl, validate_directory

MODEL = "gpt-6-astra"
HARNESS = "kwts-codex-subagent-tools-v1"
CLI = "/opt/homebrew/bin/codex"
CLI_VERSION = "0.161.0"
TIMEOUT_SECONDS = 180
DISABLED = ("shell_tool", "unified_exec", "multi_agent", "multi_agent_v2", "hooks", "apps", "plugins",
            "remote_plugin", "code_mode", "code_mode_host", "enable_mcp_apps", "computer_use", "browser_use",
            "browser_use_external", "in_app_browser", "view_image", "image_generation", "memories", "skill_search",
            "skill_mcp_dependency_install", "goals", "sleep_tool", "workspace_dependencies", "tool_suggest",
            "request_permissions_tool", "unbounded_connection_retries", "shell_snapshot")
INTERFACE = {"model": MODEL, "harness": HARNESS, "cli_version": CLI_VERSION,
             "max_proposals": 8, "max_actions": 12, "timeout_seconds_per_proposal": TIMEOUT_SECONDS,
             "output_token_cap": "2048 post-response reported-output rejection; no generation cap",
             "resolved_model_identity": "unavailable; requested model only",
             "native_response_count": "unavailable; eight host proposal batches",
             "comparison": "unmatched Codex scaffold and transport; not native API parity"}
PROMPT = ("You are a fresh isolated benchmark participant. Use only the supplied conversation and tool definitions. "
          "Return one proposal batch; do not call any native tools. The host executes your calls only after your whole "
          "response is received. A read result in this batch cannot inform another call in this batch. "
          "Return calls_json as a JSON-encoded array of objects with exactly name and arguments. "
          "Do not supply reasoning text. This is a fresh process for each proposal; the complete supplied conversation "
          "is your only episode history. Respect the remaining response and action budgets.\n")


def command(schema):
    cmd = [CLI, "exec", "--model", MODEL, "--sandbox", "read-only", "--ephemeral", "--ignore-user-config",
           "--ignore-rules", "--json", "--skip-git-repo-check", "--config", 'approval_policy="never"',
           "--config", "project_doc_max_bytes=0", "--config", 'web_search="disabled"',
           "--config", "memories.generate_memories=false", "--config", "memories.use_memories=false",
           "--config", "suppress_unstable_features_warning=true", "--enable", "skip_host_skill_discovery"]
    for feature in DISABLED:
        cmd.extend(["--disable", feature])
    return cmd + ["--output-schema", str(schema), "-"]


class TransportFailure(RuntimeError):
    def __init__(self, failure, response=None, usage=None):
        super().__init__(failure)
        self.response = response or {"content": []}
        self.usage = usage or {}


def parse_events(stdout, process_failure=None):
    failure = process_failure
    messages, usage, completed, rejected_content = [], {}, 0, []
    turn_started = False
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            failure = failure or "malformed_cli_events"
            continue
        if not isinstance(event, dict):
            failure = failure or "malformed_cli_events"
            continue
        if event.get("model") not in (None, MODEL):
            failure = failure or "model_identity_mismatch"
        kind = event.get("type")
        if kind == "turn.started":
            turn_started = True
        if kind in {"turn.failed", "error"}:
            failure = failure or "cli_error"
        if kind in {"item.started", "item.updated", "item.completed"}:
            item = event.get("item", {})
            if not isinstance(item, dict):
                failure = failure or "malformed_cli_events"
                continue
            if (not turn_started and kind == "item.completed" and item.get("type") == "error"
                    and item.get("message") == "Code Mode is unavailable because code-mode host is disabled. Code mode will fail closed; enable `features.code_mode_host` and install `codex-code-mode-host`."):
                continue
            if item.get("type") not in {"agent_message", "reasoning"}:
                failure = failure or "out_of_protocol_native_item"
            if kind == "item.completed" and item.get("type") == "agent_message":
                text = item.get("text")
                messages.append(text)
                try:
                    envelope = json.loads(text)
                    if isinstance(envelope, dict) and isinstance(envelope.get("calls_json"), str):
                        rejected_content.extend(proposal_content({"calls": json.loads(envelope["calls_json"])}))
                except (ValueError, TypeError):
                    pass
        if kind == "turn.completed":
            completed += 1
            reported = event.get("usage")
            if not isinstance(reported, dict):
                failure = failure or "malformed_usage"
            else:
                for key, value in reported.items():
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        usage[key] = usage.get(key, 0) + value
    if completed != 1 or len(messages) != 1 or not isinstance(messages[0], str):
        failure = failure or "unexpected_cli_response_count"
    output = usage.get("output_tokens")
    if isinstance(output, bool) or not isinstance(output, int) or output < 0:
        failure = failure or "unavailable_output_usage"
    elif output > 2048:
        failure = failure or "output_token_limit"
    proposal = None
    if len(messages) == 1:
        try:
            proposal = decode_proposal(messages[0])
        except (ValueError, TypeError):
            failure = failure or "malformed_response"
    if failure:
        raise TransportFailure(failure, {"content": rejected_content}, usage)
    return proposal, usage


class CodexTransport:
    def __init__(self, journal):
        self.journal = journal

    def send(self, request, context):
        # No participant-readable path points at the dataset or host journals.
        # Read-only sandbox alone is not a filesystem confidentiality boundary;
        # native tools are disabled and unexpected native items fail the episode.
        with tempfile.TemporaryDirectory(prefix="kwts-astra-") as directory:
            cwd = Path(directory)
            schema = cwd / "proposal-schema.json"
            schema.write_text(json.dumps(proposal_schema(request["tools"])))
            schema.chmod(0o444)
            argv = command(schema)
            prompt = PROMPT + json.dumps(request, ensure_ascii=False)
            self.journal({"kind": "proposal_attempt", **context, "timestamp": utc_now(), "argv": argv,
                          "prompt": prompt, "executed_tool_actions": 0})
            # Preserve the existing login through ordinary CLI authentication;
            # never read, copy or export its credential files.
            env = {k: v for k, v in os.environ.items() if k in {"HOME", "PATH", "TMPDIR", "LANG", "LC_ALL", "CODEX_HOME"}}
            process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, start_new_session=True)
            started = time.perf_counter()
            failure = None
            try:
                stdout, stderr = process.communicate(prompt, timeout=TIMEOUT_SECONDS)
            except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate()
                failure = "timeout" if isinstance(error, subprocess.TimeoutExpired) else "interrupted"
            self.journal({"kind": "proposal_result", **context, "timestamp": utc_now(),
                          "returncode": process.returncode, "elapsed_seconds": round(time.perf_counter() - started, 6),
                          "stdout": stdout, "stderr": stderr, "failure": failure})
            if failure == "interrupted":
                raise KeyboardInterrupt
            return parse_events(stdout, failure or ("cli_process_failure" if process.returncode else None))


def run_episode(case, definitions, repetition, transport, event_sink=None):
    session = EpisodeSession(case, definitions, event_sink)
    record = {"case_id": case["case_id"], "repetition": repetition, "model": MODEL, "harness": HARNESS,
              "started_at": utc_now(), "events": session.episode.events, "model_responses": session.responses,
              "usage": {}, "resolved_model_identity": None}
    started = time.perf_counter()
    while session.status == "running":
        context = {"case_id": case["case_id"], "repetition": repetition, "model_turn": len(session.responses) + 1}
        try:
            proposal, usage = transport.send(session.request(), context)
            for key, value in usage.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    record["usage"][key] = record["usage"].get(key, 0) + value
            session.accept(proposal)
        except TransportFailure as error:
            for key, value in error.usage.items():
                record["usage"][key] = record["usage"].get(key, 0) + value
            session.reject(error.response, str(error))
        except (ValueError, OSError, RuntimeError):
            session.status = "host_or_transport_error"
    record.update(status=session.status, finished_at=utc_now(), elapsed_seconds=round(time.perf_counter() - started, 6),
                  trailing_tool_calls_ignored=session.trailing_tool_calls_ignored)
    return record


def check_freeze(root, dataset, freeze_path):
    # Reuse all dataset, public-code, publication and core-budget checks. Empty
    # native model list skips only the Anthropic identity requirement.
    freeze, hashes = validate_freeze(root, dataset, freeze_path, [])
    if freeze.get("astra_interface") != INTERFACE:
        raise ValueError("Freeze must explicitly approve the exact unmatched Astra interface")
    for name in ("scripts/astra_runner.py", "scripts/episode_session.py", "OPENAI-HARNESS.md"):
        if name not in freeze["public_runner_sha256"]:
            raise ValueError("Freeze must include all Astra interface bytes")
    protocol = load_json(root / "evaluation-protocol.json")
    provider = protocol.get("second_provider", {})
    if provider.get("model") != MODEL or provider.get("harness") != HARNESS:
        raise ValueError("Astra is outside the planned cohort")
    return freeze, hashes


def run(root, dataset, output, freeze_path):
    root = Path(root).resolve()
    dataset, output, freeze_path = [private_path(root, Path(p)) for p in (dataset, output, freeze_path)]
    freeze, hashes = check_freeze(root, dataset, freeze_path)
    validate_directory(dataset)
    cases = read_jsonl(dataset / "cases.jsonl")
    if len(cases) != 30:
        raise ValueError("Expected the fixed thirty-case cohort")
    version = subprocess.check_output([CLI, "--version"], text=True).strip()
    if version != f"codex-cli {CLI_VERSION}":
        raise ValueError("CLI version differs from frozen interface")
    definitions = load_json(dataset / "tool_contract.json")["tools"]
    orders = case_orders([case["case_id"] for case in cases])
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "config.json", {**INTERFACE, "provider": "OpenAI", "repetitions": 5, "case_orders": orders,
               "dataset_sha256": hashes, "public_runner_sha256": freeze["public_runner_sha256"],
               "freeze_sha256": fingerprint(freeze_path)["sha256"], "started_at": utc_now(),
               "sampling": "temperature/reasoning omitted, provider defaults; seed unavailable", "retries": 0})
    case_map = {c["case_id"]: c for c in cases}
    records, interrupted = [], False
    with (output / "traces.jsonl").open("x") as traces, (output / "events.jsonl").open("x") as actions, (output / "transport.jsonl").open("x") as attempts:
        transport = CodexTransport(lambda row: durable_row(attempts, row))
        for rep, order in enumerate(orders, 1):
            for position, cid in enumerate(order, 1):
                current_events = {}
                def save_event(row):
                    durable_row(actions, {"repetition": rep, **row})
                    current_events[row["event"]["sequence"]] = deepcopy(row["event"])
                durable_row(attempts, {"kind": "episode_started", "case_id": cid, "repetition": rep, "timestamp": utc_now()})
                try:
                    record = run_episode(case_map[cid], definitions, rep, transport, save_event)
                except KeyboardInterrupt:
                    interrupted = True
                    record = {"case_id": cid, "repetition": rep, "model": MODEL, "harness": HARNESS,
                              "status": "interrupted", "events": list(current_events.values()), "model_responses": [], "usage": None}
                records.append(record)
                durable_row(traces, record)
                print(f"Astra repetition {rep}/5 episode {position}/30: {record['status']}", flush=True)
                if interrupted:
                    break
            if interrupted:
                break
    # Keys are loaded only by the host after participant execution has ended.
    answers, pairs = read_jsonl(dataset / "answers.jsonl"), read_jsonl(dataset / "pairs.jsonl")
    write_json(output / "results.json", {"model": MODEL, "harness": HARNESS, "planned_episodes": 150,
               "recorded_episodes": len(records), "run_status": "interrupted" if interrupted else "finished",
               "dataset_sha256": hashes, "per_repetition": per_repetition_results(cases, answers, pairs, records)})
    if interrupted:
        raise KeyboardInterrupt
    return {"model": MODEL, "harness": HARNESS, "recorded_episodes": len(records)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--freeze", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.root, args.dataset or args.root / "private/dataset", args.output,
        args.freeze or args.root / "private/pre-run-freeze.json")


if __name__ == "__main__":
    main()
