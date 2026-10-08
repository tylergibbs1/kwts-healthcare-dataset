"""Print one safe initial case view or one explicitly requested record."""

import argparse
import json
from pathlib import Path

from build_dataset import ROOT, agent_view
from run_pilot import Episode
from validate_dataset import read_jsonl

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("case_id")
parser.add_argument("--read", metavar="REFERENCE", action="append", help="Inspect a read; repeat in order to satisfy read prerequisites. No model or transmission runs.")
parser.add_argument("--dataset", type=Path, default=ROOT / "dataset")
args = parser.parse_args()
case = next((item for item in read_jsonl(args.dataset / "cases.jsonl") if item["case_id"] == args.case_id), None)
if case is None:
    parser.error("Unknown case ID")
if args.read:
    definitions = json.loads((args.dataset / "tool_contract.json").read_text())["tools"]
    episode = Episode(case, definitions)
    results = [episode.invoke("read_artifact", {"reference": ref}) for ref in args.read]
    output = results[0] if len(results) == 1 else results
else:
    output = agent_view(case)
print(json.dumps(output, indent=2, ensure_ascii=False))
