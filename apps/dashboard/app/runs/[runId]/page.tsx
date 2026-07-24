"use client";

import { useEffect, useRef, useState } from "react";
import { useParams } from "next/navigation";
import { streamRun } from "@/lib/api";
import type { NodeEvent } from "@/lib/api";
import { NodeTimeline } from "@/components/NodeTimeline";

export default function RunPage() {
  const { runId } = useParams<{ runId: string }>();
  const [events, setEvents] = useState<NodeEvent[]>([]);
  const [done, setDone] = useState(false);
  const [warnings, setWarnings] = useState<string[]>([]);
  const stopRef = useRef<() => void>(() => {});

  useEffect(() => {
    const stop = streamRun(runId, (event) => {
      setEvents((prev) => [...prev, event]);
      if (event.event === "pipeline_done") {
        setDone(true);
        setWarnings(event.warnings ?? []);
      }
      if (event.event === "error" || event.event === "stream_closed" || event.event === "timeout") {
        setDone(true);
      }
    });
    stopRef.current = stop;
    return stop;
  }, [runId]);

  const errorEvent = events.find((e) => e.event === "error");

  return (
    <div className="max-w-3xl space-y-6">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-xl font-semibold text-white">Run</h1>
          <p className="font-mono text-xs text-gray-500 mt-0.5">{runId}</p>
        </div>
        <StatusBadge done={done} error={!!errorEvent} />
      </div>

      {warnings.length > 0 && (
        <div className="rounded-lg border border-warning/30 bg-warning/5 px-4 py-3 space-y-1">
          <p className="text-xs font-medium text-warning">Warnings</p>
          {warnings.map((w, i) => (
            <p key={i} className="text-xs text-gray-300">{w}</p>
          ))}
        </div>
      )}

      {errorEvent && (
        <div className="rounded-lg border border-danger/30 bg-danger/5 px-4 py-3">
          <p className="text-xs font-medium text-danger mb-1">Pipeline Error</p>
          <p className="text-xs text-gray-300">{errorEvent.message}</p>
        </div>
      )}

      <NodeTimeline events={events} />

      {!done && (
        <p className="text-xs text-gray-500 animate-pulse">Streaming node events…</p>
      )}
    </div>
  );
}

function StatusBadge({ done, error }: { done: boolean; error: boolean }) {
  if (error) return <span className="rounded-full bg-danger/20 px-3 py-1 text-xs font-medium text-danger">Error</span>;
  if (done) return <span className="rounded-full bg-success/20 px-3 py-1 text-xs font-medium text-success">Done</span>;
  return <span className="rounded-full bg-accent/20 px-3 py-1 text-xs font-medium text-accent animate-pulse">Running</span>;
}
