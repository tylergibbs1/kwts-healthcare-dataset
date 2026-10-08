"""Export the authored pilot into separate agent inputs and evaluator records."""

from copy import deepcopy
import argparse
from hashlib import sha256
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "source" / "templates.json"
DATASET = ROOT / "dataset"


def identifier(kind, *parts):
    digest = sha256(("kwts-pilot:" + ":".join(parts)).encode()).hexdigest()[:16]
    return f"{kind}-{digest}"


def set_pointer(document, pointer, value):
    parts = [part.replace("~1", "/").replace("~0", "~") for part in pointer.split("/")[1:]]
    target = document
    for part in parts[:-1]:
        target = target[int(part)] if isinstance(target, list) else target[part]
    key = int(parts[-1]) if isinstance(target, list) else parts[-1]
    target[key] = deepcopy(value)


def differences(before, after, path=""):
    if isinstance(before, dict) and isinstance(after, dict) and before.keys() == after.keys():
        return [change for key in before for change in differences(before[key], after[key], f"{path}/{key}")]
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        return [change for index, (left, right) in enumerate(zip(before, after)) for change in differences(left, right, f"{path}/{index}")]
    return [] if before == after else [{"path": path, "before": before, "after": after}]


def agent_view(case):
    return {key: deepcopy(value) for key, value in case.items() if key != "reveals"}


def make_case(source, family, phase, variant):
    artifacts = deepcopy(family["artifacts"])
    target_ref = family["stop_patch"]["reference"]
    if variant == "stop":
        target = next(item for item in artifacts if item["reference"] == target_ref)
        set_pointer(target["data"], family["stop_patch"]["pointer"], family["stop_patch"]["value"])
    if variant == "distractor":
        artifacts.append(deepcopy(family["distractor"]))
    delayed_refs = set(family.get("delayed_refs", [target_ref]))
    delayed = [item for item in artifacts if phase == "runtime" and item["reference"] in delayed_refs]
    case = {
        "case_id": identifier("case", family["family_id"], phase, variant),
        "title": family["title"],
        "instruction": family["instruction"],
        "as_of": source["as_of"],
        "role": family.get("role", "Healthcare revenue-cycle administrative assistant"),
        "permissions": {
            "read_records": True, "transmit_supported_requests": True,
            "create_clinical_facts": False, "sign_for_other_people": False,
            "renew_patient_consent": False, "make_phone_calls": False,
        },
        "available_tools": ["read_artifact", family["action"], "complete", "escalate"],
        "artifact_index": [{"reference": item["reference"], "label": item["label"]} for item in artifacts],
        "initial_artifacts": [item for item in artifacts if item not in delayed],
        "reveals": [{
            "on_action": {"tool": "read_artifact", "arguments": {"reference": item["reference"]}},
            "artifact": item,
            **({"requires_refs": family["read_dependencies"][item["reference"]]}
               if item["reference"] in family.get("read_dependencies", {}) else {}),
        } for item in delayed],
    }
    is_stop = variant == "stop"
    answer = {
        "case_id": case["case_id"],
        "pair_id": None if variant == "distractor" else identifier("pair", family["family_id"], phase),
        "family_id": family["family_id"], "workflow": family["workflow"],
        "trigger": family["trigger"], "discovery_phase": phase, "variant": variant,
        "expected_decision": "escalate" if is_stop else "finish",
        "expected_action": None if is_stop else {"tool": family["action"], "arguments": family["action_arguments"]},
        "stop_before_tools": [family["action"]] if is_stop else [],
        "must_observe_refs": family.get("must_observe_refs", [item["reference"] for item in family["artifacts"]]),
        "escalation": {
            "reason_code": family["trigger"], "urgency": family["urgency"],
            "evidence_refs": family["evidence_refs"], "handoff_owner": family["handoff_owner"],
            "note_requirement": family["handoff_requirement"],
            **({"accepted_reason_codes": family["accepted_reason_codes"]} if "accepted_reason_codes" in family else {}),
        } if is_stop else None,
        "review_status": source.get("review_status", "author_draft"),
        "rule_provenance": family.get("rule_provenance", "Invented policy included in the case artifacts; not a Medicare coverage determination."),
    }
    return case, answer


def write_jsonl(path, records):
    path.write_text("".join(json.dumps(item, sort_keys=True, ensure_ascii=False) + "\n" for item in records))


def build(source_path=SOURCE, dataset=DATASET, examples=None):
    source = json.loads(source_path.read_text())
    plan = source.get("build_plan", {"phases": ["pre-execution", "runtime"], "include_distractors": True})
    cases, answers, pairs = [], [], []
    for family in source["families"]:
        for phase in plan["phases"]:
            finish, finish_answer = make_case(source, family, phase, "finish")
            stop, stop_answer = make_case(source, family, phase, "stop")
            left, right = deepcopy(finish), deepcopy(stop)
            left.pop("case_id")
            right.pop("case_id")
            patch = differences(left, right)
            if len(patch) != 1:
                raise ValueError(f"{family['family_id']}: twin must change exactly one field")
            cases.extend((finish, stop))
            answers.extend((finish_answer, stop_answer))
            pairs.append({"pair_id": finish_answer["pair_id"], "finish_case_id": finish["case_id"],
                          "stop_case_id": stop["case_id"], "patch": patch[0]})
        if plan["include_distractors"]:
            distractor, answer = make_case(source, family, "runtime", "distractor")
            cases.append(distractor)
            answers.append(answer)
    cases.sort(key=lambda item: item["case_id"])
    answers.sort(key=lambda item: item["case_id"])
    pairs.sort(key=lambda item: item["pair_id"])
    dataset.mkdir(parents=True, exist_ok=True)
    write_jsonl(dataset / "cases.jsonl", cases)
    write_jsonl(dataset / "agent_inputs.jsonl", [agent_view(case) for case in cases])
    write_jsonl(dataset / "answers.jsonl", answers)
    write_jsonl(dataset / "pairs.jsonl", pairs)
    examples = examples or dataset.parent / "examples"
    examples.mkdir(parents=True, exist_ok=True)
    sample, answer = make_case(source, source["families"][0], plan["phases"][-1], "stop")
    for name, value in (("case.json", sample), ("agent-input.json", agent_view(sample)), ("answer-key.json", answer)):
        (examples / name).write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    files = {}
    for name in ("agent_inputs.jsonl", "cases.jsonl", "answers.jsonl", "pairs.jsonl", "tool_contract.json"):
        data = (dataset / name).read_bytes()
        files[name] = {"sha256": sha256(data).hexdigest(), "bytes": len(data)}
    manifest = {
        "name": source.get("name", "Knows When to Stop — synthetic pilot"), "version": source["version"],
        "owner": "Grayhaven Industries", "prepared": "2026-10-08", "license": "Apache-2.0",
        "notice": source["notice"], "split": "public_development", "review_status": source.get("review_status", "author_draft"),
        "cases": len(cases), "pairs": len(pairs), "independent_case_families": len(source["families"]),
        "variants": {variant: sum(item["variant"] == variant for item in answers) for variant in ("finish", "stop", "distractor")},
        "workflows": {workflow: sum(item["workflow"] == workflow for item in answers) for workflow in ("prior_auth", "appeals", "equipment")},
        "discovery_phases": {phase: sum(item["discovery_phase"] == phase for item in answers) for phase in ("pre-execution", "runtime")},
        "source_sha256": sha256(source_path.read_bytes()).hexdigest(), "files": files,
        "commitment_scope": "Exact public pilot file bytes. No private or independently validated test set exists.",
    }
    (dataset / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Exported {len(cases)} cases, {len(pairs)} pairs, and {manifest['variants']['distractor']} distractors.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--output", type=Path, default=DATASET)
    parser.add_argument("--examples", type=Path)
    args = parser.parse_args()
    build(args.source, args.output, args.examples)
