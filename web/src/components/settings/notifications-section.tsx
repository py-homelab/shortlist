import { Card, CardContent } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { useSaveSettings } from "@/lib/queries";
import { settingBool, settingString } from "@/lib/format";
import type { Settings } from "@/lib/types";
import { useState } from "react";

import { InlineKeyField } from "./inline-key-field";

/** The events the server sends, in its order (`services/notify.py` EVENTS), grouped for reading. */
const EVENT_GROUPS: {
  title: string;
  hint?: string;
  events: { id: string; label: string }[];
}[] = [
  {
    title: "Runs",
    events: [
      { id: "run.started", label: "A run started" },
      { id: "run.finished", label: "A run finished" },
      { id: "run.partial", label: "A run finished, but some people failed" },
      { id: "run.failed", label: "A run failed" },
      { id: "run.stopped", label: "A run stopped before it finished" },
    ],
  },
  {
    title: "Jobs",
    hint: "The privacy sync and playback credits run every few minutes, so they only speak up when something changed or went wrong.",
    events: [
      { id: "job.started", label: "A job started" },
      { id: "job.finished", label: "A job finished" },
      { id: "job.failed", label: "A job failed" },
    ],
  },
  {
    title: "Privacy",
    events: [
      { id: "privacy.exposure", label: "Someone can see a row that isn’t theirs" },
    ],
  },
  {
    title: "Requests",
    events: [
      { id: "requests.waiting", label: "Titles are waiting for your approval" },
    ],
  },
  {
    title: "Updates",
    events: [{ id: "update.available", label: "A new version of Shortlist is out" }],
  },
];

const ALL_EVENTS = EVENT_GROUPS.flatMap((group) => group.events.map((e) => e.id));

function storedEvents(settings: Settings): string[] {
  const value = settings["notify.webhook.events"];
  return Array.isArray(value)
    ? value.filter((v): v is string => typeof v === "string")
    : [];
}

/**
 * Tell the owner what happened while they were asleep, on the channel they already watch.
 *
 * The owner picks the events. The server's default is a failed run and a privacy exposure — the two
 * nobody can see before their next login — so a card nobody touches stays quiet. No message names a
 * person: the server keeps names in the app.
 *
 * The address and the auth header's value are `InlineKeyField`s rather than a bespoke form: it already
 * handles the redacted sentinel (a saved value shows as dots, and saving without retyping is a no-op
 * rather than a wipe), which both need for exactly the same reason the API keys do.
 */
export function NotificationsSection({ settings }: { settings: Settings }) {
  const save = useSaveSettings();
  const [enabled, setEnabled] = useState(
    settingBool(settings, "notify.webhook.enabled"),
  );
  const [events, setEvents] = useState(() => storedEvents(settings));
  const authSaved = settingString(settings, "notify.webhook.auth_header_value") !== "";
  const [showAuth, setShowAuth] = useState(authSaved);

  // Flip immediately so the switch feels like a switch, but put it BACK if the save fails. A switch
  // left showing "on" over a server that still says off is the one outcome worse than a slow switch:
  // the owner walks away believing they will be told when a run fails, and they won't be.
  const toggle = (next: boolean) => {
    setEnabled(next);
    save.mutate(
      { "notify.webhook.enabled": next },
      { onError: () => setEnabled(!next) },
    );
  };

  // The same rule for each tick, and the whole list is sent: the setting is one list, not one key per event.
  const toggleEvent = (id: string) => {
    const previous = events;
    const chosen = new Set(previous);
    if (chosen.has(id)) chosen.delete(id);
    else chosen.add(id);
    const next = ALL_EVENTS.filter((e) => chosen.has(e));
    setEvents(next);
    save.mutate(
      { "notify.webhook.events": next },
      { onError: () => setEvents(previous) },
    );
  };

  const headerName =
    settingString(settings, "notify.webhook.auth_header_name") || "Authorization";

  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-lg font-semibold">Notifications</h2>
        <p className="text-sm text-muted-foreground">
          Shortlist runs while you’re asleep. This is how it tells you what
          happened.
        </p>
      </div>

      <Card>
        <CardContent className="space-y-4 pt-6">
          <div className="flex items-start justify-between gap-4">
            <div className="space-y-0.5">
              <p className="text-sm font-medium">Send alerts to a webhook</p>
              <p className="text-sm text-muted-foreground">
                Posts a message to Discord, Slack, Home Assistant, n8n —
                anything that accepts a webhook — for the events you tick
                below. No message ever names anybody.
              </p>
            </div>
            <Switch
              checked={enabled}
              onCheckedChange={toggle}
              aria-label="Send alerts to a webhook"
            />
          </div>

          {save.isError && (
            <p role="alert" className="text-sm text-destructive-text">
              Couldn’t save that. Try again.
            </p>
          )}

          {enabled ? (
            <>
              <InlineKeyField
                settingKey="notify.webhook.url"
                label="Webhook address"
                service="notify"
                settings={settings}
                placeholder="https://discord.com/api/webhooks/…"
                hint="Paste the address your chat app gave you. Anyone holding it can post to that channel, so it’s stored encrypted and shown as dots once saved."
                testLabel="Send a test"
              />

              {showAuth ? (
                <div className="space-y-2">
                  <p className="text-sm font-medium">Authentication</p>
                  <p className="text-sm text-muted-foreground">
                    For a receiver that needs a key, such as ntfy, Gotify or n8n.
                    Sent as a header with every message, the test included.
                  </p>
                  <InlineKeyField
                    settingKey="notify.webhook.auth_header_name"
                    label="Header name"
                    settings={{
                      ...settings,
                      "notify.webhook.auth_header_name": headerName,
                    }}
                    placeholder="Authorization"
                    secret={false}
                  />
                  <InlineKeyField
                    settingKey="notify.webhook.auth_header_value"
                    label="Header value"
                    settings={settings}
                    placeholder="Bearer …"
                    hint="Stored encrypted and shown as dots once saved."
                  />
                </div>
              ) : (
                <button
                  type="button"
                  onClick={() => setShowAuth(true)}
                  className="text-sm font-medium text-primary underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  Add authentication
                </button>
              )}

              <fieldset className="space-y-3">
                <legend className="text-sm font-medium">What to send</legend>
                {EVENT_GROUPS.map((group) => (
                  <div key={group.title} className="space-y-1.5">
                    <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                      {group.title}
                    </p>
                    {group.hint && (
                      <p className="text-xs text-muted-foreground">{group.hint}</p>
                    )}
                    <div className="flex flex-wrap gap-2">
                      {group.events.map((event) => (
                        <label
                          key={event.id}
                          className="flex cursor-pointer items-center gap-2 rounded-md border px-3 py-2 text-sm transition-colors hover:bg-muted/50"
                        >
                          <input
                            type="checkbox"
                            checked={events.includes(event.id)}
                            onChange={() => toggleEvent(event.id)}
                            className="h-4 w-4 accent-primary"
                          />
                          {event.label}
                        </label>
                      ))}
                    </div>
                  </div>
                ))}
              </fieldset>
            </>
          ) : (
            <p className="text-sm text-muted-foreground">
              Turn this on to add a webhook address. Until then, a failed run
              shows up in the bell at the top of the page — the next time you
              look.
            </p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
