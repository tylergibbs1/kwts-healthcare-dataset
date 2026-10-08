"""Parent-owned, one-case session. No answer keys or filesystem access for participants."""
from copy import deepcopy
import json
from build_dataset import agent_view
from legacy_v1_2 import Episode, SYSTEM
from run_repeated import record_rejected_proposals

MAX_RESPONSES = 8
MAX_ACTIONS = 12


class EpisodeSession:
    def __init__(self, case, definitions, event_sink=None):
        self.episode = Episode(case, definitions, event_sink)
        self.messages = [{"role": "user", "content": agent_view(case)}]
        self.responses = []
        self.status = "running"
        self.trailing_tool_calls_ignored = 0

    def request(self):
        if self.status != "running":
            raise ValueError("Session already ended")
        return {"system": SYSTEM, "tools": list(self.episode.tools.values()),
                "conversation": deepcopy(self.messages), "proposal_number": len(self.responses) + 1,
                "remaining_responses": MAX_RESPONSES - len(self.responses),
                "remaining_actions": MAX_ACTIONS - len(self.episode.events)}

    def reject(self, response, failure):
        turn = len(self.responses) + 1
        self.responses.append(deepcopy(response))
        record_rejected_proposals(self.episode, response, turn, failure)
        self.status = failure

    def accept(self, proposal):
        if self.status != "running":
            raise ValueError("Session already ended")
        if len(self.responses) >= MAX_RESPONSES:
            self.status = "turn_limit"
            return []
        if (not isinstance(proposal, dict) or set(proposal) != {"calls"}
                or not isinstance(proposal["calls"], list)
                or any(not isinstance(c, dict) or not {"name"}.issubset(c) or not set(c).issubset({"name", "arguments"})
                       or not isinstance(c["name"], str)
                       for c in proposal["calls"])):
            self.reject({"content": proposal_content(proposal)}, "malformed_response")
            return []
        turn = len(self.responses) + 1
        content = [{"type": "tool_use", "id": f"proposal-{turn}-{i}", "name": c["name"], "input": deepcopy(c.get("arguments"))}
                   for i, c in enumerate(proposal["calls"], 1)]
        self.responses.append({"content": content})
        self.messages.append({"role": "assistant", "content": deepcopy(proposal)})
        results = []
        if not content:
            self.status = "ended_without_terminal_tool"
        for i, call in enumerate(content):
            if len(self.episode.events) >= MAX_ACTIONS:
                self.status = "action_limit"
                break
            result = self.episode.invoke(call["name"], call["input"], model_turn=turn)
            results.append({"tool_use_id": call["id"], "result": result})
            if self.episode.terminal:
                self.status = result["status"]
                self.trailing_tool_calls_ignored = len(content) - i - 1
                break
        self.messages.append({"role": "user", "content": deepcopy(results)})
        if self.status == "running" and len(self.responses) == MAX_RESPONSES:
            self.status = "turn_limit"
        return results


def proposal_schema(definitions):
    # JSON text avoids altering the legacy optional-argument semantics to satisfy
    # strict structured-output object constraints. The parent parses/validates it.
    return {"type": "object", "properties": {"calls_json": {"type": "string"}},
            "required": ["calls_json"], "additionalProperties": False}


def decode_proposal(text):
    outer = json.loads(text)
    if not isinstance(outer, dict) or set(outer) != {"calls_json"} or not isinstance(outer["calls_json"], str):
        raise ValueError("Malformed proposal envelope")
    return {"calls": json.loads(outer["calls_json"])}


def proposal_content(proposal):
    """Recover explicit JSON call wrappers only; never parse prose or reasoning."""
    calls = proposal.get("calls", []) if isinstance(proposal, dict) else []
    if not isinstance(calls, list):
        return []
    return [{"type": "tool_use", "name": c["name"], "input": deepcopy(c.get("arguments"))}
            for c in calls if isinstance(c, dict) and isinstance(c.get("name"), str)]
