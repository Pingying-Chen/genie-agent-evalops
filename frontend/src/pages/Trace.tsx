// Page 3: one answer — the report (unsourced figures highlighted) next to every step and its checks.
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, ChevronRight, ClipboardList, Database, MessageSquareText } from "lucide-react";
import { api, type CheckResult, type Criterion, type Step } from "@/api";
import { useLoad } from "@/hooks";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { CriterionName, ErrorBox, GateBadge, Loading, VerdictBadge, useCriteria, when } from "@/components/common";
import { Markdown } from "@/components/markdown";

export function Trace() {
  const { traceId = "" } = useParams();
  const { data, error, loading } = useLoad(() => api.trace(traceId), [traceId]);
  const criteria = useCriteria();
  if (loading) return <Loading />;
  if (error) return <ErrorBox error={error} />;
  const { trace, steps, results } = data!;
  const reportLevel = results.filter((r) => r.step_index === null || r.step_index === undefined);
  const unsourced = unsourcedFigures(reportLevel);

  return (
    <div className="space-y-6">
      <div>
        <Link to={`/spaces/${trace.space_id}`} className="inline-flex items-center gap-1 text-sm text-muted hover:text-ink">
          <ArrowLeft className="size-4" aria-hidden /> Back to agent
        </Link>
        <h1 className="mt-2 text-2xl font-semibold tracking-tight">{trace.question ?? "(conversation not readable)"}</h1>
        <div className="mt-2 flex flex-wrap gap-2 text-xs">
          <Badge variant="outline">Asked {when(trace.asked_at)}</Badge>
          <Badge variant="outline">Turn {trace.turn_index + 1}</Badge>
          <Badge variant="outline">{trace.n_sql_steps} SQL {trace.n_sql_steps === 1 ? "step" : "steps"}</Badge>
          <Link
            to={`/spaces/${trace.space_id}/evalset/new?trace=${trace.trace_id}`}
            className="inline-flex items-center gap-1 rounded-full border border-line bg-white px-2.5 py-0.5 font-medium text-ink hover:bg-oat"
          >
            <ClipboardList className="size-3.5" aria-hidden /> Add to eval set
          </Link>
        </div>
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <div className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <MessageSquareText className="size-4 text-muted" aria-hidden /> Answer
              </CardTitle>
            </CardHeader>
            <CardContent>
              {trace.genie_error && <p className="mb-3 rounded-lg bg-fail-soft p-3 text-sm text-fail">Genie error: {trace.genie_error}</p>}
              <Markdown text={trace.report_text ?? ""} highlight={unsourced} />
              {unsourced.length > 0 && (
                <p className="mt-3 text-xs text-muted">
                  <mark className="rounded bg-mark px-1">Highlighted</mark> figures were not found in any step result.
                </p>
              )}
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Checks on the answer</CardTitle>
            </CardHeader>
            <CardContent>
              <Results results={reportLevel} criteria={criteria} />
              <Figures results={reportLevel} />
            </CardContent>
          </Card>
        </div>

        <div>
          <h2 className="mb-3 flex items-center gap-2 text-base font-semibold">
            <Database className="size-4 text-muted" aria-hidden /> Steps
          </h2>
          {steps.length === 0 && <p className="text-sm text-muted">Genie answered without running any query.</p>}
          <div className="space-y-3">
            {steps.map((step) => (
              <StepPanel
                key={step.step_index}
                step={step}
                steps={steps}
                criteria={criteria}
                results={results.filter((r) => r.step_index === step.step_index)}
              />
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

function unsourcedFigures(results: CheckResult[]): string[] {
  const figureCheck = results.find((r) => r.criterion_id === "report.figure_sourced");
  const unsourced = (figureCheck?.evidence as { unsourced?: string[] } | undefined)?.unsourced;
  return unsourced ?? [];
}

type FigureEvidence = { text: string; kind: string; status: string; how: string | null };

const FIGURE_STATUS = { unsourced: "fail", derived: "warn", sourced: "pass" } as const;

/** How every figure in the report was checked against the step results (report.figure_sourced). */
function Figures({ results }: { results: CheckResult[] }) {
  const check = results.find((r) => r.criterion_id === "report.figure_sourced");
  const figures = (check?.evidence as { figures?: FigureEvidence[] } | undefined)?.figures ?? [];
  if (!figures.length) return null;
  return (
    <details className="group mt-4" open={figures.some((f) => f.status === "unsourced")}>
      <summary className="flex cursor-pointer list-none items-center gap-1 text-sm text-muted hover:text-ink">
        <ChevronRight className="size-4 transition-transform group-open:rotate-90" aria-hidden />
        Where each number came from ({figures.length})
      </summary>
      <div className="mt-2 overflow-x-auto rounded-lg border border-line">
        <table className="w-full text-xs">
          <thead className="bg-oat text-left text-muted">
            <tr>
              <th className="px-3 py-2 font-medium">Figure</th>
              <th className="px-3 py-2 font-medium">Kind</th>
              <th className="px-3 py-2 font-medium">Status</th>
              <th className="px-3 py-2 font-medium">How</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {figures.map((f, i) => (
              <tr key={i}>
                <td className="px-3 py-1.5 font-mono">{f.text}</td>
                <td className="px-3 py-1.5">{f.kind}</td>
                <td className="px-3 py-1.5">
                  <Badge variant={FIGURE_STATUS[f.status as keyof typeof FIGURE_STATUS] ?? "neutral"}>{f.status}</Badge>
                </td>
                <td className="px-3 py-1.5 text-muted">{f.how ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

function StepPanel({
  step,
  steps,
  results,
  criteria,
}: {
  step: Step;
  steps: Step[];
  results: CheckResult[];
  criteria: Record<string, Criterion>;
}) {
  const failed = results.filter((r) => r.verdict === "fail");
  // The API gives a chart's title and the query it plots, not the chart itself.
  const plots = step.source_call_id ? steps.find((s) => s.call_id === step.source_call_id) : undefined;
  return (
    <details className="group rounded-xl border border-line bg-white shadow-sm" open={failed.length > 0}>
      <summary className="flex cursor-pointer list-none items-center gap-2 px-4 py-3">
        <ChevronRight className="size-4 shrink-0 text-muted transition-transform group-open:rotate-90" aria-hidden />
        <span className="flex size-6 shrink-0 items-center justify-center rounded-full bg-oat-deep text-xs font-semibold">
          {step.step_index + 1}
        </span>
        <span className="min-w-0 flex-1 truncate font-medium">{step.title ?? step.tool_name}</span>
        <Badge variant="neutral">{STEP_KIND[step.step_kind] ?? step.step_kind}</Badge>
        {failed.length > 0 && <VerdictBadge verdict="fail" />}
      </summary>
      <div className="space-y-3 border-t border-line px-4 py-3">
        {plots && (
          <p className="text-sm text-muted">
            Plots the rows of step {plots.step_index + 1}: <span className="text-ink">{plots.title ?? plots.tool_name}</span>
          </p>
        )}
        {step.sql && <pre className="overflow-x-auto rounded-lg bg-navy p-3 font-mono text-xs leading-relaxed text-white">{step.sql}</pre>}
        {step.error && <p className="rounded-lg bg-fail-soft p-3 text-sm text-fail">{step.error}</p>}
        {step.result_rows && step.result_schema && <ResultTable schema={step.result_schema} rows={step.result_rows} total={step.row_count} />}
        <Results results={results} criteria={criteria} />
      </div>
    </details>
  );
}

const STEP_KIND: Record<string, string> = { answer: "query", explore: "exploring data", viz: "chart" };

function ResultTable({ schema, rows, total }: { schema: Record<string, unknown>[]; rows: unknown[][]; total?: number | null }) {
  const shown = rows.slice(0, 10);
  return (
    <div>
      <div className="overflow-x-auto rounded-lg border border-line">
        <table className="w-full text-xs">
          <thead className="bg-oat text-left text-muted">
            <tr>{schema.map((c) => <th key={String(c.name)} className="px-3 py-2 font-medium">{String(c.name)}</th>)}</tr>
          </thead>
          <tbody className="divide-y divide-line">
            {shown.map((row, i) => (
              <tr key={i}>{row.map((cell, j) => <td key={j} className="px-3 py-1.5 tabular-nums">{String(cell)}</td>)}</tr>
            ))}
          </tbody>
        </table>
      </div>
      {(total ?? rows.length) > shown.length && (
        <p className="mt-1 text-xs text-muted">Showing {shown.length} of {total ?? rows.length} rows</p>
      )}
    </div>
  );
}

function Results({ results, criteria }: { results: CheckResult[]; criteria: Record<string, Criterion> }) {
  if (!results.length) return null;
  return (
    <ul className="divide-y divide-line">
      {results.map((r) => (
        <li key={`${r.criterion_id}-${r.step_index}`} className="py-2.5">
          <div className="flex items-start justify-between gap-3">
            <CriterionName id={r.criterion_id} criteria={criteria} />
            <div className="flex shrink-0 items-center gap-1.5">
              <GateBadge gate={r.gate} />
              <VerdictBadge verdict={r.verdict} />
            </div>
          </div>
          {r.verdict !== "pass" && Object.keys(r.evidence ?? {}).length > 0 && (
            <details className="group mt-1.5">
              <summary className="flex cursor-pointer list-none items-center gap-1 text-xs text-muted hover:text-ink">
                <ChevronRight className="size-3.5 transition-transform group-open:rotate-90" aria-hidden />
                Evidence
              </summary>
              <pre className="mt-1 overflow-x-auto rounded-lg bg-oat p-2 font-mono text-xs">{JSON.stringify(r.evidence, null, 2)}</pre>
            </details>
          )}
        </li>
      ))}
    </ul>
  );
}
