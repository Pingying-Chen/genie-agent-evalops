"""Train/test leakage between a space's SQL examples and its eval set.

A space's SQL examples are what Genie learns from; the eval set is what it is tested on. If a
case's question is also a SQL example, the case passes because Genie has the answer in front of
it, which says nothing about questions like it. Matching is deterministic: the same words after
normalising case, punctuation and spacing, or nearly the same (word-set Jaccard >= 0.8).
"""

import json
import re

from evalops.schemas.changes import AddSqlExample, Operation
from evalops.schemas.evalset import EvalCase, Leak

NEAR = 0.8


def words(text: str) -> list[str]:
    return re.sub(r"[^\w%]+", " ", text.lower()).split()


def similar(a: str, b: str) -> bool:
    wa, wb = words(a), words(b)
    if not wa or not wb:
        return False
    if wa == wb:
        return True
    sa, sb = set(wa), set(wb)
    return len(sa & sb) / len(sa | sb) >= NEAR


def leaks(examples: list[str], cases: list[EvalCase], splits: dict[str, str] | None = None) -> list[Leak]:
    """Every (active case turn, example) pair that match. Any turn counts: an earlier turn given
    as an example hands Genie the context the graded turn is meant to test. `splits` (case id ->
    dev | test) marks which matches are real leaks (test) and which are allowed (dev)."""
    found = []
    for case in cases:
        if case.status != "active":
            continue
        for turn, question in enumerate(case.turns):
            for example in examples:
                if similar(question, example):
                    found.append(
                        Leak(
                            case_id=case.case_id,
                            title=case.title,
                            turn=turn,
                            case_question=question,
                            example_question=example,
                            split=(splits or {}).get(case.case_id),
                        )
                    )
    return found


def added_examples(operations: list[Operation]) -> list[str]:
    return [op.question for op in operations if isinstance(op, AddSqlExample)]


def config_examples(serialized_space: str) -> list[str]:
    space = json.loads(serialized_space or "{}")
    return [
        "".join(e.get("question", [])) for e in space.get("instructions", {}).get("example_question_sqls", [])
    ]
