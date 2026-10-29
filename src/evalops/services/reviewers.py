"""Who may review a space's traces (mechanism A).

Reviewer access is a Databricks group per space, found by name: `EVALOPS_REVIEWER_GROUP_PATTERN`
(default `genie-reviewers-{space_id}`). Adding someone to the group makes them a reviewer of
that space; no table of our own.

Membership is read from the reviewer's own `/Me` (with their token): non-admins cannot list
a group's members, so the platform never needs admin rights to know who reviews.
Reviewing is separate from managing: a reviewer needs no CAN_MANAGE on the space.
"""

import re

from evalops.clients.scim import ScimClient


def group_name(pattern: str, space_id: str) -> str:
    return pattern.format(space_id=space_id)


def reviewer_space_ids(groups: list[str], pattern: str) -> list[str]:
    """Space ids encoded in the names of the caller's reviewer groups, sorted."""
    prefix, _, suffix = pattern.partition("{space_id}")
    matcher = re.compile("^" + re.escape(prefix) + r"(?P<space>[0-9a-zA-Z_-]+)" + re.escape(suffix) + "$")
    return sorted({m.group("space") for g in groups if (m := matcher.match(g))})


async def my_review_spaces(*, scim_as_viewer: ScimClient, pattern: str) -> list[str]:
    """The spaces the caller reviews, from their own group memberships."""
    return reviewer_space_ids(await scim_as_viewer.my_groups(), pattern)
