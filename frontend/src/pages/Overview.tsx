// Page 1: the spaces the viewer manages, and the review queues they are part of.
import { useState } from "react";
import { Link } from "react-router-dom";
import { ArrowRight, ClipboardCheck, ExternalLink, Hourglass, Inbox, RefreshCw } from "lucide-react";
import { api, type Me, type SpaceCard } from "@/api";
import { useLoad } from "@/hooks";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { CoverageBar, EmptyState, ErrorBox, Loading, ago, pct, when } from "@/components/common";
import { cn } from "@/lib/utils";

export function Overview() {
  const spaces = useLoad(() => api.spaces(), []);
  const me = useLoad(() => api.me(), []);
  if (spaces.loading || me.loading) return <Loading />;
  if (spaces.error) return <ErrorBox error={spaces.error} />;
  const managed = spaces.data ?? [];
  const reviews = me.data?.review_space_ids ?? [];

  return (
    <div className="space-y-10">
      {reviews.length > 0 && me.data && <Reviews me={me.data} />}
      {(managed.length > 0 || reviews.length === 0) && (
        <section>
          <h1 className="text-2xl font-semibold tracking-tight">Your Genie Agents</h1>
          <p className="mt-1 text-muted">Every real conversation is checked against your rules after each collection.</p>
          {managed.length ? (
            <div className="mt-6 grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
              {managed.map((space) => (
                <SpaceTile key={space.space_id} space={space} onCollected={spaces.reload} />
              ))}
            </div>
          ) : (
            <div className="mt-6">
              <EmptyState icon={<Inbox />} title="No Genie Agents to show">
                You see an agent here once you can manage it in Genie (CAN_MANAGE).
              </EmptyState>
            </div>
          )}
        </section>
      )}
    </div>
  );
}

function SpaceTile({ space, onCollected }: { space: SpaceCard; onCollected: () => void }) {
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function collect() {
    setBusy(true);
    setMessage(null);
    try {
      const { job_run_id } = await api.collect(space.space_id);
      setMessage(`Collection started (run ${job_run_id}). New answers appear in a few minutes.`);
      onCollected();
    } catch (err) {
      setMessage((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card className="flex flex-col transition-shadow hover:shadow-md">
      <CardHeader>
        <div className="flex items-start justify-between gap-3">
          <CardTitle>
            <Link to={`/spaces/${space.space_id}`} className="text-ink">
              {space.title ?? space.space_id}
            </Link>
          </CardTitle>
          {space.monitored ? (
            <Badge variant="pass">Monitored</Badge>
          ) : space.monitored === false ? (
            <Badge variant="warn">Not monitored</Badge>
          ) : (
            <Badge variant="neutral">Not collected yet</Badge>
          )}
        </div>
      </CardHeader>
      <CardContent className="flex flex-1 flex-col gap-4">
        {space.monitored === false && (
          <p className="rounded-lg bg-warn-soft p-3 text-sm text-warn">
            Give the collector service principal <strong>CAN_MANAGE</strong> on this agent to start checking it.
          </p>
        )}
        {space.monitored == null && (
          <p className="rounded-lg bg-oat p-3 text-sm text-muted">
            Not collected yet. If the collector service principal has <strong>CAN_MANAGE</strong> on this agent, choose
            Collect now.
          </p>
        )}
        <div>
          <div className="text-3xl font-semibold tracking-tight">{pct(space.hard_pass_rate)}</div>
          <div className="text-sm text-muted">of answers passed every must-pass check (30 days)</div>
        </div>
        <div>
          <div className="mb-1.5 text-xs font-medium uppercase tracking-wide text-muted">Conversations</div>
          {space.coverage ? <CoverageBar byStatus={space.coverage.by_status} total={space.coverage.total} /> : <span className="text-sm text-muted">—</span>}
        </div>
        <div className="mt-auto flex flex-wrap items-center justify-between gap-x-2 gap-y-3 border-t border-line pt-4">
          <span className="whitespace-nowrap text-xs text-muted" title={when(space.last_collected_at)}>
            Collected {ago(space.last_collected_at)}
          </span>
          <div className="ml-auto flex flex-wrap justify-end gap-2">
            <Button size="sm" onClick={collect} disabled={space.monitored === false || busy}>
              <RefreshCw className={cn(busy && "animate-spin")} aria-hidden />
              Collect now
            </Button>
            <Link to={`/spaces/${space.space_id}`} className={buttonVariants({ variant: "primary", size: "sm" })}>
              Open
              <ArrowRight aria-hidden />
            </Link>
          </div>
        </div>
        {message && <p className="text-xs text-muted">{message}</p>}
      </CardContent>
    </Card>
  );
}

// Reviewers label in MLflow's labeling session. Blind review: nothing here shows the judge.
function Reviews({ me }: { me: Me }) {
  return (
    <section>
      <h1 className="text-2xl font-semibold tracking-tight">Review answers</h1>
      <p className="mt-1 text-muted">Your judgement teaches the automatic checks what a good answer looks like.</p>
      <div className="mt-6 grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,22rem)]">
        <div className="space-y-4">
          {(me.review_space_ids ?? []).map((id) => {
            const url = me.review_sessions?.[id];
            return (
              <Card key={id}>
                <CardHeader>
                  <div className="flex items-center gap-3">
                    <span className="flex size-9 items-center justify-center rounded-lg bg-oat-deep text-navy">
                      <ClipboardCheck className="size-5" aria-hidden />
                    </span>
                    <div>
                      <CardTitle>{me.review_titles?.[id] ?? "Genie Agent"}</CardTitle>
                      <CardDescription className="font-mono text-xs">{id}</CardDescription>
                    </div>
                  </div>
                </CardHeader>
                <CardContent>
                  {url ? (
                    <a href={url} target="_blank" rel="noreferrer" className={cn(buttonVariants({ variant: "primary", size: "lg" }), "hover:no-underline")}>
                      Start reviewing
                      <ExternalLink aria-hidden />
                    </a>
                  ) : (
                    <p className="flex items-center gap-2 text-sm text-muted">
                      <Hourglass className="size-4" aria-hidden />
                      Your review queue is being prepared. Check back after the next review run.
                    </p>
                  )}
                </CardContent>
              </Card>
            );
          })}
        </div>
        <Card className="bg-oat-deep/50">
          <CardHeader>
            <CardTitle>How to review</CardTitle>
          </CardHeader>
          <CardContent>
            <ol className="list-decimal space-y-2 pl-5 text-sm">
              <li>Read the context at the top (when it was asked, what data the agent has), then the question and the answer.</li>
              <li>
                For each rule on the right, choose <strong>pass</strong>, <strong>fail</strong>, or <strong>na</strong> when the rule
                does not apply to this question.
              </li>
              <li>Write one sentence on why. The reasons matter as much as the choice.</li>
            </ol>
            <p className="mt-4 text-xs text-muted">
              The automatic judge's opinion is hidden until after you submit, so your judgement stays independent. It appears after the
              next review run.
            </p>
          </CardContent>
        </Card>
      </div>
    </section>
  );
}
