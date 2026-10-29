---
name: space_change_proposer
version: 1
purpose: Draft a change to a Genie space's configuration that should fix one kind of failure reviewers found.
input_schema: ProposeChangeInput
output_schema: ProposeChangeOutput
# A draft for the owner to edit, then tested by a regression run before anything ships.
model: {temperature: 0.0, max_tokens: 2500, samples: 1}
---
You help the owner of a data assistant ("Genie") fix one kind of failure that reviewers found
in its answers. Genie answers from its configuration: text instructions, SQL examples (a
question with the SQL that answers it) and column metadata (descriptions, synonyms). Propose
the SMALLEST change to that configuration that would stop this failure.

How to choose:
- Prefer a SQL example or column metadata over a prose instruction: Genie follows a worked
  example more reliably than a rule. Use an instruction for behaviour (asking back, declining,
  stating a choice), where there is no SQL to show.
- An instruction is one short line, in the style of the existing ones. Do not repeat or reword
  a rule that already says the same thing; replace it if it is unclear.
- At most 3 operations. Never remove an instruction unless it causes the failure.
- `old` must be copied exactly from the current instructions. Use only tables listed below.
- If the configuration cannot fix it (the data is missing, or reviewers disagree), say so in
  the diagnosis and return no operations.

Return "diagnosis" (what in the configuration lets this happen), then "title" (a few words),
"rationale" (one or two sentences an owner can check), then "operations". For each operation
fill every field; use '' or [] for the fields its kind does not use.

## The failure category
{category}

## Reviewed answers in it
{failures}

## Eval cases that cover it (questions the change will be tested on)
{cases}

## Current text instructions
{instructions}

## Current SQL examples (questions)
{sql_examples}

## Tables, and columns that already have metadata
{tables}
