---
name: categorize_observation
version: 1
purpose: Suggest which failure category one reviewed answer belongs to, or a new one, for the owner to accept or ignore.
input_schema: CategorizeInput
output_schema: CategorizeOutput
# A suggestion, never applied without the owner, so one sample is enough.
model: {temperature: 0.0, max_tokens: 800, samples: 1}
---
A person reviewed one answer from a data assistant ("Genie") and either failed it or wrote down
something they noticed. Sort it into a failure category, the way an analyst groups review notes
before deciding which problem to fix first.

A category names a FAILURE MODE: what the assistant did wrong, in words that would fit other
questions too ("Applies a filter the user did not ask for", "Exposes internal table names"). It
does not name the topic of the question ("20% cancellation question") or the data involved.

Work in this order, and return the JSON fields in this order:
1. "reasoning": in one or two sentences, what went wrong according to the reviewer, then whether
   an existing category describes the same failure mode. Same mode with different data = same
   category. Do not invent problems the reviewer did not describe.
2. "category": the exact name of that existing category, or a new name of at most 8 words.
3. "is_new": true only if no existing category fits.
4. "description": for a new category, one sentence on what this failure looks like in general;
   "" for an existing one.

## Existing categories (name: description; may be empty)
{categories}

## Question the assistant answered
{question}

## Reviewer's verdict
{verdict}

## Reviewer's reason
{rationale}

## What the reviewer noticed
{findings}
