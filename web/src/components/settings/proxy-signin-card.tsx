import { useState } from "react";

import { SaveStatus } from "@/components/save-status";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SecretInput, isSecretUnchanged, REDACTED } from "@/components/ui/secret-input";
import { Button } from "@/components/ui/button";
import { useAutosavedSettings } from "@/lib/autosave";
import { settingString } from "@/lib/format";
import { useSaveSettings } from "@/lib/queries";
import type { Settings } from "@/lib/types";

/**
 * Trusted-proxy sign-in: a reverse proxy that already authenticated the visitor names their Plex
 * account id in a header, and proves it is the proxy with a shared secret. Both, or neither — the
 * secret is what makes the header worth believing, so the card will not save one without the other
 * looking configured.
 */
export function ProxySignInCard({ settings }: { settings: Settings }) {
  const [header, setHeader] = useState(() => settingString(settings, "auth.proxy.header"));
  const [jwtHeader, setJwtHeader] = useState(() => settingString(settings, "auth.proxy.jwt_header"));
  const [jwtClaim, setJwtClaim] = useState(() => settingString(settings, "auth.proxy.jwt_claim"));
  const [jwksUrl, setJwksUrl] = useState(() => settingString(settings, "auth.proxy.jwks_url"));
  const [adminHosts, setAdminHosts] = useState(() => {
    const value = settings["auth.admin_hosts"];
    return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string").join(", ") : "";
  });
  const save = useAutosavedSettings({ header, jwtHeader, jwtClaim, jwksUrl, adminHosts }, () => ({
    "auth.proxy.header": header.trim(),
    "auth.proxy.jwt_header": jwtHeader.trim(),
    "auth.proxy.jwt_claim": jwtClaim.trim(),
    "auth.proxy.jwks_url": jwksUrl.trim(),
    "auth.admin_hosts": adminHosts
      .split(",")
      .map((h) => h.trim())
      .filter(Boolean),
  }));
  const secretSave = useSaveSettings();
  const stored = settingString(settings, "auth.proxy.secret");
  const [secret, setSecret] = useState(stored ? REDACTED : "");
  const untouched = isSecretUnchanged(secret);

  return (
    <Card>
      <CardContent className="space-y-3 pt-6">
        <div>
          <p className="font-medium">Trusted proxy sign-in</p>
          <p className="text-sm text-muted-foreground">
            If a reverse proxy in front of Shortlist already signs people in (authentik, Authelia,
            oauth2-proxy), it can name them here instead of each person signing in with Plex again.
            The proxy sends the visitor&rsquo;s Plex account id in the header below, and the shared
            secret in <code className="text-xs">X-Shortlist-Proxy-Secret</code>. Without the secret
            the header is ignored. Leave the header blank to switch this off.
          </p>
        </div>
        <div className="space-y-2">
          <Label htmlFor="proxy-jwt-header">JWT header (e.g. X-authentik-jwt)</Label>
          <Input
            id="proxy-jwt-header"
            placeholder="X-authentik-jwt"
            className="max-w-xs"
            value={jwtHeader}
            onChange={(e) => setJwtHeader(e.target.value)}
          />
          <Label htmlFor="proxy-jwt-claim">Claim holding the Plex account id</Label>
          <Input
            id="proxy-jwt-claim"
            placeholder="ak_proxy.user_attributes.additionalHeaders.X-Plex-Account-Id"
            className="max-w-xl"
            value={jwtClaim}
            onChange={(e) => setJwtClaim(e.target.value)}
          />
          <Label htmlFor="proxy-jwks">Signing keys URL (optional)</Label>
          <Input
            id="proxy-jwks"
            placeholder="https://auth.example.com/application/o/<app>/jwks/"
            className="max-w-xl"
            value={jwksUrl}
            onChange={(e) => setJwksUrl(e.target.value)}
          />
          <p className="text-xs text-muted-foreground">
            With a keys URL the token&rsquo;s signature is checked. Without one it is trusted only
            because nothing but the proxy can reach Shortlist &mdash; publish no port.
          </p>
        </div>
        <div className="space-y-2">
          <Label htmlFor="admin-hosts">Admin app only at these addresses</Label>
          <Input
            id="admin-hosts"
            placeholder="shortlist.home.example.com"
            className="max-w-xl"
            value={adminHosts}
            onChange={(e) => setAdminHosts(e.target.value)}
          />
          <p className="text-xs text-muted-foreground">
            Comma-separated hostnames. Under any other address &mdash; the public one people use for
            their picks &mdash; even you get only your own picks. Blank = the admin app answers
            everywhere. Set it only once the listed address reaches Shortlist, or you lock yourself out
            of this page.
          </p>
        </div>
        <div className="space-y-2">
          <Label htmlFor="proxy-header">Or: a plain header carrying the Plex account id</Label>
          <Input
            id="proxy-header"
            placeholder="X-Plex-Account-Id"
            className="max-w-xs"
            value={header}
            onChange={(e) => setHeader(e.target.value)}
          />
          <SaveStatus
            isPending={save.isPending}
            isError={save.isError}
            error={save.error}
            saved={save.saved}
            onRetry={save.retry}
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="proxy-secret">Shared secret</Label>
          <div className="flex flex-wrap items-center gap-2">
            <SecretInput
              id="proxy-secret"
              placeholder="a long random string the proxy also holds"
              className="max-w-xs"
              value={secret}
              saved={stored !== ""}
              onChange={setSecret}
            />
            <Button
              size="sm"
              onClick={() =>
                secretSave.mutate({ "auth.proxy.secret": secret }, { onSuccess: () => setSecret(REDACTED) })
              }
              loading={secretSave.isPending}
              disabled={untouched}
            >
              Save
            </Button>
          </div>
          {secretSave.isError && (
            <p role="alert" className="text-sm text-destructive-text">
              Couldn’t save. Try again.
            </p>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
