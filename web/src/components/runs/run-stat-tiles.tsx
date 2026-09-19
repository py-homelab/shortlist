import {
  Clock,
  Layers,
  Search,
  Shuffle,
  Sparkles,
  Users,
} from "lucide-react";
import { Fragment } from "react";

import { StatTile } from "@/components/stat-tile";
import { formatDuration, runElapsedMs } from "@/lib/format";
import { tokenSteps } from "@/lib/run-format";
import type { RunDetail } from "@/lib/types";

/** The finished-run stats as at-a-glance tiles (Dashboard style) rather than one dense text line. */

/**
 * A hint's parts joined by " · ", wrapping between parts before it wraps inside one: "web search" at
 * the end of one line and "467,463" at the start of the next reads as two separate figures. Each part
 * is an inline-block, so a tile too narrow for a whole part still wraps it rather than overflowing.
 * The dot rides at the end of the part before it, so no line starts with one.
 */
function HintParts({ parts }: { parts: string[] }) {
  return parts.map((part, i) => (
    <Fragment key={part}>
      {i > 0 && " "}
      <span className="inline-block">
        {part}
        {i < parts.length - 1 && " ·"}
      </span>
    </Fragment>
  ));
}

export function RunStatTiles({ run }: { run: RunDetail }) {
  const s = run.stats;
  const elapsed = runElapsedMs(run.began_at, run.finished_at);
  const failed = s.users_error ?? 0;
  // Skipped is neither a success nor a failure — a run where everyone was skipped used to read
  // "3 · all succeeded" above three rows badged "Skipped".
  const skipped = s.users_skipped ?? 0;
  const tokens = s.llm_tokens ?? 0;
  const exa = s.exa_searches ?? 0;
  const exaCacheHits = s.exa_cache_hits ?? 0;
  // Shared rows belong to nobody, so the people counters cannot see them — and a run whose only
  // work was a shared row therefore reported "0 · 46 skipped, built nothing" directly above the row
  // that had just placed 40 picks. Rows built is the honest headline for what a run DID.
  const sharedRows = run.shared_rows ?? [];
  const sharedBuilt = sharedRows.filter((row) => row.status === "ok").length;
  const perPersonRows = new Set<string>();
  for (const user of run.users) {
    for (const [slug, decision] of Object.entries(user.rows_considered ?? {})) {
      if (decision === "due") perPersonRows.add(slug);
    }
    for (const entry of user.breakdown ?? []) {
      if (entry.row_slug) perPersonRows.add(entry.row_slug);
    }
  }
  const rowsBuilt = perPersonRows.size + sharedBuilt;
  const rowsHint = [
    perPersonRows.size > 0 ? `${perPersonRows.size} per-person` : "",
    sharedBuilt > 0 ? `${sharedBuilt} shared` : "",
  ]
    .filter(Boolean)
    .join(", ");
  // "web search 467,463 · final picks 52,625" — which AI step the tokens went to.
  const steps = tokenSteps(s.llm_tokens_by_step);
  // In and out rather than one total when the run measured both: output is billed at several times the
  // input rate (5x on Claude Haiku), so a total alone cannot say where the money went. The steps stay
  // too, on a line of their own — the split replaced them outright once, and nobody asked for that. A
  // run recorded before the split was measured keeps the older wording.
  const output = s.llm_output_tokens;
  const tokenHint =
    output != null && output <= tokens ? (
      <>
        <span className="block">
          <HintParts
            parts={[
              `${(tokens - output).toLocaleString()} in`,
              `${output.toLocaleString()} out`,
            ]}
          />
        </span>
        {steps.length > 0 && (
          <span className="block">
            <HintParts parts={steps} />
          </span>
        )}
      </>
    ) : steps.length > 0 ? (
      `${steps.join(" · ")} · sent + received`
    ) : (
      "sent + received"
    );
  // The two AI tiles are conditional, so the track count has to be too. Hard-coding six left a
  // no-AI run's tiles filling two-thirds of the row with a third of it blank, which reads as
  // something that failed to load. Full class strings — Tailwind cannot see an interpolated one.
  // Six or seven across waits for `xl`: at `lg` the sidebar leaves about 720px, and seven tiles there
  // broke "6m 47s" and "520,088" over two lines each and stacked the token hint nine lines deep.
  const showTokens = tokens > 0;
  const showExa = exa > 0 || exaCacheHits > 0;
  const tiles = 4 + (showTokens ? 1 : 0) + (showExa ? 1 : 0);
  const columns =
    tiles === 6
      ? "sm:grid-cols-3 xl:grid-cols-6"
      : tiles === 5
        ? "sm:grid-cols-3 lg:grid-cols-5"
        : "lg:grid-cols-4";
  return (
    <div className={`grid grid-cols-2 gap-3 ${columns}`}>
      <StatTile
        icon={Clock}
        label="Duration"
        value={elapsed != null ? formatDuration(elapsed) : "—"}
        hint="start → finish"
      />
      <StatTile
        icon={Layers}
        label="Rows built"
        value={rowsBuilt}
        hint={rowsHint || "nothing was due"}
        tone={rowsBuilt > 0 ? "success" : undefined}
      />
      <StatTile
        icon={Users}
        label="People"
        value={s.users_ok ?? 0}
        hint={
          failed > 0
            ? `${failed} failed${skipped > 0 ? `, ${skipped} skipped` : ""}`
            : skipped > 0
              ? // Only a WARNING when the run built nothing at all. A shared-row run skips every
                // person by design, and flagging that amber said "something went wrong" about the
                // normal outcome of the thing the operator asked for.
                sharedBuilt > 0
                ? `${skipped} skipped — no per-person row was due`
                : `${skipped} skipped, built nothing`
              : // Everyone can succeed while the RUN fails (a refused share filter belongs to no
                // person) — "all succeeded" under a "Failed" badge is how that looked before.
                run.status === "error"
                ? "built, but not promoted"
                : "all succeeded"
        }
        tone={
          failed > 0
            ? "destructive"
            : (skipped > 0 && sharedBuilt === 0) || run.status === "error"
              ? "warning"
              : skipped > 0
                ? undefined
                : "success"
        }
      />
      <StatTile
        icon={Shuffle}
        label="Titles changed"
        value={`+${s.titles_added ?? 0} / −${s.titles_removed ?? 0}`}
        hint="added / rotated out"
      />
      {showTokens && (
        <StatTile
          icon={Sparkles}
          label="AI tokens"
          value={tokens.toLocaleString()}
          hint={tokenHint}
          // The owner asked whether this includes cached tokens. It is each call's input and output
          // tokens as the provider reported them: Anthropic's `input_tokens` excludes cache reads and
          // writes (and Shortlist sets no cache_control, so both are 0); OpenAI's `total_tokens` and
          // Gemini's `total_token_count` count cached input inside the prompt figure.
          title="Input and output tokens the AI provider reported for each call this run, added up — what it bills on, shown as input and output because output costs several times more. With Claude nothing is cached, so this is every token sent and received. OpenAI and Gemini count input they served from their own prompt cache in here too, and bill that part at a discount. The 7-day web-search cache saves web searches, not tokens. Turn AI sources off in Settings → Finding titles to lower it."
        />
      )}
      {showExa && (
        <StatTile
          icon={Search}
          label="Web searches"
          value={exa}
          // A warm cache means most lookups never hit the backend — showing only the "1" that did
          // made a fully-cached run look like the source did nothing. The hint names what it served.
          hint={
            exaCacheHits > 0
              ? `searched · ${exaCacheHits.toLocaleString()} from cache`
              : "web lookups · one per recent watch"
          }
          // Vendor-neutral: the same counter serves Exa and a self-hosted SearXNG.
          title="External web-search requests this run actually made — a count, not tokens. Exa bills per request and SearXNG rate-limits per request, so it is tracked apart from token spend. Results are cached for 7 days and shared across everyone, so most lookups are served from cache and cost nothing."
        />
      )}
    </div>
  );
}
