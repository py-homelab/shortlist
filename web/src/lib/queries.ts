import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

import { api } from "./api";
import { runRefetchIntervalMs, runsListRefetchIntervalMs } from "./run-format";
import { useSSE } from "./sse";
import { useLiveClock } from "./use-live-clock";
import type {
  CollectionInput,
  ReportWindow,
  Run,
  RowOverridePatch,
  RunRequest,
  Settings,
  User,
  UserPatch,
  WatchedFilters,
} from "./types";

/**
 * Every React Query key used anywhere in the app, in one place.
 *
 * Query keys are also invalidation targets — get one wrong (a typo, a copy-pasted literal) and a
 * mutation silently stops refreshing the view it's supposed to. Scattering `["report"]`-style
 * literals across pages meant the same key was hand-typed in up to three different files with no
 * way to catch a drift between them; every key lives here now, so a rename is a one-line change
 * and `invalidateQueries` calls import the exact same array their query used.
 */
export const queryKeys = {
  users: ["users"] as const,
  runs: ["runs"] as const,
  run: (id: number) => ["runs", id] as const,
  runUserTrace: (runId: number, userId: number) =>
    ["runs", runId, "trace", userId] as const,
  runSharedRowTrace: (runId: number, slug: string) =>
    ["runs", runId, "trace", "row", slug] as const,
  runLog: (runId: number) => ["run-log", runId] as const,
  settings: ["settings"] as const,
  collections: ["collections"] as const,
  curatorModels: (provider: string, credential: string) =>
    ["curator-models", provider, credential] as const,
  userRows: (id: number) => ["users", id, "rows"] as const,
  userOutcomes: (id: number) => ["users", id, "outcomes"] as const,
  userRuns: (id: number) => ["users", id, "runs"] as const,
  userRunsSummary: (id: number) => ["users", id, "runs", "summary"] as const,
  homeUsers: ["watching-account", "candidates"] as const,
  userHistory: (id: number) => ["users", id, "history"] as const,
  userWatched: (id: number, filters: WatchedFilters) =>
    ["users", id, "watched", filters] as const,
  session: ["auth", "session"] as const,
  setupState: ["setup", "state"] as const,
  apiToken: ["api-token"] as const,
  logs: (level: string, q: string, limit: number) =>
    ["logs", level, q, limit] as const,
  // The base key covers every window ("30", "90", …) for broad invalidation; `reportWindow` is
  // what each windowed query itself is keyed on.
  report: ["report"] as const,
  reportWindow: (window: ReportWindow) => ["report", window] as const,
  engagement: (window: ReportWindow) =>
    ["report", "engagement", window] as const,
  deletedRows: ["report", "deleted-rows"] as const,
  schedule: ["schedule"] as const,
  libraries: ["libraries"] as const,
  seasons: ["seasons"] as const,
  libraryCollections: (key: string) => ["library-collections", key] as const,
  ownedCollections: ["owned-collections"] as const,
  notifications: ["notifications"] as const,
  whatsNew: ["whats-new"] as const,
  syncs: ["syncs"] as const,
  version: ["version"] as const,
  imageProvider: ["image-provider"] as const,
  backups: ["backups"] as const,
  pendingRestore: ["backups", "restore"] as const,
  // The base key ("jobs") covers every job-queue query for a broad "something changed" invalidation
  // (fired after every mutation — App.tsx); `jobsCatalog` is what the catalogue query itself uses.
  jobs: ["jobs"] as const,
  jobsCatalog: ["jobs", "catalog"] as const,
  privacyStatus: ["privacy", "status"] as const,
};

/**
 * Every account's share filter, read live from plex.tv on each call.
 *
 * `staleTime` is 60s because this costs a plex.tv roster read AND a PMS collections read per call,
 * and TanStack refetches on window focus — an owner alt-tabbing would otherwise hammer both. It is
 * short enough that the page stays a reading rather than a cache: the timestamp on screen is
 * `read_at` from the response, so an older answer says so itself.
 */
export function usePrivacyStatus() {
  return useQuery({
    queryKey: queryKeys.privacyStatus,
    queryFn: api.getPrivacyStatus,
    staleTime: 60_000,
  });
}

export function useSession() {
  return useQuery({
    queryKey: queryKeys.session,
    queryFn: api.getSession,
    staleTime: 60_000,
  });
}

export function useSetupState(options: { enabled?: boolean } = {}) {
  return useQuery({
    queryKey: queryKeys.setupState,
    queryFn: api.getSetupState,
    staleTime: 30_000,
    enabled: options.enabled ?? true,
  });
}

export function useUsers() {
  return useQuery({ queryKey: queryKeys.users, queryFn: api.getUsers });
}

export function useRuns(collection?: string) {
  return useQuery({
    queryKey: collection
      ? ([...queryKeys.runs, { collection }] as const)
      : queryKeys.runs,
    queryFn: () => api.getRuns(collection),
  });
}

/** How many runs a "Load more" click fetches. Matches the server's default page. */
export const RUNS_PAGE = 50;

/**
 * The runs list, paged backwards through history.
 *
 * Cursor, not offset: runs are inserted while you read, so an offset would skip or repeat rows as
 * the list shifts under you. A short page means there is nothing older — the list is the only place
 * that knows, since the endpoint returns a plain array.
 */
export function useRunsPaged(collection?: string) {
  return useInfiniteQuery({
    queryKey: collection
      ? ([...queryKeys.runs, "paged", { collection }] as const)
      : ([...queryKeys.runs, "paged"] as const),
    queryFn: ({ pageParam }) =>
      api.getRuns(collection, pageParam as number | undefined, RUNS_PAGE),
    initialPageParam: undefined as number | undefined,
    getNextPageParam: (lastPage: Run[]) =>
      lastPage.length < RUNS_PAGE
        ? undefined
        : lastPage[lastPage.length - 1]?.id,
    // Same safety net as `useRun`: the list's SSE handler in `runs.tsx` only fires while the stream
    // is up. A tick refetches every page loaded so far, which after a few "Load more" presses is
    // several requests per 5s — accepted deliberately over the two ways to trim it, because both
    // cost correctness. `maxPages` would evict the older pages the operator just asked for, and
    // gating on `pages[0]` alone would stop polling a run that is still going but has been pushed
    // off the newest page by 50 later ones. It only ticks while a run is genuinely unfinished, and
    // React Query's default `refetchIntervalInBackground: false` pauses it on an unfocused tab.
    refetchInterval: (query) =>
      runsListRefetchIntervalMs(query.state.data?.pages),
  });
}

export function useRunsSummary() {
  return useQuery({
    queryKey: [...queryKeys.runs, "summary"] as const,
    queryFn: api.getRunsSummary,
  });
}

export function useBlockSeed(userId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (seed: {
      tmdbId: number;
      title: string;
      mediaType?: string;
      year?: number;
    }) => api.blockSeed(userId, seed),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.users }),
  });
}

export function useUnblockSeed(userId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (tmdbId: number) => api.unblockSeed(userId, tmdbId),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.users }),
  });
}

export function useDeletedRows() {
  return useQuery({
    queryKey: queryKeys.deletedRows,
    queryFn: api.getDeletedRows,
  });
}

export function useClearDeletedRows() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (slug?: string) => api.clearDeletedRows(slug),
    // The dashboard totals change, so the report has to refetch — not just this list.
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.report });
    },
  });
}

export function useSchedule() {
  return useQuery({ queryKey: queryKeys.schedule, queryFn: api.getSchedule });
}

export function useClearRuns() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: api.clearRuns,
    // Picks survive (metrics preserved), but the runs list and the dashboard's "Runs" card refresh.
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.runs });
      queryClient.invalidateQueries({ queryKey: queryKeys.report });
    },
  });
}

export function useRun(id: number, enabled = true) {
  return useQuery({
    queryKey: queryKeys.run(id),
    queryFn: () => api.getRun(id),
    enabled,
    // The safety net for a stream that is down: see `runRefetchIntervalMs`. Without it this page's
    // only refresh is `run.finished` over SSE, so a dropped connection leaves a finished run
    // reading "Running" — and a cancelled one stuck on "Stopping…" — until someone reloads.
    refetchInterval: (query) => runRefetchIntervalMs(query.state.data),
  });
}

/** The full-pipeline trace for one user in one run — fetched on demand (the blob is large), so
 *  callers gate it on `has_trace` and only enable it once the trace page is actually open. */
export function useRunUserTrace(runId: number, userId: number, enabled = true) {
  return useQuery({
    queryKey: queryKeys.runUserTrace(runId, userId),
    queryFn: () => api.getRunUserTrace(runId, userId),
    enabled,
  });
}

/** A SHARED row's trace. Same response shape as a user's — a shared row runs the same pipeline
 *  minus the per-person history stage — so one view renders both. */
export function useRunSharedRowTrace(
  runId: number,
  slug: string,
  enabled = true,
) {
  return useQuery({
    queryKey: queryKeys.runSharedRowTrace(runId, slug),
    queryFn: () => api.getRunSharedRowTrace(runId, slug),
    enabled,
  });
}

export function useSettings() {
  return useQuery({ queryKey: queryKeys.settings, queryFn: api.getSettings });
}

export function useSyncs() {
  return useQuery({ queryKey: queryKeys.syncs, queryFn: api.getSyncs });
}

/** Whether the AI provider can generate poster images — for the row editor's Generate gate. */
export function useImageProvider() {
  return useQuery({
    queryKey: queryKeys.imageProvider,
    queryFn: api.getImageProvider,
  });
}

export function useRemoveUser() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.removeUser(id),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.users }),
  });
}

export function usePatchUser() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, patch }: { id: number; patch: UserPatch }) =>
      api.patchUser(id, patch),
    // Flip the switch in the cache immediately so the toggle responds to the click, not to the
    // round-trip. Only `enabled` drives the users-list UI; other patches settle via the refetch.
    onMutate: async ({ id, patch }) => {
      if (patch.enabled === undefined) return { previous: undefined };
      await queryClient.cancelQueries({ queryKey: queryKeys.users });
      const previous = queryClient.getQueryData<User[]>(queryKeys.users);
      queryClient.setQueryData<User[]>(queryKeys.users, (old) =>
        old?.map((u) =>
          u.id === id ? { ...u, enabled: patch.enabled ?? u.enabled } : u,
        ),
      );
      return { previous };
    },
    onError: (_err, _vars, context) => {
      if (context?.previous)
        queryClient.setQueryData(queryKeys.users, context.previous);
    },
    onSettled: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.users }),
  });
}

export function useSetAllUsersEnabled() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (enabled: boolean) => api.setAllUsersEnabled(enabled),
    // Select all / none flips every row at once. Without this the switches don't move until every
    // write settles, so the click reads as "nothing happened" then everything jumps. Flip the cache
    // up front (one bulk request still runs in the background), and reconcile / roll back on settle.
    onMutate: async (enabled) => {
      await queryClient.cancelQueries({ queryKey: queryKeys.users });
      const previous = queryClient.getQueryData<User[]>(queryKeys.users);
      queryClient.setQueryData<User[]>(queryKeys.users, (old) =>
        old?.map((u) => ({ ...u, enabled })),
      );
      return { previous };
    },
    onError: (_err, _enabled, context) => {
      if (context?.previous)
        queryClient.setQueryData(queryKeys.users, context.previous);
    },
    onSettled: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.users }),
  });
}

export function useStartRun() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: RunRequest) => api.startRun(body),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.runs }),
  });
}

export function useCancelRun() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.cancelRun(id),
    // Both keys named, rather than leaning on `["runs"]` reaching `["runs", id]` by prefix match.
    // That match is real (checked against the installed @tanstack/query-core), but it is implicit:
    // renaming a key could silently stop the detail page refreshing with nothing to catch it.
    //
    // This only ever reports "cancel requested" — cancellation is cooperative, so the run finishes
    // the person it is on and then bails. What the run SETTLED as arrives later, over SSE or via
    // `runRefetchIntervalMs`; this invalidation is not a substitute for either.
    onSuccess: (_data, id) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.runs });
      queryClient.invalidateQueries({ queryKey: queryKeys.run(id) });
    },
  });
}

export function useSaveSettings() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (settings: Settings) => api.putSettings(settings),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.settings }),
  });
}

export function useApiToken() {
  return useQuery({
    queryKey: queryKeys.apiToken,
    queryFn: api.getApiToken,
    staleTime: 30_000,
  });
}

export function useCreateApiToken() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => api.createApiToken(),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.apiToken }),
  });
}

export function useRevokeApiToken() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => api.revokeApiToken(),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.apiToken }),
  });
}

export function useCollections() {
  return useQuery({
    queryKey: queryKeys.collections,
    queryFn: api.listCollections,
  });
}

export function useSaveCollection() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: number | null; body: CollectionInput }) =>
      id === null ? api.createCollection(body) : api.updateCollection(id, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.collections });
      // Renaming the default row writes the shared `row.name_template` setting, so refresh Settings
      // too — otherwise Settings → Defaults would still show the old name until a reload.
      queryClient.invalidateQueries({ queryKey: queryKeys.settings });
    },
  });
}

export function useDeleteCollection() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.deleteCollection(id),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.collections }),
  });
}

/** A short non-crypto fingerprint (FNV-1a) so a credential can key the model-list cache without the
 * raw api key sitting in the query cache / React Query Devtools. Cache discriminator only. */
function fingerprint(value: string): string {
  if (!value) return "";
  let hash = 2166136261;
  for (let i = 0; i < value.length; i++) {
    hash ^= value.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(36);
}

/** Model ids the AI provider offers, for the model picker. Sends the (possibly unsaved) provider +
 * key/URL being edited so switching provider or typing a key refetches; keyed on the provider plus a
 * fingerprint of the credential so the cache distinguishes them without holding the raw key. Callers
 * should pass a DEBOUNCED credential so typing a key doesn't refetch per keystroke. An empty result
 * leaves the free-text override. */
export function useCuratorModels(
  params: { provider: string; apiKey?: string; ollamaUrl?: string },
  enabled: boolean,
) {
  // BOTH inputs, not whichever is set first: a local/OpenAI-compatible server can now carry a key
  // as well as a URL, and keying on the key alone would serve a cached list from the previous
  // server when only the URL changed.
  // `\u0000` as an escape, not a literal NUL byte in the source. Written literally it makes
  // the file binary to every tool that reads it: `file` reports "data", and `grep` returns
  // NOTHING for the whole file rather than erroring, so a search for any symbol in here comes
  // back silently empty. The separator still has to be a character that cannot appear in a
  // key or a URL, so the value stays the same.
  const credential = `${params.apiKey ?? ""}\u0000${params.ollamaUrl ?? ""}`;
  return useQuery({
    queryKey: queryKeys.curatorModels(params.provider, fingerprint(credential)),
    queryFn: () =>
      api.getCuratorModels({
        provider: params.provider,
        api_key: params.apiKey || undefined,
        // Sent under the legacy field name the endpoint still accepts; it feeds the one
        // local/self-hosted provider's base URL either way.
        ollama_url: params.ollamaUrl || undefined,
      }),
    enabled,
    staleTime: 60_000,
    retry: false,
  });
}

/** The season catalogue. It only changes with the app, so it is never refetched; `enabled` lets a
 *  component that shows seasons only on a seasonal row avoid asking for them on every other row. */
export function useSeasons(enabled = true) {
  return useQuery({
    queryKey: queryKeys.seasons,
    queryFn: () => api.getSeasons(),
    staleTime: Infinity,
    enabled,
  });
}

export function useLibraries() {
  return useQuery({
    queryKey: queryKeys.libraries,
    queryFn: () => api.getLibraries(),
    staleTime: 60_000,
    retry: false,
  });
}

export function useLibraryCollections(key: string, enabled = true) {
  return useQuery({
    queryKey: queryKeys.libraryCollections(key),
    queryFn: () => api.getLibraryCollections(key),
    staleTime: 60_000,
    retry: false,
    enabled,
  });
}

export function useOwnedCollections(enabled = false) {
  return useQuery({
    queryKey: queryKeys.ownedCollections,
    queryFn: () => api.getOwnedCollections(),
    retry: false,
    enabled, // on demand — this scans every Plex collection, so don't fire it on page load
  });
}

export function useUserRows(id: number) {
  return useQuery({
    queryKey: queryKeys.userRows(id),
    queryFn: () => api.getUserRows(id),
  });
}

export function useUserRuns(id: number) {
  return useQuery({
    queryKey: queryKeys.userRuns(id),
    queryFn: () => api.getUserRuns(id),
  });
}

export function useUserRunsSummary(id: number) {
  return useQuery({
    queryKey: queryKeys.userRunsSummary(id),
    queryFn: () => api.getUserRunsSummary(id),
  });
}

export function useUserHistory(id: number) {
  return useQuery({
    queryKey: queryKeys.userHistory(id),
    queryFn: () => api.getUserHistory(id),
    retry: false, // a live per-user Plex read; surface the error rather than hammering
  });
}

/** A page of someone's cached watched set. `placeholderData` keeps the previous page on screen while
 *  a new search resolves — without it every keystroke blanks the list to a skeleton, which reads as
 *  "no results" for a moment and makes typing feel broken. */
/** What they did with their picks — finished, part-watched, abandoned. */
export function useUserOutcomes(id: number) {
  return useQuery({
    queryKey: queryKeys.userOutcomes(id),
    queryFn: () => api.getUserOutcomes(id),
  });
}

export function useUserWatched(id: number, filters: WatchedFilters) {
  return useQuery({
    queryKey: queryKeys.userWatched(id, filters),
    queryFn: () => api.getUserWatched(id, filters),
    placeholderData: (previous) => previous,
  });
}

/** Plex Home users the owner could move their watching to. `enabled: false` — it is a live plex.tv
 *  read behind a "look again" button, so it runs when asked rather than on mount. */
export function useHomeUserCandidates() {
  return useQuery({
    queryKey: queryKeys.homeUsers,
    queryFn: () => api.listHomeUsers(),
    retry: false,
  });
}

export function useTransferWatchHistory() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: {
      to_user_id: number;
      from_user_id?: number;
      dry_run: boolean;
    }) => api.transferWatchHistory(body),
    onSuccess: (result) => {
      // A dry run changed nothing, so refetching would only churn. A real one rewrote someone's
      // watched set, which the users list and every watch-history panel read from.
      if (result.dry_run) return;
      queryClient.invalidateQueries({ queryKey: queryKeys.users });
      queryClient.invalidateQueries({ queryKey: ["users"] });
      queryClient.invalidateQueries({ queryKey: ["watch-snapshots"] });
    },
  });
}

export function useWatchSnapshots() {
  return useQuery({
    queryKey: ["watch-snapshots"],
    queryFn: () => api.listWatchSnapshots(),
    retry: false,
  });
}

export function useUndoWatchTransfer() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: { snapshot_id: number; dry_run: boolean }) =>
      api.undoWatchTransfer(body),
    onSuccess: (result) => {
      // Same reasoning as the transfer: an undo rewrites the same watched set back again, so the
      // views reading it are just as stale afterwards.
      if (result.dry_run) return;
      queryClient.invalidateQueries({ queryKey: queryKeys.users });
      queryClient.invalidateQueries({ queryKey: ["users"] });
      queryClient.invalidateQueries({ queryKey: ["watch-snapshots"] });
    },
  });
}

export function useSetUserRowOverride(userId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      collectionId,
      patch,
    }: {
      collectionId: number;
      patch: RowOverridePatch;
    }) => api.setUserRowOverride(userId, collectionId, patch),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.userRows(userId) }),
  });
}

export function useNotifications() {
  return useQuery({
    queryKey: queryKeys.notifications,
    queryFn: api.getNotifications,
    // Poll so a failed run / new release surfaces without a manual refresh.
    refetchInterval: 60_000,
    staleTime: 30_000,
  });
}

export function useDismissNotification() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.dismissNotification(id),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.notifications }),
  });
}

export function useWhatsNew() {
  return useQuery({
    queryKey: queryKeys.whatsNew,
    queryFn: api.getWhatsNew,
    // Once per page load. An upgrade restarts the server, and the app is reloaded to reach it.
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  });
}

export function useMarkWhatsNewSeen() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (version: string) => api.markWhatsNewSeen(version),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.whatsNew }),
  });
}

export function useVersion() {
  return useQuery({
    queryKey: queryKeys.version,
    queryFn: api.getVersion,
    staleTime: 3600_000, // check once per hour
    refetchOnWindowFocus: false,
  });
}

export function useEngagement(window: ReportWindow = "30") {
  return useQuery({
    queryKey: queryKeys.engagement(window),
    queryFn: () => api.getEngagement(window),
  });
}

export function useReport(window: ReportWindow = "30") {
  return useQuery({
    queryKey: queryKeys.reportWindow(window),
    queryFn: () => api.getReport(window),
    staleTime: 60_000,
  });
}

/**
 * Has enough time passed for a per-person "picks watched" figure to mean anything?
 *
 * A pick only counts once it has had its full `matured_days` to be watched — the rule the dashboard's
 * "Needs a look" card states when it holds its warnings back. Until the OLDEST pick on
 * the server reaches that age, no pick anywhere has had its chance, so every person's rate is 0 and
 * says nothing about them. `formatHitRate` renders those as "—".
 *
 * Install-wide rather than per person, because that is the granularity the data supports: the users
 * payload carries a rate but no pick dates. It is also monotonic — once the first pick is old
 * enough this is true for good — which the windowed `landing.rate === null` would not be, since a
 * quiet fortnight can empty that cohort on a mature server and hide rates that do mean something.
 *
 * Defaults to FALSE while the report is loading, so a 0 is withheld until it is known to be real
 * rather than shown and then retracted.
 *
 * The clock comes from `useLiveClock`, not from `Date.now()` in the body: `Date.now()` is impure,
 * and calling it while rendering makes the answer depend on when React happens to re-render
 * (`react-hooks/purity` rejects it outright). The idle cadence is a minute, which is ample for a
 * threshold measured in days — and it means a page left open across the boundary starts showing
 * real rates without a reload.
 *
 * @returns True once picks are old enough for a zero to be a finding rather than a formality.
 */
export function useHitRatesMatured(): boolean {
  const report = useReport();
  const now = useLiveClock(false);
  const firstPick = report.data?.first_pick;
  const maturedDays = report.data?.overall.landing.matured_days;
  if (!firstPick || maturedDays === undefined) return false;
  const first = Date.parse(firstPick);
  if (Number.isNaN(first)) return false;
  return now - first >= maturedDays * 86_400_000;
}

/**
 * Kick off a watch-history sync, and refresh the report once it actually finishes.
 *
 * The sync runs in the background, so the POST returning tells you nothing about when it's done —
 * this used to guess with a flat 4s `setTimeout`, which could refetch before the sync landed (a
 * slow server) or long after (a fast one, leaving the "last synced" time stale in between). The
 * sync already emits `sync.finished` on the shared SSE bus the moment it's actually done; this
 * listens for that instead of guessing.
 */
export function useSyncWatched() {
  const queryClient = useQueryClient();
  useSSE({
    onSyncFinished: (event) => {
      // Both kinds move the report: `watched` is the nightly sync re-reading Plex, `credited` is the
      // live pass that runs the moment someone stops playing a pick. They are separate kinds because
      // the Jobs page announces `watched` as "watch history is up to date", which is a claim the
      // live pass cannot make — it reads nothing from Plex.
      if (event.kind === "watched" || event.kind === "credited") {
        void queryClient.invalidateQueries({ queryKey: queryKeys.report });
      }
    },
  });
  return useMutation({
    mutationFn: api.syncWatched,
  });
}

/** The app's log file. `follow` polls so a live run narrates itself without the operator reloading;
 *  `keepPreviousData` stops the list blanking out on every poll or filter change. */
export function useLogs(
  level: string,
  q: string,
  limit: number,
  follow: boolean,
) {
  return useQuery({
    queryKey: queryKeys.logs(level, q, limit),
    queryFn: () => api.getLogs({ level, q, limit }),
    // Stop polling once it's failing: re-hitting a broken endpoint every 3s buys nothing and
    // buries the real error under a stream of identical ones. The error state offers Retry.
    refetchInterval: (query) => (follow && !query.state.error ? 3000 : false),
    placeholderData: (previous) => previous,
  });
}

/** How one row has actually performed. Only fetched for a SAVED row — a row being created has no
 *  history, and asking for one would 404. */
export function useCollectionEffectiveness(id: number | null) {
  return useQuery({
    queryKey: ["collection-effectiveness", id],
    queryFn: () => api.getCollectionEffectiveness(id as number),
    enabled: id !== null,
  });
}
