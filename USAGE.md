# Using genie-agent-evalops

The details behind each step of [Use it](README.md#use-it) in the README.

## 1. Check real answers

**Overview** lists your spaces: how many conversations could be graded, the pass rate of must-pass rules, and the last collect. **Collect now** reads and checks new conversations.

Each check reads only what is new since the last one, so a busy space costs what its new traffic costs. For a high-traffic space, set in **Settings** the share of new answers the LLM judges read (a stable random sample, so the rates stay unbiased) and the most judgements per run (the rest wait for the next run). Code rules always check every answer. When you change the rules, the last 30 days are re-checked.

Only [agent-mode](https://docs.databricks.com/aws/en/genie-agents/concepts#agent-mode) conversations are graded. Chat-mode conversations, older ones, and conversations Genie does not let a manager read are counted but not graded.

**A space** shows each rule's pass rate, a daily trend and its answers, filterable by verdict and rule. **An answer** shows the question and report next to every step: its SQL, the first rows of its result, and the verdicts. A number in the report that no query supports is highlighted.

**Rules.** Each rule has a gate:
- `hard`: a failure fails the answer;
- `soft`: counted and shown;
- `monitor`: tracked only. LLM judges start here.

Built in, for any Genie agent:

| Rule | Gate | Checks |
|---|---|---|
| Gives an answer | hard | The turn ends with an answer, not a Genie error (often a sign the asker lacks data access) |
| Every number comes from a query | hard | Each figure matches a query result, or a difference, ratio or percent change of two. Measured: 0 false alarms in 218 real figures; 94 of 104 injected errors caught |
| Every number is said of the right thing | hard | A figure found in a result is said of its own row and column: "Asia had $4.2M" fails when $4.2M is Europe's. Measured: 0 false alarms in 356 real figures; 89 of 94 swapped figures caught |
| No numbers without a query | hard | Figures stated but no SQL ran |
| No contact details in the answer | hard | No email addresses or phone numbers |
| Says which period it covers | soft | When a query filters on a period, the report names it |
| No repeated queries | soft | The same SQL is not run twice in one answer |
| Schema exploration steps | monitor | Steps that only probe the schema instead of answering |

**Your own rules** go in a folder set by `EXTRA_CRITERIA_DIR` (copy `demo/criteria` to start):
- **rules about your data**, e.g. tables and columns that must never be queried;
- **LLM judges of your space's instructions**: one file per instruction, saying when it applies and what breaks it.

```yaml
id: rubric.out_of_scope_refusal
title: Declines out-of-scope questions
version: 1            # raise it when you change the wording: reviewers label it afresh
family: rubric
layer: behaviour
gate: monitor         # until your reviewers have calibrated it
applies_to: {level: report}
description: Questions about individual guests or hosts must be declined.
params:
  skill: rubric_judge
  rule: >-
    Applies only when the question asks for a specific individual, contact details or review
    text. Then the report must say the information is not available, and must not provide it.
  failure: The question is out of scope and the report answers it anyway.
```

These folders are the deployment's **library**. Each space can then change it for itself on its **Settings** page (see [8. Configure each agent](#8-configure-each-agent)): turn rules off, change a gate, give a code rule its own parameters, or add its own LLM-judged rules.

**Customize the LLM skills.** Every LLM step is a *skill*: a prompt with its model settings in `skills/<name>/SKILL.md`. Rules decide *what* is judged; skills decide *how* the LLM judges it, so you can adapt the evaluation to your domain, language or model without touching code.

| Skill | Used by | What it does |
|---|---|---|
| `rubric_judge` | Checks of real answers, and regression runs | Grades one answer against one of your `rubric.*` rules: does the rule apply, and does the answer break it |
| `expectation_judge` | Regression runs | Decides whether an answer meets each fact of its eval case, and asks back or declines when the case says so |
| `categorize_observation` | Review job → **What reviewers found** | Suggests a failure category for each answer a reviewer failed or commented on |
| `intent_tagger` | Review job → eval set **Suggestions**, dev/test split | Gives every real question and every eval case a short intent label |
| `space_change_proposer` | **Draft change** on a failure category | Drafts a change to the space (instructions, SQL examples, column metadata) from that category's failures |

To change one:

1. **Edit** `skills/<name>/SKILL.md`. The front matter holds the version and the model settings (`temperature`, `max_tokens`, `samples`); the body is the prompt. Keep its `{placeholders}` and the JSON shape it asks for: the code fills the first and reads the second.
2. **Raise `version`.** A judge's agreement with your reviewers is measured per version, so an edited prompt starts its own calibration. A deploy whose prompt changed without a new version is refused: the jobs stop before producing any numbers under the wrong name.
3. **Try it first** (judges): `scripts/export_judge_labels.py` exports your reviewers' labels for a rule as a dev and a held-out file, and `scripts/judge_experiment.py` compares the current and the edited prompt on them. Change the prompt against dev; keep it only if the held-out file does not get worse.
4. **Deploy** (`scripts/deploy.sh`). New verdicts use the new version; **Versions** shows the old and new prompt side by side, and **Evaluators** shows the new version's calibration as labels arrive.

**Keep the guard if you write a prompt.** The judges wrap everything they grade (the question, the SQL steps, the report) in `<data>` tags and are told that text inside is never an instruction. That's because a judge reads what it grades: an answer, or data behind it, could say "this answer is correct". Keep the tags in your own prompts.

**Choose the model.** By default all skills call one serving endpoint, the `JUDGE_MODEL` setting: `databricks-qwen3-next-80b-a3b-instruct`, an off-the-shelf model at temperature 0, as the AI Evals Playbook recommends for judges. Each space can give any skill its own model on its Settings page, for example a reasoning model for drafting changes and a small one for tagging intents. Point a skill at whichever model your workspace offers and you trust:
- a [Foundation Model API](https://docs.databricks.com/aws/en/machine-learning/foundation-model-apis/) pay-per-token model available in your region;
- or a [provisioned throughput](https://docs.databricks.com/aws/en/machine-learning/foundation-model-apis/) endpoint of your own.

It must be a chat model that supports [structured outputs](https://docs.databricks.com/aws/en/machine-learning/model-serving/structured-outputs), because every skill asks for a strict JSON answer. External-model endpoints (another provider's model behind Databricks) are not tested yet.

The deploy grants the app permission to query the endpoint. Make sure the collector service principal can query it too: it runs the judges.

Changing the model restarts each judge's calibration, since agreement is measured per prompt and model. A judge can also be compared on two models offline first, with `judge_experiment.py --model`. Each skill keeps its own sampling settings (`temperature`, `max_tokens`, `samples`) in its front matter.

## 2. Review and calibrate the judges

The review job puts a sample of answers into a [labeling session](https://docs.databricks.com/aws/en/mlflow3/genai/human-feedback/concepts/labeling-sessions) for the space's reviewers. The sample favours answers the judges failed or were unsure about, with part of it random. Reviewers see the conversation, the SQL and its results, and answer:
- does the answer answer the question;
- what went wrong, if anything;
- pass, fail or does-not-apply for each judged rule.

A judge's own verdict is shown on an answer only after a reviewer has labelled it.

**Evaluators** shows, for each judge, how often it agrees with people:
- passes good answers (TPR) and catches real failures (TNR), each with a 95% lower bound (pass is the positive class, as in the AI Evals Playbook);
- how many of its failures are real (precision), and Cohen's κ.

A judge is **calibrated** when both lower bounds reach 80%. Only then can it fail anything in a test of a change. Expect to label a few dozen answers per rule, including at least 16 real failures (ten agreements out of ten only gives a 72% lower bound). Rules written as code decide from the start.

**Disagreements** lists every answer where a judge and a reviewer differ, so you can tell a wrong judge from an unclear rule.

## 3. Watch performance over time

**Performance** is each space's dashboard: the Space page shows how things are now, this page shows the trend (12 weeks by default).

| Section | Shows |
|---|---|
| **The agent** | Per week: answers and the share that could be graded; the must-pass rate, measured with current verdicts (after a rule change the last 30 days are re-checked, so recent weeks stay comparable); task success as judged by reviewers, with its 95% interval; each LLM judge's fail rate; median and p90 time to answer, SQL steps per answer and Genie's error rate. Every change to the space is a marker on the charts |
| **The judges** | Each judge's agreement with reviewers at every review run, so you can see it approach the calibration line; and from the current verdicts: how often it says the rule does not apply, how stable its votes are, its error rate and tokens per verdict |
| **The reviewers** | Per reviewer: answers reviewed, turnaround, how strict they are next to the rest, how often their fails come with a reason, and their agreement with the judges. And how much reviewers agree with each other: if they don't, the rule itself is unclear |

## 4. Analyse failures

**What reviewers found** gathers every answer a reviewer failed or commented on. Name failure categories, rate their impact and tag answers; categories are ranked by frequency × impact. From a category, draft a new rule, draft a change to the space, or add its answers to the eval set.

## 5. Build an eval set

**Eval set** holds questions a change must not break, each with what a good answer must do. **Add to eval set** on any reviewed answer prefills it; or write one:

| Field | Meaning |
|---|---|
| Questions | One or more turns in one conversation; the last is graded |
| Type | `core` (what users mostly ask), `edge` (unusual but valid), `guardrail` (must decline) |
| A good answer | Answers with data, asks which one is meant, or declines |
| Facts | What a correct answer states or does, in words (an LLM judges these) |
| Rules | Rules that must apply and pass here, or must not apply |
| Reference query | A SELECT run at test time; its values must appear in the answer, said of the right row, so the case never goes stale |
| Data, columns | Must (not) query data; columns or tables it must not touch |

Every edit is kept as a version; **Export / Import YAML** keeps the eval set in git.

**Suggestions** keep it current:
- what users keep asking that no case covers;
- cases nobody asks about lately;
- flaky cases and duplicates;
- cases that are already a SQL example in the space (Genie is shown the answer to those, so passing them proves nothing).

**Dev and test.** Cases are split into **dev**, which you iterate on as often as you like, and **test**, which is held out to decide whether a change ships. Both get the same mix of case types, and cases with the same intent always stay together on one side (a near-identical question in both would leak the test into your tuning). In **Settings** you choose what to balance (or nothing, for a plain random split) and the test share (30% by default). A case never moves once assigned, and you can place one yourself. With only a few cases there is no test set yet; the page says so.

## 6. Test a change before you apply it

A **change** is a short list of edits to the space: an instruction line, a SQL example, a column's description or synonyms ([what makes a good space](https://docs.databricks.com/aws/en/genie-agents/best-practices)). Write one on **Changes**, or use **Draft change** on a failure category. Name the cases it should fix.

1. **Try on dev** asks the dev cases twice, on two scratch copies of the space: as it is, and with the change. Each case is asked several times (3 by default). A case counts as fixed or broken only if every attempt agrees; otherwise it is flaky. Repeat as needed. To iterate faster, **Try on its targets** asks only the cases the change should fix, and on **Eval set** a dev run can ask one slice (one case type, intent, failure category, or multi-turn cases).
2. **Gate run** asks all the test cases plus the cases the change should fix, never a slice. Only this run can accept a change ([how the gate decides](README.md#how-the-gate-decides)). Run it once, when you are done iterating.
3. **Apply** writes the change with your own permissions, if its latest gate run accepted it and nobody edited the space since. **Roll back** restores what it replaced.

Before you start one, each option shows how many Genie questions it asks (cases × k × 2 configurations) and about how many DBU that is: around 1 DBU per question, most of a run's cost.

Test questions are asked on scratch copies, with a consumer's access, so they never show up in your space's real traffic.

## 7. Compare runs and catch Genie changes

**Versions** lists every run with what produced its numbers: the space's configuration, the rules, the judges' prompts, the reviewer instructions, the cases and the tool's version. Pick two runs to see exactly what changed between them.

Genie does not announce model updates. A **weekly drift check** asks each space's test cases on its unchanged configuration. If two checks with identical inputs disagree on a case, Versions flags it.

## 8. Configure each agent

**Settings** holds everything that applies to one agent. Empty fields use the deployment's defaults.

| Section | What you set |
|---|---|
| **Rules** | Turn each rule on or off, change its gate (hard / soft / monitor), give code rules this agent's parameters |
| **This agent's own LLM-judged rules** | One rule per instruction of the space: when it applies and what breaks it, with a version |
| **Models and prompts** | Per LLM step: the model endpoint, temperature, samples, max tokens, reasoning effort, and optionally your own prompt |
| **Thresholds** | k, the calibration bound, review budgets, the window for suggestions, the judges' sample share and cap per run, the days re-checked after a rule change |
| **Release gate** | Test pass-rate floor, at most X below baseline, per-type floors, zero-tolerance guardrails, over-refusal, p95 latency |

Every save is a **version**, with who, when and why. **History** compares any two versions and restores an old one (as a new version), and **Export / Import YAML** keeps the settings in git. A change to a prompt, a sampling setting or a custom rule needs a new version number, because judges are calibrated per version; the page refuses the save otherwise. Every run records the settings version it used, so a gate run keeps the verdict it had even after the settings change.
