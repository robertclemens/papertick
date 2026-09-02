"use client";

import { FormEvent, useEffect, useState } from "react";
import { withBasePath } from "@/lib/base-path";
import { api, ApiError } from "@/lib/api";
import { dateTime } from "@/lib/format";
import { Badge, Card, ConfirmDialog, Dialog, Empty, ErrorText, InfoText, Spinner } from "@/components/ui";

interface KeyT {
  id: string;
  name: string;
  prefix: string;
  scopes: string;
  last_used_at: string | null;
  revoked_at: string | null;
  created_at: string;
}

/** API keys are account access, so they live beside passwords, passkeys and
 *  MFA rather than in a page of their own: everything that can sign in to this
 *  account is reviewable in one place. */
export default function ApiKeysCard() {
  const [keys, setKeys] = useState<KeyT[] | null>(null);
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [scopeRead, setScopeRead] = useState(true);
  const [scopeTrade, setScopeTrade] = useState(false);
  const [created, setCreated] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [revoking, setRevoking] = useState<KeyT | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  function load() {
    api<KeyT[]>("/api-keys").then(setKeys).catch(() => setKeys([]));
  }
  useEffect(load, []);

  async function create(e: FormEvent) {
    e.preventDefault();
    setError("");
    const scopes = [scopeRead && "read", scopeTrade && "trade"].filter(Boolean);
    if (scopes.length === 0) {
      setError("Select at least one scope");
      return;
    }
    setBusy(true);
    try {
      const res = await api<{ plaintext_key: string }>("/api-keys", {
        method: "POST",
        body: { name, scopes },
      });
      setCreated(res.plaintext_key);
      setName("");
      load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to create key");
    } finally {
      setBusy(false);
    }
  }

  async function revoke() {
    if (!revoking) return;
    setBusy(true);
    try {
      await api(`/api-keys/${revoking.id}`, { method: "DELETE" }).catch(() => {});
      setRevoking(null);
      load();
    } finally {
      setBusy(false);
    }
  }

  function closeDialog() {
    setOpen(false);
    setCreated(null);
    setCopied(false);
  }

  const live = keys?.filter((k) => !k.revoked_at) ?? [];

  return (
    <Card title="API keys" action={live.length > 0 ? <Badge value="ACTIVE" /> : undefined}>
      <p className="mb-3 text-sm text-slate-400">
        Full platform access for CLI tools and AI agents — every action in this UI is also a
        REST call. Send the key as{" "}
        <code className="rounded bg-slate-800 px-1.5 py-0.5 text-xs">Authorization: Bearer ptk_…</code>{" "}
        and see the{" "}
        <a href={withBasePath("/api/docs")} target="_blank" rel="noopener noreferrer"
           className="font-medium text-emerald-400 hover:text-emerald-300">
          interactive API reference
        </a>{" "}
        for every endpoint.
      </p>

      {!keys ? (
        <Spinner />
      ) : keys.length === 0 ? (
        <Empty>No API keys yet.</Empty>
      ) : (
        <ul className="mb-3 divide-y divide-slate-800/60">
          {keys.map((k) => (
            <li key={k.id}
                className={`flex items-center justify-between gap-3 py-2 ${k.revoked_at ? "opacity-50" : ""}`}>
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2 text-sm font-medium text-slate-100">
                  <span className="truncate">{k.name}</span>
                  <code className="text-xs font-normal text-slate-500">{k.prefix}…</code>
                  {k.scopes.split(",").map((s) => <Badge key={s} value={s} />)}
                  {k.revoked_at && <Badge value="CANCELLED" />}
                </div>
                <div className="text-xs text-slate-500">
                  {k.revoked_at
                    ? `revoked ${dateTime(k.revoked_at)}`
                    : `last used ${k.last_used_at ? dateTime(k.last_used_at) : "never"}`}
                </div>
              </div>
              {!k.revoked_at && (
                <button className="shrink-0 text-xs text-red-400 hover:text-red-300"
                        onClick={() => setRevoking(k)}>
                  Revoke
                </button>
              )}
            </li>
          ))}
        </ul>
      )}

      <button className="btn-primary" onClick={() => { setOpen(true); setError(""); }}>
        Generate key
      </button>

      <Dialog open={open} title={created ? "Key created" : "Generate API key"} onClose={closeDialog}>
        {created ? (
          <div className="space-y-4">
            <InfoText>
              Copy this key now — it is shown only once and stored hashed on the server.
            </InfoText>
            <code className="block break-all rounded-lg border border-slate-700 bg-slate-950 p-3 text-xs text-emerald-300">
              {created}
            </code>
            <div className="flex gap-2">
              <button
                className="btn-ghost flex-1"
                onClick={() => {
                  navigator.clipboard?.writeText(created).then(() => setCopied(true)).catch(() => {});
                }}
              >
                {copied ? "Copied ✓" : "Copy to clipboard"}
              </button>
              <button className="btn-primary flex-1" onClick={closeDialog}>Done</button>
            </div>
          </div>
        ) : (
          <form onSubmit={create} className="space-y-4">
            <div>
              <label className="label" htmlFor="k-name">Key name</label>
              <input id="k-name" required maxLength={100} className="input" placeholder="e.g. trading-bot"
                     value={name} onChange={(e) => setName(e.target.value)} />
            </div>
            <fieldset>
              <legend className="label">Scopes</legend>
              <label className="flex items-center gap-2 py-1 text-sm text-slate-200">
                <input type="checkbox" checked={scopeRead} onChange={(e) => setScopeRead(e.target.checked)}
                       className="accent-emerald-500" />
                <span><span className="font-medium">read</span> — balances, positions, history, market data</span>
              </label>
              <label className="flex items-center gap-2 py-1 text-sm text-slate-200">
                <input type="checkbox" checked={scopeTrade} onChange={(e) => setScopeTrade(e.target.checked)}
                       className="accent-emerald-500" />
                <span><span className="font-medium">trade</span> — place orders, deposits, schedules</span>
              </label>
            </fieldset>
            <ErrorText>{error}</ErrorText>
            <button type="submit" disabled={busy} className="btn-primary w-full">
              {busy ? "Generating…" : "Generate"}
            </button>
          </form>
        )}
      </Dialog>

      <ConfirmDialog
        open={revoking !== null}
        title="Revoke API key"
        confirmLabel="Revoke key"
        busy={busy}
        onConfirm={revoke}
        onClose={() => setRevoking(null)}
      >
        <p>
          <span className="font-medium text-slate-100">{revoking?.name}</span> stops working
          immediately. Anything still signing requests with it — a script, a cron job, an agent —
          starts failing on its next call.
        </p>
        <p>This cannot be undone; issue a new key instead.</p>
      </ConfirmDialog>
    </Card>
  );
}
