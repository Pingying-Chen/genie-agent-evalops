// Regression runs of the eval set: the list on the Eval set page, and one run case
// by case. Owners only.
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, Play, RefreshCw } from "lucide-react";
import { api, type ArmOutcome, type Comparison, type EvalRun, type GradedAttempt, type RunSplit, type Slice, type StratumMix } from "@/api";
import { useLoad } from "@/hooks";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { ErrorBox, Loading, when } from "@/components/common";
import { Markdown } from "@/components/markdown";
import { RunEstimate, useEvalK } from "@/components/estimate";
import { cn } from "@/lib/utils";

export const RUN_SPLIT: Record<RunSplit, { label: string; variant: "pass" | "neutral" | "outline" }> = {
  dev: { label: "dev run", variant: "neutral" },
  test: { label: "gate run (test)", variant: "pass" },
  all: { label: "all cases", variant: "outline" },
};

const VERDICT = {
  pass: { label: "P", title: "passed", className: "bg-pass-soft text-pass" },
  fail: { label: "F", title: "failed", className: "bg-fail-soft text-fail" },
  error: { label: "?", title: "could not be graded", className: "bg-neutral-soft text-muted" },
} as const;

export function RunsSection({
  spaceId,
  counts,
  slices,
}: {
  spaceId: string;
  counts: Record<RunSplit, number>;
  slices: StratumMix[];
}) {
  const k = useEvalK(spaceId);
  const { data, error, loading, reload } = useLoad(() => api.evaluations(spaceId), [spaceId]);
  const [started, setStarted] = useState<string | null>(null);
  const [actionError, setActionError] = useState<Error | null>(null);
  const [split, setSplit] = useState<RunSplit>("dev");
  const [picked, setPicked] = useState(-1); // index into `slices`; -1 = every case of the split
  // A slice's cases in the chosen split; a gate (test) run always asks every test case.
  const casesIn = (s: StratumMix) => (split === "dev" ? s.dev : s.dev + s.test);
  const slice = split !== "test" ? slices[picked] : undefined;
  // Unassigned cases are split when the run starts, so this is a floor.
  const cases = slice ? casesIn(slice) : counts[split];

  async function start() {
    setActionError(null);
    const scope = slice && { dimension: slice.dimension, value: slice.value, targets_only: false };
    try {
      await api.startEvaluation(spaceId, { label: "", split, scope });
      setStarted("Started. It appears below within a minute; each case takes one to two minutes per attempt.");
    } catch (err) {
      setActionError(err as Error);
    }
  }

  return (
    <section className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-lg font-semibold">Regression runs</h2>
          <p className="text-sm text-muted">
            The cases of one split asked several times on a scratch copy of the agent, as a consumer would see it, then graded
            with every rule and the case’s own expectations. Dev runs are for iterating and can ask one slice; a test run is a
            look at the whole held-out set.
          </p>
        </div>
        <div className="flex gap-2">
          <Button onClick={reload} aria-label="Refresh runs">
            <RefreshCw className="size-4" aria-hidden />
          </Button>
          <select
            aria-label="Which cases"
            className="h-9 rounded-lg border border-line bg-white px-2 text-sm"
            value={split}
            onChange={(e) => setSplit(e.target.value as RunSplit)}
          >
            <option value="dev">Dev cases</option>
            <option value="test">Test cases</option>
            <option value="all">All cases</option>
          </select>
          {split !== "test" && slices.length > 0 && (
            <select
              aria-label="Which part"
              className="h-9 rounded-lg border border-line bg-white px-2 text-sm"
              value={picked}
              onChange={(e) => setPicked(Number(e.target.value))}
            >
              <option value={-1}>Every case</option>
              {slices.map(
                (s, i) =>
                  casesIn(s) > 0 && (
                    <option key={i} value={i}>
                      Only {s.dimension} = {s.value}
                    </option>
                  ),
              )}
            </select>
          )}
          <Button variant="primary" onClick={start} disabled={!counts.all}>
            <Play className="size-4" aria-hidden /> Run on the current agent
          </Button>
        </div>
      </div>
      <RunEstimate cases={cases} k={k} arms={1} />
      {started && <p className="text-sm text-pass">{started}</p>}
      {actionError && <ErrorBox error={actionError} />}
      {error && <ErrorBox error={error} />}
      {loading && !data ? (
        <Loading />
      ) : data && data.runs.length > 0 ? (
        <Card className="overflow-hidden">
          <ul className="divide-y divide-line">
            {data.runs.map((r) => (
              <RunRow key={r.run_id} spaceId={spaceId} run={r} />
            ))}
          </ul>
        </Card>
      ) : (
        <p className="text-sm text-muted">No runs yet.</p>
      )}
    </section>
  );
}

function RunRow({ spaceId, run }: { spaceId: string; run: EvalRun }) {
  return (
    <li className="flex flex-wrap items-center gap-x-4 gap-y-1 px-4 py-3 text-sm">
      <Link to={`/spaces/${spaceId}/evaluations/${run.run_id}`} className="font-medium text-ink hover:underline">
        {run.label || when(run.started_at)}
      </Link>
      <Badge variant={run.status === "succeeded" ? "outline" : run.status === "running" ? "neutral" : run.status === "partial" ? "warn" : "fail"}>{run.status}</Badge>
      <Badge variant={RUN_SPLIT[run.split ?? "all"].variant}>{RUN_SPLIT[run.split ?? "all"].label}</Badge>
      {run.scope && <Badge variant="outline">only {run.scope}</Badge>}
      {(run.arms ?? []).map((a) => (
        <span key={a.arm} className="tabular-nums text-muted">
          {a.arm}: <strong className="text-ink">{a.pass_all}</strong>/{a.cases} pass every time
          {a.flaky > 0 && <>, {a.flaky} flaky</>}
          {a.errored > 0 && <>, {a.errored} not graded</>}
        </span>
      ))}
      <span className="text-xs text-muted">k = {run.k}</span>
      {run.error && <span className="text-xs text-fail">{run.error}</span>}
    </li>
  );
}

export function EvalRunPage() {
  const { spaceId = "", runId = "" } = useParams();
  const { data, error, loading } = useLoad(() => api.evaluation(spaceId, runId), [spaceId, runId]);
  if (loading) return <Loading />;
  if (error) return <ErrorBox error={error} />;
  const run = data!;
  const problems = run.preflight?.problems ?? [];

  return (
    <div className="space-y-6">
      <div>
        <Link to={`/spaces/${spaceId}/evalset`} className="inline-flex items-center gap-1 text-sm text-muted hover:text-ink">
          <ArrowLeft className="size-4" aria-hidden /> Eval set
        </Link>
        <h1 className="mt-2 text-2xl font-semibold tracking-tight">Regression run {run.label && `· ${run.label}`}</h1>
        <p className="mt-1 text-sm text-muted">
          {when(run.started_at)} · {RUN_SPLIT[run.split ?? "all"].label} · {run.status} · each case asked {run.k} times · rules {run.criteria_version ?? "—"} · as {run.tester}
        </p>
      </div>
      {problems.length > 0 && (
        <p className="rounded-lg bg-fail-soft p-3 text-sm text-fail">The tester failed its access check, so nothing was asked: {problems.join("; ")}</p>
      )}
      {run.error && problems.length === 0 && <p className="rounded-lg bg-warn-soft p-3 text-sm text-warn">{run.error}</p>}
      {run.comparison && <ComparisonView comparison={run.comparison} />}
      <div className="flex flex-wrap gap-3">
        {(run.arms ?? []).map((a) => (
          <Card key={a.arm} className="min-w-56">
            <CardContent className="pt-5">
              <div className="text-xs uppercase tracking-wide text-muted">
                {a.arm} <span className="font-mono normal-case">{a.config_version?.slice(0, 8)}</span>
              </div>
              <div className="mt-1 text-2xl font-semibold tabular-nums">
                {a.pass_all}/{a.cases}
              </div>
              <div className="text-sm text-muted">cases passed every time (pass^{run.k})</div>
              <div className="mt-1 text-xs text-muted tabular-nums">
                {a.pass_any} passed at least once · {a.flaky} flaky · {a.errored} not graded
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
      {(run.slices ?? []).length > 0 && <SlicesView slices={run.slices!} k={run.k} />}
      <ul className="space-y-4">
        {run.cases.map((c) => (
          <li key={c.case_id}>
            <Card>
              <CardContent className="space-y-3 pt-5">
                <div className="flex flex-wrap items-center gap-2">
                  <Link to={`/spaces/${spaceId}/evalset/${c.case_id}`} className="font-medium text-ink hover:underline">
                    {c.title ?? c.case_id}
                  </Link>
                  <span className="font-mono text-xs text-muted">v{c.case_version}</span>
                  {c.split && <Badge variant={c.split === "test" ? "pass" : "outline"}>{c.split}</Badge>}
                  {Object.entries(c.arms).map(([arm, o]) => (
                    <OutcomeBadge key={arm} arm={arm} outcome={o} />
                  ))}
                </div>
                <div className="space-y-3">
                  {c.attempts.map((a) => (
                    <AttemptView key={`${a.arm}-${a.attempt}`} attempt={a} />
                  ))}
                </div>
              </CardContent>
            </Card>
          </li>
        ))}
      </ul>
    </div>
  );
}

const CASE_OUTCOME: Record<string, { label: string; variant: "pass" | "fail" | "warn" | "neutral" | "outline" }> = {
  fixed: { label: "fixed", variant: "pass" },
  broken: { label: "broken", variant: "fail" },
  still_passes: { label: "still passes", variant: "outline" },
  still_fails: { label: "still fails", variant: "warn" },
  flaky: { label: "flaky", variant: "neutral" },
  not_graded: { label: "not graded", variant: "neutral" },
  leaked: { label: "leaked: a SQL example in the change", variant: "warn" },
  unconfirmed: { label: "unconfirmed: only an uncalibrated judge", variant: "warn" },
};

function ComparisonView({ comparison: c }: { comparison: Comparison }) {
  const tone = c.verdict === "accept" ? "bg-pass-soft text-pass" : c.verdict === "reject" ? "bg-fail-soft text-fail" : "bg-warn-soft text-warn";
  return (
    <Card>
      <CardContent className="space-y-3 pt-5">
        <div className={cn("rounded-lg p-3 text-sm", tone)}>
          <strong className="uppercase tracking-wide">{c.verdict}</strong>
          {!c.decides && <span className="ml-2 text-xs font-medium normal-case opacity-80">(dev run: does not decide)</span>}
          {c.reasons.length > 0 && <>: {c.reasons.join("; ")}</>}
        </div>
        {(c.notes ?? []).length > 0 && (
          <ul className="space-y-0.5 text-xs text-muted">
            {c.notes!.map((n) => (
              <li key={n}>{n}</li>
            ))}
          </ul>
        )}
        {(c.gate ?? []).length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-xs uppercase tracking-wide text-muted">
                <tr>
                  <th className="py-1.5 pr-3 font-medium">Gate check</th>
                  <th className="py-1.5 pr-3 font-medium">Evaluator</th>
                  <th className="py-1.5 pr-3 font-medium">Baseline</th>
                  <th className="py-1.5 pr-3 font-medium">Candidate</th>
                  <th className="py-1.5 pr-3 font-medium">Threshold</th>
                  <th className="py-1.5 font-medium">Result</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {c.gate!.map((g) => (
                  <tr key={g.name}>
                    <td className="py-1.5 pr-3">{g.name}</td>
                    <td className="py-1.5 pr-3 text-muted">{g.evaluator}</td>
                    <td className="py-1.5 pr-3 tabular-nums">{g.baseline ?? "—"}</td>
                    <td className="py-1.5 pr-3 tabular-nums">{g.candidate}</td>
                    <td className="py-1.5 pr-3 text-muted">{g.threshold}</td>
                    <td className="py-1.5">
                      {g.passed == null ? (
                        <Badge variant="neutral">not measured</Badge>
                      ) : g.passed ? (
                        <Badge variant="pass">pass</Badge>
                      ) : g.decides ? (
                        <Badge variant="fail">fail</Badge>
                      ) : (
                        <Badge variant="warn" title="Rests on a judge that is not calibrated: shown, not counted">
                          fail (reports only)
                        </Badge>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <div className="flex flex-wrap gap-2 text-sm">
          {Object.entries(c.counts)
            .filter(([k, n]) => CASE_OUTCOME[k] && (n > 0 || k !== "unconfirmed"))
            .map(([k, n]) => (
              <Badge key={k} variant={CASE_OUTCOME[k].variant}>
                {CASE_OUTCOME[k].label}: {n}
              </Badge>
            ))}
        </div>
        <ul className="divide-y divide-line text-sm">
          {c.cases.map((x) => (
            <li key={x.case_id} className="flex flex-wrap items-center gap-2 py-1.5">
              <Badge variant={CASE_OUTCOME[x.outcome].variant}>{CASE_OUTCOME[x.outcome].label}</Badge>
              {x.title ?? x.case_id}
              {x.target && <span className="text-xs text-muted">(target)</span>}
              {x.split && <span className="text-xs text-muted">{x.split}</span>}
              {x.example_match && <span className="text-xs text-warn">a SQL example in the change</span>}
              {(x.unconfirmed_by ?? []).length > 0 && (
                <span className="text-xs text-warn">only by {x.unconfirmed_by!.join(", ")} (not calibrated)</span>
              )}
            </li>
          ))}
        </ul>
        <p className="text-xs text-muted">
          Fixed or broken means every attempt agreed. A split on either side is flaky: Genie’s noise, not evidence either way.
        </p>
      </CardContent>
    </Card>
  );
}

const SLICE_DIMENSION: Record<Slice["dimension"], string> = {
  split: "Split",
  type: "Type",
  intent: "Intent",
  category: "Failure category",
  turns: "Turns",
};

function pct(x: number | null | undefined) {
  return x == null ? "—" : `${Math.round(x * 100)}%`;
}

/** pass^k per arm for each value of each dimension, with a 95% interval; small slices are greyed. */
function SlicesView({ slices, k }: { slices: Slice[]; k: number }) {
  const arms = [...new Set(slices.map((s) => s.arm))];
  const dims = [...new Set(slices.map((s) => s.dimension))];
  return (
    <details className="text-sm" open>
      <summary className="cursor-pointer font-semibold">By slice</summary>
      <p className="mt-1 text-xs text-muted">
        Cases passed every time (pass^{k}) with a 95% interval. Grey slices are smaller than the space’s minimum stratum: read
        them, do not decide on them.
      </p>
      <div className="mt-2 grid gap-3 lg:grid-cols-2">
        {dims.map((d) => {
          const values = [...new Set(slices.filter((s) => s.dimension === d).map((s) => s.value))];
          return (
            <Card key={d}>
              <CardContent className="overflow-x-auto pt-4">
                <h3 className="text-xs font-semibold uppercase tracking-wide text-muted">{SLICE_DIMENSION[d]}</h3>
                <table className="mt-1 w-full text-sm">
                  <thead className="text-xs text-muted">
                    <tr>
                      <th className="py-1 text-left font-normal" />
                      {arms.map((a) => (
                        <th key={a} className="py-1 text-right font-normal">
                          {a}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {values.map((v) => (
                      <tr key={v} className="border-t border-line">
                        <td className="py-1 pr-2">{v}</td>
                        {arms.map((a) => {
                          const s = slices.find((x) => x.dimension === d && x.value === v && x.arm === a);
                          return (
                            <td key={a} className={cn("py-1 text-right tabular-nums", s?.too_small && "text-muted")}>
                              {s ? (
                                <span title={`95% interval ${pct(s.low)}–${pct(s.high)}`}>
                                  {s.pass_all}/{s.cases}{" "}
                                  <span className="text-xs text-muted">
                                    [{pct(s.low)}–{pct(s.high)}]
                                  </span>
                                </span>
                              ) : (
                                "—"
                              )}
                            </td>
                          );
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </CardContent>
            </Card>
          );
        })}
      </div>
    </details>
  );
}

function OutcomeBadge({ arm, outcome: o }: { arm: string; outcome: ArmOutcome }) {
  const label = o.pass_all ? "passes every time" : o.flaky ? "flaky" : o.pass_any ? "passes sometimes" : o.fails ? "fails" : "not graded";
  return <Badge variant={o.pass_all ? "pass" : o.flaky || !o.fails ? "warn" : "fail"}>{`${arm}: ${label}`}</Badge>;
}

function AttemptView({ attempt: a }: { attempt: GradedAttempt }) {
  const [open, setOpen] = useState(false);
  const v = VERDICT[a.verdict];
  const failed = (a.expectations ?? []).filter((e) => e.passed === false);
  return (
    <div className="rounded-lg border border-line">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        className="flex w-full flex-wrap items-center gap-2 px-3 py-2 text-left text-sm hover:bg-oat"
      >
        <span title={v.title} className={cn("inline-flex size-6 items-center justify-center rounded font-mono text-xs font-semibold", v.className)}>
          {v.label}
        </span>
        <span className="text-muted">
          {a.arm} #{a.attempt + 1} · {Math.round(a.seconds ?? 0)} s
        </span>
        {(a.hard_fails ?? []).map((h) => (
          <Badge key={h} variant="fail">
            {h}
          </Badge>
        ))}
        {failed.map((e) => (
          <Badge key={e.kind + e.name} variant="warn" title={e.detail}>
            {e.kind}: {e.name.length > 40 ? e.name.slice(0, 40) + "…" : e.name}
          </Badge>
        ))}
        {a.error && <span className="text-xs text-muted">{a.error}</span>}
      </button>
      {open && (
        <div className="space-y-3 border-t border-line px-3 py-3 text-sm">
          {(a.expectations ?? []).length > 0 && (
            <ul className="space-y-1">
              {a.expectations!.map((e) => (
                <li key={e.kind + e.name}>
                  <span className={e.passed ? "text-pass" : e.passed === false ? "text-fail" : "text-muted"}>
                    {e.passed ? "✓" : e.passed === false ? "✗" : "?"}
                  </span>{" "}
                  <span className="font-medium">{e.kind}</span> {e.name}
                  {e.detail && <div className="ml-4 text-xs text-muted">{e.detail}</div>}
                </li>
              ))}
            </ul>
          )}
          {a.report_text && (
            <div className="rounded-lg bg-oat p-3">
              <Markdown text={a.report_text} highlight={[]} />
            </div>
          )}
        </div>
      )}
    </div>
  );
}
