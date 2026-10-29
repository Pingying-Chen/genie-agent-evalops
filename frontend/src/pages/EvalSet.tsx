// Page 7: the eval set. What consumers ask and what a good answer must do; every
// edit is a new version, retired cases keep their history. Owners only.
import { useEffect, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { ArrowLeft, ClipboardList, Download, Upload } from "lucide-react";
import { api, type CaseSplit, type Criterion, type EvalCase, type EvalCaseBody, type EvalSetResponse, type SplitSettings } from "@/api";
import { useLoad } from "@/hooks";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState, ErrorBox, Loading, useCriteria, when } from "@/components/common";
import { cn } from "@/lib/utils";
import { RunsSection } from "@/pages/Evaluations";

const BEHAVIOUR = {
  answer: "Answers with data",
  clarify: "Asks which one is meant",
  refuse: "Declines",
} as const;

const CASE_TYPE = {
  core: "Core: what users mostly ask",
  edge: "Edge: unusual but valid",
  guardrail: "Guardrail: must refuse or hold back",
} as const;

const DIMENSION = {
  type: "Type",
  intent: "Intent",
  category: "Failure category",
  turns: "Single or multi-turn",
} as const;
type Dimension = keyof typeof DIMENSION;

export function EvalSet() {
  const { spaceId = "" } = useParams();
  const { data, error, loading, reload } = useLoad(() => api.evalset(spaceId), [spaceId]);
  const [message, setMessage] = useState<string | null>(null);
  const [actionError, setActionError] = useState<Error | null>(null);

  if (loading && !data) return <Loading />;
  if (error) return <ErrorBox error={error} />;
  const { cases, coverage } = data!;
  const active = cases.filter((c) => c.status === "active");
  const splitOf = new Map((data!.splits ?? []).map((s) => [s.case_id, s]));

  async function override(caseId: string, split: "dev" | "test" | null) {
    setActionError(null);
    try {
      await api.overrideSplit(spaceId, caseId, split);
      reload();
    } catch (err) {
      setActionError(err as Error);
    }
  }

  async function exportYaml() {
    const text = await api.exportEvalset(spaceId);
    const url = URL.createObjectURL(new Blob([text], { type: "application/yaml" }));
    const link = Object.assign(document.createElement("a"), { href: url, download: `evalset-${spaceId.slice(0, 8)}.yaml` });
    link.click();
    URL.revokeObjectURL(url);
  }

  async function importYaml(file: File) {
    setActionError(null);
    setMessage(null);
    try {
      const r = await api.importEvalset(spaceId, await file.text());
      setMessage(`Imported: ${r.created} new, ${r.updated} changed, ${r.unchanged} unchanged.`);
      reload();
    } catch (err) {
      setActionError(err as Error);
    }
  }

  return (
    <div className="space-y-8">
      <div>
        <Link to={`/spaces/${spaceId}`} className="inline-flex items-center gap-1 text-sm text-muted hover:text-ink">
          <ArrowLeft className="size-4" aria-hidden /> Back to the agent
        </Link>
        <h1 className="mt-2 text-2xl font-semibold tracking-tight">Eval set</h1>
        <p className="mt-1 max-w-3xl text-sm text-muted">
          Questions to ask before a change to the agent goes live, each with what a good answer must do. Add them from
          reviewed answers (“Add to eval set” on an answer) or write them here. Every edit is kept as a new version.
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <Link to={`/spaces/${spaceId}/evalset/new`}>
          <Button variant="primary">New case</Button>
        </Link>
        <Link to={`/spaces/${spaceId}/changes`}>
          <Button>Changes to test</Button>
        </Link>
        <Button onClick={exportYaml} disabled={!cases.length}>
          <Download className="size-4" aria-hidden /> Export YAML
        </Button>
        <label className="inline-flex h-9 cursor-pointer items-center gap-2 rounded-lg border border-line bg-white px-4 text-sm font-medium shadow-sm hover:bg-oat">
          <Upload className="size-4" aria-hidden /> Import YAML
          <input
            type="file"
            accept=".yaml,.yml"
            className="sr-only"
            onChange={(e) => e.target.files?.[0] && importYaml(e.target.files[0])}
          />
        </label>
        {message && <span className="text-sm text-pass">{message}</span>}
      </div>
      {actionError && <ErrorBox error={actionError} />}

      <RunsSection
        spaceId={spaceId}
        counts={{ dev: data!.split_summary?.dev ?? 0, test: data!.split_summary?.test ?? 0, all: active.length }}
        slices={data!.split_summary?.mix ?? []}
      />

      <SplitSection spaceId={spaceId} data={data!} onChanged={reload} />

      <Suggestions spaceId={spaceId} cases={cases} onChanged={reload} />

      {coverage.length > 0 && (
        <section>
          <h2 className="text-lg font-semibold">Failure categories covered</h2>
          <p className="mt-1 text-sm text-muted">A failure people found should have a case, so a change cannot bring it back unnoticed.</p>
          <div className="mt-3 flex flex-wrap gap-2">
            {coverage.map((c) => (
              <Badge key={c.category_id} variant={c.active_cases ? "pass" : "warn"}>
                {c.name}
                {c.priority != null && <span className="opacity-70"> · priority {c.priority}</span>} ·{" "}
                {c.active_cases ? `${c.active_cases} case${c.active_cases > 1 ? "s" : ""}` : "no case"}
              </Badge>
            ))}
          </div>
        </section>
      )}

      <section>
        <h2 className="text-lg font-semibold">
          Cases <span className="text-sm font-normal text-muted">{active.length} active</span>
        </h2>
        {cases.length === 0 ? (
          <div className="mt-3">
            <EmptyState icon={<ClipboardList />} title="No cases yet">
              Open a reviewed answer and choose “Add to eval set”, or start a new case.
            </EmptyState>
          </div>
        ) : (
          <Card className="mt-3 overflow-hidden">
            <ul className="divide-y divide-line">
              {cases.map((c) => (
                <li key={c.case_id} className={cn("px-4 py-3", c.status === "retired" && "opacity-60")}>
                  <Link to={`/spaces/${spaceId}/evalset/${c.case_id}`} className="font-medium text-ink hover:underline">
                    {c.title}
                  </Link>
                  <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-muted">
                    {c.status === "active" && <SplitChoice split={splitOf.get(c.case_id)} onChange={(s) => override(c.case_id, s)} />}
                    {c.type && c.type !== "core" && <Badge variant="outline">{c.type}</Badge>}
                    <Badge variant="outline">{BEHAVIOUR[c.expect?.behaviour ?? "answer"]}</Badge>
                    {(c.turns?.length ?? 0) > 1 && <Badge variant="outline">{c.turns.length} turns</Badge>}
                    {Object.keys(c.expect?.criteria ?? {}).length > 0 && <Badge variant="outline">{Object.keys(c.expect!.criteria!).length} rules</Badge>}
                    {(c.expect?.facts?.length ?? 0) > 0 && <Badge variant="outline">{c.expect!.facts!.length} {c.expect!.facts!.length === 1 ? "fact" : "facts"}</Badge>}
                    {c.expect?.reference_sql && <Badge variant="outline">reference query</Badge>}
                    {c.status === "retired" && <Badge variant="neutral">retired</Badge>}
                    <span>
                      v{c.version} · {c.updated_by} · {when(c.updated_at)}
                    </span>
                  </div>
                </li>
              ))}
            </ul>
          </Card>
        )}
      </section>
    </div>
  );
}

/** Dev is iterated on; test is held out for the gate. Owners choose what to stratify by. */
function SplitSection({ spaceId, data, onChanged }: { spaceId: string; data: EvalSetResponse; onChanged: () => void }) {
  const summary = data.split_summary;
  const saved = data.split_settings ?? { stratify_by: ["type", "intent"], test_share: 0.3, min_stratum: 4 };
  const [form, setForm] = useState<SplitSettings>(saved);
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  if (!summary) return null;
  const dims = (form.stratify_by ?? []) as Dimension[];

  async function act(work: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await work();
      onChanged();
    } catch (err) {
      setError(err as Error);
    } finally {
      setBusy(false);
    }
  }

  const byDimension = (Object.keys(DIMENSION) as Dimension[])
    .map((d) => ({ d, rows: summary.mix.filter((m) => m.dimension === d) }))
    .filter((x) => x.rows.length > 0);

  return (
    <section className="space-y-3">
      <div>
        <h2 className="text-lg font-semibold">Dev and test</h2>
        <p className="mt-1 max-w-3xl text-sm text-muted">
          Iterate a change on <b>dev</b> as often as you like. <b>Test</b> is held out: a gate run asks it once to decide. Both
          get the same mix of{" "}
          {(saved.stratify_by ?? []).length
            ? (saved.stratify_by as Dimension[]).map((d) => DIMENSION[d].toLowerCase()).join(" and ")
            : "cases (plain random split)"}
          , so dev results predict test.
        </p>
      </div>
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <Badge variant="neutral">{summary.dev} dev</Badge>
        <Badge variant={summary.test ? "pass" : "warn"}>{summary.test} test</Badge>
        {summary.unassigned > 0 && <Badge variant="outline">{summary.unassigned} not assigned</Badge>}
        <span className="text-muted">
          {Math.round((saved.test_share ?? 0.3) * 100)}% test · strata under {saved.min_stratum} cases are merged
        </span>
        <Button size="sm" disabled={busy} onClick={() => act(() => api.assignSplits(spaceId))}>
          Assign splits
        </Button>
        <Button size="sm" variant="ghost" onClick={() => setEditing(!editing)}>
          {editing ? "Close settings" : "Settings"}
        </Button>
      </div>
      {summary.warnings.length > 0 && (
        <ul className="space-y-0.5 text-sm text-warn">
          {summary.warnings.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      )}
      {error && <ErrorBox error={error} />}
      {editing && (
        <Card>
          <CardContent className="space-y-4 pt-5 text-sm">
            <Field label="Stratify by" hint="Each combination gets the same test share. None = a plain random split. Cases with the same intent always stay on one side, so a near-identical question cannot leak from dev into test.">
              <div className="flex flex-wrap gap-3">
                {(Object.keys(DIMENSION) as Dimension[]).filter((d) => d !== "intent").map((d) => (
                  <label key={d} className="flex items-center gap-1.5">
                    <input
                      type="checkbox"
                      checked={dims.includes(d)}
                      onChange={(e) => setForm({ ...form, stratify_by: e.target.checked ? [...dims, d] : dims.filter((x) => x !== d) })}
                    />
                    {DIMENSION[d]}
                  </label>
                ))}
              </div>
            </Field>
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Test share (%)" htmlFor="split-share">
                <input
                  id="split-share"
                  type="number"
                  min={5}
                  max={80}
                  className={INPUT}
                  value={Math.round((form.test_share ?? 0.3) * 100)}
                  onChange={(e) => setForm({ ...form, test_share: Number(e.target.value) / 100 })}
                />
              </Field>
              <Field label="Smallest stratum" hint="Smaller ones fall back to a coarser stratum." htmlFor="split-min">
                <input
                  id="split-min"
                  type="number"
                  min={2}
                  max={50}
                  className={INPUT}
                  value={form.min_stratum ?? 4}
                  onChange={(e) => setForm({ ...form, min_stratum: Number(e.target.value) })}
                />
              </Field>
            </div>
            <p className="text-xs text-muted">
              Settings apply to cases assigned from now on. Re-splitting moves cases that were already dev or test, so do it before
              the first gate run; cases you placed yourself stay.
            </p>
            <div className="flex flex-wrap gap-2">
              <Button variant="primary" size="sm" disabled={busy} onClick={() => act(() => api.putSplitSettings(spaceId, form))}>
                Save settings
              </Button>
              <Button
                size="sm"
                disabled={busy}
                onClick={() =>
                  window.confirm("Re-split every automatically assigned case?") &&
                  act(async () => {
                    await api.putSplitSettings(spaceId, form);
                    await api.assignSplits(spaceId, true);
                  })
                }
              >
                Save and re-split
              </Button>
            </div>
          </CardContent>
        </Card>
      )}
      {byDimension.length > 0 && (
        <details className="text-sm">
          <summary className="cursor-pointer text-muted hover:text-ink">Mix of the active cases</summary>
          <div className="mt-2 grid gap-3 sm:grid-cols-2">
            {byDimension.map(({ d, rows }) => (
              <Card key={d}>
                <CardContent className="pt-4">
                  <h3 className="text-xs font-semibold uppercase tracking-wide text-muted">{DIMENSION[d]}</h3>
                  <table className="mt-1 w-full text-sm">
                    <thead className="text-xs text-muted">
                      <tr>
                        <th className="py-1 text-left font-normal" />
                        <th className="py-1 text-right font-normal">dev</th>
                        <th className="py-1 text-right font-normal">test</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((m) => (
                        <tr key={m.value} className="border-t border-line">
                          <td className="py-1">{m.value}</td>
                          <td className="py-1 text-right tabular-nums">{m.dev + m.unassigned}</td>
                          <td className={cn("py-1 text-right tabular-nums", !m.test && "text-muted")}>{m.test}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </CardContent>
              </Card>
            ))}
          </div>
        </details>
      )}
    </section>
  );
}

function SplitChoice({ split, onChange }: { split?: CaseSplit; onChange: (s: "dev" | "test" | null) => void }) {
  const mine = split && split.assigned_by !== "auto";
  return (
    <select
      aria-label="Split"
      title={split ? `${split.stratum} · ${mine ? `set by ${split.assigned_by}` : "assigned automatically"}` : "Assigned at the next run"}
      className={cn(
        "rounded-full border px-2 py-0.5 text-xs font-medium",
        split?.split === "test" ? "border-pass/40 bg-pass/10 text-pass" : "border-line bg-white text-muted",
      )}
      value={mine ? split!.split : ""}
      onChange={(e) => onChange((e.target.value || null) as "dev" | "test" | null)}
    >
      <option value="">{split ? `${split.split} (auto)` : "not assigned"}</option>
      <option value="dev">dev (mine)</option>
      <option value="test">test (mine)</option>
    </select>
  );
}

type Expectation = NonNullable<EvalCaseBody["expect"]>;
const NO_EXPECTATION: Expectation = { behaviour: "answer" };
/** Drift and hygiene: what users ask that no case covers, and cases to reconsider. */
function Suggestions({ spaceId, cases, onChanged }: { spaceId: string; cases: EvalCase[]; onChanged: () => void }) {
  const { data, error, reload } = useLoad(() => api.evalsetSuggestions(spaceId), [spaceId]);
  const [actionError, setActionError] = useState<Error | null>(null);
  if (error) return <ErrorBox error={error} />;
  if (!data) return null;
  const s = data;
  const leaked = s.leaks ?? [];
  const nothing = !s.add.length && !s.retire.length && !s.flaky.length && !s.duplicates.length && !leaked.length;

  async function retire(caseId: string) {
    const c = cases.find((x) => x.case_id === caseId);
    if (!c) return;
    setActionError(null);
    try {
      const { case_id: _id, space_id: _space, version, updated_by: _by, updated_at: _at, ...body } = c;
      await api.updateCase(spaceId, caseId, { ...body, status: "retired", base_version: version });
      reload();
      onChanged();
    } catch (err) {
      setActionError(err as Error);
    }
  }

  return (
    <section className="space-y-3">
      <div>
        <h2 className="text-lg font-semibold">Suggestions</h2>
        <p className="text-sm text-muted">
          From the last {s.window_days} days of questions, each tagged with an intent by the review job ({s.tagged_questions}{" "}
          tagged{s.untagged_questions ? `, ${s.untagged_questions} waiting for the next review run` : ""}). Nothing changes until you
          act.
        </p>
      </div>
      {actionError && <ErrorBox error={actionError} />}
      {nothing && <p className="text-sm text-muted">Nothing to suggest: every intent users ask has a case.</p>}
      {s.add.length > 0 && (
        <Card>
          <CardContent className="space-y-2 pt-5">
            <h3 className="text-sm font-semibold">Users ask this, and no case covers it</h3>
            <ul className="divide-y divide-line">
              {s.add.map((a) => (
                <li key={a.intent} className="py-2 text-sm">
                  <span className="font-medium">{a.intent}</span> <span className="text-muted">· asked {a.count} times</span>
                  <ul className="mt-1 space-y-0.5">
                    {a.examples.map((e) => (
                      <li key={e.trace_id} className="flex flex-wrap items-center gap-2 text-muted">
                        “{e.question}”
                        <Link to={`/spaces/${spaceId}/evalset/new?trace=${e.trace_id}`} className="text-xs font-medium text-link hover:underline">
                          Add to eval set
                        </Link>
                      </li>
                    ))}
                  </ul>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      )}
      {leaked.length > 0 && (
        <Card>
          <CardContent className="space-y-2 pt-5 text-sm">
            <h3 className="font-semibold">Already a SQL example of the agent (train/test leakage)</h3>
            <p className="text-xs text-muted">
              Genie is shown these questions with their answers, so passing them says nothing about similar questions. Reword the
              case, or replace the example with a different question of the same kind.
            </p>
            <ul className="space-y-1">
              {leaked.map((l) => (
                <li key={`${l.case_id}-${l.turn}`}>
                  <Link to={`/spaces/${spaceId}/evalset/${l.case_id}`} className="hover:underline">
                    {l.title}
                  </Link>{" "}
                  {l.split === "dev" ? <Badge variant="outline">dev: allowed</Badge> : <Badge variant="warn">{l.split ?? "no split"}</Badge>}{" "}
                  <span className="text-muted">
                    turn {l.turn + 1} “{l.case_question}” ≈ example “{l.example_question}”
                  </span>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      )}
      {(s.retire.length > 0 || s.flaky.length > 0 || s.duplicates.length > 0) && (
        <Card>
          <CardContent className="space-y-3 pt-5 text-sm">
            {s.retire.length > 0 && (
              <div>
                <h3 className="font-semibold">Nobody asked anything like these lately</h3>
                <ul className="mt-1 space-y-1">
                  {s.retire.map((r) => (
                    <li key={r.case_id} className="flex flex-wrap items-center gap-2">
                      {r.title} <span className="text-muted">({r.intent})</span>
                      <Button size="sm" variant="ghost" onClick={() => retire(r.case_id)}>
                        Retire
                      </Button>
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {s.flaky.length > 0 && (
              <div>
                <h3 className="font-semibold">Flaky on the current agent</h3>
                <p className="text-xs text-muted">Split verdicts in several runs: they measure Genie’s noise. Tighten the expectations or raise k.</p>
                <ul className="mt-1 space-y-1">
                  {s.flaky.map((f) => (
                    <li key={f.case_id}>
                      <Link to={`/spaces/${spaceId}/evalset/${f.case_id}`} className="hover:underline">
                        {f.title}
                      </Link>{" "}
                      <span className="text-muted">
                        flaky in {f.flaky_runs} of its last {f.runs} runs
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {s.duplicates.length > 0 && (
              <div>
                <h3 className="font-semibold">Possible duplicates</h3>
                <ul className="mt-1 space-y-1">
                  {s.duplicates.map((d) => (
                    <li key={d.intent}>
                      <span className="text-muted">{d.intent}:</span> {d.titles.join(" · ")}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </CardContent>
        </Card>
      )}
    </section>
  );
}

const EMPTY: EvalCaseBody = { title: "", turns: [""], expect: NO_EXPECTATION, category_ids: [], type: "core", origin: "written", status: "active" };

/** New case (blank, or drafted from `?trace=`) and editing an existing one. */
export function EvalCaseEditor() {
  const { spaceId = "", caseId } = useParams();
  const [params] = useSearchParams();
  const traceId = params.get("trace");
  const navigate = useNavigate();
  const criteria = useCriteria();
  const categories = useLoad(() => api.observations(spaceId), [spaceId]);
  const [base, setBase] = useState<EvalCase | null>(null);
  const [form, setForm] = useState<EvalCaseBody | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setError(null);
    const load = caseId
      ? api.evalset(spaceId).then((s) => {
          const found = s.cases.find((c) => c.case_id === caseId);
          if (!found) throw new Error("No such case in this space.");
          setBase(found);
          return found as EvalCaseBody;
        })
      : traceId
        ? api.draftCase(spaceId, traceId)
        : Promise.resolve(EMPTY);
    load.then(setForm).catch(setError);
  }, [spaceId, caseId, traceId]);

  if (error && !form) return <ErrorBox error={error} />;
  if (!form) return <Loading />;
  const expect: Expectation = form.expect ?? NO_EXPECTATION;
  const set = (patch: Partial<EvalCaseBody>) => setForm({ ...form, ...patch });
  const setExpect = (patch: Partial<Expectation>) => set({ expect: { ...expect, ...patch } });
  const rubricish = Object.values(criteria).sort((a, b) => a.id.localeCompare(b.id));

  async function save() {
    setSaving(true);
    setError(null);
    const body: EvalCaseBody = {
      ...form!,
      turns: form!.turns.map((t: string) => t.trim()).filter(Boolean),
      expect: { ...expect, facts: (expect.facts ?? []).map((f: string) => f.trim()).filter(Boolean) },
    };
    try {
      if (base) await api.updateCase(spaceId, base.case_id, { ...body, base_version: base.version });
      else await api.createCase(spaceId, body);
      navigate(`/spaces/${spaceId}/evalset`);
    } catch (err) {
      setError(err as Error);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="max-w-3xl space-y-6">
      <div>
        <Link to={`/spaces/${spaceId}/evalset`} className="inline-flex items-center gap-1 text-sm text-muted hover:text-ink">
          <ArrowLeft className="size-4" aria-hidden /> Eval set
        </Link>
        <h1 className="mt-2 text-2xl font-semibold tracking-tight">
          {base ? `Edit case (version ${base.version})` : traceId ? "Add an answer to the eval set" : "New case"}
        </h1>
        {form.source && (
          <p className="mt-1 text-sm text-muted">
            From{" "}
            <Link to={`/traces/${form.source.trace_id}`} className="text-link hover:underline">
              this answer
            </Link>
            {form.source.reviewer_verdict && <>, which a reviewer {form.source.reviewer_verdict === "fail" ? "failed" : "passed"}</>}.
            Rewrite the drafted facts as what a correct answer must do.
          </p>
        )}
      </div>

      <Card>
        <CardContent className="space-y-5 pt-5">
          <Field label="Title" htmlFor="case-title">
            <input id="case-title" className={INPUT} value={form.title} maxLength={200} onChange={(e) => set({ title: e.target.value })} />
          </Field>
          <Field label="Questions, one per line" hint="Asked in order in one conversation; the last one is graded." htmlFor="case-turns">
            <textarea
              id="case-turns"
              rows={Math.max(2, form.turns.length + 1)}
              className={INPUT}
              value={form.turns.join("\n")}
              onChange={(e) => set({ turns: e.target.value.split("\n") })}
            />
          </Field>
          <Field label="Type" hint="Used to keep dev and test the same mix." htmlFor="case-type">
            <select
              id="case-type"
              className={INPUT}
              value={form.type ?? "core"}
              onChange={(e) => set({ type: e.target.value as keyof typeof CASE_TYPE })}
            >
              {Object.entries(CASE_TYPE).map(([k, v]) => (
                <option key={k} value={k}>
                  {v}
                </option>
              ))}
            </select>
          </Field>
          <Field label="A good answer" htmlFor="case-behaviour">
            <select
              id="case-behaviour"
              className={INPUT}
              value={expect.behaviour ?? "answer"}
              onChange={(e) => setExpect({ behaviour: e.target.value as keyof typeof BEHAVIOUR })}
            >
              {Object.entries(BEHAVIOUR).map(([k, v]) => (
                <option key={k} value={k}>
                  {v}
                </option>
              ))}
            </select>
          </Field>
          <Field label="What it must state or do, one per line" hint="Judged against the answer by an LLM." htmlFor="case-facts">
            <textarea
              id="case-facts"
              rows={3}
              className={INPUT}
              value={(expect.facts ?? []).join("\n")}
              onChange={(e) => setExpect({ facts: e.target.value.split("\n") })}
            />
          </Field>
          <Field label="Rules for this question" hint="Every must-pass rule applies anyway. Mark rules that must apply and pass here, or must not apply.">
            <RuleChoices
              criteria={rubricish}
              value={expect.criteria ?? {}}
              onChange={(criteria) => setExpect({ criteria })}
            />
          </Field>
          <Field
            label="Reference query (optional)"
            hint="One SELECT, run as the tester when the case is asked; its values must appear in the answer."
            htmlFor="case-sql"
          >
            <textarea
              id="case-sql"
              rows={4}
              className={cn(INPUT, "font-mono text-xs")}
              value={expect.reference_sql ?? ""}
              onChange={(e) => setExpect({ reference_sql: e.target.value || null })}
            />
          </Field>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Data" htmlFor="case-data">
              <select
                id="case-data"
                className={INPUT}
                value={expect.tools?.data ?? "any"}
                onChange={(e) => setExpect({ tools: { ...expect.tools, data: e.target.value as "any" | "required" | "none" } })}
              >
                <option value="any">Either</option>
                <option value="required">Must query data</option>
                <option value="none">Must not query data</option>
              </select>
            </Field>
            <Field label="Columns or tables it must not touch" hint="Comma separated." htmlFor="case-mnq">
              <input
                id="case-mnq"
                className={INPUT}
                value={(expect.tools?.must_not_query ?? []).join(", ")}
                onChange={(e) =>
                  setExpect({
                    tools: { data: expect.tools?.data ?? "any", must_not_query: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) },
                  })
                }
              />
            </Field>
          </div>
          {(categories.data?.categories?.length ?? 0) > 0 && (
            <Field label="Failure categories it covers">
              <div className="flex flex-wrap gap-2">
                {categories.data!.categories!.map((c) => {
                  const on = form.category_ids?.includes(c.id);
                  return (
                    <button
                      key={c.id}
                      type="button"
                      aria-pressed={on}
                      onClick={() =>
                        set({ category_ids: on ? form.category_ids!.filter((x: string) => x !== c.id) : [...(form.category_ids ?? []), c.id] })
                      }
                      className={cn(
                        "rounded-full border px-3 py-1 text-xs font-medium",
                        on ? "border-navy bg-navy text-white" : "border-line bg-white text-muted hover:text-ink",
                      )}
                    >
                      {c.name}
                    </button>
                  );
                })}
              </div>
            </Field>
          )}
          {base && (
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={form.status === "retired"}
                onChange={(e) => set({ status: e.target.checked ? "retired" : "active" })}
              />
              Retired (not asked any more; its history is kept)
            </label>
          )}
        </CardContent>
      </Card>
      {error && <ErrorBox error={error} />}
      <div className="flex gap-2">
        <Button variant="primary" onClick={save} disabled={saving || !form.title.trim() || !form.turns.some((t: string) => t.trim())}>
          {saving ? "Saving…" : base ? "Save as a new version" : "Add to eval set"}
        </Button>
        <Link to={`/spaces/${spaceId}/evalset`}>
          <Button>Cancel</Button>
        </Link>
      </div>
    </div>
  );
}

const INPUT =
  "w-full rounded-lg border border-line bg-white px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-link/40";

function Field({ label, hint, htmlFor, children }: { label: string; hint?: string; htmlFor?: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1.5">
      <label htmlFor={htmlFor} className="block text-sm font-medium">
        {label}
      </label>
      {children}
      {hint && <p className="text-xs text-muted">{hint}</p>}
    </div>
  );
}

function RuleChoices({
  criteria,
  value,
  onChange,
}: {
  criteria: Criterion[];
  value: Record<string, "pass" | "na">;
  onChange: (v: Record<string, "pass" | "na">) => void;
}) {
  return (
    <ul className="divide-y divide-line rounded-lg border border-line">
      {criteria.map((c) => (
        <li key={c.id} className="flex flex-wrap items-center justify-between gap-2 px-3 py-2 text-sm">
          <span title={c.description}>{c.title ?? c.id}</span>
          <select
            aria-label={`${c.title ?? c.id}: expectation`}
            className="rounded-md border border-line bg-white px-2 py-1 text-xs"
            value={value[c.id] ?? ""}
            onChange={(e) => {
              const next = { ...value };
              if (e.target.value) next[c.id] = e.target.value as "pass" | "na";
              else delete next[c.id];
              onChange(next);
            }}
          >
            <option value="">No expectation</option>
            <option value="pass">Must apply and pass</option>
            <option value="na">Must not apply</option>
          </select>
        </li>
      ))}
    </ul>
  );
}
