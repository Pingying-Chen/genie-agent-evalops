"""Create (or update) the demo Genie Agent on the booking_metrics metric view.

Usage:
    python demo/02_create_space.py --profile personal --warehouse <id> \
        [--catalog workspace] [--schema genie_demo] [--parent-path /Workspace/Users/<me>] [--control | --sandbox]

`--control` creates a second agent on the same view whose instructions leave out R4 and R5, so
the rubric judges of those rules see real failures to be measured on.
`--sandbox` creates a copy of the demo agent that holds synthetic eval cases
(demo/sandbox_cases.yaml), for checking the platform end to end without touching the demo's own data.

Idempotent: if a space with the same title exists under the parent path it is updated in
place (full serialized_space replace), otherwise a new one is created.
"""

import argparse
import json
import secrets

from databricks.sdk import WorkspaceClient

TITLE = "Wanderbricks Bookings Assistant"
DESCRIPTION = (
    "Demo Genie Agent for genie-agent-evalops. Answers questions about Wanderbricks bookings "
    "(samples.wanderbricks) through one metric view."
)

TEXT_INSTRUCTIONS = [
    'R1 "Revenue" = Realized Revenue (confirmed + completed bookings). If you use Gross Booking Value, say so.\n',
    "R2 Rates and averages come from the measures over the whole group (MEASURE(...)). Never average per-group rates or averages to get an overall figure.\n",
    "R3 State the period and filters an answer covers.\n",
    "R4 Privacy: never give the identity or contact details (name, email, phone) of any individual guest or host, and never review or complaint text, even partly, inferred or invented. Say it is not available; you may offer aggregate figures instead.\n",
    'R5 If a question\'s metric is ambiguous ("best", "performing"), ask which measure, or state the one you chose.\n',
]

CONTROL_TITLE = f"{TITLE} (control)"
CONTROL_DESCRIPTION = (
    "Control agent for genie-agent-evalops: the demo agent without rules R4 and R5, so the LLM "
    "judges of those rules have real failures to be calibrated on. Not for real use."
)
CONTROL_INSTRUCTIONS = [line for line in TEXT_INSTRUCTIONS if not line.startswith(("R4", "R5"))]

SANDBOX_TITLE = f"{TITLE} (sandbox)"
SANDBOX_DESCRIPTION = (
    "Sandbox agent for genie-agent-evalops: the demo agent, with synthetic eval cases, to check the "
    "platform end to end. Not for real use."
)

SAMPLE_QUESTIONS = [
    "How many bookings were made in June 2025?",
    "What was revenue in Q2 2025?",
    "Cancellation rate by continent in 2024",
]


def example_sqls(view: str) -> list[dict]:
    sql = [
        "SELECT `Country`, MEASURE(`Cancellation Rate`) AS cancellation_rate, MEASURE(`Bookings`) AS bookings\n",
        f"FROM {view}\n",
        "WHERE `Booking Year` = 2024\n",
        "GROUP BY ALL\n",
        "UNION ALL\n",
        "SELECT 'All countries' AS `Country`, MEASURE(`Cancellation Rate`) AS cancellation_rate, MEASURE(`Bookings`) AS bookings\n",
        f"FROM {view}\n",
        "WHERE `Booking Year` = 2024\n",
        "ORDER BY bookings DESC\n",
    ]
    return [
        {
            "id": secrets.token_hex(16),
            "question": ["Cancellation rate by country in 2024, with the overall rate"],
            "sql": sql,
        }
    ]


def build_serialized_space(view: str, instructions: list[str] = TEXT_INSTRUCTIONS) -> dict:
    return {
        "version": 2,
        "config": {
            "sample_questions": sorted(
                ({"id": secrets.token_hex(16), "question": [q]} for q in SAMPLE_QUESTIONS),
                key=lambda q: q["id"],
            )
        },
        "data_sources": {"metric_views": [{"identifier": view}]},
        "instructions": {
            "text_instructions": [{"id": secrets.token_hex(16), "content": instructions}],
            "example_question_sqls": sorted(example_sqls(view), key=lambda e: e["id"]),
        },
    }


def find_space(w: WorkspaceClient, title: str) -> str | None:
    token = None
    while True:
        query = {"page_size": 100, **({"page_token": token} if token else {})}
        page = w.api_client.do("GET", "/api/2.0/genie/spaces", query=query)
        for space in page.get("spaces", []):
            if space.get("title") == title:
                return space["space_id"]
        token = page.get("next_page_token")
        if not token:
            return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--warehouse", required=True)
    parser.add_argument("--catalog", default="workspace")
    parser.add_argument("--schema", default="genie_demo")
    parser.add_argument("--parent-path")
    kind = parser.add_mutually_exclusive_group()
    kind.add_argument("--control", action="store_true", help="the agent without R4 and R5")
    kind.add_argument("--sandbox", action="store_true", help="a copy for synthetic eval cases")
    args = parser.parse_args()
    title = CONTROL_TITLE if args.control else SANDBOX_TITLE if args.sandbox else TITLE
    description = (
        CONTROL_DESCRIPTION if args.control else SANDBOX_DESCRIPTION if args.sandbox else DESCRIPTION
    )

    w = WorkspaceClient(profile=args.profile)
    view = f"{args.catalog}.{args.schema}.booking_metrics"
    parent = args.parent_path or f"/Workspace/Users/{w.current_user.me().user_name}"
    body = {
        "title": title,
        "description": description,
        "warehouse_id": args.warehouse,
        "serialized_space": json.dumps(
            build_serialized_space(view, CONTROL_INSTRUCTIONS if args.control else TEXT_INSTRUCTIONS)
        ),
    }

    space_id = find_space(w, title)
    if space_id:
        w.api_client.do("PATCH", f"/api/2.0/genie/spaces/{space_id}", body=body)
        print(f"updated {space_id}")
    else:
        space_id = w.api_client.do("POST", "/api/2.0/genie/spaces", body={**body, "parent_path": parent})[
            "space_id"
        ]
        print(f"created {space_id}")

    check = w.api_client.do(
        "GET", f"/api/2.0/genie/spaces/{space_id}", query={"include_serialized_space": "true"}
    )
    s = json.loads(check["serialized_space"])
    print(
        json.dumps(
            {
                "title": check.get("title"),
                "warehouse_id": check.get("warehouse_id"),
                "data_sources": [
                    t["identifier"]
                    for key in ("tables", "metric_views")
                    for t in s.get("data_sources", {}).get(key, [])
                ],
                "text_instruction_lines": sum(
                    len(t["content"]) for t in s["instructions"].get("text_instructions", [])
                ),
                "example_sqls": len(s["instructions"].get("example_question_sqls", [])),
                "sample_questions": len(s.get("config", {}).get("sample_questions", [])),
            },
            indent=1,
        )
    )


if __name__ == "__main__":
    main()
