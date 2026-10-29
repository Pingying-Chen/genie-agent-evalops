// Page 5: where the LLM judges and the reviewers disagree, and the owner's diagnosis of each
// (judge wrong, rule unclear, label wrong), which decides what to change.
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, Scale } from "lucide-react";
import { api, type Criterion, type Disagreement } from "@/api";
import { useLoad } from "@/hooks";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { CriterionName, EmptyState, ErrorBox, Loading, VerdictBadge, useCriteria } from "@/components/common";
import { cn } from "@/lib/utils";

const KIND: Record<Disagreement["kind"], { label: string; variant: "fail" | "warn" | "neutral" }> = {
  missed_failure: { label: "Missed failure", variant: "fail" },
  false_alarm: { label: "False alarm", variant: "warn" },
  applicability: { label: "Applies?", variant: "neutral" },
};

const CAUSES: { value: NonNullable<Disagreement["cause"]>; label: string; fix: string }[] = [
  { value: "judge_wrong", label: "Judge wrong", fix: "Fix the judge: its prompt or examples (a new skill version)." },
  { value: "rule_unclear", label: "Rule unclear", fix: "Rewrite the rule text: a new criterion version, then relabel." },
  { value: "label_wrong", label: "Label wrong", fix: "Change the label in the Review App; the next review run picks it up." },
];

export function Disagreements() {
  const { spaceId = "" } = useParams();
  const { data, error, loading, reload } = useLoad(() => api.disagreements(spaceId), [spaceId]);
  const criteria = useCriteria();
  const [actionError, setActionError] = useState<Error | null>(null);

  if (loading && !data) return <Loading />;
  if (error) return <ErrorBox error={error} />;
  const groups = new Map<string, Disagreement[]>();
  for (const d of data!.items) groups.set(d.criterion_id, [...(groups.get(d.criterion_id) ?? []), d]);

  async function mark(d: Disagreement, cause: Disagreement["cause"] = null, note: string) {
    setActionError(null);
    try {
      await api.markDisagreement(spaceId, d.trace_id, d.criterion_id, { cause: cause ?? null, note });
    } catch (err) {
      setActionError(err as Error);
    }
    reload();
  }

  return (
    <div className="space-y-8">
      <div>
        <Link to={`/spaces/${spaceId}`} className="inline-flex items-center gap-1 text-sm text-muted hover:text-ink">
          <ArrowLeft className="size-4" aria-hidden /> Back to the agent
        </Link>
        <h1 className="mt-2 text-2xl font-semibold tracking-tight">Where the judges disagree with people</h1>
        <p className="mt-1 max-w-3xl text-sm text-muted">
          Every labelled answer where a judge’s current verdict differs from the reviewer’s. Read both reasons and mark the cause:
          it decides whether to change the judge, the rule, or the label. Missed failures come first: a judge that misses
          them cannot be trusted to gate.
        </p>
      </div>

      {actionError && <ErrorBox error={actionError} />}

      {groups.size === 0 ? (
        <EmptyState icon={<Scale />} title="No disagreements">
          The judges agree with every label so far, or nothing is labelled yet. Agreement only means something once people
          have failed some answers.
        </EmptyState>
      ) : (
        [...groups.entries()].map(([criterionId, items]) => (
          <JudgeSection key={criterionId} criterionId={criterionId} items={items} criteria={criteria} onMark={mark} />
        ))
      )}
    </div>
  );
}

function JudgeSection({
  criterionId,
  items,
  criteria,
  onMark,
}: {
  criterionId: string;
  items: Disagreement[];
  criteria: Record<string, Criterion>;
  onMark: (d: Disagreement, cause: Disagreement["cause"], note: string) => Promise<void>;
}) {
  const count = (pred: (d: Disagreement) => boolean) => items.filter(pred).length;
  return (
    <section>
      <h2 className="text-lg font-semibold">
        <CriterionName id={criterionId} criteria={criteria} />
      </h2>
      <div className="mt-2 flex flex-wrap gap-2 text-sm">
        {(Object.keys(KIND) as Disagreement["kind"][]).map((k) => (
          <Badge key={k} variant={KIND[k].variant}>
            {KIND[k].label}: {count((d) => d.kind === k)}
          </Badge>
        ))}
        <span className="text-muted">·</span>
        {CAUSES.map((c) => (
          <Badge key={c.value} variant="outline">
            {c.label}: {count((d) => d.cause === c.value)}
          </Badge>
        ))}
        <Badge variant="outline">Not diagnosed: {count((d) => !d.cause)}</Badge>
      </div>
      <ul className="mt-3 space-y-3">
        {items.map((d) => (
          <DisagreementCard key={`${d.trace_id}-${d.reviewer}`} d={d} onMark={onMark} />
        ))}
      </ul>
    </section>
  );
}

function DisagreementCard({
  d,
  onMark,
}: {
  d: Disagreement;
  onMark: (d: Disagreement, cause: Disagreement["cause"], note: string) => Promise<void>;
}) {
  const [note, setNote] = useState(d.note ?? "");
  const chosen = CAUSES.find((c) => c.value === d.cause);
  return (
    <li>
      <Card>
        <CardContent className="space-y-3 pt-5">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={KIND[d.kind].variant}>{KIND[d.kind].label}</Badge>
            <span className="font-mono text-xs text-muted">rule v{d.criterion_version}</span>
          </div>
          <Link to={`/traces/${d.trace_id}`} className="block font-medium text-ink hover:underline">
            {d.question ?? d.trace_id}
          </Link>
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="rounded-lg bg-oat p-3 text-sm">
              <div className="mb-1 flex items-center gap-2 text-xs font-medium uppercase tracking-wide text-muted">
                Reviewer <VerdictBadge verdict={d.label} />
              </div>
              <div>{d.rationale || <span className="text-muted">No reason given</span>}</div>
              <div className="mt-1 text-xs text-muted">{d.reviewer}</div>
            </div>
            <div className="rounded-lg bg-oat p-3 text-sm">
              <div className="mb-1 flex items-center gap-2 text-xs font-medium uppercase tracking-wide text-muted">
                Judge <VerdictBadge verdict={d.verdict} />
              </div>
              <div>{d.judge_reason || <span className="text-muted">No reason recorded</span>}</div>
              <div className="mt-1 truncate font-mono text-xs text-muted" title={d.judge}>
                {(d.votes ?? []).length > 0 && <>votes {(d.votes ?? []).join(", ")} · </>}
                {d.judge}
              </div>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm text-muted">Cause:</span>
            {CAUSES.map((c) => (
              <button
                key={c.value}
                type="button"
                aria-pressed={d.cause === c.value}
                onClick={() => onMark(d, d.cause === c.value ? null : c.value, note)}
                className={cn(
                  "rounded-full border px-3 py-1 text-xs font-medium transition-colors",
                  d.cause === c.value ? "border-navy bg-navy text-white" : "border-line bg-white text-muted hover:text-ink",
                )}
              >
                {c.label}
              </button>
            ))}
            <input
              className="min-w-48 flex-1 rounded-lg border border-line bg-white px-3 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-link/40"
              value={note}
              maxLength={1000}
              placeholder="Note (saved with the cause)"
              aria-label="Note"
              onChange={(e) => setNote(e.target.value)}
              onBlur={() => d.cause && note !== (d.note ?? "") && onMark(d, d.cause, note)}
            />
          </div>
          {chosen && <p className="text-xs text-muted">Next: {chosen.fix}</p>}
        </CardContent>
      </Card>
    </li>
  );
}
