"use client";

import { FormEvent, useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { dateTime } from "@/lib/format";
import { Badge, Card, Dialog, Empty, ErrorText, InfoText, Spinner } from "@/components/ui";

interface KeyT {
  id: string;
  name: string;
  prefix: string;
  scopes: string;
  last_used_at: string | null;
  revoked_at: string | null;
  created_at: string;
}

export default function KeysPage() {
  const [keys, setKeys] = useState<KeyT[] | null>(null);
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [scopeRead, setScopeRead] = useState(true);
  const [scopeTrade, setScopeTrade] = useState(false);
  const [created, setCreated] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
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

  async function revoke(id: string) {
    await api(`/api-keys/${id}`, { method: "DELETE" }).catch(() => {});
    load();
  }

  function closeDialog() {
    setOpen(false);
    setCreated(null);
    setCopied(false);
  }

  return (
    <div className="space-y-6">
      <header className="flex items-end justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">API keys</h1>
          <p className="mt-1 text-sm text-slate-400">
            Full platform access for CLI tools and AI agents. Send as{" "}
            <code className="rounded bg-slate-800 px-1.5 py-0.5 text-xs">Authorization: Bearer ptk_…</code>{" "}
            — see the{" "}
            <a href="/api/docs" target="_blank" rel="noopener noreferrer"
               className="font-medium text-emerald-400 hover:text-emerald-300">
              interactive API reference
            </a>{" "}
            for every endpoint.
          </p>
        </div>
        <div className="flex shrink-0 gap-2">
          <a className="btn-ghost" href="/api/docs" target="_blank" rel="noopener noreferrer">
            API docs ↗
          </a>
          <button className="btn-primary" onClick={() => { setOpen(true); setError(""); }}>
            Generate key
          </button>
        </div>
      </header>

      <Card>
        {!keys ? (
          <Spinner />
        ) : keys.length === 0 ? (
          <Empty>No API keys yet.</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="table-base">
              <thead>
                <tr><th>Name</th><th>Key</th><th>Scopes</th><th>Last used</th><th>Status</th><th></th></tr>
              </thead>
              <tbody>
                {keys.map((k) => (
                  <tr key={k.id} className={k.revoked_at ? "opacity-50" : ""}>
                    <td className="font-medium">{k.name}</td>
                    <td><code className="text-xs text-slate-400">{k.prefix}…</code></td>
                    <td>
                      <div className="flex gap-1">
                        {k.scopes.split(",").map((s) => <Badge key={s} value={s} />)}
                      </div>
                    </td>
                    <td className="text-xs">{dateTime(k.last_used_at)}</td>
                    <td>{k.revoked_at ? <Badge value="CANCELLED" /> : <Badge value="ACTIVE" />}</td>
                    <td className="text-right">
                      {!k.revoked_at && (
                        <button className="text-xs text-red-400 hover:text-red-300" onClick={() => revoke(k.id)}>
                          Revoke
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

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
    </div>
  );
}
