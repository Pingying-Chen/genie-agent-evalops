// Versions: every run with what produced its numbers, and a diff of any two:
// "what changed between these two numbers". Owners only.
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, GitCompare } from "lucide-react";
import { api, type ManifestDiff } from "@/api";
import { useLoad } from "@/hooks";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { ErrorBox, Loading, when } from "@/components/common";
import { cn } from "@/lib/utils";

const KIND = { check: "check", eval: "regression", review: "review" } as const;

function short(v?: string | null) {
  return v ? v.replace("sha256:", "").slice(0, 8) : "—";
}

export function Versions() {
  const { spaceId = "" } = useParams();
  const { data, error, loading } = useLoad(() => api.versions(spaceId), [spaceId]);
  const [picked, setPicked] = useState<string[]>([]);
  const [diff, setDiff] = useState<ManifestDiff | null>(null);
  const [diffError, setDiffError] = useState<Error | null>(null);

  if (loading) return <Loading />;
  if (error) return <ErrorBox error={error} />;
  const v = data!;

  function toggle(runId: string) {
    setDiff(null);
    setPicked((p) => (p.includes(runId) ? p.filter((x) => x !== runId) : [...p.slice(-1), runId]));
  }

  async function compare() {
    setDiffError(null);
    // Older first, so the diff reads "from a to b".
    const [a, b] = [...picked].sort(
      (x, y) => +new Date(v.runs.find((r) => r.run_id === x)!.created_at) - +new Date(v.runs.find((r) => r.run_id === y)!.created_at),
    );
    try {
      setDiff(await api.versionsDiff(spaceId, a, b));
    } catch (err) {
      setDiffError(err as Error);
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <Link to={`/spaces/${spaceId}`} className="inline-flex items-center gap-1 text-sm text-muted hover:text-ink">
          <ArrowLeft className="size-4" aria-hidden /> Back to the agent
        </Link>
        <h1 className="mt-2 text-2xl font-semibold tracking-tight">Versions</h1>
        <p className="mt-1 max-w-3xl text-sm text-muted">
          Every check, regression and review run records what produced its numbers: the agent’s configuration, each rule,
          judge prompt and reviewer guideline (by content), the cases asked, the settings and the platform’s code. Pick two
          runs to see what changed between them.
        </p>
      </div>

      {(v.drift ?? []).length > 0 && (
        <Card>
          <CardContent className="space-y-2 pt-5 text-sm">
            <h2 className="font-semibold text-warn">Genie changed while nothing we control did</h2>
            <p className="text-xs text-muted">
              Two weekly drift checks on identical inputs, with cases that passed every time in one and failed every time in the
              other: likely a Genie platform or model update.
            </p>
            <ul className="space-y-1">
              {v.drift!.map((d) => (
                <li key={d.later_run}>
                  {d.flipped.length} case{d.flipped.length > 1 ? "s" : ""} flipped between{" "}
                  <span className="font-mono">{d.earlier_run.slice(0, 8)}</span> and{" "}
                  <span className="font-mono">{d.later_run.slice(0, 8)}</span>
                  {d.note && <span className="text-muted"> · {d.note}</span>}
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      )}

      <section className="space-y-3">
        <div className="flex flex-wrap items-center gap-3">
          <h2 className="text-lg font-semibold">Runs</h2>
          <Button size="sm" variant="primary" disabled={picked.length !== 2} onClick={compare}>
            <GitCompare className="size-4" aria-hidden /> Compare the two picked
          </Button>
          <span className="text-xs text-muted">{picked.length}/2 picked</span>
        </div>
        {diffError && <ErrorBox error={diffError} />}
        {diff && <DiffView diff={diff} />}
        {v.runs.length === 0 ? (
          <p className="text-sm text-muted">No runs recorded yet: manifests start with the next check, review or regression run.</p>
        ) : (
          <Card className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-xs text-muted">
                <tr className="text-left">
                  <th className="px-3 py-2 font-normal" />
                  <th className="px-3 py-2 font-normal">When</th>
                  <th className="px-3 py-2 font-normal">Run</th>
                  <th className="px-3 py-2 font-normal">Agent configuration</th>
                  <th className="px-3 py-2 font-normal">Rules</th>
                  <th className="px-3 py-2 font-normal">Texts</th>
                  <th className="px-3 py-2 font-normal">Cases</th>
                  <th className="px-3 py-2 font-normal">Code</th>
                </tr>
              </thead>
              <tbody>
                {v.runs.map((r) => (
                  <tr key={`${r.run_kind}-${r.run_id}`} className={cn("border-t border-line", picked.includes(r.run_id) && "bg-oat")}>
                    <td className="px-3 py-2">
                      <input
                        type="checkbox"
                        aria-label={`Pick run ${r.run_id.slice(0, 8)}`}
                        checked={picked.includes(r.run_id)}
                        onChange={() => toggle(r.run_id)}
                      />
                    </td>
                    <td className="whitespace-nowrap px-3 py-2">{when(r.created_at)}</td>
                    <td className="px-3 py-2">
                      <Badge variant="outline">{KIND[r.run_kind]}</Badge> {r.label && <span className="text-muted">{r.label}</span>}
                    </td>
                    <td className="px-3 py-2 font-mono text-xs">
                      {Object.entries(r.config_versions)
                        .map(([arm, h]) => `${arm} ${short(h)}`)
                        .join(" · ") || "—"}
                    </td>
                    <td className="px-3 py-2 font-mono text-xs">{short(r.criteria_set)}</td>
                    <td className="px-3 py-2 tabular-nums">{r.artifacts}</td>
                    <td className="px-3 py-2 tabular-nums">{r.cases || "—"}</td>
                    <td className="px-3 py-2 font-mono text-xs">{short(r.platform_commit)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
        )}
      </section>

      {v.config_history.length > 0 && (
        <section>
          <h2 className="text-lg font-semibold">Agent configuration history</h2>
          <ul className="mt-2 space-y-1 text-sm">
            {v.config_history.map((h) => (
              <li key={h.version}>
                <span className="font-mono text-xs">{short(h.version)}</span> <span className="text-muted">since {when(h.captured_at)}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

function DiffView({ diff }: { diff: ManifestDiff }) {
  const nothing =
    !diff.fields.length && !diff.artifacts.length && !diff.cases_added.length && !diff.cases_removed.length && !diff.cases_changed.length;
  return (
    <Card>
      <CardContent className="space-y-4 pt-5 text-sm">
        <h3 className="font-semibold">
          From <span className="font-mono">{diff.a.run_id.slice(0, 8)}</span> ({when(diff.a.created_at)}) to{" "}
          <span className="font-mono">{diff.b.run_id.slice(0, 8)}</span> ({when(diff.b.created_at)})
        </h3>
        {nothing && <p className="text-muted">Nothing we record differs: the same agent, rules, prompts, guidelines, cases and settings.</p>}
        {diff.fields.length > 0 && (
          <ul className="space-y-1">
            {diff.fields.map((f) => (
              <li key={f.field}>
                <span className="font-medium">{f.field}</span>:{" "}
                <span className="font-mono text-xs">{JSON.stringify(f.before) ?? "—"}</span> →{" "}
                <span className="font-mono text-xs">{JSON.stringify(f.after) ?? "—"}</span>
              </li>
            ))}
          </ul>
        )}
        {(diff.cases_added.length > 0 || diff.cases_removed.length > 0 || diff.cases_changed.length > 0) && (
          <p className="text-muted">
            Cases: {diff.cases_added.length} added, {diff.cases_removed.length} removed, {diff.cases_changed.length} at a new version or
            split.
          </p>
        )}
        {diff.artifacts.map((x) => (
          <details key={`${x.kind}-${x.name}`} className="rounded-lg border border-line">
            <summary className="cursor-pointer px-3 py-2">
              <Badge variant={x.change === "changed" ? "warn" : "outline"}>{x.change}</Badge> {x.kind} <span className="font-medium">{x.name}</span>{" "}
              <span className="text-muted">
                {x.before_version ?? "—"} → {x.after_version ?? "—"}
                {x.change === "changed" && x.before_version === x.after_version && " (same version number, new text)"}
              </span>
            </summary>
            <div className="grid gap-2 border-t border-line p-3 md:grid-cols-2">
              <pre className="max-h-80 overflow-auto whitespace-pre-wrap rounded bg-oat p-2 text-xs">{x.before ?? "—"}</pre>
              <pre className="max-h-80 overflow-auto whitespace-pre-wrap rounded bg-oat p-2 text-xs">{x.after ?? "—"}</pre>
            </div>
          </details>
        ))}
      </CardContent>
    </Card>
  );
}
