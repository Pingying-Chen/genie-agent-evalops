// What a regression run will ask before it starts: the AI Evals Playbook tracks cost per run.
// Genie's answers are most of it; about 1 DBU per question was measured with the
// system.billing.usage table. Dollars, read from the billing tables, come later.
import { api } from "@/api";
import { useLoad } from "@/hooks";

const DBU_PER_QUESTION = 1;

/** The space's k (times each case is asked per configuration), from its settings. */
export function useEvalK(spaceId: string): number {
  const effective = useLoad(() => api.profileEffective(spaceId), [spaceId]);
  return Number(effective.data?.thresholds?.eval_k ?? 3);
}

/** "N Genie questions, about N DBU" for cases × k × configurations. */
export function estimateText(cases: number, k: number, arms: number): string {
  const n = cases * k * arms;
  return `${n} Genie questions, about ${n * DBU_PER_QUESTION} DBU`;
}

export function RunEstimate({ cases, k, arms }: { cases: number; k: number; arms: number }) {
  return (
    <span className="text-xs text-muted tabular-nums">
      {cases} case{cases === 1 ? "" : "s"} × k {k}
      {arms > 1 && <> × {arms} configurations</>} = {estimateText(cases, k, arms)}
    </span>
  );
}
