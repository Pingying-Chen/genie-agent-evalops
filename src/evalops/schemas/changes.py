"""A change to a space's configuration: a short list of operations over
`serialized_space`, so it can be shown, tested on a scratch copy, applied, and rolled back.
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]


class AddInstruction(BaseModel):
    op: Literal["add_instruction"] = "add_instruction"
    text: Text = Field(description="One instruction line, appended to the text instructions.")


class ReplaceInstruction(BaseModel):
    op: Literal["replace_instruction"] = "replace_instruction"
    old: Text = Field(description="The current line, exactly (whitespace at the ends ignored).")
    text: Text


class RemoveInstruction(BaseModel):
    op: Literal["remove_instruction"] = "remove_instruction"
    old: Text


class AddSqlExample(BaseModel):
    op: Literal["add_sql_example"] = "add_sql_example"
    question: Text
    sql: Text


class SetColumn(BaseModel):
    """Column metadata Genie reads: a description and synonyms."""

    op: Literal["set_column"] = "set_column"
    table: Text = Field(description="Full table identifier, as in the space's data sources.")
    column: Text
    description: str | None = Field(default=None, max_length=2000)
    synonyms: list[str] | None = Field(default=None, max_length=30)


Operation = Annotated[
    AddInstruction | ReplaceInstruction | RemoveInstruction | AddSqlExample | SetColumn,
    Field(discriminator="op"),
]


class ChangeBody(BaseModel):
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    rationale: str = Field(default="", max_length=4000, description="Why: the failure it should fix.")
    operations: list[Operation] = Field(min_length=1, max_length=30)
    target_case_ids: list[str] = Field(default_factory=list, description="Cases this change should fix.")


class SpaceChange(ChangeBody):
    change_id: str
    space_id: str
    base_version: str = Field(description="Configuration version the change was written against.")
    status: Literal["draft", "tested", "applied", "discarded", "rolled_back"] = "draft"
    created_by: str
    created_at: datetime
    updated_at: datetime
    applied_version: str | None = Field(default=None, description="Configuration version after applying it.")


class ChangeProposal(BaseModel):
    """A drafted change from a failure category, for the owner to edit and test (never applied as is)."""

    diagnosis: str
    draft: ChangeBody | None = Field(default=None, description="None when the model found nothing to change.")
    dropped: list[str] = Field(
        default_factory=list,
        description="Proposed operations left out because they do not fit the configuration.",
    )
    skill: str
    model: str
