// Typed client for the backend. Types come from the backend's OpenAPI spec
// (`npm run gen:types`), so a field renamed in Python fails the TypeScript build here.
import type { components } from "./api-types";

type Schemas = components["schemas"];
export type SpaceCard = Schemas["SpaceCard"];
export type SpaceSummary = Schemas["SpaceSummary"];
export type TraceListItem = Schemas["TraceListItem"];
export type TracePage = Schemas["Page_TraceListItem_"];
export type TraceDetail = Schemas["TraceDetail"];
export type Step = Schemas["Step"];
export type CheckResult = Schemas["CheckResult"];
export type Me = Schemas["Me"];
export type CollectResponse = Schemas["CollectResponse"];
export type RunStatus = Schemas["RunStatus"];
export type Criterion = Schemas["Criterion"];
export type CriteriaList = Schemas["CriteriaList"];
export type JudgeCalibration = Schemas["JudgeCalibration"];
export type Agreement = Schemas["JudgeRow"]; // agreement with people, plus the judge's own fail rate
export type BlindSpot = Schemas["BlindSpot"];
export type Observation = Schemas["Observation"];
export type Observations = Schemas["Observations"];
export type FailureCategory = Schemas["FailureCategory"];
export type CategorySummary = Schemas["CategorySummary"];
export type CategoryCreate = Schemas["CategoryCreate"];
export type CategoryUpdate = Schemas["CategoryUpdate"];
export type CriterionDraft = Schemas["CriterionDraft"];
export type Disagreement = Schemas["Disagreement"];
export type Disagreements = Schemas["Disagreements"];
export type CauseUpdate = Schemas["CauseUpdate"];
export type AgentPerformance = Schemas["AgentPerformance"];
export type AgentWeek = Schemas["AgentWeek"];
export type SpaceVersionMark = Schemas["SpaceVersionMark"];
export type EvalSetResponse = Schemas["EvalSet"];
export type EvalCase = Schemas["EvalCase"];
export type EvalCaseBody = Schemas["EvalCaseBody-Input"]; // what the app sends; the draft comes back as -Output
export type EvalCaseUpdate = Schemas["EvalCaseUpdate"];
export type ImportResult = Schemas["ImportResult"];
export type EvalRuns = Schemas["EvalRuns"];
export type EvalRun = Schemas["EvalRun"];
export type EvalRunDetail = Schemas["EvalRunDetail"];
export type ArmOutcome = Schemas["ArmOutcome"];
export type GradedAttempt = Schemas["GradedAttempt"];
export type EvalStart = Schemas["EvalStart"];
export type EvalStarted = Schemas["EvalStarted"];
export type ChangeBody = Schemas["ChangeBody"];
export type ChangeView = Schemas["ChangeView"];
export type Comparison = Schemas["Comparison"];
export type ChangeProposal = Schemas["ChangeProposal"];
export type EvalSetSuggestions = Schemas["EvalSetSuggestions"];
export type SplitSettings = Schemas["SplitSettings"];
export type CaseSplit = Schemas["CaseSplit"];
export type SplitSummary = Schemas["SplitSummary"];
export type StratumMix = Schemas["StratumMix"];
export type Slice = Schemas["Slice"];
export type RunSplit = NonNullable<EvalStart["split"]>;
export type RunScope = Schemas["RunScope"];
export type Versions = Schemas["Versions"];
export type Evaluators = Schemas["Evaluators"];
export type SpaceProfile = Schemas["SpaceProfile"];
export type ProfileSave = Schemas["ProfileSave"];
export type ProfileDiff = Schemas["ProfileDiff"];
export type LibraryView = Schemas["LibraryView"];
export type EffectiveProfile = Schemas["EffectiveProfile"];
export type GateCheck = Schemas["GateCheck"];
export type Evaluator = Schemas["Evaluator"];
export type ManifestDiff = Schemas["ManifestDiff"];
export type JudgePerformance = Schemas["JudgePerformance"];
export type JudgeHistoryPoint = Schemas["JudgeHistoryPoint"];
export type ReviewerPerformance = Schemas["ReviewerPerformance"];
export type PairAgreement = Schemas["PairAgreement"];

/** Every backend error has this shape (errors.py); `code` is stable, `message` is for people. */
export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public requestId?: string | null,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, init);
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const error = body?.error;
    throw new ApiError(response.status, error?.code ?? "unknown", error?.message ?? response.statusText, error?.request_id);
  }
  if (response.status === 204) return null as T;
  return response.json() as Promise<T>;
}

function json(method: string, body: unknown): RequestInit {
  return { method, headers: { "content-type": "application/json" }, body: JSON.stringify(body) };
}

function query(params: Record<string, string | number | undefined | null>): string {
  const entries = Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "");
  return entries.length ? `?${new URLSearchParams(entries.map(([k, v]) => [k, String(v)]))}` : "";
}

export const api = {
  me: () => request<Me>("/me"),
  criteria: () => request<CriteriaList>("/criteria"),
  spaces: () => request<SpaceCard[]>("/spaces"),
  summary: (spaceId: string, from?: string, to?: string) =>
    request<SpaceSummary>(`/spaces/${spaceId}/summary${query({ from, to })}`),
  traces: (spaceId: string, filters: { verdict?: string; criterion?: string; cursor?: string | null; limit?: number }) =>
    request<TracePage>(`/spaces/${spaceId}/traces${query(filters)}`),
  trace: (traceId: string) => request<TraceDetail>(`/traces/${traceId}`),
  calibration: (spaceId: string) => request<JudgeCalibration>(`/spaces/${spaceId}/calibration`),
  observations: (spaceId: string) => request<Observations>(`/spaces/${spaceId}/observations`),
  createCategory: (spaceId: string, body: CategoryCreate) =>
    request<FailureCategory>(`/spaces/${spaceId}/categories`, json("POST", body)),
  updateCategory: (spaceId: string, categoryId: string, body: CategoryUpdate) =>
    request<FailureCategory>(`/spaces/${spaceId}/categories/${categoryId}`, json("PATCH", body)),
  agentPerformance: (spaceId: string, weeks?: number) =>
    request<AgentPerformance>(`/spaces/${spaceId}/performance/agent${query({ weeks })}`),
  judgePerformance: (spaceId: string) => request<JudgePerformance>(`/spaces/${spaceId}/performance/judges`),
  reviewerPerformance: (spaceId: string) => request<ReviewerPerformance>(`/spaces/${spaceId}/performance/reviewers`),
  evalset: (spaceId: string) => request<EvalSetResponse>(`/spaces/${spaceId}/evalset`),
  putSplitSettings: (spaceId: string, body: SplitSettings) =>
    request<SplitSettings>(`/spaces/${spaceId}/evalset/split-settings`, json("PUT", body)),
  assignSplits: (spaceId: string, reset = false) =>
    request<EvalSetResponse>(`/spaces/${spaceId}/evalset/split`, json("POST", { reset })),
  overrideSplit: (spaceId: string, caseId: string, split: "dev" | "test" | null) =>
    request<EvalSetResponse>(`/spaces/${spaceId}/evalset/${caseId}/split`, json("PUT", { split })),
  evalsetSuggestions: (spaceId: string) => request<EvalSetSuggestions>(`/spaces/${spaceId}/evalset/suggestions`),
  createCase: (spaceId: string, body: EvalCaseBody) => request<EvalCase>(`/spaces/${spaceId}/evalset`, json("POST", body)),
  updateCase: (spaceId: string, caseId: string, body: EvalCaseUpdate) =>
    request<EvalCase>(`/spaces/${spaceId}/evalset/${caseId}`, json("PUT", body)),
  draftCase: (spaceId: string, traceId: string) =>
    request<Schemas["EvalCaseBody-Output"]>(`/spaces/${spaceId}/evalset/draft${query({ trace_id: traceId })}`),
  exportEvalset: async (spaceId: string) => {
    const response = await fetch(`/api/spaces/${spaceId}/evalset/export`);
    if (!response.ok) throw new ApiError(response.status, "export_failed", response.statusText);
    return response.text();
  },
  importEvalset: (spaceId: string, text: string) =>
    request<ImportResult>(`/spaces/${spaceId}/evalset/import`, {
      method: "POST",
      headers: { "content-type": "application/yaml" },
      body: text,
    }),
  evaluations: (spaceId: string) => request<EvalRuns>(`/spaces/${spaceId}/evaluations`),
  evaluation: (spaceId: string, runId: string) => request<EvalRunDetail>(`/spaces/${spaceId}/evaluations/${runId}`),
  startEvaluation: (spaceId: string, body: EvalStart) =>
    request<EvalStarted>(`/spaces/${spaceId}/evaluations`, json("POST", body)),
  changes: (spaceId: string) => request<ChangeView[]>(`/spaces/${spaceId}/changes`),
  createChange: (spaceId: string, body: ChangeBody) => request<ChangeView>(`/spaces/${spaceId}/changes`, json("POST", body)),
  proposeChange: (spaceId: string, categoryId: string) =>
    request<ChangeProposal>(`/spaces/${spaceId}/categories/${categoryId}/propose-change`, { method: "POST" }),
  testChange: (spaceId: string, changeId: string, split: "dev" | "test" = "dev", scope?: RunScope) =>
    request<EvalStarted>(`/spaces/${spaceId}/changes/${changeId}/test`, json("POST", { split, scope })),
  applyChange: (spaceId: string, changeId: string, override = false) =>
    request<ChangeView>(`/spaces/${spaceId}/changes/${changeId}/apply`, json("POST", { override })),
  rollbackChange: (spaceId: string, changeId: string) =>
    request<ChangeView>(`/spaces/${spaceId}/changes/${changeId}/rollback`, { method: "POST" }),
  discardChange: (spaceId: string, changeId: string) =>
    request<ChangeView>(`/spaces/${spaceId}/changes/${changeId}/discard`, { method: "POST" }),
  disagreements: (spaceId: string) => request<Disagreements>(`/spaces/${spaceId}/disagreements`),
  markDisagreement: (spaceId: string, traceId: string, criterionId: string, body: CauseUpdate) =>
    request<Disagreement>(`/spaces/${spaceId}/disagreements/${traceId}/${criterionId}`, json("PUT", body)),
  draftCriterion: (spaceId: string, categoryId: string) =>
    request<CriterionDraft>(`/spaces/${spaceId}/categories/${categoryId}/draft`),
  deleteCategory: (spaceId: string, categoryId: string) =>
    request<null>(`/spaces/${spaceId}/categories/${categoryId}`, { method: "DELETE" }),
  tagObservation: (spaceId: string, traceId: string, categoryIds: string[]) =>
    request<{ category_ids: string[] }>(
      `/spaces/${spaceId}/observations/${traceId}/categories`,
      json("PUT", { category_ids: categoryIds }),
    ),
  profile: (spaceId: string) => request<SpaceProfile>(`/spaces/${spaceId}/profile`),
  saveProfile: (spaceId: string, body: ProfileSave) =>
    request<SpaceProfile>(`/spaces/${spaceId}/profile`, json("PUT", body)),
  profileHistory: (spaceId: string) => request<SpaceProfile[]>(`/spaces/${spaceId}/profile/history`),
  profileDiff: (spaceId: string, a: number, b: number) =>
    request<ProfileDiff>(`/spaces/${spaceId}/profile/diff${query({ a, b })}`),
  restoreProfile: (spaceId: string, version: number) =>
    request<SpaceProfile>(`/spaces/${spaceId}/profile/restore/${version}`, { method: "POST" }),
  profileLibrary: (spaceId: string) => request<LibraryView>(`/spaces/${spaceId}/profile/library`),
  profileEffective: (spaceId: string) => request<EffectiveProfile>(`/spaces/${spaceId}/profile/effective`),
  exportProfile: async (spaceId: string) => {
    const response = await fetch(`/api/spaces/${spaceId}/profile/export`);
    if (!response.ok) throw new ApiError(response.status, "export_failed", response.statusText);
    return response.text();
  },
  importProfile: (spaceId: string, text: string) =>
    request<SpaceProfile>(`/spaces/${spaceId}/profile/import`, {
      method: "POST",
      headers: { "content-type": "application/yaml" },
      body: text,
    }),
  evaluators: (spaceId: string) => request<Evaluators>(`/spaces/${spaceId}/evaluators`),
  versions: (spaceId: string) => request<Versions>(`/spaces/${spaceId}/versions`),
  versionsDiff: (spaceId: string, a: string, b: string) =>
    request<ManifestDiff>(`/spaces/${spaceId}/versions/diff${query({ a, b })}`),
  collect: (spaceId: string) => request<CollectResponse>(`/spaces/${spaceId}/collect`, { method: "POST" }),
  run: (runId: number) => request<RunStatus>(`/runs/${runId}`),
};
