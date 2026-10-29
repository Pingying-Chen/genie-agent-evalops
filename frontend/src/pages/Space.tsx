// Page 2: one space — headline numbers, the criteria, and the filterable list of answers.
import { useEffect, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { ArrowLeft, ClipboardList, Filter, FlaskConical, History, LineChart, Scale, SlidersHorizontal, Target, X } from "lucide-react";
import { api, type Agreement, type Criterion, type JudgeCalibration, type TraceListItem } from "@/api";
import { useLoad } from "@/hooks";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  CoverageBar,
  CriterionName,
  EmptyState,
  ErrorBox,
  GATE,
  GateBadge,
  Loading,
  Trend,
  VerdictBadge,
  pct,
  useCriteria,
  when,
} from "@/components/common";
import { cn } from "@/lib/utils";

const VERDICT_FILTERS = [
  { value: "", label: "All" },
  { value: "pass", label: "Passed" },
  { value: "fail", label: "Failed" },
  { value: "not_gradable", label: "Not gradable" },
];

export function Space() {
  const { spaceId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const verdict = params.get("verdict") ?? "";
  const criterion = params.get("criterion") ?? "";
  const summary = useLoad(() => api.summary(spaceId), [spaceId]);
  const criteria = useCriteria();

  function filter(next: Record<string, string>) {
    const merged = { verdict, criterion, ...next };
    setParams(Object.fromEntries(Object.entries(merged).filter(([, v]) => v)));
  }

  if (summary.loading) return <Loading />;
  if (summary.error) return <ErrorBox error={summary.error} />;
  const s = summary.data!;
  const graded = s.coverage.by_status.ok ?? 0;
  const failing = s.criteria.filter((c) => c.gate !== "monitor" && c.n_fail > 0).length;

  return (
    <div className="space-y-8">
      <div>
        <Link to="/" className="inline-flex items-center gap-1 text-sm text-muted hover:text-ink">
          <ArrowLeft className="size-4" aria-hidden /> All agents
        </Link>
        <h1 className="mt-2 text-2xl font-semibold tracking-tight">{s.title ?? spaceId}</h1>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
          <p className="font-mono text-xs text-muted">{spaceId}</p>
          <Link to={`/spaces/${spaceId}/performance`} className="inline-flex items-center gap-1 text-sm font-medium text-ink hover:underline">
            <LineChart className="size-4 text-muted" aria-hidden /> Performance over time →
          </Link>
          <Link to={`/spaces/${spaceId}/evalset`} className="inline-flex items-center gap-1 text-sm font-medium text-ink hover:underline">
            <ClipboardList className="size-4 text-muted" aria-hidden /> Eval set →
          </Link>
          <Link to={`/spaces/${spaceId}/changes`} className="inline-flex items-center gap-1 text-sm font-medium text-ink hover:underline">
            <FlaskConical className="size-4 text-muted" aria-hidden /> Changes →
          </Link>
          <Link to={`/spaces/${spaceId}/versions`} className="inline-flex items-center gap-1 text-sm font-medium text-ink hover:underline">
            <History className="size-4 text-muted" aria-hidden /> Versions →
          </Link>
          <Link to={`/spaces/${spaceId}/evaluators`} className="inline-flex items-center gap-1 text-sm font-medium text-ink hover:underline">
            <Scale className="size-4 text-muted" aria-hidden /> Evaluators →
          </Link>
          <Link to={`/spaces/${spaceId}/settings`} className="inline-flex items-center gap-1 text-sm font-medium text-ink hover:underline">
            <SlidersHorizontal className="size-4 text-muted" aria-hidden /> Settings →
          </Link>
        </div>
      </div>

      <div className="grid gap-5 md:grid-cols-3">
        <Card>
          <CardHeader>
            <CardDescription>Conversations, {s.window_from} – {s.window_to}</CardDescription>
            <CardTitle className="text-3xl">{s.coverage.total}</CardTitle>
          </CardHeader>
          <CardContent>
            <CoverageBar byStatus={s.coverage.by_status} total={s.coverage.total} />
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardDescription>Rules with failures</CardDescription>
            <CardTitle className={cn("text-3xl", failing ? "text-fail" : "text-pass")}>{failing}</CardTitle>
          </CardHeader>
          <CardContent className="text-sm text-muted">
            of {s.criteria.filter((c) => c.gate !== "monitor").length} must-pass and should-pass rules, over {graded} graded answers
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardDescription>Must-pass rate by day</CardDescription>
          </CardHeader>
          <CardContent>
            <Trend points={s.daily} />
          </CardContent>
        </Card>
      </div>
      {s.judge_sample_rate < 1 && (
        <p className="-mt-4 text-sm text-muted">
          LLM-judged rules read a {pct(s.judge_sample_rate)} sample of answers; their rates count judged answers only.{" "}
          <Link to={`/spaces/${spaceId}/settings`} className="text-link hover:underline">Change in Settings</Link>
        </p>
      )}

      <section>
        <h2 className="text-lg font-semibold">Rules</h2>
        <Card className="mt-3 overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-oat text-left text-xs uppercase tracking-wide text-muted">
                <tr>
                  <th className="px-4 py-3 font-medium">Rule</th>
                  <th className="px-4 py-3 font-medium">Type</th>
                  <th className="px-4 py-3 text-right font-medium">Answers checked</th>
                  <th className="px-4 py-3 text-right font-medium">Failed</th>
                  <th className="px-4 py-3 text-right font-medium">Pass rate</th>
                  <th className="px-4 py-3" />
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {s.criteria.map((c) => (
                  <tr key={c.criterion_id} className={cn(criterion === c.criterion_id && "bg-oat")}>
                    <td className="px-4 py-3">
                      <CriterionName id={c.criterion_id} criteria={criteria} />
                    </td>
                    <td className="px-4 py-3">
                      <GateBadge gate={c.gate} />
                    </td>
                    <td className="px-4 py-3 text-right tabular-nums">{c.n}</td>
                    <td className={cn("px-4 py-3 text-right tabular-nums", c.n_fail > 0 && c.gate !== "monitor" && "font-semibold text-fail")}>
                      {c.n_fail}
                    </td>
                    <td className="px-4 py-3 text-right tabular-nums">{pct(c.pass_rate, 1)}</td>
                    <td className="px-4 py-3 text-right">
                      {c.n_fail > 0 && (
                        <Button variant="link" size="sm" onClick={() => filter({ criterion: c.criterion_id, verdict: "" })}>
                          Show answers
                        </Button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      </section>

      <Calibration spaceId={spaceId} criteria={criteria} />

      <section>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 className="text-lg font-semibold">Answers</h2>
          <div className="flex flex-wrap items-center gap-2">
            <div className="inline-flex rounded-lg border border-line bg-white p-0.5" role="tablist">
              {VERDICT_FILTERS.map((f) => (
                <button
                  key={f.value || "all"}
                  role="tab"
                  aria-selected={verdict === f.value}
                  onClick={() => filter({ verdict: f.value })}
                  className={cn(
                    "rounded-md px-3 py-1 text-sm transition-colors",
                    verdict === f.value ? "bg-navy text-white" : "text-muted hover:text-ink",
                  )}
                >
                  {f.label}
                </button>
              ))}
            </div>
            {criterion && (
              <Badge variant="outline" className="py-1">
                <Filter aria-hidden />
                {criteria[criterion]?.title ?? criterion}
                <button onClick={() => filter({ criterion: "" })} aria-label="clear rule filter" className="ml-1 text-muted hover:text-ink">
                  <X className="size-3.5" />
                </button>
              </Badge>
            )}
          </div>
        </div>
        <TraceList spaceId={spaceId} verdict={verdict} criterion={criterion} criteria={criteria} />
      </section>
    </div>
  );
}

function TraceList({
  spaceId,
  verdict,
  criterion,
  criteria,
}: {
  spaceId: string;
  verdict: string;
  criterion: string;
  criteria: Record<string, Criterion>;
}) {
  const navigate = useNavigate();
  const [items, setItems] = useState<TraceListItem[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(false);

  async function load(next: string | null, append: boolean) {
    setLoading(true);
    try {
      const page = await api.traces(spaceId, { verdict, criterion, cursor: next, limit: 25 });
      setItems((prev) => (append ? [...prev, ...page.items] : page.items));
      setCursor(page.next_cursor ?? null);
      setError(null);
    } catch (err) {
      setError(err as Error);
    } finally {
      setLoading(false);
    }
  }

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => void load(null, false), [spaceId, verdict, criterion]);

  if (error) return <ErrorBox error={error} />;
  if (!items.length && !loading)
    return (
      <div className="mt-3">
        <EmptyState icon={<Filter />} title="No answers match these filters" />
      </div>
    );
  return (
    <>
      <Card className="mt-3 overflow-hidden">
        <ul className="divide-y divide-line">
          {items.map((t) => {
            // Tracking-only rules never fail an answer: show them apart, in grey.
            const failed = t.failed_criteria ?? [];
            const blocking = failed.filter((c) => criteria[c]?.gate !== "monitor");
            const noted = failed.filter((c) => criteria[c]?.gate === "monitor");
            return (
              <li
                key={t.trace_id}
                onClick={() => navigate(`/traces/${t.trace_id}`)}
                className="flex cursor-pointer flex-col gap-2 px-4 py-3 hover:bg-oat sm:flex-row sm:items-center"
              >
                <div className="min-w-0 flex-1">
                  <Link to={`/traces/${t.trace_id}`} className="font-medium text-ink" onClick={(e) => e.stopPropagation()}>
                    {t.question ?? <em className="text-muted">(conversation not readable)</em>}
                  </Link>
                  <div className="text-xs text-muted">{when(t.asked_at)}</div>
                </div>
                <div className="flex flex-wrap items-center gap-1.5 sm:justify-end">
                  {blocking.map((c) => (
                    <Badge key={c} variant={criteria[c]?.gate === "soft" ? "warn" : "fail"} title={c}>
                      {criteria[c]?.title ?? c}
                    </Badge>
                  ))}
                  {noted.map((c) => (
                    <Badge key={c} variant="neutral" title={`${GATE.monitor.label}: ${c}`}>
                      {criteria[c]?.title ?? c}
                    </Badge>
                  ))}
                  <VerdictBadge verdict={t.verdict} />
                </div>
              </li>
            );
          })}
        </ul>
      </Card>
      {cursor && (
        <div className="mt-4 text-center">
          <Button onClick={() => load(cursor, true)} disabled={loading}>
            {loading ? "Loading…" : "Load more"}
          </Button>
        </div>
      )}
    </>
  );
}

// Labels needed, with no misses, for a Wilson 95% lower bound to reach `bound`: n / (n + z^2) >= bound.
function labelsNeeded(bound: number): number {
  const z2 = 1.959964 ** 2;
  return Math.ceil((bound * z2) / (1 - bound));
}

/** Human review progress and how well each LLM judge agrees with it (owners only: blind review). */
function Calibration({ spaceId, criteria }: { spaceId: string; criteria: Record<string, Criterion> }) {
  const { data, error, loading } = useLoad(() => api.calibration(spaceId), [spaceId]);
  if (loading) return null;
  if (error) return <ErrorBox error={error} />;
  const c = data!;
  if (!c.queued && !c.judges.length) return null; // human review not set up for this space
  const bound = c.min_lower_bound;
  const needed = labelsNeeded(bound);

  return (
    <section>
      <h2 className="flex items-center gap-2 text-lg font-semibold">
        <Scale className="size-4 text-muted" aria-hidden /> Human review
      </h2>
      <p className="mt-1 text-sm text-muted">
        People judge whether each answer solves what was asked. Their rule labels also calibrate the LLM judges, which stay
        tracking only until they catch real failures without flagging good answers.
      </p>
      <Card className="mt-3">
        <CardContent className="space-y-5 pt-5">
          <TaskSuccess calibration={c} />
          <div>
            <div className="flex items-baseline justify-between gap-3 text-sm">
              <span>
                <strong className="tabular-nums">{c.labeled}</strong> of <span className="tabular-nums">{c.queued}</span> answers in the
                review queue labelled
              </span>
              <span className="text-xs text-muted">{c.computed_at ? `as of ${when(c.computed_at)}` : "updated by the next review run"}</span>
            </div>
            <div className="mt-1.5 h-2 overflow-hidden rounded-full bg-oat-deep">
              <div className="h-full rounded-full bg-navy" style={{ width: `${c.queued ? (100 * c.labeled) / c.queued : 0}%` }} />
            </div>
          </div>
          {c.judges.length ? (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="text-left text-xs uppercase tracking-wide text-muted">
                  <tr>
                    <th className="py-2 pr-4 font-medium">Rule</th>
                    <th className="py-2 pr-4 text-right font-medium" title="Labels where the reviewer said the rule applies (pass or fail): what the rates count">
                      Rule applies
                    </th>
                    <th className="py-2 pr-4 text-right font-medium" title="Labels where the judge and the reviewer agree on whether the rule applies at all">
                      Agrees it applies
                    </th>
                    <th className="py-2 pr-4 text-right font-medium" title="Of the answers people failed, how many the judge failed (TNR: recall of the fail class)">
                      Catches failures
                    </th>
                    <th className="py-2 pr-4 text-right font-medium" title="Of the answers people passed (the rule applied), how many the judge passed too (TPR)">
                      Leaves good answers
                    </th>
                    <th className="py-2 pr-4 text-right font-medium" title="Cohen's kappa on fail vs not fail: agreement beyond chance (1 = perfect, 0 = no better than chance)">
                      Kappa
                    </th>
                    <th className="py-2 pr-4 text-right font-medium" title="Of the answers the judge said the rule applies to, the share it failed; below, corrected for its measured TPR and TNR">
                      Judge fail rate
                    </th>
                    <th className="whitespace-nowrap py-2 font-medium">Status</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-line">
                  {c.judges.map((j) => (
                    <tr key={`${j.criterion_id}-${j.criterion_version}-${j.judge}`}>
                      <td className="max-w-[18rem] py-2.5 pr-4">
                        <CriterionName id={j.criterion_id} criteria={criteria} />
                        <div className="truncate font-mono text-xs text-muted" title={j.judge}>
                          v{j.criterion_version} · {j.judge}
                        </div>
                      </td>
                      <td className="py-2.5 pr-4 text-right tabular-nums">
                        {j.n_applies ?? 0}
                        <div className="text-xs text-muted" title="Labels where the reviewer said the rule does not apply (na) are not counted in the rates">
                          of {j.n} labelled
                        </div>
                        {j.n_judge_error > 0 && <div className="text-xs text-muted">+{j.n_judge_error} judge errors</div>}
                      </td>
                      <td className="py-2.5 pr-4 text-right tabular-nums">
                        {j.applicability_agree ?? 0}/{j.n}
                      </td>
                      <Rate hits={j.tn} total={j.tn + j.fp} lower={j.tnr_lb} bound={bound} />
                      <Rate hits={j.tp} total={j.tp + j.fn} lower={j.tpr_lb} bound={bound} />
                      <td className="py-2.5 pr-4 text-right tabular-nums" title={j.kappa == null ? "Undefined until both raters have used fail and not fail" : undefined}>
                        {j.kappa == null ? "—" : j.kappa.toFixed(2)}
                      </td>
                      <td className="py-2.5 pr-4 text-right tabular-nums">
                        {j.observed_applies ? (
                          <>
                            {pct((j.observed_fails ?? 0) / j.observed_applies)}
                            <div className="text-xs text-muted">
                              {j.corrected_fail_rate == null ? `of ${j.observed_applies}` : `corrected ${pct(j.corrected_fail_rate)}`}
                            </div>
                          </>
                        ) : (
                          <span className="text-muted">—</span>
                        )}
                      </td>
                      <td className="py-2.5">
                        <CalibrationBadge judge={j} bound={bound} />
                        <Promotion judge={j} criterion={criteria[j.criterion_id]} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="text-sm text-muted">No agreement yet: it appears after reviewers label answers and the review job runs.</p>
          )}
          {c.judges.length > 0 && (
            <Link to={`/spaces/${c.space_id}/disagreements`} className="inline-block text-sm font-medium text-ink hover:underline">
              Where the judges disagree with people →
            </Link>
          )}
          <p className="text-xs text-muted">
            A judge is ready to gate when both rates have a 95% lower bound of at least {pct(bound)}. With no disagreements that takes{" "}
            {needed} answers people failed and {needed} they did not.
          </p>
        </CardContent>
      </Card>
    </section>
  );
}

function Rate({ hits, total, lower, bound }: { hits: number; total: number; lower?: number | null; bound: number }) {
  if (!total) return <td className="py-2.5 pr-4 text-right text-muted">—</td>;
  return (
    <td className="py-2.5 pr-4 text-right tabular-nums">
      {hits}/{total}
      <div className={cn("text-xs", lower != null && lower >= bound ? "text-pass" : "text-muted")}>at least {pct(lower)}</div>
    </td>
  );
}

/** What to change once a judge is calibrated: judges start in shadow (`gate: monitor`). */
function Promotion({ judge, criterion }: { judge: Agreement; criterion?: Criterion }) {
  if (!criterion) return null;
  if (criterion.gate !== "monitor") return <div className="mt-1 text-xs text-muted">Gating ({criterion.gate})</div>;
  if (!judge.calibrated) return null;
  return (
    <div className="mt-1 max-w-56 text-xs text-muted">
      In <code className="font-mono">{criterion.id}.yaml</code> set <code className="font-mono">gate: soft</code> (or{" "}
      <code className="font-mono">hard</code> if a failure must block) and deploy.
    </div>
  );
}

function CalibrationBadge({ judge, bound }: { judge: Agreement; bound: number }) {
  if (judge.calibrated) return <Badge variant="pass">Ready to gate</Badge>;
  // A point estimate below the bound is a real disagreement, not just too few labels.
  const disagrees = [judge.tpr, judge.tnr].some((r) => r != null && r < bound);
  return disagrees ? <Badge variant="warn">Disagrees with people</Badge> : <Badge variant="neutral">Needs more labels</Badge>;
}

/** System level: did the answer solve what was asked, and do the rules explain the failures? */
function TaskSuccess({ calibration: c }: { calibration: JudgeCalibration }) {
  const reviewed = c.reviewed ?? 0;
  const failed = c.task_failed ?? 0;
  const caught = c.caught ?? 0;
  const spots = c.blind_spots ?? [];
  return (
    <div className="rounded-lg bg-oat p-4">
      <div className="flex items-center gap-2 text-sm font-semibold">
        <Target className="size-4 text-muted" aria-hidden /> Does the answer solve what was asked?
      </div>
      {reviewed ? (
        <div className="mt-3 grid gap-4 sm:grid-cols-2">
          <div>
            <div className="text-2xl font-semibold tabular-nums">{pct((reviewed - failed) / reviewed)}</div>
            <div className="text-sm text-muted">
              of {reviewed} reviewed answers, as judged by people ({failed} failed)
            </div>
          </div>
          <div>
            <div className={cn("text-2xl font-semibold tabular-nums", failed && caught < failed && "text-warn")}>
              {failed ? `${caught} of ${failed}` : "—"}
            </div>
            <div className="text-sm text-muted">failed answers that at least one rule caught (rule coverage)</div>
          </div>
          {(c.multi_turn ?? 0) > 0 && (
            <div className="sm:col-span-2">
              <div className="text-sm">
                <strong className="tabular-nums">{c.conversations_outcome}</strong> of{" "}
                <span className="tabular-nums">{c.conversations}</span> conversations ended with a passing answer;{" "}
                <strong className="tabular-nums">{c.conversations_strict}</strong> had no failed answer at all.
              </div>
              <div className="text-xs text-muted">
                Counted over reviewed answers only ({c.multi_turn} conversations have more than one). The gap is conversations
                where the user had to recover from a wrong answer.
              </div>
            </div>
          )}
        </div>
      ) : (
        <p className="mt-2 text-sm text-muted">No task-success verdicts yet: reviewers answer this first for every answer.</p>
      )}
      {reviewed > 0 && (
        <Link to={`/spaces/${c.space_id}/failures`} className="mt-3 inline-block text-sm font-medium text-ink hover:underline">
          Everything reviewers found: failures and findings →
        </Link>
      )}
      {spots.length > 0 && (
        <div className="mt-4">
          <div className="text-xs font-medium uppercase tracking-wide text-muted">Blind spots: failed by people, missed by every rule</div>
          <ul className="mt-2 divide-y divide-line rounded-lg border border-line bg-white">
            {spots.map((s) => (
              <li key={s.trace_id} className="px-3 py-2 text-sm">
                <Link to={`/traces/${s.trace_id}`} className="font-medium text-ink">
                  {s.question ?? s.trace_id}
                </Link>
                {s.task_rationale && <div className="text-muted">{s.task_rationale}</div>}
                {s.findings && <div className="text-muted">Findings: {s.findings}</div>}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
