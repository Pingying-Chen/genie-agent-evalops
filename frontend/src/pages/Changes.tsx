// Configuration changes: written here, tested on scratch copies against the
// current configuration, applied by the owner. Owners only.
import { useEffect, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { ArrowLeft, FlaskConical, Plus, Trash2 } from "lucide-react";
import { api, type ChangeBody, type ChangeProposal, type ChangeView, type EvalCase } from "@/api";
import { useLoad } from "@/hooks";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState, ErrorBox, Loading, when } from "@/components/common";
import { cn } from "@/lib/utils";
import { estimateText, useEvalK } from "@/components/estimate";

type Operation = ChangeBody["operations"][number];

const OPS: { value: Operation["op"]; label: string }[] = [
  { value: "add_instruction", label: "Add an instruction" },
  { value: "replace_instruction", label: "Replace an instruction" },
  { value: "remove_instruction", label: "Remove an instruction" },
  { value: "add_sql_example", label: "Add a SQL example" },
  { value: "set_column", label: "Describe a column" },
];

const STATUS: Record<ChangeView["status"], "neutral" | "pass" | "warn" | "fail" | "outline"> = {
  draft: "outline",
  tested: "neutral",
  applied: "pass",
  discarded: "outline",
  rolled_back: "warn",
};

function blank(op: Operation["op"]): Operation {
  switch (op) {
    case "add_instruction":
      return { op, text: "" };
    case "replace_instruction":
      return { op, old: "", text: "" };
    case "remove_instruction":
      return { op, old: "" };
    case "add_sql_example":
      return { op, question: "", sql: "" };
    case "set_column":
      return { op, table: "", column: "", description: null, synonyms: null };
  }
}

export function Changes() {
  const { spaceId = "" } = useParams();
  const changes = useLoad(() => api.changes(spaceId), [spaceId]);
  const evalset = useLoad(() => api.evalset(spaceId), [spaceId]);
  const k = useEvalK(spaceId);
  const [writing, setWriting] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [actionError, setActionError] = useState<Error | null>(null);
  const [override, setOverride] = useState<string | null>(null);
  const [params] = useSearchParams();
  const category = params.get("category");
  const [proposal, setProposal] = useState<ChangeProposal | null>(null);
  const [proposing, setProposing] = useState(false);

  useEffect(() => {
    if (!category) return;
    setProposing(true);
    setActionError(null);
    api
      .proposeChange(spaceId, category)
      .then((p) => {
        setProposal(p);
        setWriting(true);
      })
      .catch((err) => setActionError(err as Error))
      .finally(() => setProposing(false));
  }, [spaceId, category]);

  if (changes.loading && !changes.data) return <Loading />;
  if (changes.error) return <ErrorBox error={changes.error} />;
  const cases = (evalset.data?.cases ?? []).filter((c) => c.status === "active");
  const devCount = evalset.data?.split_summary?.dev ?? 0;
  const testIds = new Set((evalset.data?.splits ?? []).filter((s) => s.split === "test").map((s) => s.case_id));

  /** Run an action; false when it failed (the error is shown). */
  async function act(fn: () => Promise<unknown>, done: string): Promise<boolean> {
    setActionError(null);
    setMessage(null);
    setOverride(null);
    try {
      await fn();
      setMessage(done);
      changes.reload();
      return true;
    } catch (err) {
      setActionError(err as Error);
      return false;
    }
  }

  return (
    <div className="space-y-8">
      <div>
        <Link to={`/spaces/${spaceId}`} className="inline-flex items-center gap-1 text-sm text-muted hover:text-ink">
          <ArrowLeft className="size-4" aria-hidden /> Back to the agent
        </Link>
        <h1 className="mt-2 text-2xl font-semibold tracking-tight">Changes to the agent</h1>
        <p className="mt-1 max-w-3xl text-sm text-muted">
          A change is tested before it goes live: the eval set is asked on two scratch copies at once, one as the agent is now
          and one with the change, and every case is compared. It is accepted only if nothing that passed breaks and the cases
          it targets are fixed.
        </p>
      </div>
      {message && <p className="text-sm text-pass">{message}</p>}
      {actionError && <ErrorBox error={actionError} />}

      {proposing && <p className="text-sm text-muted">Drafting a change from the failure category…</p>}
      {proposal && (
        <div className="rounded-lg bg-oat p-4 text-sm">
          <div className="font-medium">Drafted by {proposal.skill} ({proposal.model}). Check it before testing.</div>
          <p className="mt-1">{proposal.diagnosis}</p>
          {!proposal.draft && <p className="mt-1 text-warn">It found nothing in the configuration to change.</p>}
          {(proposal.dropped?.length ?? 0) > 0 && (
            <p className="mt-1 text-xs text-muted">Left out because they do not fit the configuration: {proposal.dropped!.join("; ")}</p>
          )}
        </div>
      )}
      {writing ? (
        <ChangeForm
          key={proposal ? "proposal" : "blank"}
          initial={proposal?.draft ?? undefined}
          spaceId={spaceId}
          cases={cases}
          onCancel={() => setWriting(false)}
          onSaved={() => {
            setWriting(false);
            changes.reload();
          }}
        />
      ) : (
        <Button variant="primary" onClick={() => setWriting(true)}>
          <Plus className="size-4" aria-hidden /> New change
        </Button>
      )}

      {changes.data!.length === 0 ? (
        <EmptyState icon={<FlaskConical />} title="No changes yet">
          Write one for a failure the eval set has a case for, then test it.
        </EmptyState>
      ) : (
        <ul className="space-y-3">
          {changes.data!.map((c) => (
            <li key={c.change_id}>
              <Card>
                <CardContent className="space-y-2 pt-5">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium">{c.title}</span>
                    <Badge variant={STATUS[c.status]}>{c.status.replace("_", " ")}</Badge>
                    <span className="text-xs text-muted">
                      {c.created_by} · {when(c.created_at)} · written against <span className="font-mono">{c.base_version.slice(0, 8)}</span>
                    </span>
                  </div>
                  {c.rationale && <p className="text-sm text-muted">{c.rationale}</p>}
                  <ul className="list-inside list-disc text-sm">
                    {c.steps.map((s, i) => (
                      <li key={i}>{s}</li>
                    ))}
                  </ul>
                  {(c.leaks ?? []).some((l) => l.split !== "dev") && (
                    <div className="rounded-lg bg-warn-soft p-3 text-sm text-warn">
                      Train/test leakage: an example it adds is also asked by the eval set outside dev, so Genie would be tested on
                      the answer it was given. Those cases cannot count as fixed; use a different question for the example.
                      <ul className="mt-1 list-inside list-disc text-xs">
                        {(c.leaks ?? []).filter((l) => l.split !== "dev").map((l) => (
                          <li key={`${l.case_id}-${l.turn}-${l.example_question}`}>
                            “{l.example_question}” ≈ “{l.case_question}” (turn {l.turn + 1} of “{l.title}”)
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                  {(c.leaks ?? []).some((l) => l.split === "dev") && (
                    <p className="text-xs text-muted">
                      Taken from dev (allowed, few-shot examples may come from dev):{" "}
                      {(c.leaks ?? [])
                        .filter((l) => l.split === "dev")
                        .map((l) => `“${l.title}”`)
                        .join(", ")}
                      . Their fix in a dev run proves little.
                    </p>
                  )}
                  {(c.test_targets?.length ?? 0) > 0 && (
                    <p className="text-xs text-warn">
                      It targets test cases ({c.test_targets!.map((id) => cases.find((x) => x.case_id === id)?.title ?? id).join(", ")}): it
                      was written against the held-out set. Move them to dev, or expect its gate result to be optimistic.
                    </p>
                  )}
                  {(c.gate_runs ?? 0) > 0 && (
                    <p className={cn("text-xs", (c.gate_runs ?? 0) > 1 ? "text-warn" : "text-muted")}>
                      {c.gate_runs} gate run{c.gate_runs! > 1 ? "s" : ""}
                      {c.gate_runs! > 1 && ": the test set was looked at between them, so an accept is optimistic"}.
                    </p>
                  )}
                  {(c.target_case_ids?.length ?? 0) > 0 && (
                    <p className="text-xs text-muted">
                      Should fix: {c.target_case_ids!.map((id) => cases.find((x) => x.case_id === id)?.title ?? id).join(", ")}
                    </p>
                  )}
                  {(c.status === "draft" || c.status === "tested") && (
                    <>
                    <ChangeEstimate dev={devCount} test={testIds} targets={c.target_case_ids ?? []} k={k} />
                    <div className="flex flex-wrap gap-2 pt-1">
                      <Button
                        size="sm"
                        variant="primary"
                        onClick={() =>
                          act(
                            () => api.testChange(spaceId, c.change_id, "dev"),
                            "Dev run started: it shows on the Eval set page in a minute and takes a few minutes per case.",
                          )
                        }
                      >
                        <FlaskConical className="size-4" aria-hidden /> Try on dev
                      </Button>
                      {(c.target_case_ids?.length ?? 0) > 0 && (
                        <Button
                          size="sm"
                          onClick={() =>
                            act(
                              () => api.testChange(spaceId, c.change_id, "dev", { targets_only: true }),
                              "Dev run on the cases this change should fix started: it shows on the Eval set page in a minute.",
                            )
                          }
                        >
                          Try on its targets
                        </Button>
                      )}
                      <Button
                        size="sm"
                        onClick={() =>
                          window.confirm(
                            "Run the gate: the held-out test cases plus this change's targets. Only this run can let the change be applied; run it once, when you are done iterating.",
                          ) &&
                          act(
                            () => api.testChange(spaceId, c.change_id, "test"),
                            "Gate run started: it shows on the Eval set page in a minute.",
                          )
                        }
                      >
                        Gate run (test)
                      </Button>
                      {c.status === "tested" && (
                        <Button
                          size="sm"
                          onClick={() =>
                            act(() => api.applyChange(spaceId, c.change_id), "Applied to the agent with your permission.").then(
                              (ok) => !ok && setOverride(c.change_id),
                            )
                          }
                        >
                          Apply to the agent
                        </Button>
                      )}
                      <Button size="sm" onClick={() => act(() => api.discardChange(spaceId, c.change_id), "Discarded.")}>
                        Discard
                      </Button>
                    </div>
                    </>
                  )}
                  {override === c.change_id && actionError?.message.includes("latest test was") && (
                    <div className="flex flex-wrap items-center gap-2 rounded-lg bg-warn-soft p-3 text-sm text-warn">
                      Its test was not accepted. Apply it anyway only if you have checked the cases yourself.
                      <Button size="sm" onClick={() => act(() => api.applyChange(spaceId, c.change_id, true), "Applied (override).")}>
                        Apply anyway
                      </Button>
                    </div>
                  )}
                  {c.status === "applied" && (
                    <div className="flex flex-wrap items-center gap-2 pt-1 text-sm">
                      <span className="text-muted">
                        Live since {when(c.updated_at)} as <span className="font-mono">{c.applied_version?.slice(0, 8)}</span>.
                      </span>
                      <Button size="sm" onClick={() => act(() => api.rollbackChange(spaceId, c.change_id), "Rolled back.")}>
                        Roll back
                      </Button>
                    </div>
                  )}
                </CardContent>
              </Card>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

const INPUT =
  "w-full rounded-lg border border-line bg-white px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-link/40";

function ChangeForm({
  initial,
  spaceId,
  cases,
  onCancel,
  onSaved,
}: {
  initial?: ChangeBody;
  spaceId: string;
  cases: EvalCase[];
  onCancel: () => void;
  onSaved: () => void;
}) {
  const [title, setTitle] = useState(initial?.title ?? "");
  const [rationale, setRationale] = useState(initial?.rationale ?? "");
  const [targets, setTargets] = useState<string[]>(initial?.target_case_ids ?? []);
  const [ops, setOps] = useState<Operation[]>(initial?.operations?.length ? initial.operations : [blank("add_instruction")]);
  const [error, setError] = useState<Error | null>(null);
  const [saving, setSaving] = useState(false);

  const setOp = (i: number, patch: Partial<Operation>) => setOps(ops.map((o, k) => (k === i ? ({ ...o, ...patch } as Operation) : o)));

  async function save() {
    setSaving(true);
    setError(null);
    try {
      await api.createChange(spaceId, { title, rationale, operations: ops, target_case_ids: targets });
      onSaved();
    } catch (err) {
      setError(err as Error);
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card>
      <CardContent className="space-y-4 pt-5">
        <div className="space-y-1.5">
          <label htmlFor="change-title" className="block text-sm font-medium">
            Title
          </label>
          <input id="change-title" className={INPUT} value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} />
        </div>
        <div className="space-y-1.5">
          <label htmlFor="change-why" className="block text-sm font-medium">
            Why: the failure it should fix
          </label>
          <textarea id="change-why" rows={2} className={INPUT} value={rationale} onChange={(e) => setRationale(e.target.value)} />
        </div>
        <div className="space-y-2">
          <div className="text-sm font-medium">What to change</div>
          {ops.map((o, i) => (
            <div key={i} className="space-y-2 rounded-lg border border-line p-3">
              <div className="flex items-center gap-2">
                <select
                  aria-label={`Operation ${i + 1}`}
                  className="rounded-md border border-line bg-white px-2 py-1 text-sm"
                  value={o.op}
                  onChange={(e) => setOps(ops.map((x, k) => (k === i ? blank(e.target.value as Operation["op"]) : x)))}
                >
                  {OPS.map((x) => (
                    <option key={x.value} value={x.value}>
                      {x.label}
                    </option>
                  ))}
                </select>
                {ops.length > 1 && (
                  <button type="button" aria-label="Remove this operation" className="text-muted hover:text-fail" onClick={() => setOps(ops.filter((_, k) => k !== i))}>
                    <Trash2 className="size-4" />
                  </button>
                )}
              </div>
              {"old" in o && (
                <input className={INPUT} placeholder="The current instruction line, exactly" value={o.old} onChange={(e) => setOp(i, { old: e.target.value })} />
              )}
              {"text" in o && (
                <textarea rows={2} className={INPUT} placeholder="The new instruction" value={o.text} onChange={(e) => setOp(i, { text: e.target.value })} />
              )}
              {o.op === "add_sql_example" && (
                <>
                  <input className={INPUT} placeholder="Question" value={o.question} onChange={(e) => setOp(i, { question: e.target.value })} />
                  <textarea rows={4} className={cn(INPUT, "font-mono text-xs")} placeholder="SQL" value={o.sql} onChange={(e) => setOp(i, { sql: e.target.value })} />
                </>
              )}
              {o.op === "set_column" && (
                <div className="grid gap-2 sm:grid-cols-2">
                  <input className={INPUT} placeholder="catalog.schema.table" value={o.table} onChange={(e) => setOp(i, { table: e.target.value })} />
                  <input className={INPUT} placeholder="column" value={o.column} onChange={(e) => setOp(i, { column: e.target.value })} />
                  <input
                    className={cn(INPUT, "sm:col-span-2")}
                    placeholder="Description (optional)"
                    value={o.description ?? ""}
                    onChange={(e) => setOp(i, { description: e.target.value || null })}
                  />
                  <input
                    className={cn(INPUT, "sm:col-span-2")}
                    placeholder="Synonyms, comma separated (optional)"
                    value={(o.synonyms ?? []).join(", ")}
                    onChange={(e) => setOp(i, { synonyms: e.target.value ? e.target.value.split(",").map((s) => s.trim()) : null })}
                  />
                </div>
              )}
            </div>
          ))}
          <Button size="sm" onClick={() => setOps([...ops, blank("add_instruction")])}>
            <Plus className="size-4" aria-hidden /> Another operation
          </Button>
        </div>
        {cases.length > 0 && (
          <div className="space-y-1.5">
            <div className="text-sm font-medium">Cases it should fix</div>
            <div className="flex flex-wrap gap-2">
              {cases.map((c) => {
                const on = targets.includes(c.case_id);
                return (
                  <button
                    key={c.case_id}
                    type="button"
                    aria-pressed={on}
                    onClick={() => setTargets(on ? targets.filter((t) => t !== c.case_id) : [...targets, c.case_id])}
                    className={cn(
                      "rounded-full border px-3 py-1 text-xs font-medium",
                      on ? "border-navy bg-navy text-white" : "border-line bg-white text-muted hover:text-ink",
                    )}
                  >
                    {c.title}
                  </button>
                );
              })}
            </div>
          </div>
        )}
        {error && <ErrorBox error={error} />}
        <div className="flex gap-2">
          <Button variant="primary" onClick={save} disabled={saving || !title.trim()}>
            {saving ? "Checking…" : "Save change"}
          </Button>
          <Button onClick={onCancel}>Cancel</Button>
        </div>
      </CardContent>
    </Card>
  );
}

/** What each way of testing a change asks: both configurations, k times per case. */
function ChangeEstimate({ dev, test, targets, k }: { dev: number; test: Set<string>; targets: string[]; k: number }) {
  const gate = test.size + targets.filter((id) => !test.has(id)).length;
  const parts: [string, number][] = [["Dev", dev], ...(targets.length ? [["Targets", targets.length] as [string, number]] : []), ["Gate", gate]];
  return (
    <p className="text-xs text-muted tabular-nums">
      {parts.map(([label, cases]) => `${label}: ${estimateText(cases, k, 2)}`).join(" · ")} (cases × k {k} × 2
      configurations).
    </p>
  );
}
