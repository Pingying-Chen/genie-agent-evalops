"""Ask the demo Genie Agent its seed questions in agent mode, to create traffic for the collector.

Usage:
    python demo/03_seed_questions.py --profile personal --space <space_id> [--only S01,S03] [--multi-turn]
        [--probes]   # the R4 / R5 rule probes instead of the seed questions

Each question starts a new conversation (multi-turn scripts continue one). Raw SSE events
are written to demo/out/<id>.jsonl so the agent API's response shape can be inspected.
"""

import argparse
import json
import pathlib

import requests
import yaml
from databricks.sdk import WorkspaceClient

HERE = pathlib.Path(__file__).parent


def ask(
    w: WorkspaceClient, space: str, text: str, conversation_id: str | None
) -> tuple[str | None, list[dict]]:
    body = {
        "input": [{"role": "user", "type": "message", "content": [{"type": "input_text", "text": text}]}],
        "enable_viz": True,
    }
    if conversation_id:
        body["conversation_id"] = conversation_id
    headers = {**w.config.authenticate(), "Accept": "text/event-stream"}
    url = f"{w.config.host}/api/2.0/genie/agents/{space}/responses"
    events, event_name = [], None
    with requests.post(url, json=body, headers=headers, stream=True, timeout=900) as r:
        r.raise_for_status()
        for raw in r.iter_lines(decode_unicode=True):
            if not raw:
                continue
            if raw.startswith("event:"):
                event_name = raw[6:].strip()
            elif raw.startswith("data:"):
                data = raw[5:].strip()
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    payload = data
                events.append({"event": event_name, "data": payload})
                if isinstance(payload, dict):
                    conversation_id = (
                        conversation_id
                        or payload.get("conversation_id")
                        or (payload.get("response") or {}).get("conversation_id")
                    )
    return conversation_id, events


def summarize(events: list[dict]) -> str:
    sql_calls = sum(
        1
        for e in events
        if isinstance(e["data"], dict)
        and (e["data"].get("item") or {}).get("name") == "execute_sql"
        and e["event"]
        and e["event"].endswith("done")
    )
    kinds = sorted({e["event"] for e in events if e["event"]})
    return f"{len(events)} events, sql_calls={sql_calls}, event types={kinds}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--space", required=True)
    parser.add_argument("--only", help="comma-separated question ids")
    parser.add_argument("--multi-turn", action="store_true")
    parser.add_argument("--probes", action="store_true", help="ask the rule probes instead")
    args = parser.parse_args()

    w = WorkspaceClient(profile=args.profile)
    seeds = yaml.safe_load((HERE / "seed_questions.yaml").read_text())
    out = HERE / "out"
    out.mkdir(exist_ok=True)
    only = set(args.only.split(",")) if args.only else None

    for q in seeds["rule_probes" if args.probes else "single_turn"]:
        if only and q["id"] not in only:
            continue
        conv, events = ask(w, args.space, q["question"], None)
        name = f"{args.space[:8]}_{q['id']}" if args.probes else q["id"]  # probes run on two agents
        (out / f"{name}.jsonl").write_text("\n".join(json.dumps(e) for e in events))
        print(f"{q['id']} conv={conv} {summarize(events)}")

    if args.multi_turn:
        for script in seeds["multi_turn"]:
            if only and script["id"] not in only:
                continue
            conv = None
            for i, turn in enumerate(script["turns"], 1):
                conv, events = ask(w, args.space, turn, conv)
                (out / f"{script['id']}_t{i}.jsonl").write_text("\n".join(json.dumps(e) for e in events))
                print(f"{script['id']} turn {i} conv={conv} {summarize(events)}")


if __name__ == "__main__":
    main()
