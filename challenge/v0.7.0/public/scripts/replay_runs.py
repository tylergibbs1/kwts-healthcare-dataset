"""Recover private incomplete runs from durable journals without calling a provider."""

import argparse
from copy import deepcopy
import json
from pathlib import Path

from build_challenge import ROOT, fingerprint, load_json, private_path, write_json
from run_repeated import DATASET_FILES, HARNESS, REPETITIONS, case_orders, per_repetition_results
from validate_dataset import read_jsonl


def recover_records(cases, trace_rows, journal_rows, model, harness=HARNESS, repetitions=REPETITIONS):
    planned = {(rep, case["case_id"]) for rep in range(1, repetitions + 1) for case in cases}
    records, events = {}, {}
    for row in trace_rows:
        key = (row.get("repetition"), row.get("case_id"))
        if key not in planned or key in records:
            raise ValueError("Unknown or duplicate durable trace episode")
        records[key] = deepcopy(row)
    for row in journal_rows:
        event = row["event"]
        key = (row.get("repetition"), event["case_id"])
        sequence = event["sequence"]
        if key not in planned or not isinstance(sequence, int) or sequence < 1 or row.get("stage") not in {"attempt", "result"}:
            raise ValueError("Invalid action journal row")
        position = (*key, sequence)
        previous = events.get(position)
        if previous is not None and row["stage"] == "attempt":
            raise ValueError("Duplicate action attempt in journal")
        if row["stage"] == "result" and (previous is None or any(previous[field] != event[field] for field in ("tool", "arguments", "model_turn", "attempted_transmission"))):
            raise ValueError("Action result has no matching durable attempt")
        if row["stage"] == "result" and (previous.get("result") is not None or not isinstance(event.get("result"), dict)):
            raise ValueError("Duplicate or malformed journal result")
        events[position] = deepcopy(event)
    for key in planned:
        durable = [event for (rep, cid, seq), event in sorted(events.items()) if (rep, cid) == key]
        if durable and [event["sequence"] for event in durable] != list(range(1, len(durable) + 1)):
            raise ValueError("Journal action sequence has gaps")
        if key in records:
            if records[key].get("events", []) != durable:
                raise ValueError("Completed trace disagrees with the durable action journal")
        else:
            records[key] = {"case_id": key[1], "repetition": key[0], "model": model, "harness": harness,
                            "status": "interrupted" if durable else "missing", "events": durable,
                            "model_responses": [], "usage": None, "recovered_from_journal": True}
    return [records[key] for key in sorted(records)]


def read_durable_prefix(path):
    """Only an unterminated final line may be ignored after process death."""
    if not path.exists():
        return [], False
    data = path.read_bytes()
    lines = data.splitlines(keepends=True)
    rows, truncated = [], False
    for index, line in enumerate(lines):
        try:
            rows.append(json.loads(line))
        except (ValueError, UnicodeError):
            if index != len(lines) - 1 or line.endswith(b"\n"):
                raise ValueError("Malformed complete journal line; refusing silent repair") from None
            truncated = True
    return rows, truncated


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New private recovery directory; never overwrite native files")
    args = parser.parse_args()
    dataset = private_path(args.root, args.dataset or args.root / "private/dataset")
    run = private_path(args.root, args.run)
    output = private_path(args.root, args.output)
    config = load_json(run / "config.json")
    cases = read_jsonl(dataset / "cases.jsonl")
    if config.get("dataset_sha256") != {name: fingerprint(dataset / name)["sha256"] for name in DATASET_FILES}:
        raise ValueError("Recovery dataset differs from the original run bytes")
    if config["case_orders"] != case_orders([row["case_id"] for row in cases]):
        raise ValueError("Run ordering differs from the frozen deterministic schedule")
    traces, partial_trace = read_durable_prefix(run / "traces.jsonl")
    events, partial_journal = read_durable_prefix(run / "events.jsonl")
    records = recover_records(cases, traces, events, config["model"], config["harness"])
    answers, pairs = read_jsonl(dataset / "answers.jsonl"), read_jsonl(dataset / "pairs.jsonl")
    output.mkdir(parents=True, exist_ok=False)
    from build_dataset import write_jsonl
    write_jsonl(output / "traces.jsonl", records)
    write_json(output / "results.json", {"model": config["model"], "harness": config["harness"], "run_status": "recovered_without_reexecution",
               "ignored_unterminated_trace_tail": partial_trace, "ignored_unterminated_journal_tail": partial_journal,
               "per_repetition": per_repetition_results(cases, answers, pairs, records)})
    write_json(output / "config.json", {**config, "recovery_only": True, "provider_calls": 0})
    print("Recovery saved privately. Native traces and scores remain unchanged; no episode was re-run.")


if __name__ == "__main__":
    main()
