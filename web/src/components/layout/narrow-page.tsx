import type { ReactNode } from "react";

/**
 * The width for forms and short lists: about 1000px, starting where every page starts.
 *
 * The app shell lets a page run to 1800px because tables (Runs, Logs, Users, Requests) use that room. A
 * form or a list of a few items does not: its labels sit on the left, and the switches and buttons that
 * belong to them end up a screen away. Which pages are narrow is decided in one place, `App.tsx`
 * (owner decision 2026-09-14).
 *
 * Left-aligned, not centred: centred, the title of a narrow page sat ~300px right of a wide page's at
 * 1920px, so moving between Rows and Runs made the header jump sideways on every click.
 */
export function NarrowPage({ children }: { children: ReactNode }) {
  return <div className="w-full max-w-5xl">{children}</div>;
}
