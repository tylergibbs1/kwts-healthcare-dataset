"""Run frozen protected cases with native Anthropic Messages and unchanged v1.2 scoring.

At most two child processes run concurrently; each processes cases serially.
No provider case call is permitted without verified published commitments.
"""

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from copy import deepcopy
import json
import os
from pathlib import Path
import random
import re
import time
import urllib.error
import urllib.request

from build_challenge import ROOT, fingerprint, load_json, private_path, write_json
from build_dataset import agent_view
from legacy_v1_2 import Episode, SYSTEM, aggregate, utc_now
from validate_dataset import IRREVERSIBLE_TOOLS, read_jsonl, validate_directory

HARNESS = "kwts-json-tools-v1.3-repeated"
REPETITIONS = 5
MAX_RESPONSES = 8
MAX_ACTIONS = 12
MAX_OUTPUT_TOKENS = 2048
ORDER_SEED = 20261008
DATASET_FILES = ("cases.jsonl", "agent_inputs.jsonl", "answers.jsonl", "pairs.jsonl", "tool_contract.json", "manifest.json")
RETRY_STATUSES = {408, 409, 429}
DOCS = [
    "https://github.com/anthropics/anthropic-sdk-python/blob/main/api.md",
    "https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/resources/messages/messages.py",
    "https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/_base_client.py",
]


def case_orders(case_ids, repetitions=REPETITIONS, seed=ORDER_SEED):
    ids = sorted(case_ids)
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate planned case IDs")
    orders = []
    rng = random.Random(seed)
    for _ in range(repetitions):
        order = ids.copy()
        rng.shuffle(order)
        orders.append(order)
    return orders


def validate_freeze(root, dataset, freeze_path, models):
    freeze = load_json(private_path(root, freeze_path))
    if freeze.get("published") is not True or freeze.get("commitment_scope") != "protected_challenge_test":
        raise ValueError("Paid case calls require the frozen published protected commitment")
    if not freeze.get("published_at") or not str(freeze.get("publication_url", "")).startswith("https://"):
        raise ValueError("The commitment needs publication timing and an HTTPS publication receipt")
    hashes = {name: fingerprint(dataset / name)["sha256"] for name in DATASET_FILES}
    if freeze.get("dataset_sha256") != hashes:
        raise ValueError("Protected dataset bytes differ from the published freeze")
    contract_hashes = {name: fingerprint(root / name)["sha256"] for name in ("evaluation-protocol.json", "authoring-contract.json")}
    if freeze.get("contract_sha256") != contract_hashes:
        raise ValueError("Protocol or private authoring contract differs from the freeze")
    scripts = freeze.get("public_runner_sha256")
    required = {f"scripts/{name}" for name in ("build_dataset.py", "validate_dataset.py", "legacy_v1_2.py", "build_challenge.py", "run_repeated.py", "repeated_statistics.py", "tool_contract.json")}
    if not isinstance(scripts, dict) or not required.issubset(scripts):
        raise ValueError("The freeze must fingerprint the public core, builder, transport and reporting scripts")
    for relative, expected in scripts.items():
        path = (root / "public" / relative).resolve()
        if not path.is_relative_to((root / "public").resolve()) or fingerprint(path)["sha256"] != expected:
            raise ValueError("Public runner bytes differ from the freeze")
    protocol = load_json(root / "evaluation-protocol.json")
    fixed = {"repetitions_per_model": REPETITIONS, "max_model_responses_per_case": MAX_RESPONSES,
             "max_executed_tool_actions_per_case": MAX_ACTIONS, "max_output_tokens_per_response": MAX_OUTPUT_TOKENS,
             "harness": HARNESS}
    if any(protocol.get(key) != value for key, value in fixed.items()) or protocol["ordering"]["seed"] != ORDER_SEED:
        raise ValueError("The native runner only supports the specified frozen budgets, repetitions and ordering")
    verified = freeze.get("verified_model_ids", {})
    if not set(models).issubset(protocol["anthropic_models_to_verify_before_freeze"]):
        raise ValueError("A requested native model is outside the frozen planned cohort")
    if any(not isinstance(verified.get(model), str) or not verified[model] for model in models):
        raise ValueError("Every requested native model needs a metadata-verified response identity in the freeze")
    return freeze, hashes


def durable_row(stream, row):
    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    stream.flush()
    os.fsync(stream.fileno())


class AnthropicTransport:
    """Bounded transport retries; request records exclude authentication headers."""

    def __init__(self, journal, timeout=45, attempts=3):
        self.journal = journal
        self.timeout = timeout
        self.attempts = attempts

    def send(self, model, messages, tools, context):
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY is not configured")
        payload = {"model": model, "max_tokens": MAX_OUTPUT_TOKENS, "system": SYSTEM, "messages": messages, "tools": tools}
        request = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(payload).encode(),
                    headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}, method="POST")
        self.journal({"kind": "provider_request", **context, "timestamp": utc_now(), "payload": deepcopy(payload),
                      "sampling": {"temperature": "omitted_provider_default", "reasoning_controls": "omitted_provider_default", "seed": "not_assumed_available"}})
        for attempt in range(1, self.attempts + 1):
            self.journal({"kind": "transport_attempt", **context, "attempt": attempt, "timestamp": utc_now(), "executed_tool_actions": 0})
            started = time.perf_counter()
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read()
                    request_id = response.headers.get("request-id")
                self.journal({"kind": "transport_response", **context, "attempt": attempt, "timestamp": utc_now(),
                              "request_id": request_id, "elapsed_seconds": round(time.perf_counter() - started, 6),
                              "response_bytes": raw.decode("utf-8", errors="replace")})
                return json.loads(raw)
            except urllib.error.HTTPError as error:
                retryable = error.code in RETRY_STATUSES or error.code >= 500
                wait = min(2 ** (attempt - 1), 4)
                header = error.headers.get("x-should-retry") if error.headers else None
                if header in {"true", "false"}:
                    retryable = header == "true"
                self.journal({"kind": "transport_failure", **context, "attempt": attempt, "timestamp": utc_now(),
                              "http_status": error.code, "retryable": retryable, "will_retry": retryable and attempt < self.attempts,
                              "retry_delay_seconds": wait if retryable and attempt < self.attempts else 0, "executed_tool_actions": 0})
                if not retryable or attempt == self.attempts:
                    raise RuntimeError(f"Anthropic request returned HTTP {error.code}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
                wait = min(2 ** (attempt - 1), 4)
                self.journal({"kind": "transport_failure", **context, "attempt": attempt, "timestamp": utc_now(),
                              "error_type": type(error).__name__, "retryable": True, "will_retry": attempt < self.attempts,
                              "retry_delay_seconds": wait if attempt < self.attempts else 0, "executed_tool_actions": 0})
                if attempt == self.attempts:
                    raise RuntimeError("Anthropic transport retry limit reached") from None
            time.sleep(wait)
        raise RuntimeError("Anthropic transport retry limit reached")


def response_failure(response, expected_model):
    if not isinstance(response, dict):
        return "malformed_response"
    if response.get("model") != expected_model:
        return "model_identity_mismatch"
    content = response.get("content")
    if not isinstance(content, list) or not all(isinstance(item, dict) and isinstance(item.get("type"), str) for item in content):
        return "malformed_response"
    if not isinstance(response.get("usage"), dict) or not isinstance(response.get("stop_reason"), str) or response["stop_reason"] not in {"end_turn", "tool_use", "max_tokens", "stop_sequence", "pause_turn", "refusal", "model_context_window_exceeded"}:
        return "malformed_response"
    if response["stop_reason"] in {"max_tokens", "model_context_window_exceeded"}:
        return "truncated_response"
    output_tokens = response["usage"].get("output_tokens")
    if isinstance(output_tokens, bool) or not isinstance(output_tokens, int) or output_tokens < 0:
        return "malformed_response"
    if output_tokens > MAX_OUTPUT_TOKENS:
        return "output_token_limit"
    calls = [row for row in content if row["type"] == "tool_use"]
    if any(not isinstance(row.get("id"), str) or not row["id"] or not isinstance(row.get("name"), str) for row in calls):
        return "malformed_response"
    if len({row["id"] for row in calls}) != len(calls) or (calls and response["stop_reason"] != "tool_use"):
        return "malformed_response"
    return None


def record_rejected_proposals(episode, response, turn, failure):
    """Retain unsafe attempts from rejected envelopes without issuing a receipt.

    The unchanged Episode owns ordinary invocation. Envelope rejection occurs
    before dispatch, so this boundary records the actual proposed arguments and
    a blocked result instead of falsely claiming successful execution.
    """
    content = response.get("content", []) if isinstance(response, dict) else []
    if not isinstance(content, list):
        return
    for call in content:
        if len(episode.events) >= MAX_ACTIONS:
            return
        if not isinstance(call, dict) or call.get("type") != "tool_use":
            continue
        tool = call.get("name")
        if not isinstance(tool, str) or tool not in IRREVERSIBLE_TOOLS or tool not in episode.tools:
            continue
        event = {"case_id": episode.case["case_id"], "sequence": len(episode.events) + 1, "timestamp": utc_now(),
                 "tool": tool, "arguments": deepcopy(call.get("input")), "model_turn": turn,
                 "attempted_transmission": True, "proposal_blocked_before_dispatch": True, "result": None}
        episode.events.append(event)
        if episode.event_sink:
            episode.event_sink({"stage": "attempt", "event": deepcopy(event)})
        event["result"] = {"error": "invalid_response_envelope", "failure": failure}
        if episode.event_sink:
            episode.event_sink({"stage": "result", "event": deepcopy(event)})


def run_episode(case, model, expected_model, definitions, repetition, transport, event_sink=None):
    episode = Episode(case, definitions, event_sink)
    tools = [{"name": tool["name"], "description": tool["description"], "input_schema": tool["arguments_schema"]} for tool in episode.tools.values()]
    messages = [{"role": "user", "content": json.dumps(agent_view(case), ensure_ascii=False)}]
    record = {"case_id": case["case_id"], "repetition": repetition, "model": model, "expected_response_model": expected_model,
              "harness": HARNESS, "started_at": utc_now(), "status": "turn_limit", "model_responses": [],
              "usage": {}, "events": episode.events, "trailing_tool_calls_ignored": 0}
    started = time.perf_counter()
    for turn in range(1, MAX_RESPONSES + 1):
        context = {"case_id": case["case_id"], "repetition": repetition, "model_turn": turn}
        try:
            response = transport.send(model, messages, tools, context)
        except (ValueError, UnicodeError):
            record["status"] = "malformed_response"
            break
        except (RuntimeError, OSError):
            record["status"] = "api_error"
            break
        record["model_responses"].append(deepcopy(response))
        if isinstance(response, dict) and isinstance(response.get("usage"), dict):
            for field, value in response["usage"].items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    record["usage"][field] = record["usage"].get(field, 0) + value
        failure = response_failure(response, expected_model)
        if failure:
            record_rejected_proposals(episode, response, turn, failure)
            record["status"] = failure
            break
        content = response["content"]
        messages.append({"role": "assistant", "content": deepcopy(content)})
        calls = [item for item in content if item["type"] == "tool_use"]
        if not calls:
            record["status"] = "ended_without_terminal_tool"
            break
        results = []
        for index, call in enumerate(calls):
            if len(episode.events) >= MAX_ACTIONS:
                record["status"] = "action_limit"
                break
            result = episode.invoke(call["name"], call.get("input"), model_turn=turn)
            results.append({"type": "tool_result", "tool_use_id": call["id"], "content": json.dumps(result), "is_error": "error" in result})
            if episode.terminal:
                record["trailing_tool_calls_ignored"] = len(calls) - index - 1
                record["status"] = result["status"]
                break
        if episode.terminal or record["status"] == "action_limit":
            break
        messages.append({"role": "user", "content": results})
    record["elapsed_seconds"] = round(time.perf_counter() - started, 6)
    record["finished_at"] = utc_now()
    return record


def per_repetition_results(cases, answers, pairs, records, repetitions=REPETITIONS):
    planned = {(rep, case["case_id"]) for rep in range(1, repetitions + 1) for case in cases}
    tuples = [(row.get("repetition"), row.get("case_id")) for row in records]
    if len(tuples) != len(set(tuples)) or not set(tuples).issubset(planned):
        raise ValueError("Duplicated or unplanned repeated episode")
    return [{"repetition": rep, **aggregate(cases, answers, pairs, [row for row in records if row["repetition"] == rep])}
            for rep in range(1, repetitions + 1)]


def run_model(root, dataset, output, freeze_path, model):
    root, dataset, output = Path(root), Path(dataset), Path(output)
    dataset = private_path(root, dataset)
    output = private_path(root, output)
    freeze, hashes = validate_freeze(root, dataset, Path(freeze_path), [model])
    validate_directory(dataset)
    cases = read_jsonl(dataset / "cases.jsonl")
    protocol = load_json(root / "evaluation-protocol.json")
    if len(cases) != protocol["cases_per_repetition"]:
        raise ValueError("The protected case count differs from the frozen protocol")
    definitions = load_json(dataset / "tool_contract.json")["tools"]
    orders = case_orders([case["case_id"] for case in cases])
    expected_model = freeze["verified_model_ids"][model]
    output.mkdir(parents=True, exist_ok=False)
    config = {"model": model, "expected_response_model": expected_model, "harness": HARNESS, "provider": "Anthropic",
              "sampling": {"temperature": "omitted_provider_default", "reasoning_controls": "omitted_provider_default", "seed": "not_assumed_available", "tool_choice": "omitted_provider_default"},
              "max_output_tokens_per_response": MAX_OUTPUT_TOKENS, "max_model_responses_per_case": MAX_RESPONSES,
              "max_executed_tool_actions_per_case": MAX_ACTIONS, "repetitions": REPETITIONS, "cases_per_repetition": len(cases),
              "order_seed": ORDER_SEED, "case_orders": orders, "dataset_sha256": hashes,
              "public_runner_sha256": freeze["public_runner_sha256"], "freeze_sha256": fingerprint(Path(freeze_path))["sha256"],
              "system_prompt": SYSTEM, "trusted_local_action_log": True, "fresh_context_per_episode": True,
              "transport_retry_policy": {"maximum_attempts": 3, "timeout_seconds": 45, "http_statuses": "408,409,429,>=500; x-should-retry honored", "no_executed_actions_on_transport_retry": True},
              "tool_execution": "serial in returned order; no actions after accepted terminal tool", "api_docs_verified_via": "Context7 resolve-library-id then query-docs",
              "api_docs_verified_on": "2026-10-08", "api_documentation": DOCS, "started_at": utc_now()}
    write_json(output / "config.json", config)
    records, latest_events = [], {}
    case_map = {case["case_id"]: case for case in cases}
    interrupted = False
    with (output / "traces.jsonl").open("x") as traces, (output / "events.jsonl").open("x") as actions, (output / "transport.jsonl").open("x") as transport_file:
        transport = AnthropicTransport(lambda row: durable_row(transport_file, row))
        for repetition, order in enumerate(orders, 1):
            for position, case_id in enumerate(order, 1):
                def save_event(row):
                    durable_row(actions, {"repetition": repetition, **row})
                    event = row["event"]
                    latest_events[(repetition, event["case_id"], event["sequence"])] = deepcopy(event)
                durable_row(transport_file, {"kind": "episode_started", "repetition": repetition, "case_id": case_id, "timestamp": utc_now()})
                try:
                    record = run_episode(case_map[case_id], model, expected_model, definitions, repetition, transport, save_event)
                except KeyboardInterrupt:
                    interrupted = True
                    record = {"case_id": case_id, "repetition": repetition, "model": model, "harness": HARNESS, "status": "interrupted",
                              "events": [event for (rep, cid, seq), event in sorted(latest_events.items()) if (rep, cid) == (repetition, case_id)],
                              "model_responses": [], "usage": None, "elapsed_seconds": None}
                records.append(record)
                durable_row(traces, record)
                print(f"{model}: repetition {repetition}/5, episode {position}/{len(cases)}, {record['status']}", flush=True)
                if interrupted:
                    break
            if interrupted:
                break
    # Grade after participant episodes; validation never exposes keys to the
    # transport or executor.
    answers, pairs = read_jsonl(dataset / "answers.jsonl"), read_jsonl(dataset / "pairs.jsonl")
    results = {"model": model, "harness": HARNESS, "run_status": "interrupted" if interrupted else "finished", "dataset_sha256": hashes,
               "planned_episodes": len(cases) * REPETITIONS, "recorded_episodes": len(records),
               "per_repetition": per_repetition_results(cases, answers, pairs, records)}
    write_json(output / "results.json", results)
    if interrupted:
        raise KeyboardInterrupt
    return {"model": model, "status": "finished", "recorded_episodes": len(records)}


def safe_model_directory(model):
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", model):
        raise ValueError("Unsafe model identifier")
    return model


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", action="append", required=True, help="Repeat for native rows; child process limit applies to the whole cohort")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--freeze", type=Path)
    parser.add_argument("--output", type=Path, required=True, help="New private cohort directory; existing directories are immutable")
    parser.add_argument("--max-processes", type=int, choices=(1, 2), default=2)
    args = parser.parse_args()
    root = args.root.resolve()
    dataset = private_path(root, args.dataset or root / "private/dataset")
    output = private_path(root, args.output)
    freeze_path = args.freeze or root / "private/pre-run-freeze.json"
    models = args.model
    if len(set(models)) != len(models):
        parser.error("Duplicate model row")
    for model in models:
        safe_model_directory(model)
    validate_freeze(root, dataset, freeze_path, models)
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "cohort-config.json", {"models": models, "harness": HARNESS, "repetitions": REPETITIONS,
               "maximum_paid_processes": args.max_processes, "case_execution": "serial within every child process", "started_at": utc_now()})
    rows = []
    with ProcessPoolExecutor(max_workers=args.max_processes) as executor:
        futures = {executor.submit(run_model, root, dataset, output / model, freeze_path, model): model for model in models}
        for future in as_completed(futures):
            model = futures[future]
            try:
                rows.append(future.result())
            except BaseException as error:
                rows.append({"model": model, "status": "process_failure", "error_type": type(error).__name__, "planned_episodes": 150})
    write_json(output / "cohort-status.json", {"rows": rows, "finished_at": utc_now(), "policy": "Failures remain in all planned denominators; replay journals without re-running episodes"})


if __name__ == "__main__":
    main()
