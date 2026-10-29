// Domain pieces shared by the pages, built on the ui/ primitives. Wording is for people who do
// not read criterion ids: gates and verdicts are shown in plain words, with an icon.
import type { ReactNode } from "react";
import { AlertTriangle, CheckCircle2, CircleSlash, Eye, MinusCircle, XCircle } from "lucide-react";
import { ApiError, api, type Criterion } from "@/api";
import { useLoad } from "@/hooks";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

export function pct(value: number | null | undefined, digits = 0): string {
  return value === null || value === undefined ? "—" : `${(value * 100).toFixed(digits)}%`;
}

export function when(iso: string | null | undefined): string {
  return iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "—";
}

/** "3 hours ago"; the exact time is in the title tooltip. */
export function ago(iso: string | null | undefined): string {
  if (!iso) return "never";
  const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} h ago`;
  return `${Math.round(hours / 24)} days ago`;
}

// ------------------------------------------------------------------ verdicts and gates

const VERDICT = {
  pass: { label: "Pass", variant: "pass", Icon: CheckCircle2 },
  fail: { label: "Fail", variant: "fail", Icon: XCircle },
  na: { label: "Not applicable", variant: "neutral", Icon: MinusCircle },
  error: { label: "Check error", variant: "warn", Icon: AlertTriangle },
  not_gradable: { label: "Not gradable", variant: "neutral", Icon: CircleSlash },
} as const;

export function VerdictBadge({ verdict }: { verdict: string }) {
  const v = VERDICT[verdict as keyof typeof VERDICT] ?? VERDICT.na;
  return (
    <Badge variant={v.variant}>
      <v.Icon aria-hidden />
      {v.label}
    </Badge>
  );
}

export const GATE: Record<string, { label: string; help: string }> = {
  hard: { label: "Must pass", help: "A failure here fails the whole answer." },
  soft: { label: "Should pass", help: "Reported, but does not fail the answer." },
  monitor: { label: "Tracking only", help: "Observed for trends. Not counted as a failure (e.g. an LLM judge not yet calibrated by people)." },
};

export function GateBadge({ gate }: { gate: string }) {
  const g = GATE[gate] ?? { label: gate, help: "" };
  return (
    <Badge variant={gate === "hard" ? "outline" : "neutral"} title={g.help} className={gate === "hard" ? "text-ink" : undefined}>
      {gate === "monitor" && <Eye aria-hidden />}
      {g.label}
    </Badge>
  );
}

// ------------------------------------------------------------------ criteria names

/** Criteria by id (loaded once per page): readable title, gate and description. */
export function useCriteria(): Record<string, Criterion> {
  const { data } = useLoad(() => api.criteria(), []);
  return Object.fromEntries((data?.criteria ?? []).map((c) => [c.id, c]));
}

export function CriterionName({ id, criteria }: { id: string; criteria: Record<string, Criterion> }) {
  const c = criteria[id];
  return (
    <span title={c?.description}>
      <span className="font-medium">{c?.title ?? id}</span>
      {c?.title && <span className="block font-mono text-xs text-muted">{id}</span>}
    </span>
  );
}

// ------------------------------------------------------------------ states

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="space-y-3" aria-busy="true" aria-label={label}>
      <div className="h-7 w-1/3 animate-pulse rounded-md bg-oat-deep" />
      <div className="h-28 animate-pulse rounded-xl bg-oat-deep" />
      <div className="h-28 animate-pulse rounded-xl bg-oat-deep" />
    </div>
  );
}

export function ErrorBox({ error }: { error: Error }) {
  const apiError = error instanceof ApiError ? error : null;
  return (
    <div role="alert" className="rounded-xl border border-fail/30 bg-fail-soft p-4 text-sm text-fail">
      <div className="flex items-center gap-2 font-medium">
        <XCircle className="size-4" aria-hidden />
        {apiError?.status === 403 ? "You do not have access to this." : "Something went wrong."}
      </div>
      <p className="mt-1 text-ink">{error.message}</p>
      {apiError?.requestId && <p className="mt-1 font-mono text-xs text-muted">request id {apiError.requestId}</p>}
    </div>
  );
}

export function EmptyState({ icon, title, children }: { icon: ReactNode; title: string; children?: ReactNode }) {
  return (
    <div className="rounded-xl border border-dashed border-line bg-white p-8 text-center">
      <div className="mx-auto mb-3 flex size-10 items-center justify-center rounded-full bg-oat-deep text-muted [&_svg]:size-5">{icon}</div>
      <p className="font-medium">{title}</p>
      {children && <div className="mt-1 text-sm text-muted">{children}</div>}
    </div>
  );
}

// ------------------------------------------------------------------ small charts

const STATUS: Record<string, { label: string; bar: string }> = {
  ok: { label: "graded", bar: "bg-pass" },
  owner_only: { label: "private (owner only)", bar: "bg-warn" },
  pre_agent_mode: { label: "before agent mode", bar: "bg-warn/60" },
  chat_mode: { label: "chat mode", bar: "bg-muted/50" },
  error: { label: "unreadable", bar: "bg-fail" },
};

/** How many conversations could be graded, split by why not. */
export function CoverageBar({ byStatus, total }: { byStatus: Record<string, number>; total: number }) {
  if (!total) return <span className="text-sm text-muted">No conversations yet</span>;
  const shown = Object.keys(STATUS).filter((s) => byStatus[s]);
  return (
    <div>
      <div className="flex h-2 overflow-hidden rounded-full bg-oat-deep" aria-label="coverage">
        {shown.map((s) => (
          <div key={s} className={STATUS[s].bar} style={{ width: `${(byStatus[s] / total) * 100}%` }} title={`${STATUS[s].label}: ${byStatus[s]}`} />
        ))}
      </div>
      <div className="mt-1.5 flex flex-wrap gap-x-3 text-xs text-muted">
        {shown.map((s) => (
          <span key={s} className="inline-flex items-center gap-1">
            <span className={cn("size-2 rounded-full", STATUS[s].bar)} />
            {byStatus[s]} {STATUS[s].label}
          </span>
        ))}
      </div>
    </div>
  );
}

/** Daily must-pass rate as a small SVG line. Days are UTC days, as the backend groups them. */
export function Trend({ points }: { points: { day: string; hard_pass_rate: number | null; n_traces: number }[] }) {
  const values = points.filter((p) => p.hard_pass_rate !== null);
  if (values.length === 0) return <span className="text-sm text-muted">No checked answers in this window</span>;
  const w = 320;
  const h = 72;
  const x = (i: number) => (values.length === 1 ? w / 2 : (i / (values.length - 1)) * (w - 16) + 8);
  const y = (v: number) => h - 8 - v * (h - 16);
  const path = values.map((p, i) => `${i ? "L" : "M"}${x(i)},${y(p.hard_pass_rate!)}`).join(" ");
  const last = values[values.length - 1];
  return (
    <div>
      <svg viewBox={`0 0 ${w} ${h}`} className="h-18 w-full" role="img" aria-label="daily must-pass rate">
        <line x1="0" x2={w} y1={y(1)} y2={y(1)} className="stroke-line" strokeDasharray="4 4" />
        <path d={path} className="stroke-navy" strokeWidth="2" fill="none" />
        {values.map((p, i) => (
          <circle key={p.day} cx={x(i)} cy={y(p.hard_pass_rate!)} r="3.5" className="fill-navy">
            <title>{`${p.day} (UTC): ${pct(p.hard_pass_rate)} of ${p.n_traces}`}</title>
          </circle>
        ))}
      </svg>
      <p className="text-xs text-muted">
        Latest UTC day, {last.day}: {pct(last.hard_pass_rate)} of {last.n_traces} answers
      </p>
    </div>
  );
}
