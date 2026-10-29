"""Give yourself the servicePrincipal.user role on service principals, so the bundle can run jobs as them.

    uv run python scripts/sp_user.py --profile <p> <application id> [<application id> ...]

A workspace admin does not have this role by default, and Free Edition has no account console to
grant it in. Repeating it is safe.
"""

import argparse

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.iam import GrantRule, RuleSetUpdateRequest

ROLE = "roles/servicePrincipal.user"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("sps", nargs="+", metavar="application_id")
    args = parser.parse_args()
    w = WorkspaceClient(profile=args.profile)
    if not w.config.account_id:
        parser.error(f"profile {args.profile} has no account_id: run `databricks auth login` again")
    me = f"users/{w.current_user.me().user_name}"
    for sp in args.sps:
        name = f"accounts/{w.config.account_id}/servicePrincipals/{sp}/ruleSets/default"
        current = w.account_access_control_proxy.get_rule_set(name, "")
        rules = list(current.grant_rules or [])
        if any(r.role == ROLE and me in (r.principals or []) for r in rules):
            print(f"{sp}: already {ROLE}")
            continue
        rules.append(GrantRule(role=ROLE, principals=[me]))
        w.account_access_control_proxy.update_rule_set(
            name, RuleSetUpdateRequest(name=name, etag=current.etag, grant_rules=rules)
        )
        print(f"{sp}: granted {ROLE} to {me}")


if __name__ == "__main__":
    main()
