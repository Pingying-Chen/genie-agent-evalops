// Page 6: how the agent, its judges and its reviewers change over time (owners only).
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, LineChart, Users } from "lucide-react";
import { api, type AgentWeek, type Criterion, type JudgeHistoryPoint, type PairAgreement, type SpaceVersionMark } from "@/api";
import { useLoad } from "@/hooks";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState, ErrorBox, Loading, pct, useCriteria, when } from "@/components/common";

export function Performance() {
  const { spaceId = "" } = useParams();
  const criteria = useCriteria();
  return (
    <div className="space-y-10">
      <div>
        <Link to={`/spaces/${spaceId}`} className="inline-flex items-center gap-1 text-sm text-muted hover:text-ink">
          <ArrowLeft className="size-4" aria-hidden /> Back to the agent
        </Link>
        <h1 className="mt-2 text-2xl font-semibold tracking-tight">Performance over time</h1>
        <p className="mt-1 max-w-3xl text-sm text-muted">
          How the agent changes week by week, measured with today’s rules so every week uses the same ruler. Weeks are UTC,
          Monday to Sunday, by when the question was asked.
        </p>
      </div>
      <AgentSection spaceId={spaceId} criteria={criteria} />
      <JudgesSection spaceId={spaceId} criteria={criteria} />
      <ReviewersSection spaceId={spaceId} />
    </div>
  );
}

function AgentSection({ spaceId, criteria }: { spaceId: string; criteria: Record<string, Criterion> }) {
  const { data, error, loading } = useLoad(() => api.agentPerformance(spaceId), [spaceId]);
  if (loading) return <Loading />;
  if (error) return <ErrorBox error={error} />;
  const weeks = data!.weeks;
  if (!weeks.some((w) => w.answers))
    return <EmptyState icon={<LineChart />} title="No answers in the last 12 weeks" />;
  const judgeIds = [...new Set(weeks.flatMap((w) => (w.judges ?? []).map((j) => j.criterion_id)))].sort();
  const versions = data!.versions ?? [];

  return (
    <section className="space-y-4">
      <h2 className="text-lg font-semibold">The agent</h2>
      <div className="grid gap-5 lg:grid-cols-2">
        <ChartCard title="Must-pass rate" note="Share of checked answers no must-pass rule failed. Bars: answers asked.">
          <WeekChart
            weeks={weeks}
            versions={versions}
            bars={(w) => w.answers}
            lines={[{ label: "must-pass", value: (w) => (w.checked ? (w.checked - w.hard_failed) / w.checked : null), n: (w) => w.checked }]}
          />
        </ChartCard>
        <ChartCard
          title="Task success, as judged by people"
          note="Did the answer solve what was asked. The band is the 95% interval: with few reviews in a week it is wide, so read the trend, not one week."
        >
          <WeekChart
            weeks={weeks}
            versions={versions}
            band={(w) => (w.reviewed ? [w.task_low ?? 0, w.task_high ?? 1] : null)}
            lines={[{ label: "task success", value: (w) => (w.reviewed ? w.task_passed / w.reviewed : null), n: (w) => w.reviewed }]}
          />
        </ChartCard>
        {judgeIds.map((id) => (
          <ChartCard
            key={id}
            title={`Judge: ${criteria[id]?.title ?? id}`}
            note="Fail rate on the answers the judge says the rule applies to. Solid: corrected for the judge’s measured error rates, once it beats chance."
          >
            <WeekChart
              weeks={weeks}
              versions={versions}
              lines={[
                {
                  label: "judge fail rate",
                  dashed: true,
                  value: (w) => {
                    const j = w.judges?.find((x) => x.criterion_id === id);
                    return j?.applies ? j.fails / j.applies : null;
                  },
                  n: (w) => w.judges?.find((x) => x.criterion_id === id)?.applies ?? 0,
                },
                { label: "corrected", value: (w) => w.judges?.find((x) => x.criterion_id === id)?.corrected_fail_rate ?? null },
              ]}
            />
          </ChartCard>
        ))}
        <ChartCard title="Time to answer" note="Median and 90th percentile, question to finished report.">
          <WeekChart
            weeks={weeks}
            versions={versions}
            unit="s"
            lines={[
              { label: "median", value: (w) => w.median_seconds ?? null },
              { label: "p90", dashed: true, value: (w) => w.p90_seconds ?? null },
            ]}
          />
        </ChartCard>
      </div>
      {versions.length > 0 && (
        <div className="text-sm">
          <h3 className="font-semibold">Configuration changes</h3>
          <p className="text-xs text-muted">The dashed lines above. Seen by the collect job, so dated by the next collection after the edit.</p>
          <ul className="mt-2 space-y-1">
            {versions.map((v) => (
              <li key={v.version} className="tabular-nums">
                <span className="font-mono text-xs text-muted">{v.version.slice(0, 8)}</span> · {when(v.captured_at)} · {describeChanges(v)}
              </li>
            ))}
          </ul>
        </div>
      )}
      <WeekTable weeks={weeks} />
    </section>
  );
}

function JudgesSection({ spaceId, criteria }: { spaceId: string; criteria: Record<string, Criterion> }) {
  const { data, error, loading } = useLoad(() => api.judgePerformance(spaceId), [spaceId]);
  if (loading) return <Loading />;
  if (error) return <ErrorBox error={error} />;
  const { tracks, latest, min_lower_bound: bound } = data!;
  const day = (p: JudgeHistoryPoint) => p.computed_at.slice(5, 10);

  return (
    <section className="space-y-4">
      <div>
        <h2 className="text-lg font-semibold">The LLM judges</h2>
        <p className="mt-1 max-w-3xl text-sm text-muted">
          How far each judge can be trusted, as people label more answers. A line is the 95% lower bound of a rate, so it
          rises as labels arrive and falls when the judge disagrees. A judge is ready to gate when both lines are above the
          green line ({pct(bound)}). A new judge or rule version starts a new chart.
        </p>
      </div>
      {tracks.length === 0 ? (
        <EmptyState icon={<LineChart />} title="No agreement measured yet">
          It appears after reviewers label answers and the review job runs.
        </EmptyState>
      ) : (
        <div className="grid gap-5 lg:grid-cols-2">
          {tracks.map((t) => {
            const last = t.points[t.points.length - 1];
            return (
              <ChartCard
                key={`${t.criterion_id}-${t.criterion_version}-${t.judge}`}
                title={`${criteria[t.criterion_id]?.title ?? t.criterion_id} · rule v${t.criterion_version}`}
                note={`${t.judge}. Latest: ${last.tn + last.fp} answers people failed, ${last.tp + last.fn} they passed, ${last.n - (last.n_applies ?? 0)} where the rule does not apply.`}
              >
                <Chart
                  points={t.points}
                  x={day}
                  threshold={bound}
                  lines={[
                    { label: "catches failures (TNR, lower bound)", value: (p) => p.tnr_lb ?? null, n: (p) => p.tn + p.fp },
                    { label: "passes good answers (TPR, lower bound)", dashed: true, value: (p) => p.tpr_lb ?? null, n: (p) => p.tp + p.fn },
                  ]}
                />
                <div className="flex flex-wrap gap-4 text-xs text-muted">
                  <span>— catches failures</span>
                  <span>- - leaves good answers alone</span>
                </div>
              </ChartCard>
            );
          })}
        </div>
      )}
      {latest.length > 0 && (
        <Card className="overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-oat text-left text-xs uppercase tracking-wide text-muted">
                <tr>
                  <th className="px-4 py-2 font-medium">Judge, current verdicts</th>
                  <th className="px-4 py-2 text-right font-medium">Verdicts</th>
                  <th className="px-4 py-2 text-right font-medium" title="The judge said the rule does not apply">Not applicable</th>
                  <th className="px-4 py-2 text-right font-medium" title="Of the verdicts with several samples, the share where every sample agreed">
                    Votes unanimous
                  </th>
                  <th className="px-4 py-2 text-right font-medium">Errors</th>
                  <th className="px-4 py-2 text-right font-medium" title="Verdicts judged in this run; the rest were reused because nothing they depend on changed">
                    Judged now
                  </th>
                  <th className="px-4 py-2 text-right font-medium">Tokens per verdict</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line tabular-nums">
                {latest.map((b) => (
                  <tr key={`${b.criterion_id}-${b.criterion_version}`}>
                    <td className="px-4 py-2">{criteria[b.criterion_id]?.title ?? b.criterion_id}</td>
                    <td className="px-4 py-2 text-right">{b.verdicts}</td>
                    <td className="px-4 py-2 text-right">{pct(b.na / b.verdicts)}</td>
                    <td className="px-4 py-2 text-right">{b.sampled ? `${pct(b.unanimous / b.sampled)} of ${b.sampled}` : "—"}</td>
                    <td className={b.errors ? "px-4 py-2 text-right text-fail" : "px-4 py-2 text-right"}>{b.errors}</td>
                    <td className="px-4 py-2 text-right">{b.judged}</td>
                    <td className="px-4 py-2 text-right">{b.tokens_per_verdict == null ? "—" : Math.round(b.tokens_per_verdict)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </section>
  );
}

function ReviewersSection({ spaceId }: { spaceId: string }) {
  const { data, error, loading } = useLoad(() => api.reviewerPerformance(spaceId), [spaceId]);
  if (loading) return <Loading />;
  if (error) return <ErrorBox error={error} />;
  const { reviewers, pairs, task_fail_rate: spaceTask, rule_fail_rate: spaceRule } = data!;

  return (
    <section className="space-y-4">
      <div>
        <h2 className="text-lg font-semibold">The reviewers</h2>
        <p className="mt-1 max-w-3xl text-sm text-muted">
          Whether review is keeping up, and whether people grade the same way. A reviewer much stricter or more lenient than
          the space, or one who rarely gives a reason, is worth a conversation about the rubric, not a verdict on the person.
        </p>
      </div>
      {reviewers.length === 0 ? (
        <EmptyState icon={<Users />} title="Nobody has reviewed yet" />
      ) : (
        <>
          <Card className="overflow-hidden">
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="bg-oat text-left text-xs uppercase tracking-wide text-muted">
                  <tr>
                    <th className="px-4 py-2 font-medium">Reviewer</th>
                    <th className="px-4 py-2 text-right font-medium">Answers reviewed</th>
                    <th className="px-4 py-2 font-medium" title="Share of answers they failed on task success; the line is the space's rate">
                      Fails answers
                    </th>
                    <th className="px-4 py-2 text-right font-medium" title="Rule labels: fails, and where they said the rule does not apply">
                      Rule labels
                    </th>
                    <th className="px-4 py-2 text-right font-medium" title="Share of their fails (answers and rules) that have a reason">
                      Fails with a reason
                    </th>
                    <th className="px-4 py-2 text-right font-medium" title="Median time from an answer being queued to their first verdict on it">
                      Turnaround
                    </th>
                    <th className="px-4 py-2 text-right font-medium" title="Their rule labels against the judges' current verdicts; kappa on fail vs not fail">
                      Agrees with judges
                    </th>
                    <th className="px-4 py-2 font-medium">Last active</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-line tabular-nums">
                  {reviewers.map((r) => {
                    const fails = r.task_failed + r.rule_fails;
                    return (
                      <tr key={r.reviewer}>
                        <td className="px-4 py-2.5">{r.reviewer}</td>
                        <td className="px-4 py-2.5 text-right">
                          {r.turns_reviewed}
                          <div className="text-xs text-muted">{r.active_7d} in the last 7 days</div>
                        </td>
                        <td className="px-4 py-2.5">
                          <RateBar value={r.turns_reviewed ? r.task_failed / r.turns_reviewed : null} reference={spaceTask ?? null} />
                        </td>
                        <td className="px-4 py-2.5 text-right">
                          {r.rule_labels}
                          <div className="text-xs text-muted">
                            {r.rule_fails} fail · {r.rule_na} not applicable
                          </div>
                        </td>
                        <td className="px-4 py-2.5 text-right">
                          {fails ? pct((r.task_fails_with_reason + r.rule_fails_with_reason) / fails) : "—"}
                        </td>
                        <td className="px-4 py-2.5 text-right">{hours(r.median_turnaround_hours)}</td>
                        <td className="px-4 py-2.5 text-right">
                          <Agreement value={r.with_judges} />
                        </td>
                        <td className="px-4 py-2.5 text-xs text-muted">{when(r.last_active)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </Card>
          <p className="text-xs text-muted">
            Across the space, people failed {pct(spaceTask)} of the answers they reviewed, and {pct(spaceRule)} of the rule labels
            where the rule applied.
          </p>
        </>
      )}

      <Card>
        <CardContent className="space-y-3 pt-5">
          <h3 className="text-sm font-semibold">Do reviewers agree with each other?</h3>
          {pairs.length === 0 ? (
            <p className="text-sm text-muted">
              Not measurable yet: it needs two reviewers who reviewed the same answers. Agreement between people is the check
              that the rubric is clear; without it, low judge agreement cannot tell a weak judge from a vague rule. Add a
              second person to the space’s reviewer group and ask both to review the same session.
            </p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="text-left text-xs uppercase tracking-wide text-muted">
                  <tr>
                    <th className="py-2 pr-4 font-medium">Reviewers</th>
                    <th className="py-2 pr-4 text-right font-medium">Task success</th>
                    <th className="py-2 text-right font-medium">Rule labels</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-line tabular-nums">
                  {pairs.map((p) => (
                    <tr key={`${p.a}-${p.b}`}>
                      <td className="py-2 pr-4">
                        {p.a}
                        <div className="text-xs text-muted">and {p.b}</div>
                      </td>
                      <td className="py-2 pr-4 text-right">
                        <Agreement value={p.task} />
                      </td>
                      <td className="py-2 text-right">
                        <Agreement value={p.rules} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
    </section>
  );
}

function hours(h: number | null | undefined): string {
  if (h == null) return "—";
  return h < 1 ? `${Math.round(h * 60)} min` : h < 48 ? `${h.toFixed(1)} h` : `${(h / 24).toFixed(1)} days`;
}

function Agreement({ value: a }: { value: PairAgreement }) {
  if (!a.n) return <span className="text-muted">—</span>;
  return (
    <>
      {a.exact}/{a.n} same
      <div className="text-xs text-muted" title="Cohen's kappa on fail vs not fail: 1 = perfect, 0 = chance">
        {a.kappa == null ? "kappa needs fails from both" : `kappa ${a.kappa.toFixed(2)}`}
      </div>
    </>
  );
}

/** A 0-100% bar with the space's rate as a tick, to compare one reviewer with everyone. */
function RateBar({ value, reference }: { value: number | null; reference: number | null }) {
  if (value == null) return <span className="text-muted">—</span>;
  return (
    <div className="flex min-w-40 items-center gap-2">
      <div className="relative h-2 flex-1 rounded-full bg-oat-deep">
        <div className="h-full rounded-full bg-navy" style={{ width: `${value * 100}%` }} />
        {reference != null && <div className="absolute -top-1 h-4 w-0.5 bg-fail" style={{ left: `${reference * 100}%` }} title={`space: ${pct(reference)}`} />}
      </div>
      <span className="w-10 text-right">{pct(value)}</span>
    </div>
  );
}

function ChartCard({ title, note, children }: { title: string; note: string; children: React.ReactNode }) {
  return (
    <Card>
      <CardContent className="space-y-2 pt-5">
        <h3 className="text-sm font-semibold">{title}</h3>
        {children}
        <p className="text-xs text-muted">{note}</p>
      </CardContent>
    </Card>
  );
}

type Line<T> = { label: string; value: (p: T) => number | null; n?: (p: T) => number; dashed?: boolean };

/** Weeks on x, as the week's Monday. */
function WeekChart({
  weeks,
  versions = [],
  ...rest
}: Omit<ChartProps<AgentWeek>, "points" | "x"> & { weeks: AgentWeek[]; versions?: SpaceVersionMark[] }) {
  const marks = versions
    .map((v) => ({ at: weeks.findIndex((w) => w.week === v.week), label: `${v.version.slice(0, 8)}: ${describeChanges(v)}` }))
    .filter((m) => m.at >= 0);
  return <Chart {...rest} marks={marks} points={weeks} x={(w) => w.week.slice(5)} />;
}

function describeChanges(v: SpaceVersionMark): string {
  const parts = Object.entries(v.changes ?? {}).map(([k, d]) => `${d > 0 ? "+" : ""}${d} ${k.replace("_", " ")}`);
  return parts.length ? parts.join(", ") : "content edited";
}

type ChartProps<T> = {
  points: T[];
  x: (p: T) => string;
  lines: Line<T>[];
  bars?: (p: T) => number;
  band?: (p: T) => [number, number] | null;
  threshold?: number;
  marks?: { at: number; label: string }[];
  unit?: string;
};

/** Rates (0-1) by default; `unit` switches to a value axis from 0 to the max. */
function Chart<T>({ points: weeks, x: xLabel, lines, bars, band, threshold, marks = [], unit }: ChartProps<T>) {
  const W = 480;
  const H = 180;
  const L = 40;
  const R = 24; // room for the last week's date label
  const T = 12;
  const B = 26;
  const x = (i: number) => L + (weeks.length === 1 ? (W - L - R) / 2 : (i / (weeks.length - 1)) * (W - L - R));
  const values = lines.flatMap((l) => weeks.map(l.value)).filter((v): v is number => v != null);
  const top = unit ? Math.max(1, ...values) * 1.1 : 1;
  const y = (v: number) => T + (1 - v / top) * (H - T - B);
  const ticks = unit ? [0, top / 2, top] : [0, 0.5, 1];
  const fmt = (v: number) => (unit ? `${Math.round(v)}${unit}` : pct(v));
  const barMax = bars ? Math.max(1, ...weeks.map(bars)) : 1;
  const step = (W - L - R) / Math.max(1, weeks.length - 1);
  const labelEvery = Math.ceil(weeks.length / 6);

  if (!values.length) return <p className="py-10 text-center text-sm text-muted">Nothing measured in this window yet.</p>;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label={lines.map((l) => l.label).join(", ")}>
      {ticks.map((t) => (
        <g key={t}>
          <line x1={L} x2={W - R} y1={y(t)} y2={y(t)} className="stroke-line" />
          <text x={L - 6} y={y(t) + 4} textAnchor="end" className="fill-muted text-[10px]">
            {fmt(t)}
          </text>
        </g>
      ))}
      {marks.map((m) => (
        <line key={m.label} x1={x(m.at)} x2={x(m.at)} y1={T} y2={H - B} className="stroke-warn" strokeDasharray="3 3" strokeWidth={1.5}>
          <title>{m.label}</title>
        </line>
      ))}
      {threshold != null && (
        <line x1={L} x2={W - R} y1={y(threshold)} y2={y(threshold)} className="stroke-pass" strokeDasharray="4 3" strokeWidth={1.5} />
      )}
      {weeks.map((w, i) =>
        i % labelEvery === 0 || i === weeks.length - 1 ? (
          <text key={i} x={x(i)} y={H - 8} textAnchor="middle" className="fill-muted text-[10px]">
            {xLabel(w)}
          </text>
        ) : null,
      )}
      {bars &&
        weeks.map((w, i) => {
          const h = (bars(w) / barMax) * (H - T - B) * 0.35;
          return (
            <rect key={i} x={x(i) - Math.min(14, step * 0.3)} width={Math.min(28, step * 0.6)} y={H - B - h} height={h} className="fill-oat-deep">
              <title>{`${xLabel(w)}: ${bars(w)}`}</title>
            </rect>
          );
        })}
      {band &&
        weeks.map((w, i) => {
          const b = band(w);
          return b ? <line key={i} x1={x(i)} x2={x(i)} y1={y(b[0])} y2={y(b[1])} className="stroke-link/25" strokeWidth={10} strokeLinecap="round" /> : null;
        })}
      {lines.map((l) => {
        const pts = weeks.map((w, i) => [i, l.value(w)] as const).filter(([, v]) => v != null) as [number, number][];
        return (
          <g key={l.label}>
            <path
              d={pts.map(([i, v], k) => `${k ? "L" : "M"}${x(i)},${y(v)}`).join(" ")}
              fill="none"
              className="stroke-navy"
              strokeWidth={2}
              strokeDasharray={l.dashed ? "5 4" : undefined}
            />
            {pts.map(([i, v]) => (
              <circle key={i} cx={x(i)} cy={y(v)} r={3.5} className={l.dashed ? "fill-white stroke-navy" : "fill-navy"}>
                <title>{`${xLabel(weeks[i])} · ${l.label}: ${fmt(v)}${l.n ? ` of ${l.n(weeks[i])}` : ""}`}</title>
              </circle>
            ))}
          </g>
        );
      })}
    </svg>
  );
}

function WeekTable({ weeks }: { weeks: AgentWeek[] }) {
  const shown = weeks.filter((w) => w.answers).reverse();
  return (
    <Card className="overflow-hidden">
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-oat text-left text-xs uppercase tracking-wide text-muted">
            <tr>
              <th className="px-4 py-2 font-medium">Week of</th>
              <th className="px-4 py-2 text-right font-medium">Answers</th>
              <th className="px-4 py-2 text-right font-medium">Graded</th>
              <th className="px-4 py-2 text-right font-medium">Must-pass</th>
              <th className="px-4 py-2 text-right font-medium">Reviewed</th>
              <th className="px-4 py-2 text-right font-medium">Task success</th>
              <th className="px-4 py-2 text-right font-medium">SQL steps</th>
              <th className="px-4 py-2 text-right font-medium">Genie errors</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-line tabular-nums">
            {shown.map((w) => (
              <tr key={w.week}>
                <td className="px-4 py-2">{w.week}</td>
                <td className="px-4 py-2 text-right">{w.answers}</td>
                <td className="px-4 py-2 text-right">{w.graded}</td>
                <td className="px-4 py-2 text-right">{w.checked ? pct((w.checked - w.hard_failed) / w.checked) : "—"}</td>
                <td className="px-4 py-2 text-right">{w.reviewed}</td>
                <td className="px-4 py-2 text-right">
                  {w.reviewed ? `${pct(w.task_passed / w.reviewed)} (${pct(w.task_low)}–${pct(w.task_high)})` : "—"}
                </td>
                <td className="px-4 py-2 text-right">{w.sql_steps == null ? "—" : w.sql_steps.toFixed(1)}</td>
                <td className="px-4 py-2 text-right">{w.genie_errors}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
