"""Tests for reviewer membership: the per-space group convention, read from the caller's /Me."""

import respx

from evalops.clients.scim import ScimClient
from evalops.services.reviewers import group_name, my_review_spaces, reviewer_space_ids

PATTERN = "genie-reviewers-{space_id}"


def test_the_group_name_is_derived_from_the_space_id():
    assert group_name(PATTERN, "s1") == "genie-reviewers-s1"


def test_reviewer_spaces_are_parsed_from_matching_group_names_only():
    groups = [
        "users",
        "genie-reviewers-01f1abc",
        "genie-reviewers-02f2def",
        "genie-reviewers-",
        "x-genie-reviewers-03",
    ]
    assert reviewer_space_ids(groups, PATTERN) == ["01f1abc", "02f2def"]


def test_a_custom_pattern_with_a_suffix_works():
    assert reviewer_space_ids(["team-a-s9-reviewers", "team-a-s9"], "team-a-{space_id}-reviewers") == ["s9"]


async def test_my_review_spaces_reads_the_callers_own_groups(http, auth):
    scim = ScimClient(http, auth)
    with respx.mock(base_url=auth.host) as mock:
        me = mock.get("/api/2.0/preview/scim/v2/Me").respond(
            200,
            json={
                "userName": "r@example.com",
                "groups": [{"display": "users"}, {"display": "genie-reviewers-s1"}],
            },
        )

        assert await my_review_spaces(scim_as_viewer=scim, pattern=PATTERN) == ["s1"]

    assert me.calls.last.request.headers["Authorization"] == "Bearer test"  # the caller's own token
