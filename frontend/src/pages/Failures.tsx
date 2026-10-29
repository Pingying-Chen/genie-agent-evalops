// Page 4: what reviewers found in one space — failed turns and findings, the failure categories
// the owner sorts them into, and which category to work on first (frequency x impact).
import { useState, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, Download, FileCode, FlaskConical, Plus, SearchCheck, Trash2, X } from "lucide-react";
import { api, type CategorySummary, type Criterion, type CriterionDraft, type Observation } from "@/api";
import { useLoad } from "@/hooks";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { CriterionName, EmptyState, ErrorBox, Loading, pct, useCriteria, when } from "@/components/common";
import { cn } from "@/lib/utils";

// Frequency x Impact, both 1-5, as in the AI Evals and Analytics Playbook.
const IMPACT: Record<number, string> = {
  5: "5 · harm: privacy, safety, compliance",
  4: "4 · wrong answer the user would act on",
  3: "3 · wrong or incomplete, likely noticed",
  2: "2 · extra effort: rephrase, follow up",
  1: "1 · minor annoyance",
};
const LEVELS = [5, 4, 3, 2, 1];

const FIELD = "rounded-lg border border-line bg-white px-3 py-1.5 text-sm text-ink focus:outline-none focus:ring-2 focus:ring-link/40";

export function Failures() {
  const { spaceId = "" } = useParams();
  const { data, error, loading, reload } = useLoad(() => api.observations(spaceId), [spaceId]);
  const criteria = useCriteria();
  const [actionError, setActionError] = useState<Error | null>(null);

  if (loading && !data) return <Loading />;
  if (error) return <ErrorBox error={error} />;
  const o = data!;
  const failed = o.items.filter((i) => i.task_success === "fail");
  const blind = failed.filter((i) => !i.caught_by?.length).length;
  const noted = o.items.length - failed.length;

  /** Run a change, then reload; a failure is shown above the page instead of being lost. */
  async function act(change: () => Promise<unknown>) {
    setActionError(null);
    try {
      await change();
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
        <h1 className="mt-2 text-2xl font-semibold tracking-tight">What reviewers found</h1>
        <p className="mt-1 max-w-3xl text-sm text-muted">
          Answers people failed, and answers they passed but wrote findings on. Sort them into kinds of failure, rate how much
          each one hurts, and work on the highest priority first: a failure no rule catches is where the next rule comes from.
        </p>
      </div>

      {actionError && <ErrorBox error={actionError} />}

      {o.items.length ? (
        <>
          <Categories spaceId={spaceId} categories={o.categories ?? []} criteria={criteria} reviewed={o.reviewed} act={act} />
          <section>
            <h2 className="text-lg font-semibold">Observations</h2>
            <p className="mt-1 text-sm text-muted">
              Of <strong className="tabular-nums text-ink">{o.reviewed}</strong> reviewed answers:{" "}
              <strong className="tabular-nums text-ink">{failed.length}</strong> failed (
              <strong className="tabular-nums text-ink">{blind}</strong> missed by every rule),{" "}
              <strong className="tabular-nums text-ink">{noted}</strong> passed with findings.
            </p>
            <ul className="mt-3 space-y-3">
              {o.items.map((item) => (
                <ObservationCard
                  key={item.trace_id}
                  item={item}
                  categories={o.categories ?? []}
                  criteria={criteria}
                  onTag={(ids) => act(() => api.tagObservation(spaceId, item.trace_id, ids))}
                  onCreate={(name, description = "") =>
                    act(async () => {
                      const created = await api.createCategory(spaceId, { name, impact: 3, description });
                      await api.tagObservation(spaceId, item.trace_id, [...(item.category_ids ?? []), created.id]);
                    })
                  }
                />
              ))}
            </ul>
          </section>
        </>
      ) : (
        <EmptyState icon={<SearchCheck />} title="Nothing found yet">
          Reviewers have not failed an answer or written findings. They appear here after the next review run.
        </EmptyState>
      )}
    </div>
  );
}

function Categories({
  spaceId,
  categories,
  criteria,
  reviewed,
  act,
}: {
  spaceId: string;
  categories: CategorySummary[];
  criteria: Record<string, Criterion>;
  reviewed: number;
  act: (change: () => Promise<unknown>) => Promise<void>;
}) {
  const [name, setName] = useState("");
  const [impact, setImpact] = useState(3);
  const [description, setDescription] = useState("");
  const [draft, setDraft] = useState<CriterionDraft | null>(null);

  function create(event: FormEvent) {
    event.preventDefault();
    if (!name.trim()) return;
    void act(() => api.createCategory(spaceId, { name, impact, description })).then(() => {
      setName("");
      setDescription("");
    });
  }

  return (
    <section>
      <h2 className="text-lg font-semibold">Failure categories</h2>
      <p className="mt-1 text-sm text-muted">
        Priority = frequency × impact, each 1–5 (25 = most urgent). Frequency is the share of the {reviewed} reviewed answers
        tagged with the category: under 1% scores 1, 1–5% scores 2, 5–15% 3, 15–30% 4, 30% or more 5. “Caught” counts tagged
        answers at least one rule failed; once a rule covers a category, link it.
      </p>
      <Card className="mt-3 overflow-hidden">
        {categories.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-oat text-left text-xs uppercase tracking-wide text-muted">
                <tr>
                  <th className="px-4 py-3 font-medium">Category</th>
                  <th className="px-4 py-3 text-right font-medium">Answers</th>
                  <th className="px-4 py-3 text-right font-medium">Frequency</th>
                  <th className="px-4 py-3 font-medium">Impact</th>
                  <th className="px-4 py-3 text-right font-medium">Priority</th>
                  <th className="px-4 py-3 text-right font-medium">Caught</th>
                  <th className="px-4 py-3 font-medium">Covered by rule</th>
                  <th className="px-4 py-3" />
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {categories.map((c) => (
                  <tr key={c.id}>
                    <td className="px-4 py-3">
                      <div className="font-medium">{c.name}</div>
                      {c.description && <div className="text-xs text-muted">{c.description}</div>}
                    </td>
                    <td className="px-4 py-3 text-right tabular-nums">
                      {c.turns}
                      {c.failed < c.turns && <div className="text-xs text-muted">{c.failed} failed</div>}
                    </td>
                    <td className="px-4 py-3 text-right tabular-nums">
                      {pct(c.frequency)}
                      {c.frequency_score != null && <div className="text-xs text-muted">score {c.frequency_score}</div>}
                    </td>
                    <td className="px-4 py-3">
                      <select
                        aria-label={`Impact of ${c.name}`}
                        className={FIELD}
                        value={c.impact ?? 3}
                        onChange={(e) => act(() => api.updateCategory(spaceId, c.id, { impact: Number(e.target.value) }))}
                      >
                        {LEVELS.map((v) => (
                          <option key={v} value={v}>
                            {IMPACT[v]}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td className="px-4 py-3 text-right font-semibold tabular-nums">
                      {c.priority ?? "—"}
                    </td>
                    <td className={cn("px-4 py-3 text-right tabular-nums", c.turns > 0 && c.caught < c.turns && "text-warn")}>
                      {c.caught} of {c.turns}
                      {c.linked_caught != null && (
                        <div className="text-xs text-muted">
                          linked rule: {c.linked_caught} of {c.turns}
                          {c.linked_false_alarms ? `, ${c.linked_false_alarms} false alarm${c.linked_false_alarms > 1 ? "s" : ""}` : ""}
                        </div>
                      )}
                    </td>
                    <td className="px-4 py-3">
                      <select
                        aria-label={`Rule that covers ${c.name}`}
                        className={cn(FIELD, "max-w-56")}
                        value={c.criterion_id ?? ""}
                        onChange={(e) =>
                          act(() => api.updateCategory(spaceId, c.id, { criterion_id: e.target.value || null }))
                        }
                      >
                        <option value="">None yet</option>
                        {Object.values(criteria).map((rule) => (
                          <option key={rule.id} value={rule.id}>
                            {rule.title ?? rule.id}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td className="whitespace-nowrap px-4 py-3 text-right">
                      <Button
                        variant="ghost"
                        size="sm"
                        title="Draft a rule for this category"
                        onClick={() => act(async () => setDraft(await api.draftCriterion(spaceId, c.id)))}
                      >
                        <FileCode aria-hidden /> Draft rule
                      </Button>
                      <Link
                        to={`/spaces/${spaceId}/changes?category=${c.id}`}
                        title="Draft a change to the agent that should fix this, to test before it goes live"
                        className="inline-flex h-8 items-center gap-1.5 rounded-lg px-3 text-sm font-medium text-muted hover:bg-oat-deep hover:text-ink"
                      >
                        <FlaskConical className="size-4" aria-hidden /> Draft change
                      </Link>
                      <Button
                        variant="ghost"
                        size="sm"
                        aria-label={`Delete ${c.name}`}
                        onClick={() => {
                          if (window.confirm(`Delete “${c.name}” and untag its ${c.turns} answers?`)) {
                            void act(() => api.deleteCategory(spaceId, c.id));
                          }
                        }}
                      >
                        <Trash2 aria-hidden />
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <form onSubmit={create} className="flex flex-wrap items-end gap-3 border-t border-line bg-oat/50 p-4 first:border-t-0">
          <label className="flex min-w-48 flex-1 flex-col gap-1 text-xs font-medium text-muted">
            New category
            <input className={FIELD} value={name} maxLength={80} placeholder="e.g. Ignores a follow-up's filter" onChange={(e) => setName(e.target.value)} />
          </label>
          <label className="flex flex-col gap-1 text-xs font-medium text-muted">
            Impact
            <select className={FIELD} value={impact} onChange={(e) => setImpact(Number(e.target.value))}>
              {LEVELS.map((v) => (
                <option key={v} value={v}>
                  {IMPACT[v]}
                </option>
              ))}
            </select>
          </label>
          <label className="flex min-w-64 flex-[2] flex-col gap-1 text-xs font-medium text-muted">
            What it looks like (optional)
            <input className={FIELD} value={description} maxLength={1000} onChange={(e) => setDescription(e.target.value)} />
          </label>
          <Button type="submit" variant="primary" disabled={!name.trim()}>
            <Plus aria-hidden /> Add
          </Button>
        </form>
      </Card>
      {draft && <DraftPanel draft={draft} onClose={() => setDraft(null)} />}
    </section>
  );
}

/** The drafted criterion file: edit it, add it to the criteria folder, deploy, then link it. */
function DraftPanel({ draft, onClose }: { draft: CriterionDraft; onClose: () => void }) {
  function download() {
    const url = URL.createObjectURL(new Blob([draft.yaml], { type: "text/yaml" }));
    const link = Object.assign(document.createElement("a"), { href: url, download: draft.filename });
    link.click();
    URL.revokeObjectURL(url);
  }

  return (
    <Card className="mt-4">
      <CardContent className="space-y-3 pt-5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <div className="font-medium">
              Draft rule <span className="font-mono text-sm">{draft.filename}</span>
            </div>
            <p className="text-sm text-muted">
              Write <code>params.rule</code>, check the failure text, then add the file to the criteria folder and deploy. After the
              next check run, link the category to <span className="font-mono">{draft.criterion_id}</span> to see whether it
              catches these answers without flagging good ones.
            </p>
          </div>
          <div className="flex gap-2">
            <Button size="sm" type="button" onClick={download}>
              <Download aria-hidden /> Download
            </Button>
            <Button size="sm" variant="ghost" aria-label="Close draft" onClick={onClose}>
              <X aria-hidden />
            </Button>
          </div>
        </div>
        <pre className="max-h-96 overflow-auto rounded-lg bg-oat p-4 font-mono text-xs leading-relaxed">{draft.yaml}</pre>
      </CardContent>
    </Card>
  );
}

function ObservationCard({
  item,
  categories,
  criteria,
  onTag,
  onCreate,
}: {
  item: Observation;
  categories: CategorySummary[];
  criteria: Record<string, Criterion>;
  onTag: (ids: string[]) => void;
  onCreate: (name: string, description?: string) => void;
}) {
  const [draft, setDraft] = useState("");
  const failed = item.task_success === "fail";
  const caught = item.caught_by ?? [];
  const tagged = item.category_ids ?? [];
  const suggestion = item.suggestion;

  function toggle(id: string) {
    onTag(tagged.includes(id) ? tagged.filter((t) => t !== id) : [...tagged, id]);
  }

  return (
    <li>
      <Card>
        <CardContent className="space-y-3 pt-5">
          <div className="flex flex-wrap items-center gap-2">
            {failed ? <Badge variant="fail">Failed</Badge> : <Badge variant="neutral">Passed, with findings</Badge>}
            {failed && !caught.length && <Badge variant="warn">Blind spot: no rule caught it</Badge>}
            {item.turn_index > 0 && <Badge variant="outline">Follow-up, turn {item.turn_index + 1}</Badge>}
            <span className="text-xs text-muted">{when(item.asked_at)}</span>
          </div>
          <Link to={`/traces/${item.trace_id}`} className="block font-medium text-ink hover:underline">
            {item.question ?? item.trace_id}
          </Link>
          <dl className="grid gap-2 text-sm sm:grid-cols-[8rem_1fr]">
            {item.task_rationale && (
              <>
                <dt className="text-muted">Reviewer’s reason</dt>
                <dd>{item.task_rationale}</dd>
              </>
            )}
            {item.findings && (
              <>
                <dt className="text-muted">Findings</dt>
                <dd>{item.findings}</dd>
              </>
            )}
            <dt className="text-muted">Caught by</dt>
            <dd>
              {caught.length ? (
                <ul className="space-y-1">
                  {caught.map((id) => (
                    <li key={id}>
                      <CriterionName id={id} criteria={criteria} />
                    </li>
                  ))}
                </ul>
              ) : (
                <span className="text-muted">No rule</span>
              )}
            </dd>
            <dt className="text-muted">Categories</dt>
            <dd className="flex flex-wrap items-center gap-2">
              {categories.map((c) => {
                const on = tagged.includes(c.id);
                return (
                  <button
                    key={c.id}
                    type="button"
                    aria-pressed={on}
                    onClick={() => toggle(c.id)}
                    className={cn(
                      "rounded-full border px-3 py-1 text-xs font-medium transition-colors",
                      on ? "border-navy bg-navy text-white" : "border-line bg-white text-muted hover:text-ink",
                    )}
                  >
                    {c.name}
                  </button>
                );
              })}
              <form
                className="flex items-center gap-1"
                onSubmit={(e) => {
                  e.preventDefault();
                  if (draft.trim()) onCreate(draft);
                  setDraft("");
                }}
              >
                <input
                  className={cn(FIELD, "w-48 py-1 text-xs")}
                  value={draft}
                  maxLength={80}
                  placeholder="New category…"
                  aria-label="New category for this answer"
                  onChange={(e) => setDraft(e.target.value)}
                />
                <Button type="submit" variant="ghost" size="sm" disabled={!draft.trim()} aria-label="Add category">
                  <Plus aria-hidden />
                </Button>
              </form>
            </dd>
          </dl>
          {suggestion && (
            <div className="flex flex-wrap items-start justify-between gap-3 rounded-lg border border-dashed border-line bg-oat/60 px-3 py-2 text-sm">
              <div className="min-w-0 flex-1">
                <div>
                  <span className="text-muted">Suggested category: </span>
                  <span className="font-medium">{suggestion.category}</span>
                  {!suggestion.category_id && <Badge variant="outline" className="ml-2">new</Badge>}
                </div>
                {suggestion.reason && <div className="mt-0.5 text-xs text-muted">{suggestion.reason}</div>}
              </div>
              <Button
                size="sm"
                onClick={() =>
                  suggestion.category_id
                    ? onTag([...tagged, suggestion.category_id])
                    : onCreate(suggestion.category, suggestion.description ?? "")
                }
              >
                {suggestion.category_id ? "Accept" : "Create and accept"}
              </Button>
            </div>
          )}
          <div className="text-xs text-muted">Reviewed by {item.reviewer}</div>
        </CardContent>
      </Card>
    </li>
  );
}
