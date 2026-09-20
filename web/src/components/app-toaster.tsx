import { CircleAlert, CircleCheck, Loader2 } from "lucide-react";
import { Toaster } from "sonner";

/**
 * The app's toasts, themed to its own tokens rather than left on sonner's defaults, which render a
 * WHITE card on a dark-only app. `richColors` is deliberately off: it paints success/error in
 * sonner's palette, which does not match ours.
 *
 * Shared rather than inlined per surface, because there are two: the admin shell and the picks page,
 * which lives outside it. When the picks page had no toaster of its own, every `toast()` it made was
 * dropped on the floor — a request filed from the grid said nothing at all, and the deck only looked
 * like it worked because the card flies away.
 *
 * `position` differs by surface: the picks deck owns the bottom of the viewport with its action row,
 * so its toasts come from the top.
 */
export function AppToaster({
  position = "bottom-right",
}: {
  position?: "bottom-right" | "top-center";
}) {
  return (
    <Toaster
      position={position}
      closeButton
      theme="dark"
      gap={8}
      toastOptions={{
        classNames: {
          toast:
            "!bg-elevated !border-border !text-foreground !rounded-lg !shadow-xl !gap-3 !px-4 !py-3 !text-sm",
          title: "!text-sm !font-medium !leading-tight",
          description: "!text-xs !text-muted-foreground !leading-snug",
          icon: "!m-0 !self-start !mt-0.5",
          closeButton:
            "!bg-elevated !border-border !text-muted-foreground hover:!text-foreground",
        },
      }}
      icons={{
        // The app's own spinner, so a running toast matches every other "in flight" indicator
        // instead of introducing a second visual language for the same idea.
        loading: (
          <Loader2 className="h-4 w-4 animate-spin text-primary" aria-hidden />
        ),
        success: <CircleCheck className="h-4 w-4 text-success" aria-hidden />,
        error: (
          <CircleAlert className="h-4 w-4 text-destructive-text" aria-hidden />
        ),
      }}
    />
  );
}
