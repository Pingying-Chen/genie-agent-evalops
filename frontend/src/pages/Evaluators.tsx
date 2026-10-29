// Evaluators: who or what judges this agent's answers, and which of them may
// decide a gate. An LLM judge reports until people have shown it agrees with them. Owners only.
import { Link, useParams } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
import { api, type Evaluator } from "@/api";
import { useLoad } from "@/hooks";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { ErrorBox, Loading, when } from "@/components/common";

const STATUS = {
  deterministic: { label: "code: decides", variant: "outline" },
  calibrated: { label: "calibrated: decides", variant: "pass" },
  uncalibrated: { label: "not calibrated: reports only", variant: "warn" },
  human: { label: "people", variant: "neutral" },
} as const;

function pct(x?: number | null) {
  return x == null ? "—" : `${Math.round(x * 100)}%`;
}

export function EvaluatorsPage() {
  const { spaceId = "" } = useParams();
  const { data, error, loading } = useLoad(() => api.evaluators(spaceId), [spaceId]);
  if (loading) return <Loading />;
  if (error) return <ErrorBox error={error} />;
  const list = data!.evaluators;
  const groups: [string, Evaluator[]][] = [
    ["LLM judges", list.filter((e) => e.kind === "llm_judge")],
    ["Code", list.filter((e) => e.kind === "code")],
    ["People", list.filter((e) => e.kind === "human")],
  ];

  return (
    <div className="space-y-6">
      <div>
        <Link to={`/spaces/${spaceId}`} className="inline-flex items-center gap-1 text-sm text-muted hover:text-ink">
          <ArrowLeft className="size-4" aria-hidden /> Back to the agent
        </Link>
        <h1 className="mt-2 text-2xl font-semibold tracking-tight">Evaluators</h1>
        <p className="mt-1 max-w-3xl text-sm text-muted">
          Every verdict comes from an evaluator. Code decides from the start. An LLM judge may decide a gate only once both
          Wilson lower bounds of its agreement with reviewers (passing good answers: TPR; catching failures: TNR) reach{" "}
          {pct(data!.min_lower_bound)}; until then its fails are shown but cannot accept or reject a change.
        </p>
      </div>
      {groups
        .filter(([, items]) => items.length > 0)
        .map(([title, items]) => (
          <section key={title} className="space-y-3">
            <h2 className="text-lg font-semibold">{title}</h2>
            <div className="grid gap-3 lg:grid-cols-2">
              {items.map((e) => (
                <EvaluatorCard key={e.id} e={e} />
              ))}
            </div>
          </section>
        ))}
    </div>
  );
}

function EvaluatorCard({ e }: { e: Evaluator }) {
  const cal = e.calibration;
  const m = e.measured;
  return (
    <Card>
      <CardContent className="space-y-2 pt-5 text-sm">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium">{e.name}</span>
          <Badge variant={STATUS[e.status].variant}>{STATUS[e.status].label}</Badge>
          {e.gate && <span className="text-xs text-muted">gate: {e.gate}</span>}
        </div>
        {e.version && <p className="font-mono text-xs text-muted">{e.version}</p>}
        <p className="line-clamp-4 text-muted" title={e.rubric}>
          {e.rubric}
        </p>
        {cal && (
          <table className="w-full text-xs tabular-nums">
            <tbody>
              <tr>
                <td className="py-0.5 text-muted">Passes good answers (TPR)</td>
                <td className="text-right">
                  <RateText rate={cal.tpr} lower={cal.tpr_lb} hits={cal.tp} total={cal.tp + cal.fn} none="no passes labelled yet" />
                </td>
              </tr>
              <tr>
                <td className="py-0.5 text-muted">Catches failures (TNR)</td>
                <td className="text-right">
                  <RateText rate={cal.tnr} lower={cal.tnr_lb} hits={cal.tn} total={cal.tn + cal.fp} none="no failures labelled yet" />
                </td>
              </tr>
              <tr>
                <td className="py-0.5 text-muted">Its fails that are real (fail precision)</td>
                <td className="text-right">
                  <RateText rate={cal.precision} lower={cal.precision_lb} hits={cal.tn} total={cal.tn + cal.fn} none="it has failed nothing labelled" />
                </td>
              </tr>
              <tr>
                <td className="py-0.5 text-muted">Cohen’s κ</td>
                <td className="text-right">{cal.kappa == null ? "—" : cal.kappa.toFixed(2)}</td>
              </tr>
              <tr>
                <td className="py-0.5 text-muted">Measured</td>
                <td className="text-right">{cal.computed_at ? when(cal.computed_at) : "—"}</td>
              </tr>
            </tbody>
          </table>
        )}
        {m && (
          <p className="text-xs">
            Measured {m.measured_on}: {m.false_alarms} false alarms on {m.negatives} correct items; caught {m.caught} of{" "}
            {m.positives} errors ({pct(m.caught / m.positives)}).{" "}
            <span className="text-muted" title={m.method}>
              How
            </span>
          </p>
        )}
        {e.note && <p className="text-xs text-muted">{e.note}</p>}
      </CardContent>
    </Card>
  );
}

function RateText({ rate, lower, hits, total, none }: { rate?: number | null; lower?: number | null; hits: number; total: number; none: string }) {
  if (!total) return <span className="text-muted">{none}</span>;
  return (
    <>
      {pct(rate)} <span className="text-muted">≥ {pct(lower)}</span> ({hits}/{total})
    </>
  );
}
