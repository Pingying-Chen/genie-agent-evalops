"""Apply a change's operations to a space configuration.

Pure: configuration text in, configuration text out. An operation that does not fit the
configuration (a line to replace that is not there, a column the space does not have) raises
`ValidationFailed`, so a change never silently does less than it says.
"""

import copy
import json
import secrets

from evalops.errors import ValidationFailed
from evalops.schemas.changes import (
    AddInstruction,
    AddSqlExample,
    Operation,
    RemoveInstruction,
    ReplaceInstruction,
    SetColumn,
)


def _new_id() -> str:
    """Genie's ids are 32 lowercase hex characters."""
    return secrets.token_hex(16)


def _instruction_lines(space: dict) -> list[str]:
    blocks = space.get("instructions", {}).get("text_instructions", [])
    return [line for b in blocks for line in "".join(b.get("content", [])).splitlines()]


def _set_instruction_lines(space: dict, lines: list[str]) -> None:
    """Keep the first block's id (one block, one line per content entry, as the demo writes it)."""
    instructions = space.setdefault("instructions", {})
    blocks = instructions.get("text_instructions") or [{"id": _new_id()}]
    instructions["text_instructions"] = [{**blocks[0], "content": [f"{line}\n" for line in lines]}]


def apply(serialized_space: str, operations: list[Operation]) -> str:
    space = copy.deepcopy(json.loads(serialized_space or "{}"))
    for n, op in enumerate(operations, start=1):
        if isinstance(op, AddInstruction | ReplaceInstruction | RemoveInstruction):
            lines = _instruction_lines(space)
            stripped = [line.strip() for line in lines]
            if isinstance(op, AddInstruction):
                lines.append(op.text)
            else:
                if op.old not in stripped:
                    raise ValidationFailed(f"Operation {n}: no instruction line reads {op.old!r}.")
                i = stripped.index(op.old)
                if isinstance(op, ReplaceInstruction):
                    lines[i] = op.text
                else:
                    del lines[i]
            _set_instruction_lines(space, [line for line in lines if line.strip()])
        elif isinstance(op, AddSqlExample):
            examples = space.setdefault("instructions", {}).setdefault("example_question_sqls", [])
            examples.append(
                {
                    "id": _new_id(),
                    "question": [op.question],
                    "sql": [f"{line}\n" for line in op.sql.splitlines()],
                }
            )
            examples.sort(key=lambda e: e.get("id", ""))  # Genie rejects examples not sorted by id
        elif isinstance(op, SetColumn):
            tables = space.get("data_sources", {}).get("tables", [])
            table = next((t for t in tables if t.get("identifier") == op.table), None)
            if table is None:
                raise ValidationFailed(f"Operation {n}: the space has no table {op.table!r}.")
            configs = table.setdefault("column_configs", [])
            column = next((c for c in configs if c.get("column_name") == op.column), None)
            if column is None:
                column = {"column_name": op.column}
                configs.append(column)
            if op.description is not None:
                column["description"] = [op.description]
            if op.synonyms is not None:
                column["synonyms"] = [s.strip() for s in op.synonyms if s.strip()]
    return json.dumps(space)


def describe(operations: list[Operation]) -> list[str]:
    """One line per operation, for the change's page and the run's record."""
    out = []
    for op in operations:
        if isinstance(op, AddInstruction):
            out.append(f"Add instruction: {op.text}")
        elif isinstance(op, ReplaceInstruction):
            out.append(f"Replace instruction “{op.old}” with: {op.text}")
        elif isinstance(op, RemoveInstruction):
            out.append(f"Remove instruction: {op.old}")
        elif isinstance(op, AddSqlExample):
            out.append(f"Add SQL example for: {op.question}")
        elif isinstance(op, SetColumn):
            parts = [
                p
                for p in (
                    "description" if op.description is not None else "",
                    "synonyms" if op.synonyms is not None else "",
                )
                if p
            ]
            out.append(f"Set {' and '.join(parts) or 'nothing'} of {op.table}.{op.column}")
    return out
