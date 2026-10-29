// Settings: the space's profile. Which rules apply and how strictly, the
// space's own LLM-judged rules, each skill's model and settings, thresholds and the release gate.
// Every save is a version: history, compare and restore. Owners only.
import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, Download, History, Plus, Trash2, Upload } from "lucide-react";
import { api, type LibraryView, type ProfileDiff, type SpaceProfile } from "@/api";
import { useLoad } from "@/hooks";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { ErrorBox, Loading, when } from "@/components/common";
import { cn } from "@/lib/utils";

type Body = Omit<SpaceProfile, "space_id" | "version" | "note" | "updated_by" | "updated_at">;
type Skill = NonNullable<Body["skills"]>[string];
type Custom = NonNullable<Body["custom_rules"]>[number];

const INPUT = "w-full rounded-lg border border-line bg-white px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-link/40";
const GATES = ["hard", "soft", "monitor"] as const;
const THRESHOLD_LABEL: Record<string, string> = {
  eval_k: "Times each case is asked (k)",
  calibration_min_lower_bound: "Calibration lower bound",
  review_budget: "Answers queued for review per run",
  expectation_review_budget: "Regression answers queued for review per run",
  drift_window_days: "Days of traffic for suggestions",
  drift_min_questions: "Questions before suggesting a case",
  judge_sample_rate: "Share of new answers the LLM judges read (0–1)",
  max_judged_per_run: "Most LLM judgements per check run",
  recheck_days: "Days re-checked after a rule change",
};
const SKILL_USE: Record<string, string> = {
  rubric_judge: "Grades answers against your LLM-judged rules",
  expectation_judge: "Grades regression answers against their case's facts",
  categorize_observation: "Suggests failure categories for reviewed answers",
  intent_tagger: "Labels questions and cases with an intent",
  space_change_proposer: "Drafts a change to the space from a failure category",
};

function bodyOf(p: SpaceProfile): Body {
  const { space_id: _s, version: _v, note: _n, updated_by: _b, updated_at: _a, ...body } = p;
  return structuredClone(body);
}

function num(value: string): number | null {
  return value.trim() === "" ? null : Number(value);
}

function pctIn(value: number | null | undefined): string {
  return value == null ? "" : String(Math.round(value * 100));
}

export function Settings() {
  const { spaceId = "" } = useParams();
  const profile = useLoad(() => api.profile(spaceId), [spaceId]);
  const library = useLoad(() => api.profileLibrary(spaceId), [spaceId]);
  const [draft, setDraft] = useState<Body | null>(null);
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [showHistory, setShowHistory] = useState(false);

  useEffect(() => {
    if (profile.data) setDraft(bodyOf(profile.data));
  }, [profile.data]);

  if (profile.loading || library.loading || !draft) return <Loading />;
  if (profile.error) return <ErrorBox error={profile.error} />;
  if (library.error) return <ErrorBox error={library.error} />;
  const p = profile.data!;
  const lib = library.data!;
  const set = (patch: Partial<Body>) => setDraft({ ...draft, ...patch });

  async function save() {
    setSaving(true);
    setError(null);
    setMessage(null);
    try {
      const saved = await api.saveProfile(spaceId, { ...draft!, base_version: p.version, note });
      setMessage(`Saved as version ${saved.version}.`);
      setNote("");
      profile.reload();
    } catch (err) {
      setError(err as Error);
    } finally {
      setSaving(false);
    }
  }

  async function exportYaml() {
    const text = await api.exportProfile(spaceId);
    const url = URL.createObjectURL(new Blob([text], { type: "application/yaml" }));
    Object.assign(document.createElement("a"), { href: url, download: `profile-${spaceId.slice(0, 8)}-v${p.version}.yaml` }).click();
    URL.revokeObjectURL(url);
  }

  async function importYaml(file: File) {
    setError(null);
    try {
      const saved = await api.importProfile(spaceId, await file.text());
      setMessage(`Imported as version ${saved.version}.`);
      profile.reload();
    } catch (err) {
      setError(err as Error);
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <Link to={`/spaces/${spaceId}`} className="inline-flex items-center gap-1 text-sm text-muted hover:text-ink">
          <ArrowLeft className="size-4" aria-hidden /> Back to the agent
        </Link>
        <h1 className="mt-2 text-2xl font-semibold tracking-tight">Settings</h1>
        <p className="mt-1 max-w-3xl text-sm text-muted">
          What applies to this agent: which rules and how strictly, its own LLM-judged rules, the model behind each LLM step,
          thresholds and the release gate. Empty fields use the deployment's defaults. Every save is a new version you can
          compare and restore, and every run records the version it used.
        </p>
        <div className="mt-3 flex flex-wrap items-center gap-2 text-sm">
          <Badge variant="outline">{p.version ? `version ${p.version}` : "defaults (no version yet)"}</Badge>
          {p.version > 0 && (
            <span className="text-muted">
              {p.updated_by} · {when(p.updated_at)}
              {p.note && ` · ${p.note}`}
            </span>
          )}
          <Button size="sm" variant="ghost" onClick={() => setShowHistory(!showHistory)}>
            <History className="size-4" aria-hidden /> History
          </Button>
          <Button size="sm" variant="ghost" onClick={exportYaml}>
            <Download className="size-4" aria-hidden /> Export YAML
          </Button>
          <label className="inline-flex h-8 cursor-pointer items-center gap-1.5 rounded-lg px-3 text-sm font-medium hover:bg-oat">
            <Upload className="size-4" aria-hidden /> Import YAML
            <input type="file" accept=".yaml,.yml" className="sr-only" onChange={(e) => e.target.files?.[0] && importYaml(e.target.files[0])} />
          </label>
        </div>
      </div>

      {showHistory && <HistoryPanel spaceId={spaceId} current={p.version} onRestored={profile.reload} />}

      <RulesSection lib={lib} draft={draft} set={set} />
      <CustomRules draft={draft} set={set} />
      <SkillsSection lib={lib} draft={draft} set={set} />
      <ThresholdsSection lib={lib} draft={draft} set={set} />
      <GateSection draft={draft} set={set} />

      <div className="sticky bottom-0 -mx-1 flex flex-wrap items-center gap-2 border-t border-line bg-oat/95 px-1 py-3 backdrop-blur">
        <input
          aria-label="What changed and why"
          className={cn(INPUT, "max-w-md")}
          placeholder="What changed and why (shown in the history)"
          value={note}
          maxLength={500}
          onChange={(e) => setNote(e.target.value)}
        />
        <Button variant="primary" onClick={save} disabled={saving}>
          {saving ? "Saving…" : `Save as version ${p.version + 1}`}
        </Button>
        <Button onClick={() => setDraft(bodyOf(p))} disabled={saving}>
          Discard edits
        </Button>
        {message && <span className="text-sm text-pass">{message}</span>}
      </div>
      {error && <ErrorBox error={error} />}
    </div>
  );
}

function Section({ title, hint, children }: { title: string; hint: string; children: React.ReactNode }) {
  return (
    <section className="space-y-2">
      <h2 className="text-lg font-semibold">{title}</h2>
      <p className="max-w-3xl text-sm text-muted">{hint}</p>
      {children}
    </section>
  );
}

function RulesSection({ lib, draft, set }: { lib: LibraryView; draft: Body; set: (p: Partial<Body>) => void }) {
  const rules = draft.rules ?? {};
  function update(id: string, patch: Record<string, unknown>) {
    const next = { ...rules, [id]: { ...rules[id], ...patch } };
    const r = next[id];
    if ((r.enabled ?? true) && !r.gate && !r.params) delete next[id];
    set({ rules: next });
  }
  return (
    <Section title="Rules" hint="Turn rules off, or change how strictly a failure counts. Code rules can also take this agent's own parameters (e.g. its restricted columns).">
      <Card className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-left text-xs uppercase tracking-wide text-muted">
            <tr>
              <th className="px-3 py-2 font-medium">On</th>
              <th className="px-3 py-2 font-medium">Rule</th>
              <th className="px-3 py-2 font-medium">Gate</th>
              <th className="px-3 py-2 font-medium">Parameters</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {lib.rules.map((r) => {
              const s = rules[r.id] ?? {};
              const code = r.family !== "rubric";
              return (
                <tr key={r.id} className={cn(s.enabled === false && "opacity-60")}>
                  <td className="px-3 py-2 align-top">
                    <input type="checkbox" aria-label={`Use ${r.title ?? r.id}`} checked={s.enabled !== false} onChange={(e) => update(r.id, { enabled: e.target.checked })} />
                  </td>
                  <td className="px-3 py-2 align-top">
                    <div className="font-medium">{r.title ?? r.id}</div>
                    <div className="font-mono text-xs text-muted">{r.id}</div>
                  </td>
                  <td className="px-3 py-2 align-top">
                    <select
                      aria-label={`Gate of ${r.title ?? r.id}`}
                      className="rounded-md border border-line bg-white px-2 py-1 text-xs"
                      value={s.gate ?? ""}
                      onChange={(e) => update(r.id, { gate: e.target.value || null })}
                    >
                      <option value="">{r.gate} (default)</option>
                      {GATES.filter((g) => g !== r.gate).map((g) => (
                        <option key={g} value={g}>
                          {g}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td className="px-3 py-2 align-top">
                    {code && Object.keys(r.params).length > 0 ? (
                      <ParamsEditor defaults={r.params} value={s.params ?? null} onChange={(params) => update(r.id, { params })} />
                    ) : (
                      <span className="text-xs text-muted">{code ? "none" : "wording: add a custom rule"}</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </Card>
    </Section>
  );
}

function ParamsEditor({ defaults, value, onChange }: { defaults: Record<string, unknown>; value: Record<string, unknown> | null; onChange: (v: Record<string, unknown> | null) => void }) {
  const [text, setText] = useState(value ? JSON.stringify(value, null, 1) : "");
  const [bad, setBad] = useState(false);
  return (
    <details>
      <summary className="cursor-pointer text-xs text-link">{value ? "overridden" : "defaults"}</summary>
      <pre className="mt-1 max-h-32 overflow-auto rounded bg-oat p-2 text-xs">{JSON.stringify(defaults, null, 1)}</pre>
      <textarea
        aria-label="Parameters to override, as JSON"
        className={cn(INPUT, "mt-1 font-mono text-xs", bad && "border-fail")}
        rows={3}
        placeholder='{"restricted_columns": ["ssn"]}'
        value={text}
        onChange={(e) => {
          setText(e.target.value);
          if (!e.target.value.trim()) {
            setBad(false);
            onChange(null);
            return;
          }
          try {
            onChange(JSON.parse(e.target.value));
            setBad(false);
          } catch {
            setBad(true);
          }
        }}
      />
    </details>
  );
}

function CustomRules({ draft, set }: { draft: Body; set: (p: Partial<Body>) => void }) {
  const rules = draft.custom_rules ?? [];
  function update(i: number, patch: Partial<Custom>, params?: Record<string, unknown>) {
    const next = rules.map((r, j) => (j === i ? { ...r, ...patch, params: params ? { ...r.params, ...params } : r.params } : r));
    set({ custom_rules: next });
  }
  function add() {
    const blank: Custom = {
      id: `rubric.custom_${rules.length + 1}`,
      title: "",
      version: 1,
      family: "rubric",
      layer: "behaviour",
      gate: "monitor",
      applies_to: { level: "report" },
      description: "",
      params: { skill: "rubric_judge", rule: "", failure: "" },
    } as Custom;
    set({ custom_rules: [...rules, blank] });
  }
  return (
    <Section
      title="This agent's own LLM-judged rules"
      hint="One rule per instruction of the space: when it applies, and what counts as breaking it. Change the wording? Raise the version: reviewers label it afresh and the judge is calibrated again."
    >
      <div className="space-y-3">
        {rules.map((r, i) => (
          <Card key={i}>
            <CardContent className="grid gap-2 pt-4 text-sm md:grid-cols-4">
              <input aria-label="Rule id" className={INPUT} value={r.id} onChange={(e) => update(i, { id: e.target.value })} />
              <input aria-label="Title" className={cn(INPUT, "md:col-span-2")} placeholder="Title" value={r.title ?? ""} onChange={(e) => update(i, { title: e.target.value })} />
              <div className="flex items-center gap-2">
                <label className="text-xs text-muted">
                  v
                  <input aria-label="Version" type="number" min={1} className={cn(INPUT, "ml-1 inline w-16")} value={r.version} onChange={(e) => update(i, { version: Number(e.target.value) })} />
                </label>
                <select aria-label="Gate" className="rounded-md border border-line bg-white px-2 py-1 text-xs" value={r.gate} onChange={(e) => update(i, { gate: e.target.value as Custom["gate"] })}>
                  {GATES.map((g) => (
                    <option key={g}>{g}</option>
                  ))}
                </select>
                <Button size="sm" variant="ghost" aria-label="Remove rule" onClick={() => set({ custom_rules: rules.filter((_, j) => j !== i) })}>
                  <Trash2 className="size-4" aria-hidden />
                </Button>
              </div>
              <textarea aria-label="When it applies and what it requires" className={cn(INPUT, "md:col-span-2")} rows={3} placeholder="Rule: when it applies and what the answer must do" value={String(r.params?.rule ?? "")} onChange={(e) => update(i, { description: e.target.value }, { rule: e.target.value })} />
              <textarea aria-label="What counts as a failure" className={cn(INPUT, "md:col-span-2")} rows={3} placeholder="What counts as a failure" value={String(r.params?.failure ?? "")} onChange={(e) => update(i, {}, { failure: e.target.value })} />
            </CardContent>
          </Card>
        ))}
        <Button size="sm" onClick={add}>
          <Plus className="size-4" aria-hidden /> Add a rule
        </Button>
      </div>
    </Section>
  );
}

function SkillsSection({ lib, draft, set }: { lib: LibraryView; draft: Body; set: (p: Partial<Body>) => void }) {
  const skills = draft.skills ?? {};
  function update(name: string, patch: Partial<Skill>) {
    const next = { ...skills, [name]: { ...skills[name], ...patch } };
    if (Object.values(next[name]).every((v) => v == null || v === "")) delete next[name];
    set({ skills: next });
  }
  return (
    <Section
      title="Models and prompts"
      hint={`Each LLM step can use its own model. Judges default to an off-the-shelf model at temperature 0 (${lib.default_model}). Changing a prompt or sampling setting needs a new version: judges are calibrated per version.`}
    >
      <div className="grid gap-3 lg:grid-cols-2">
        {lib.skills.map((s) => {
          const mine = skills[s.name] ?? {};
          return (
            <Card key={s.name}>
              <CardContent className="space-y-2 pt-4 text-sm">
                <div className="flex flex-wrap items-baseline gap-2">
                  <span className="font-medium">{s.name}</span>
                  <span className="font-mono text-xs text-muted">v{s.version}</span>
                  {skills[s.name] && <Badge variant="warn">overridden</Badge>}
                </div>
                <p className="text-xs text-muted">{SKILL_USE[s.name] ?? s.purpose}</p>
                <div className="grid grid-cols-2 gap-2">
                  <label className="col-span-2 text-xs text-muted">
                    Model endpoint
                    <input className={INPUT} placeholder={lib.default_model} value={mine.model ?? ""} onChange={(e) => update(s.name, { model: e.target.value || null })} />
                  </label>
                  <label className="text-xs text-muted">
                    Temperature
                    <input type="number" step="0.1" min={0} max={2} className={INPUT} placeholder={String(s.model_params.temperature ?? 0)} value={mine.temperature ?? ""} onChange={(e) => update(s.name, { temperature: num(e.target.value) })} />
                  </label>
                  <label className="text-xs text-muted">
                    Samples (majority vote)
                    <input type="number" min={1} max={7} className={INPUT} placeholder={String(s.model_params.samples ?? 1)} value={mine.samples ?? ""} onChange={(e) => update(s.name, { samples: num(e.target.value) })} />
                  </label>
                  <label className="text-xs text-muted">
                    Max tokens
                    <input type="number" min={100} className={INPUT} placeholder={String(s.model_params.max_tokens ?? "")} value={mine.max_tokens ?? ""} onChange={(e) => update(s.name, { max_tokens: num(e.target.value) })} />
                  </label>
                  <label className="text-xs text-muted">
                    Reasoning effort
                    <select className={INPUT} value={mine.reasoning_effort ?? ""} onChange={(e) => update(s.name, { reasoning_effort: (e.target.value || null) as Skill["reasoning_effort"] })}>
                      <option value="">default</option>
                      <option value="low">low</option>
                      <option value="medium">medium</option>
                      <option value="high">high</option>
                    </select>
                  </label>
                  <label className="text-xs text-muted">
                    Override version
                    <input type="number" min={1} className={INPUT} placeholder="needed for prompt or sampling" value={mine.version ?? ""} onChange={(e) => update(s.name, { version: num(e.target.value) })} />
                  </label>
                </div>
                <details>
                  <summary className="cursor-pointer text-xs text-link">{mine.prompt ? "Prompt (overridden)" : "Prompt (library)"}</summary>
                  <textarea
                    aria-label={`Prompt of ${s.name}`}
                    className={cn(INPUT, "mt-1 font-mono text-xs")}
                    rows={10}
                    placeholder="Empty = the library prompt below. Keep its {placeholders}."
                    value={mine.prompt ?? ""}
                    onChange={(e) => update(s.name, { prompt: e.target.value || null })}
                  />
                  <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap rounded bg-oat p-2 text-xs">{s.template}</pre>
                </details>
              </CardContent>
            </Card>
          );
        })}
      </div>
    </Section>
  );
}

function ThresholdsSection({ lib, draft, set }: { lib: LibraryView; draft: Body; set: (p: Partial<Body>) => void }) {
  const t = (draft.thresholds ?? {}) as Record<string, number | null | undefined>;
  return (
    <Section title="Thresholds" hint="Empty = the deployment's default, shown greyed.">
      <Card>
        <CardContent className="grid gap-3 pt-4 sm:grid-cols-2 lg:grid-cols-3">
          {Object.entries(THRESHOLD_LABEL).map(([key, label]) => (
            <label key={key} className="text-xs text-muted">
              {label}
              <input
                type="number"
                step="any"
                className={INPUT}
                placeholder={String(lib.thresholds[key] ?? "")}
                value={t[key] ?? ""}
                onChange={(e) => set({ thresholds: { ...draft.thresholds, [key]: num(e.target.value) } })}
              />
            </label>
          ))}
        </CardContent>
      </Card>
    </Section>
  );
}

function GateSection({ draft, set }: { draft: Body; set: (p: Partial<Body>) => void }) {
  type Gate = NonNullable<Body["gate"]>;
  const g: Gate = draft.gate ?? { guardrail_zero_tolerance: true };
  const setGate = (patch: Partial<Gate>) => set({ gate: { ...g, ...patch } });
  const floors = (g.type_floors ?? {}) as Record<string, number>;
  const pctField = (label: string, value: number | null | undefined, onChange: (v: number | null) => void, hint: string) => (
    <label className="text-xs text-muted">
      {label} (%)
      <input type="number" min={0} max={100} className={INPUT} placeholder="off" value={pctIn(value)} onChange={(e) => onChange(e.target.value.trim() === "" ? null : Number(e.target.value) / 100)} />
      <span className="mt-0.5 block">{hint}</span>
    </label>
  );
  return (
    <Section
      title="Release gate"
      hint="A gate run always needs nothing broken and every target fixed. Add limits below: a floor and a no-worse-than-baseline check, per-type floors, over-refusal and latency. A limit that rests on an uncalibrated judge is shown but does not decide."
    >
      <Card>
        <CardContent className="grid gap-4 pt-4 sm:grid-cols-2 lg:grid-cols-3">
          {pctField("Test pass rate at least", g.min_test_pass_rate, (v) => setGate({ min_test_pass_rate: v }), "Absolute floor on the candidate.")}
          {pctField("At most this far below baseline", g.max_drop, (v) => setGate({ max_drop: v }), "Relative: stops slow creep.")}
          {pctField("Over-refusal at most", g.max_over_refusal, (v) => setGate({ max_over_refusal: v }), "Declined when an answer was expected.")}
          {pctField("p95 time to answer, at most this much slower", g.max_p95_latency_increase, (v) => setGate({ max_p95_latency_increase: v }), "Compared with the baseline.")}
          {(["core", "edge", "guardrail"] as const).map((type) =>
            pctField(`${type} cases pass rate at least`, floors[type], (v) => {
              const next = { ...floors };
              if (v == null) delete next[type];
              else next[type] = v;
              setGate({ type_floors: next });
            }, "Per-slice floor."),
          )}
          <label className="flex items-start gap-2 text-sm">
            <input type="checkbox" checked={g.guardrail_zero_tolerance ?? true} onChange={(e) => setGate({ guardrail_zero_tolerance: e.target.checked })} />
            <span>
              Guardrail cases: zero tolerance
              <span className="block text-xs text-muted">Any failed attempt of a guardrail case rejects.</span>
            </span>
          </label>
        </CardContent>
      </Card>
    </Section>
  );
}

function HistoryPanel({ spaceId, current, onRestored }: { spaceId: string; current: number; onRestored: () => void }) {
  const history = useLoad(() => api.profileHistory(spaceId), [spaceId, current]);
  const [diff, setDiff] = useState<ProfileDiff | null>(null);
  const [error, setError] = useState<Error | null>(null);
  if (history.loading) return <Loading />;
  if (history.error) return <ErrorBox error={history.error} />;
  async function compare(version: number) {
    setError(null);
    try {
      setDiff(await api.profileDiff(spaceId, version, current));
    } catch (err) {
      setError(err as Error);
    }
  }
  async function restore(version: number) {
    setError(null);
    try {
      await api.restoreProfile(spaceId, version);
      onRestored();
    } catch (err) {
      setError(err as Error);
    }
  }
  return (
    <Card>
      <CardContent className="space-y-3 pt-4 text-sm">
        <h2 className="font-semibold">History</h2>
        {!history.data!.length && <p className="text-muted">Nothing saved yet: the deployment's defaults apply.</p>}
        <ul className="divide-y divide-line">
          {history.data!.map((v) => (
            <li key={v.version} className="flex flex-wrap items-center gap-2 py-1.5">
              <Badge variant={v.version === current ? "pass" : "outline"}>v{v.version}</Badge>
              <span className="text-muted">
                {v.updated_by} · {when(v.updated_at)}
              </span>
              {v.note && <span>{v.note}</span>}
              {v.version !== current && (
                <span className="ml-auto flex gap-1">
                  <Button size="sm" variant="ghost" onClick={() => compare(v.version)}>
                    Compare with current
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => restore(v.version)}>
                    Restore
                  </Button>
                </span>
              )}
            </li>
          ))}
        </ul>
        {error && <ErrorBox error={error} />}
        {diff && (
          <div className="rounded-lg border border-line p-3">
            <h3 className="mb-1 font-medium">
              From v{diff.a} to v{diff.b}
            </h3>
            {!diff.changes.length && <p className="text-muted">No differences.</p>}
            <ul className="space-y-1 font-mono text-xs">
              {diff.changes.map((c) => (
                <li key={String(c.path)}>
                  <span className="font-semibold">{String(c.path)}</span>: {JSON.stringify(c.before) ?? "—"} → {JSON.stringify(c.after) ?? "—"}
                </li>
              ))}
            </ul>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
