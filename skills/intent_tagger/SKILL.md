---
name: intent_tagger
version: 1
purpose: Give each question asked of a data assistant a short intent label, so production traffic can be compared with the eval set.
input_schema: IntentInput
output_schema: IntentOutput
# Labels feed suggestions an owner reviews; one sample, cached per question.
model: {temperature: 0.0, max_tokens: 2500, samples: 1}
---
You label questions people asked a data assistant ("Genie") with their INTENT: what they
want measured, broken down by what, and what kind of filter, without the specific values.
Two questions that a single test question would cover get the same intent.

- Write the intent as a short lowercase phrase, 3 to 8 words, e.g. "revenue by destination",
  "cancellation rate above a threshold", "personal details of a host".
- Leave out specific values (years, names, numbers): "bookings in 2024" and "bookings in 2023"
  are both "bookings for a period".
- A follow-up's intent is what the follow-up adds, read with the question it follows
  ("only the ones above 20%" after a ranking is "ranking filtered by a threshold").
- REUSE an intent from the list below whenever it fits; make a new one only when none does.

Return one tag per numbered question, in order: "number" and "intent".

## Intents already in use
{intents}

## Questions
{questions}
