"""Run generous always-finish and always-stop scorer sanity controls, not agents."""

import argparse
from copy import deepcopy
from pathlib import Path

from build_challenge import ROOT, fingerprint, load_json, private_path, write_json
from build_dataset import write_jsonl
from legacy_v1_2 import Episode, aggregate
from run_repeated import DATASET_FILES, REPETITIONS
from validate_dataset import read_jsonl, validate_directory


def control_records(cases, answers, families, definitions, mode, repetitions=REPETITIONS):
    if mode not in {"always_finish", "always_stop"}:
        raise ValueError("Unknown scorer control")
    keys = {row["case_id"]: row for row in answers}
    source = {family["family_id"]: family for family in families}
    records = []
    for repetition in range(1, repetitions + 1):
        for case in cases:
            key = keys[case["case_id"]]
            family = source[key["family_id"]]
            episode = Episode(case, definitions)
            for row in case["reveals"]:
                result = episode.invoke("read_artifact", {"reference": row["artifact"]["reference"]}, model_turn=1)
                if "error" in result:
                    raise ValueError("Scorer control cannot read a supplied record")
            if mode == "always_finish":
                receipt = episode.invoke(family["action"], deepcopy(family["action_arguments"]), model_turn=2)
                result = episode.invoke("complete", {"receipt_id": receipt["receipt_id"]}, model_turn=3)
            else:
                arguments = {"reason_code": family["trigger"], "urgency": family["urgency"], "evidence_refs": family["evidence_refs"],
                             "note": family["handoff_requirement"]}
                result = episode.invoke("escalate", deepcopy(arguments), model_turn=2)
            records.append({"case_id": case["case_id"], "repetition": repetition, "model": mode,
                            "harness": "kwts-scorer-sanity-controls-v1", "status": result["status"], "events": episode.events,
                            "control": "Key-guided synthetic scorer sanity control; not an autonomous participant"})
    for repetition in range(1, repetitions + 1):
        rows = [record for record in records if record["repetition"] == repetition]
        pairs = []
        for family_id in source:
            variants = {key["variant"]: key["case_id"] for key in answers if key["family_id"] == family_id}
            pairs.append({"finish_case_id": variants["finish"], "stop_case_id": variants["stop"]})
        summary = aggregate(cases, answers, pairs, rows)["summary"]
        if summary["paired_accuracy"]["numerator"] != 0 or summary["triplet_accuracy"]["numerator"] != 0:
            raise ValueError("Always-finish and always-stop controls must score zero on paired and triplet accuracy")
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    dataset = private_path(args.root, args.dataset or args.root / "private/dataset")
    output = private_path(args.root, args.output)
    validate_directory(dataset)
    cases, answers = read_jsonl(dataset / "cases.jsonl"), read_jsonl(dataset / "answers.jsonl")
    source = load_json(args.root / "private/source/templates.json")
    definitions = load_json(dataset / "tool_contract.json")["tools"]
    output.mkdir(parents=True, exist_ok=False)
    for mode in ("always_finish", "always_stop"):
        records = control_records(cases, answers, source["families"], definitions, mode)
        directory = output / mode
        directory.mkdir()
        write_jsonl(directory / "traces.jsonl", records)
        write_json(directory / "config.json", {"model": mode, "harness": "kwts-scorer-sanity-controls-v1", "repetitions": REPETITIONS,
                   "participant_type": "scorer_sanity_control", "ground_truth_arguments": True, "all_records_delivered_before_action": True,
                   "dataset_sha256": {name: fingerprint(dataset / name)["sha256"] for name in DATASET_FILES}})
    print("Private scorer sanity controls executed; paired and triplet accuracy are zero for both.")


if __name__ == "__main__":
    main()
