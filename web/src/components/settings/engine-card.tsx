import { useMutation } from "@tanstack/react-query";
import { PlugZap } from "lucide-react";
import { useState } from "react";

import { SaveStatus } from "@/components/save-status";
import { InlineKeyField } from "@/components/settings/inline-key-field";
import { TestResult } from "@/components/test-result";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api } from "@/lib/api";
import { useAutosavedSettings } from "@/lib/autosave";
import { settingString } from "@/lib/format";
import type { Settings } from "@/lib/types";

export type EngineBackend = "builtin" | "http";
export type EngineFallback = "builtin" | "none";

export function asEngineBackend(value: unknown): EngineBackend {
  return value === "http" ? "http" : "builtin";
}

export function asEngineFallback(value: unknown): EngineFallback {
  return value === "none" ? "none" : "builtin";
}

/** Whether an external engine is the configured backend — the sources card hides its toggles then. */
export function usesExternalEngine(settings: Settings): boolean {
  return asEngineBackend(settings["engine.backend"]) === "http";
}

const BACKEND_LABELS: Record<EngineBackend, string> = {
  builtin: "Shortlist’s own",
  http: "An engine of your own (HTTP)",
};

const FALLBACK_LABELS: Record<EngineFallback, string> = {
  builtin: "Use Shortlist’s own engine for that person tonight",
  none: "Leave their rows as they are",
};

/**
 * Which engine ranks each person's candidates. Shortlist's own is the default and needs nothing;
 * an external one is a service the owner runs, reached at a URL, sent each person's seeds and watch
 * history and answering with an ordered list. The sources card above configures the built-in one
 * and is hidden while an external engine is chosen — its toggles would do nothing.
 */
export function EngineCard({ settings }: { settings: Settings }) {
  const [backend, setBackend] = useState<EngineBackend>(() =>
    asEngineBackend(settings["engine.backend"]),
  );
  const [url, setUrl] = useState<string>(() =>
    settingString(settings, "engine.url"),
  );
  const [timeout, setTimeoutS] = useState<number>(() => {
    const value = Number(settings["engine.timeout_s"]);
    return Number.isFinite(value) ? Math.min(600, Math.max(1, value)) : 30;
  });
  const [fallback, setFallback] = useState<EngineFallback>(() =>
    asEngineFallback(settings["engine.fallback"]),
  );
  const save = useAutosavedSettings({ backend, url, timeout, fallback }, () => ({
    "engine.backend": backend,
    "engine.url": url.trim(),
    "engine.timeout_s": timeout,
    "engine.fallback": fallback,
  }));
  const test = useMutation({ mutationFn: () => api.testConnection("engine") });

  return (
    <Card>
      <CardContent className="space-y-4 pt-6">
        <div className="space-y-2">
          <Label htmlFor="engine-backend">Which engine ranks each person’s titles</Label>
          <p className="text-sm text-muted-foreground">
            Shortlist’s own needs nothing set up. An engine of your own is a
            service you run that answers Shortlist’s{" "}
            <code className="text-xs">/v1/recommend</code> protocol; each
            person’s seeds and watch history are sent to it, and to nowhere
            else.
          </p>
          <select
            id="engine-backend"
            value={backend}
            onChange={(e) => setBackend(asEngineBackend(e.target.value))}
            className="h-9 w-full rounded-md border bg-background px-3 text-sm"
          >
            {(Object.keys(BACKEND_LABELS) as EngineBackend[]).map((choice) => (
              <option key={choice} value={choice}>
                {BACKEND_LABELS[choice]}
              </option>
            ))}
          </select>
        </div>
        {backend === "http" && (
          <>
            <div className="space-y-2 border-t pt-4">
              <Label htmlFor="engine-url">Engine address</Label>
              <div className="flex flex-wrap items-center gap-2">
                <Input
                  id="engine-url"
                  placeholder="http://recommendarr:8090"
                  className="max-w-md"
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                />
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => test.mutate()}
                  loading={test.isPending}
                  disabled={url.trim() === "" || !save.saved}
                >
                  {!test.isPending && <PlugZap aria-hidden="true" />}
                  Test
                </Button>
              </div>
              {test.isSuccess && <TestResult result={test.data} />}
              {test.isError && <TestResult error={test.error} />}
            </div>
            <InlineKeyField
              settingKey="engine.token"
              service="engine"
              label="Engine token (optional)"
              placeholder="Bearer token, if the engine expects one"
              hint="Sent as an Authorization header on every call. Leave empty for an engine on a private network."
              settings={settings}
            />
            <div className="space-y-2 border-t pt-4">
              <Label htmlFor="engine-timeout">Wait at most (seconds)</Label>
              <p className="text-sm text-muted-foreground">
                An engine answers from a nightly build, so a few seconds is
                plenty. The limit is what keeps one hung engine from stalling
                everyone’s run.
              </p>
              <Input
                id="engine-timeout"
                type="number"
                min={1}
                max={600}
                className="max-w-[8rem]"
                value={timeout}
                onChange={(e) => {
                  const value = Number(e.target.value);
                  if (Number.isFinite(value))
                    setTimeoutS(Math.min(600, Math.max(1, Math.round(value))));
                }}
              />
            </div>
            <div className="space-y-2 border-t pt-4">
              <Label htmlFor="engine-fallback">
                If the engine is down or has nothing for someone
              </Label>
              <select
                id="engine-fallback"
                value={fallback}
                onChange={(e) => setFallback(asEngineFallback(e.target.value))}
                className="h-9 w-full rounded-md border bg-background px-3 text-sm"
              >
                {(Object.keys(FALLBACK_LABELS) as EngineFallback[]).map(
                  (choice) => (
                    <option key={choice} value={choice}>
                      {FALLBACK_LABELS[choice]}
                    </option>
                  ),
                )}
              </select>
              <p className="text-sm text-muted-foreground">
                Either way the run report says which engine each person’s rows
                came from.
              </p>
            </div>
          </>
        )}
        <SaveStatus
          isPending={save.isPending}
          isError={save.isError}
          error={save.error}
          saved={save.saved}
          onRetry={save.retry}
        />
      </CardContent>
    </Card>
  );
}
