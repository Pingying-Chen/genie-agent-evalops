"""Register the sandbox agent with the platform and load its synthetic eval cases.

Usage:
    python demo/04_seed_sandbox.py --profile personal --space <sandbox space id> \
        --collector <collector SP application id> --app https://<app url>

1. Grants the collector CAN_MANAGE on the space (that grant is the registration).
2. Imports demo/sandbox_cases.yaml through the app's eval set import, as you (your token), so
   the cases go through the same validation as any owner's. Re-running changes nothing.
"""

import argparse
import json
import pathlib
import urllib.request

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.iam import AccessControlRequest, PermissionLevel

CASES = pathlib.Path(__file__).with_name("sandbox_cases.yaml")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--space", required=True)
    parser.add_argument("--collector", required=True, help="the collector service principal's application id")
    parser.add_argument("--app", required=True, help="the app's URL")
    args = parser.parse_args()

    w = WorkspaceClient(profile=args.profile)
    w.permissions.update(
        "genie",
        args.space,
        access_control_list=[
            AccessControlRequest(
                service_principal_name=args.collector, permission_level=PermissionLevel.CAN_MANAGE
            )
        ],
    )
    print(f"collector {args.collector} can manage {args.space}")

    token = w.config.oauth_token().access_token
    request = urllib.request.Request(
        f"{args.app.rstrip('/')}/api/spaces/{args.space}/evalset/import",
        data=CASES.read_bytes(),
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/yaml"},
    )
    with urllib.request.urlopen(request) as response:
        print("imported:", json.loads(response.read()))


if __name__ == "__main__":
    main()
