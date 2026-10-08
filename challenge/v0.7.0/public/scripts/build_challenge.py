"""Compose and validate protected triplets without exporting private examples."""

import argparse
from collections import Counter
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import random

from build_dataset import agent_view, differences, make_case, write_jsonl
from legacy_v1_2 import Episode, argument_error, score_case
from validate_dataset import DATA_FILES, reserved_keys, validate_directory, validate_records

ROOT = Path(__file__).resolve().parents[2]
VARIANTS = ("finish", "stop", "distractor")
CORE_FILES = {
    "build_dataset.py": "build_dataset.py",
    "validate_dataset.py": "validate_dataset.py",
    "legacy_v1_2.py": "run_pilot.py",
}
PACKET_BINDING_CLARIFICATION = (
    " Transmission uses the current case-selected final packet or revision for the exact supplied IDs; "
    "arbitrary, older, or draft content is not selected."
)


def load_json(path):
    return json.loads(path.read_text())


def fingerprint(path):
    data = path.read_bytes()
    return {"sha256": sha256(data).hexdigest(), "bytes": len(data)}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def private_path(root, path):
    path = path.resolve()
    if not path.is_relative_to((root / "private").resolve()):
        raise ValueError("Protected data and run output must remain under challenge30/private")
    return path


def scalar_values(value):
    if isinstance(value, dict):
        return [scalar for item in value.values() for scalar in scalar_values(item)]
    if isinstance(value, list):
        return [scalar for item in value for scalar in scalar_values(item)]
    return [value]


def verify_tool_clarification(contract, baseline):
    """Enforce the reviewed description-only delta against the public v0.6 contract."""
    expected = deepcopy(baseline)
    tools = {item["name"]: item for item in expected["tools"]}
    tools["submit_authorization"]["description"] += PACKET_BINDING_CLARIFICATION
    tools["send_appeal"]["description"] = tools["send_appeal"]["description"].replace(
        "to the synthetic MAC,", "to the synthetic payer identified by payer_id,"
    ) + PACKET_BINDING_CLARIFICATION
    if contract != expected:
        raise ValueError("Only the two reviewed tool descriptions may differ from v0.6; names, schemas, required fields and flags must remain unchanged")
    return {"description_only_changes": ["submit_authorization", "send_appeal"],
            "tool_names_parameter_schemas_required_fields_flags_preserved": True,
            "context_selected_packet_revision_binding": True, "synthetic_payer_id_target": True}


def verify_core(root):
    baseline = root.parent / "release90"
    checks = {}
    for copied, original in CORE_FILES.items():
        path = root / "public/scripts" / copied
        expected = baseline / "scripts" / original
        if path.read_bytes() != expected.read_bytes():
            raise ValueError(f"{copied}: historical core must remain byte-identical")
        checks[copied] = fingerprint(path)
    tool = root / "public/scripts/tool_contract.json"
    baseline_tool = baseline / "dataset/tool_contract.json"
    clarification = verify_tool_clarification(load_json(tool), load_json(baseline_tool))
    checks["tool_contract.json"] = {**fingerprint(tool), "baseline_sha256": fingerprint(baseline_tool)["sha256"], **clarification}
    return checks


def validate_source(source, contract, protocol, definitions):
    errors = []
    if source["version"] != contract["version"] or source["as_of"] != contract["as_of"] or source["as_of"] != protocol["case_clock"]:
        errors.append("Source version or evaluation clock disagrees with the frozen contracts")
    if source.get("review_status") not in {"author_draft", "ai_reviewed"}:
        errors.append("Unsupported AI review status")
    if tuple(contract["variants"]) != VARIANTS:
        errors.append("The challenge requires exactly three variants per family")
    families = source["families"]
    slots = {slot["family_id"]: slot for slot in contract["family_slots"]}
    if len(slots) != contract["total_families"] or len(families) != contract["total_families"]:
        errors.append("The family count disagrees with the authoring contract")
    if Counter(family["family_id"] for family in families) != Counter(slots.keys()):
        errors.append("Missing, duplicate or unexpected protected family IDs")
    tools = {item["name"]: item for item in definitions}
    arguments_seen = []
    for family in families:
        label = family["family_id"]
        slot = slots.get(label)
        if slot is None:
            continue
        if any(family.get(field) != slot[field] for field in ("workflow", "trigger", "phase")):
            errors.append(f"{label}: coverage slot or delivery phase differs from the contract")
        if "read_dependencies" in family:
            errors.append(f"{label}: read dependencies are prohibited")
        artifacts = family["artifacts"]
        if not 5 <= len(artifacts) <= 7:
            errors.append(f"{label}: baseline requires five to seven artifacts")
        refs = [item["reference"] for item in artifacts]
        distractor_ref = family["distractor"]["reference"]
        all_refs = refs + [distractor_ref]
        if len(set(all_refs)) != len(all_refs):
            errors.append(f"{label}: artifact references must be unique")
        requests = [item for item in artifacts if item["reference"].startswith("desk://") and "message" in item["data"]]
        if len(requests) != 1:
            errors.append(f"{label}: exactly one ordinary staff-request artifact is required")
        delayed = family.get("delayed_refs", [])
        if len(set(delayed)) != len(delayed) or not set(delayed).issubset(all_refs):
            errors.append(f"{label}: invalid delayed artifact list")
        if family["phase"] == "runtime":
            initial = [item for item in artifacts if item["reference"] not in delayed]
            if initial != requests or distractor_ref not in delayed:
                errors.append(f"{label}: runtime exposes only the staff request; all other records are delayed")
        else:
            initial = artifacts
        observed, evidence = family["must_observe_refs"], family["evidence_refs"]
        if len(set(observed)) != len(observed) or len(observed) < 2 or not set(observed).issubset(refs):
            errors.append(f"{label}: observation support needs at least two distinct baseline records")
        if not evidence or len(set(evidence)) != len(evidence) or not set(evidence).issubset(refs):
            errors.append(f"{label}: unavailable or duplicate escalation evidence")
        patch = family["stop_patch"]
        if patch["reference"] not in observed or not isinstance(patch["pointer"], str) or not patch["pointer"].startswith("/"):
            errors.append(f"{label}: decisive scalar patch must be observed and use a JSON pointer")
        if isinstance(patch["value"], (dict, list)):
            errors.append(f"{label}: stop patch must replace one scalar")
        urgency = "urgent" if family["trigger"] in {"deadline_risk", "emergent_risk"} else "routine"
        if family["urgency"] != urgency:
            errors.append(f"{label}: urgency disagrees with the fixed contract")
        if "accepted_reason_codes" in family and family["trigger"] not in family["accepted_reason_codes"]:
            errors.append(f"{label}: accepted reasons must include the assigned trigger")
        if "accepted_reason_codes" in family and set(family["accepted_reason_codes"]) != {family["trigger"]} and not family.get("reason_overlap_justification"):
            errors.append(f"{label}: overlapping reasons require written private justification")
        tool = tools.get(family["action"])
        if not tool or not tool["irreversible"]:
            errors.append(f"{label}: unavailable irreversible action")
            continue
        error = argument_error(family["action_arguments"], tool["arguments_schema"])
        if error:
            errors.append(f"{label}: exact finish arguments violate the copied contract: {error}")
        support_refs = set(observed) | {item["reference"] for item in initial}
        support = scalar_values([item["data"] for item in artifacts if item["reference"] in support_refs])
        if any(value not in support for value in scalar_values(family["action_arguments"])):
            errors.append(f"{label}: exact finish argument lacks delivered-record support")
        arguments_seen.append(json.dumps({"tool": family["action"], "arguments": family["action_arguments"]}, sort_keys=True))
    if len(set(arguments_seen)) != len(arguments_seen):
        errors.append("Families must have distinct transmission arguments")
    if errors:
        raise ValueError("\n".join(errors))


def compose_source(root):
    contract = load_json(root / "authoring-contract.json")
    families, registries = [], []
    for group in ("a", "b"):
        families.extend(load_json(root / "private/authors" / f"group-{group}-families.json"))
        registry_path = root / "private/authors" / f"group-{group}-sources.json"
        registries.append(load_json(registry_path))
    slots = {row["family_id"]: index for index, row in enumerate(contract["family_slots"])}
    families.sort(key=lambda family: slots.get(family["family_id"], len(slots)))
    return {"name": contract["name"], "version": contract["version"], "as_of": contract["as_of"],
            "review_status": "author_draft", "human_validation": "pending",
            "notice": "Protected synthetic challenge pilot. AI review and healthcare sign-off are separate. No patient data, live systems or HealthAdminBench content. No clinical validity, regulatory compliance or deployment-safety claim.",
            "build_plan": {"composer": "public/scripts/build_challenge.py", "variants": list(VARIANTS), "phase_policy": "Each family.phase; all variants share that phase", "split": contract["split"]},
            "source_registries": registries, "families": families}


def validate_registries(source):
    """Preserve author source mappings; substantive verification is separate review."""
    ids, mapped = set(), set()
    for registry in source["source_registries"]:
        sources = registry.get("sources", registry.get("primary_sources", []))
        mappings = registry.get("family_source_mappings", [])
        local_ids = {item["source_id"] for item in sources}
        if len(local_ids) != len(sources) or ids.intersection(local_ids):
            raise ValueError("Author source IDs must be unique across registries")
        ids.update(local_ids)
        for item in sources:
            required = {"source_id", "url", "title", "publisher"}
            if not required.issubset(item) or not item.get("url", "").startswith("https://"):
                raise ValueError("Primary source entries need a URL, title and publisher")
            if not any(item.get(field) for field in ("accessed_date", "accessed_on", "accessed")):
                raise ValueError("Source access date is missing")
            if not any(item.get(field) for field in ("section", "locator")) or not any(item.get(field) for field in ("fact_scope", "exact_fact_scope", "verified_fact_scope")):
                raise ValueError("Source section and exact fact scope are required")
            if not any(item.get(field) for field in ("synthetic_rule_boundary", "synthetic_boundary", "local_rule_boundary")):
                raise ValueError("Source facts must be separated from synthetic rules")
        for mapping in mappings:
            family_id = mapping["family_id"]
            if family_id in mapped:
                raise ValueError("Duplicate family source mapping")
            mapped.add(family_id)
            refs = mapping.get("source_ids", mapping.get("primary_source_ids", []))
            fiction = mapping.get("local_fiction_only", False) is True or mapping.get("basis") == "local-fiction-only"
            if not refs and not fiction:
                raise ValueError("Every family needs verified sources or an explicit local-fiction-only declaration")
            if not set(refs).issubset(local_ids):
                raise ValueError("Family cites unavailable source IDs")
    if mapped != {family["family_id"] for family in source["families"]}:
        raise ValueError("Source registries must map every protected family exactly once")


def compose_records(source, contract, protocol, definitions):
    validate_source(source, contract, protocol, definitions)
    cases, answers, pairs = [], [], []
    for family in source["families"]:
        triplet = {variant: make_case(source, family, family["phase"], variant) for variant in VARIANTS}
        finish, finish_key = triplet["finish"]
        stop, _ = triplet["stop"]
        left, right = deepcopy(finish), deepcopy(stop)
        left.pop("case_id")
        right.pop("case_id")
        patch = differences(left, right)
        if len(patch) != 1 or isinstance(patch[0]["before"], (dict, list)) or isinstance(patch[0]["after"], (dict, list)):
            raise ValueError("Finish/stop twins must differ by exactly one scalar field")
        distractor = deepcopy(triplet["distractor"][0])
        distractor["case_id"] = finish["case_id"]
        extra_index = distractor["artifact_index"].pop()
        extra = distractor["reveals"].pop()["artifact"] if family["phase"] == "runtime" else distractor["initial_artifacts"].pop()
        if distractor != finish or extra != family["distractor"] or extra_index != {key: extra[key] for key in ("reference", "label")}:
            raise ValueError("Benign variant may only add its authored unrelated record")
        cases.extend(triplet[variant][0] for variant in VARIANTS)
        answers.extend(triplet[variant][1] for variant in VARIANTS)
        pairs.append({"pair_id": finish_key["pair_id"], "finish_case_id": finish["case_id"], "stop_case_id": stop["case_id"], "patch": patch[0]})
    cases.sort(key=lambda row: row["case_id"])
    answers.sort(key=lambda row: row["case_id"])
    pairs.sort(key=lambda row: row["pair_id"])
    inputs = [agent_view(case) for case in cases]
    errors = validate_records(cases, inputs, answers, pairs)
    if errors:
        raise ValueError("\n".join(errors))
    if len(cases) != contract["total_cases"] or len(pairs) != contract["total_families"]:
        raise ValueError("Export count disagrees with the authoring contract")
    return cases, inputs, answers, pairs


def prove_execution_budget(cases, answers, definitions, protocol):
    keys = {row["case_id"]: row for row in answers}
    modes = {mode: {"max_model_responses": 0, "max_executed_actions": 0} for mode in ("all_delayed_records_batched", "relevant_records_sequential")}
    for case in cases:
        key = keys[case["case_id"]]
        for mode, maxima in modes.items():
            refs = [row["artifact"]["reference"] for row in case["reveals"]]
            if mode == "relevant_records_sequential":
                wanted = set(key["must_observe_refs"])
                if key["escalation"]:
                    wanted.update(key["escalation"]["evidence_refs"])
                refs = [ref for ref in refs if ref in wanted]
            episode = Episode(case, definitions)
            turn = 0
            for ref in refs:
                turn = turn + 1 if mode == "relevant_records_sequential" else 1
                if "error" in episode.invoke("read_artifact", {"reference": ref}, model_turn=turn):
                    raise ValueError("A required read is unreachable")
            turn += 1
            if key["expected_decision"] == "finish":
                action = key["expected_action"]
                receipt = episode.invoke(action["tool"], action["arguments"], model_turn=turn)
                turn += 1
                result = episode.invoke("complete", {"receipt_id": receipt["receipt_id"]}, model_turn=turn)
            else:
                handoff = key["escalation"]
                arguments = {field: deepcopy(handoff[field]) for field in ("reason_code", "urgency", "evidence_refs")}
                arguments["note"] = handoff["note_requirement"]
                result = episode.invoke("escalate", arguments, model_turn=turn)
            score = score_case(case, key, {"status": result.get("status"), "events": episode.events})
            if not score["passed"] or (key["escalation"] and not all(score["handoff"].values())):
                raise ValueError("The key-guided executor path cannot satisfy its answer")
            actions = len(episode.events)
            if turn > protocol["max_model_responses_per_case"] or actions > protocol["max_executed_tool_actions_per_case"]:
                raise ValueError("Required sequential or full-batch path exceeds the frozen budget")
            maxima["max_model_responses"] = max(maxima["max_model_responses"], turn)
            maxima["max_executed_actions"] = max(maxima["max_executed_actions"], actions)
    return {"basis": "Key-guided local executor feasibility; no provider calls or autonomous agent results", "cases_checked_per_path": len(cases),
            "limits": {"model_responses": protocol["max_model_responses_per_case"], "executed_actions": protocol["max_executed_tool_actions_per_case"]}, **modes}


def write_review_packets(root, source, cases, answers):
    shuffled = deepcopy(cases)
    random.Random(20261008).shuffle(shuffled)
    environments = []
    for case in shuffled:
        env = agent_view(case)
        env["initial_artifacts"] += [deepcopy(row["artifact"]) for row in case["reveals"]]
        if reserved_keys(env):
            raise ValueError("Evaluator labels leaked into the blind review environment")
        environments.append(env)
    packets = root / "private/review_packets"
    packets.mkdir(parents=True, exist_ok=True)
    write_jsonl(packets / "blinded_cases.jsonl", environments)
    workflow_by_case = {answer["case_id"]: answer["workflow"] for answer in answers}
    for workflow in ("prior_auth", "appeals", "equipment"):
        write_jsonl(packets / f"{workflow}.jsonl", [env for env in environments if workflow_by_case[env["case_id"]] == workflow])
    human = root / "private/review-packet"
    write_json(human / "human-review.json", {"human_validation": "pending", "environments": environments,
               "answer_keys": answers, "source_registries": source["source_registries"],
               "review_scope": "Administrative authority, supplied clinical directions, primary-source scope, ambiguity, finish arguments, stop reasons, urgency and actionable handoffs"})
    write_json(human / "signoff-template.json", {"human_validation": "pending", "reviewer_name": None, "credentials_and_scope": None,
               "reviewed_at": None, "reviewed_exact_file_sha256": None, "findings": [], "decision": "pending",
               "limitations": "No healthcare reviewer has signed off. AI review is separate and does not establish clinical validity, compliance or deployment safety."})


def build(root=ROOT):
    root = root.resolve()
    if (root / "private/pre-run-freeze.json").exists():
        raise ValueError("The challenge is frozen; substantive case/key changes require a new version")
    core = verify_core(root)
    source = compose_source(root)
    validate_registries(source)
    contract, protocol = load_json(root / "authoring-contract.json"), load_json(root / "evaluation-protocol.json")
    tool_bytes = (root / "public/scripts/tool_contract.json").read_bytes()
    definitions = json.loads(tool_bytes)["tools"]
    cases, inputs, answers, pairs = compose_records(source, contract, protocol, definitions)
    budgets = prove_execution_budget(cases, answers, definitions, protocol)
    dataset = private_path(root, root / "private/dataset")
    dataset.mkdir(parents=True, exist_ok=True)
    source_path = root / "private/source/templates.json"
    write_json(source_path, source)
    for name, rows in (("cases", cases), ("agent_inputs", inputs), ("answers", answers), ("pairs", pairs)):
        write_jsonl(dataset / f"{name}.jsonl", rows)
    (dataset / "tool_contract.json").write_bytes(tool_bytes)
    manifest = {"name": source["name"], "version": source["version"], "split": contract["split"], "review_status": source["review_status"],
                "human_validation": "pending", "cases": len(cases), "pairs": len(pairs), "independent_case_families": len(source["families"]),
                "variants": {variant: sum(row["variant"] == variant for row in answers) for variant in VARIANTS},
                "workflows": dict(Counter(row["workflow"] for row in answers)), "discovery_phases": dict(Counter(row["discovery_phase"] for row in answers)),
                "source_sha256": fingerprint(source_path)["sha256"], "files": {name: fingerprint(dataset / name) for name in sorted(DATA_FILES)},
                "legacy_core": core, "execution_budget": budgets,
                "source_boundary": "All ten new families use explicit local-fiction-only administrative procedures; no CMS facts are used in the new set",
                "protection": "Private active challenge: no cases, keys, IDs, sources, notes or traces may be published"}
    write_json(dataset / "manifest.json", manifest)
    checked = validate_directory(dataset)
    write_review_packets(root, source, cases, answers)
    write_json(root / "private/build-checks.json", {"status": "passed", "counts": checked, "core_byte_identity": core,
               "execution_budget": budgets, "single_scalar_twins": True, "distractor_only_adds_one_record": True,
               "family_phase_respected": True, "read_dependencies": False, "agent_view_evaluator_labels": False,
               "exact_finish_arguments_supported": True, "blind_packets_full_environments_only": True, "human_validation": "pending"})
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    manifest = build(args.root)
    print(f"Private challenge built: {manifest['cases']} cases, {manifest['pairs']} pairs, {manifest['independent_case_families']} families. AI and human review pending.")


if __name__ == "__main__":
    main()
