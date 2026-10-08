"""Verify all four complete runs and write a separate post-run summary."""

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import importlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
FREEZE_SHA256 = "364edd1ce730f7073f6ee69ecffb4026b2046664f53f6dbdc1171ee35c642909"
PUBLIC_COMMIT = "18fc69d6287337b4afadfb21c55a0e6c0c5a9f19"
RUN_FILES = ("results.json", "config.json", "traces.jsonl", "events.jsonl")
DATA_FILES = ("cases.jsonl", "answers.jsonl", "pairs.jsonl", "tool_contract.json")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def fingerprint(path):
    data = path.read_bytes()
    return {"sha256": sha256(data).hexdigest(), "bytes": len(data)}


def jsonl(data, label):
    rows = []
    for number, line in enumerate(data.decode("utf-8").splitlines(), 1):
        require(bool(line.strip()), f"{label}:{number}: blank record")
        row = json.loads(line)
        require(isinstance(row, dict), f"{label}:{number}: record must be an object")
        rows.append(row)
    return rows


def verify_freeze(root):
    require(fingerprint(root / "pre-run-freeze.json")["sha256"] == FREEZE_SHA256, "Pre-run freeze differs from the published commitment")
    freeze = json.loads((root / "pre-run-freeze.json").read_text())
    require(len(freeze["files"]) == 52, "Expected all 52 frozen input files")
    for relative, expected in freeze["files"].items():
        path = (root / relative).resolve()
        require(path.is_relative_to(root), f"Frozen path escapes release: {relative}")
        require(fingerprint(path) == expected, f"Frozen input changed: {relative}")
    require("scripts/summarize_runs.py" not in freeze["files"], "Reporting helper must be outside the pre-run commitment")
    protocol = json.loads((root / "evaluation-protocol.json").read_text())
    require(freeze["models"] == protocol["models"] and len(protocol["models"]) == 4, "Freeze/protocol model list differs")
    require(freeze["case_count"] == protocol["cases_per_model"] == 90, "Expected 90 fixed cases per model")
    require(fingerprint(root / "scripts/run_pilot.py")["sha256"] == freeze["harness_sha256"], "Frozen evaluator hash differs")
    return freeze, protocol


def numeric_usage(responses):
    total = Counter()
    for response in responses:
        for field, value in response.get("usage", {}).items():
            if isinstance(value, (int, float)):
                total[field] += value
    return dict(total)


def verify_episode(case, record, model, protocol, harness, definitions):
    case_id = case["case_id"]
    require(record["model"] == model and record["harness"] == protocol["harness"], f"{model}/{case_id}: requested model or harness differs")
    responses, events = record.get("model_responses", []), record["events"]
    require(len(responses) <= protocol["max_model_responses_per_case"], f"{model}/{case_id}: response cap exceeded")
    require(len(events) <= protocol["max_executed_tool_actions_per_case"], f"{model}/{case_id}: action cap exceeded")
    proposals = []
    for turn, response in enumerate(responses, 1):
        require(response["model"] == model, f"{model}/{case_id}: response model ID differs")
        # Inspect public tool-use proposals only; other content blocks remain opaque.
        calls = [block for block in response.get("content", []) if block.get("type") == "tool_use"]
        require(calls or turn == len(responses), f"{model}/{case_id}: response followed an episode-ending response")
        proposals.extend((turn, call) for call in calls)
    interrupted = record["status"] == "interrupted"
    require(record["status"] in {"complete", "escalated", "action_limit", "turn_limit", "ended_without_terminal_tool", "api_error", "interrupted"}, f"{model}/{case_id}: status is not emitted by the frozen runner")
    require(record.get("usage") is None if interrupted else record["usage"] == numeric_usage(responses), f"{model}/{case_id}: recorded usage differs from returned response usage")
    if not interrupted:
        require(len(proposals) >= len(events), f"{model}/{case_id}: executed action lacks a model proposal")
    episode = harness.Episode(case, definitions)
    journal = []
    previous_turn = 0
    for sequence, event in enumerate(events, 1):
        require(event["case_id"] == case_id and event["sequence"] == sequence, f"{model}/{case_id}: event sequence or case ID differs")
        turn = event["model_turn"]
        require(isinstance(turn, int) and not isinstance(turn, bool) and previous_turn <= turn <= protocol["max_model_responses_per_case"] and turn > 0, f"{model}/{case_id}: invalid event turn")
        previous_turn = turn
        if not interrupted:
            proposal_turn, proposal = proposals[sequence - 1]
            require((turn, event["tool"], event["arguments"]) == (proposal_turn, proposal.get("name"), proposal.get("input")), f"{model}/{case_id}: event differs from its tool-use proposal")
        attempt = deepcopy(event)
        attempt["result"] = None
        journal.append({"stage": "attempt", "event": attempt})
        if event["result"] is None:
            require(interrupted and sequence == len(events), f"{model}/{case_id}: unexplained unfinished tool attempt")
            definition = episode.tools.get(event["tool"])
            require(event["attempted_transmission"] == bool(definition and definition["irreversible"]), f"{model}/{case_id}: unfinished attempt boundary flag differs")
            continue
        result = episode.invoke(event["tool"], event["arguments"], model_turn=turn)
        require(result == event["result"] and episode.events[-1]["attempted_transmission"] == event["attempted_transmission"], f"{model}/{case_id}: tool result or transmission flag differs from the frozen executor")
        journal.append({"stage": "result", "event": event})
    if not interrupted:
        ignored = len(proposals) - len(events)
        terminal = (events[-1]["result"] or {}).get("status") if events else None
        if record["status"] in {"complete", "escalated"}:
            require(terminal == record["status"], f"{model}/{case_id}: terminal record lacks its accepted terminal action")
        if terminal in {"complete", "escalated"}:
            require(record["status"] == terminal and events[-1]["model_turn"] == len(responses), f"{model}/{case_id}: terminal status/response differs")
            require(record["trailing_tool_calls_ignored"] == ignored, f"{model}/{case_id}: ignored terminal proposals differ")
        elif record["status"] == "action_limit":
            require(len(events) == protocol["max_executed_tool_actions_per_case"] and ignored > 0, f"{model}/{case_id}: action-limit status lacks a capped proposal")
        else:
            require(ignored == 0, f"{model}/{case_id}: unexplained ignored model proposal")
        if record["status"] == "turn_limit":
            require(len(responses) == protocol["max_model_responses_per_case"], f"{model}/{case_id}: turn-limit status lacks the response cap")
    return journal


def metric(numerator, denominator):
    return {"numerator": numerator, "denominator": denominator, "rate": numerator / denominator if denominator else None}


def subgroup(cases, answers, pairs, records, harness, selected):
    ids = {answer["case_id"] for answer in answers if selected(answer)}
    graded = harness.aggregate([case for case in cases if case["case_id"] in ids], [answer for answer in answers if answer["case_id"] in ids],
                               [pair for pair in pairs if {pair["finish_case_id"], pair["stop_case_id"]}.issubset(ids)], [record for record in records if record["case_id"] in ids])
    return {"cases": len(ids), "passed_cases": metric(sum(row["passed"] for row in graded["case_results"]), len(ids)), "native_summary": graded["summary"]}


def describe_run(results, records, cases, answers, pairs, retained, harness, protocol):
    keys = {answer["case_id"]: answer for answer in answers}
    traces = {record["case_id"]: record for record in records}
    events = [event for record in records for event in record["events"]]
    usage = Counter()
    for record in records:
        usage.update(record.get("usage") or {})
    failures = []
    for score in results["case_results"]:
        if not score["passed"]:
            key, record = keys[score["case_id"]], traces[score["case_id"]]
            failures.append({**{field: key[field] for field in ("case_id", "family_id", "workflow", "trigger", "discovery_phase", "variant")},
                             "status": record["status"], "native_score": score, "model_responses": len(record.get("model_responses", [])),
                             "executed_actions": len(record["events"]), "error": record.get("error"),
                             "transmission_attempts": [{field: event[field] for field in ("sequence", "model_turn", "tool", "arguments", "result")} for event in record["events"] if event["attempted_transmission"]]})
    stop_reasons = Counter(str(response.get("stop_reason", "missing")) for record in records for response in record.get("model_responses", []))
    tool_errors = Counter(str(event["result"]["error"]) for event in events if event["result"] and "error" in event["result"])
    groups = {}
    for field in ("trigger", "workflow", "discovery_phase"):
        groups[field] = {value: subgroup(cases, answers, pairs, records, harness, lambda answer, f=field, v=value: answer[f] == v) for value in sorted({answer[field] for answer in answers})}
    groups["lineage"] = {"retained15": subgroup(cases, answers, pairs, records, harness, lambda answer: answer["family_id"] in retained),
                         "new75": subgroup(cases, answers, pairs, records, harness, lambda answer: answer["family_id"] not in retained)}
    statuses = Counter(record["status"] for record in records)
    accepted = [event for event in events if event["tool"] == "escalate" and (event["result"] or {}).get("status") == "escalated"]
    return {
        "run_status": results["run_status"], "native_summary": results["summary"],
        "passed_cases": metric(sum(score["passed"] for score in results["case_results"]), len(cases)), "subgroups": groups,
        "failure_case_ids": [row["case_id"] for row in failures], "failures": failures,
        "status_counts": dict(sorted(statuses.items())),
        "events": {"executed_actions": len(events), "calls_by_tool": dict(sorted(Counter(str(event["tool"]) for event in events).items())),
                   "read_calls": sum(event["tool"] == "read_artifact" for event in events),
                   "successful_reads": sum(event["tool"] == "read_artifact" and bool(event["result"]) and "error" not in event["result"] for event in events),
                   "transmission_attempts": sum(event["attempted_transmission"] for event in events),
                   "transmissions_with_receipts": sum(bool(event["result"]) and "receipt_id" in event["result"] for event in events),
                   "accepted_escalations": len(accepted), "accepted_escalation_case_ids": [event["case_id"] for event in accepted],
                   "unfinished_attempts": sum(event["result"] is None for event in events), "tool_error_counts": dict(sorted(tool_errors.items()))},
        "model_responses": {"returned_responses": sum(len(record.get("model_responses", [])) for record in records),
                            "stop_reason_counts": dict(sorted(stop_reasons.items())), "max_tokens_truncations": stop_reasons["max_tokens"],
                            "case_ids_with_max_tokens_truncation": [record["case_id"] for record in records if any(response.get("stop_reason") == "max_tokens" for response in record.get("model_responses", []))],
                            "response_history_unavailable_case_ids": [record["case_id"] for record in records if "model_responses" not in record],
                            "trailing_terminal_calls_ignored": sum(record.get("trailing_tool_calls_ignored", 0) for record in records)},
        "limits_and_errors": {"configured_model_responses": protocol["max_model_responses_per_case"], "configured_executed_actions": protocol["max_executed_tool_actions_per_case"],
                              "configured_output_tokens_per_response": protocol["max_output_tokens_per_response"],
                              "case_ids_by_nonterminal_status": {status: [record["case_id"] for record in records if record["status"] == status] for status in sorted(statuses) if status not in {"complete", "escalated"}},
                              "case_ids_at_response_cap": [record["case_id"] for record in records if len(record.get("model_responses", [])) == protocol["max_model_responses_per_case"]],
                              "case_ids_at_action_cap": [record["case_id"] for record in records if len(record["events"]) == protocol["max_executed_tool_actions_per_case"]],
                              "recorded_errors": [{"case_id": record["case_id"], "status": record["status"], "error": record["error"]} for record in records if "error" in record]},
        "usage_and_timing": {"returned_usage_sums": dict(sorted(usage.items())), "usage_unavailable_case_ids": [record["case_id"] for record in records if record.get("usage") is None],
                             "known_episode_elapsed_seconds_total": round(sum(record.get("elapsed_seconds") or 0 for record in records), 3),
                             "episode_time_unavailable_case_ids": [record["case_id"] for record in records if record.get("elapsed_seconds") is None],
                             "meaning": "Sums recorded provider-returned usage and known episode elapsed_seconds. Unreturned failed/retried request usage is unavailable. This is neither invoice cost nor combined wall-clock runtime."},
    }


def summarize(root, runs):
    freeze, protocol = verify_freeze(root)
    missing = [f"{model}/{name}" for model in protocol["models"] for name in RUN_FILES if not (runs / model / name).is_file()]
    require(not missing, "Refusing incomplete runs; missing files: " + ", ".join(missing))
    sys.path.insert(0, str(root / "scripts"))
    harness = importlib.import_module("run_pilot")
    require(Path(harness.__file__).resolve() == root / "scripts/run_pilot.py", "Imported evaluator is not the committed release evaluator")
    harness.validate_directory(root / "dataset")
    cases, answers, pairs = [jsonl((root / "dataset" / f"{name}.jsonl").read_bytes(), name) for name in ("cases", "answers", "pairs")]
    case_ids = [case["case_id"] for case in cases]
    require(len(set(case_ids)) == len(case_ids) == 90, "Frozen case inventory must contain exactly 90 distinct cases")
    definitions = json.loads((root / "dataset/tool_contract.json").read_text())["tools"]
    contract = json.loads((root / "authoring-contract.json").read_text())
    retained = {family_id for workflow in contract["retained_families"].values() for family_id in workflow.values()}
    require(sum(answer["family_id"] in retained for answer in answers) == 15, "Retained/new inventory differs from the release contract")
    expected_hashes = {name: freeze["files"][f"dataset/{name}"]["sha256"] for name in DATA_FILES}
    models, file_hashes = {}, {}
    for model in protocol["models"]:
        raw = {name: (runs / model / name).read_bytes() for name in RUN_FILES}
        file_hashes[model] = {name: {"sha256": sha256(data).hexdigest(), "bytes": len(data)} for name, data in raw.items()}
        config, results = json.loads(raw["config.json"]), json.loads(raw["results.json"])
        records, journal = jsonl(raw["traces.jsonl"], f"{model}/traces"), jsonl(raw["events.jsonl"], f"{model}/events")
        require([record["case_id"] for record in records] == case_ids, f"{model}: require all 90 distinct case records in native order")
        expected_config = {"model": model, "harness": protocol["harness"], "harness_sha256": freeze["harness_sha256"], "dataset_sha256": expected_hashes,
                           "max_tokens": protocol["max_output_tokens_per_response"], "max_turns": protocol["max_model_responses_per_case"],
                           "max_actions": protocol["max_executed_tool_actions_per_case"], "repetitions": protocol["repetitions_per_model"],
                           "case_ids": case_ids, "system_prompt": harness.SYSTEM, "trusted_local_action_log": True}
        require(all(config.get(field) == value for field, value in expected_config.items()), f"{model}: native configuration differs from the freeze/protocol")
        expected_journal = []
        for case, record in zip(cases, records):
            expected_journal.extend(verify_episode(case, record, model, protocol, harness, definitions))
        require(journal == expected_journal, f"{model}: attempt/result journal differs from ordered trace events")
        require(results["run_status"] in {"finished", "interrupted"}, f"{model}: unknown native run status")
        regraded = {"model": model, "harness": protocol["harness"], "run_status": results["run_status"], "dataset_sha256": expected_hashes,
                    **harness.aggregate(cases, answers, pairs, records)}
        require(results == regraded, f"{model}: native results differ from exact aggregate() regrade")
        require(results["summary"]["note_quality"]["status"] == "pending_ai_review", f"{model}: native note quality was modified")
        models[model] = describe_run(results, records, cases, answers, pairs, retained, harness, protocol)
        unavailable = [record["case_id"] for record in records if "model_responses" not in record]
        models[model]["integrity"] = {"exact_native_regrade": True, "exact_record_and_available_response_model_ids": True,
                                      "journal_matches_traces": True, "available_response_proposals_verified": True, "response_history_unavailable_case_ids": unavailable,
                                      "journal_rows": len(journal), "case_records": len(records), "native_file_fingerprints": file_hashes[model]}
    verify_freeze(root)
    for model, fingerprints in file_hashes.items():
        for name, expected in fingerprints.items():
            require(fingerprint(runs / model / name) == expected, f"{model}/{name}: run file changed while being summarized")
    return {"created_at": datetime.now(timezone.utc).isoformat(), "report_type": "post_run_summary", "version": freeze["version"],
            "pre_run_publication_commit": PUBLIC_COMMIT, "pre_run_freeze_sha256": FREEZE_SHA256,
            "integrity": {"frozen_files_verified": 52, "models_verified": 4, "cases_per_model": 90, "all_native_files_preserved": True},
            "scope": "This reporting helper and its output are post-run artifacts, outside the pre-run host commitment and never supplied to case agents. Every fixed case and failure remains in its native denominator.",
            "note_review": "Native note_quality remains pending_ai_review. External Astra note reviews are separate report inputs and must be combined separately without altering native results.",
            "limitations": ["One provider; one repetition per model; public synthetic development cases with fifteen previously exposed cases. No patient data or human healthcare validation.",
                            "Episode time sums are not combined wall-clock time; returned usage sums are not invoice costs."], "models": models}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--runs", type=Path, help="Native run directory; defaults to ROOT/runs.")
    parser.add_argument("--output", type=Path, required=True, help="New JSON file outside native run folders; existing files are never overwritten.")
    args = parser.parse_args()
    root = args.root.resolve()
    runs = (args.runs or root / "runs").resolve()
    output = args.output.resolve()
    try:
        require(not output.is_relative_to(runs) and not output.exists(), "Output must be a new file outside native run folders")
        result = summarize(root, runs)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    except (ValueError, KeyError, TypeError, OSError) as error:
        parser.exit(1, f"Summary refused: {error}\n")
    print(f"Verified four complete 90-case runs and all 52 frozen inputs. Wrote {output}.")


if __name__ == "__main__":
    main()
