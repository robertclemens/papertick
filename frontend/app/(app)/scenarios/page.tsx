"use client";

import { ChangeEvent, FormEvent, useRef, useState } from "react";
import {
  api,
  ApiError,
  DeletedScenarioT,
  download,
  getScenario,
  MAX_SCENARIOS,
  ScenarioT,
  setScenario,
} from "@/lib/api";
import { shortDate } from "@/lib/format";
import { Card, ConfirmDialog, Dialog, Empty, ErrorText, InfoText, Spinner } from "@/components/ui";
import { useScenarios } from "@/components/scenario-context";

/** "3 days" / "18 hours" / "under an hour" — a countdown people can act on. */
function timeLeft(d: DeletedScenarioT): string {
  if (d.days_left >= 1) return `${d.days_left} day${d.days_left === 1 ? "" : "s"}`;
  if (d.hours_left >= 1) return `${d.hours_left} hour${d.hours_left === 1 ? "" : "s"}`;
  return "under an hour";
}

export default function ScenariosPage() {
  // the sidebar switcher reads the same lists, so every change here shows up
  // there without a reload
  const { scenarios, deleted, refresh } = useScenarios();
  // destructive actions all route through a confirmation
  const [confirmDelete, setConfirmDelete] = useState<ScenarioT | null>(null);
  const [confirmPurge, setConfirmPurge] = useState<DeletedScenarioT | null>(null);
  const [confirmPurgeAll, setConfirmPurgeAll] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const [createOpen, setCreateOpen] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [copyFrom, setCopyFrom] = useState("");
  const [copyMode, setCopyMode] = useState<"position" | "full">("position");

  const [renaming, setRenaming] = useState<ScenarioT | null>(null);
  const [draftName, setDraftName] = useState("");

  const [importing, setImporting] = useState<ScenarioT | null | "new">(null);
  const fileRef = useRef<HTMLInputElement>(null);


  const activeId = getScenario();
  const active = scenarios?.find((s) => s.id === activeId) ?? scenarios?.find((s) => s.is_active);

  function switchTo(s: ScenarioT) {
    setScenario(s.id);
    window.location.reload();
  }

  async function create(e: FormEvent) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      const created = await api<ScenarioT>("/scenarios", {
        method: "POST",
        body: {
          name,
          description: description || null,
          copy_from_id: copyFrom || null,
          copy_mode: copyMode,
        },
      });
      setCreateOpen(false);
      setName("");
      setDescription("");
      setCopyFrom("");
      setCopyMode("position");
      setNotice(`Created “${created.name}”.`);
      refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to create scenario");
    } finally {
      setBusy(false);
    }
  }

  async function rename(e: FormEvent) {
    e.preventDefault();
    if (!renaming) return;
    setBusy(true);
    try {
      await api(`/scenarios/${renaming.id}`, { method: "PATCH", body: { name: draftName } });
      setRenaming(null);
      refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to rename");
    } finally {
      setBusy(false);
    }
  }

  async function setBackdating(s: ScenarioT, allow: boolean) {
    setError("");
    await api(`/scenarios/${s.id}`, { method: "PATCH", body: { allow_backdated: allow } })
      .catch(() => setError("Could not change the past-dated trade setting"));
    refresh();
  }

  async function makeDefault(s: ScenarioT) {
    await api(`/scenarios/${s.id}`, { method: "PATCH", body: { is_default: true } })
      .catch(() => {});
    setNotice(`“${s.name}” opens by default when you sign in.`);
    refresh();
  }

  async function remove(s: ScenarioT) {
    setError("");
    setBusy(true);
    try {
      await api(`/scenarios/${s.id}`, { method: "DELETE" });
      if (s.id === activeId) setScenario(null);
      setConfirmDelete(null);
      setNotice(`Deleted “${s.name}”. You can restore it below for the next 30 days.`);
      refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to delete scenario");
      setConfirmDelete(null);
    } finally {
      setBusy(false);
    }
  }

  async function restore(d: DeletedScenarioT) {
    setError("");
    try {
      const back = await api<ScenarioT>(`/scenarios/${d.id}/restore`, { method: "POST" });
      setNotice(`Restored “${back.name}”.`);
      refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to restore scenario");
    }
  }

  async function purgeOne(d: DeletedScenarioT) {
    setError("");
    setBusy(true);
    try {
      await api(`/scenarios/${d.id}/purge`, { method: "DELETE" });
      setConfirmPurge(null);
      setNotice(`“${d.name}” has been permanently deleted.`);
      refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to purge scenario");
      setConfirmPurge(null);
    } finally {
      setBusy(false);
    }
  }

  async function purgeAll() {
    setError("");
    setBusy(true);
    try {
      const res = await api<{ purged: number }>("/scenarios/deleted/purge", { method: "DELETE" });
      setConfirmPurgeAll(false);
      setNotice(`Permanently deleted ${res.purged} scenario${res.purged === 1 ? "" : "s"}.`);
      refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to purge scenarios");
      setConfirmPurgeAll(false);
    } finally {
      setBusy(false);
    }
  }

  async function exportOne(s: ScenarioT) {
    setError("");
    try {
      await download(`/scenarios/${s.id}/export`, `papertick-scenario-${s.name}.json`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Export failed");
    }
  }

  async function onFile(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file || importing === null) return;
    setError("");
    setBusy(true);
    try {
      const payload = JSON.parse(await file.text());
      const target = importing === "new" ? null : importing.id;
      const result = await api<ScenarioT>("/scenarios/import", {
        method: "POST",
        body: { payload, target_scenario_id: target },
      });
      setNotice(
        target
          ? `Replaced “${result.name}” with the imported file.`
          : `Imported “${result.name}”.`
      );
      refresh();
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message
          : err instanceof SyntaxError ? "That file is not valid JSON"
          : "Import failed"
      );
    } finally {
      setImporting(null);
      setBusy(false);
    }
  }

  const atCap = (scenarios?.length ?? 0) >= MAX_SCENARIOS;

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Scenarios</h1>
          <p className="mt-1 text-sm text-slate-400">
            Independent tracks of accounts, holdings and history. Everything in the app —
            trades, statements, contribution limits — belongs to the scenario you are in.
          </p>
        </div>
        <div className="flex gap-2">
          <button className="btn-ghost" onClick={() => { setImporting("new"); fileRef.current?.click(); }}>
            Import…
          </button>
          <button className="btn-primary" disabled={atCap}
                  title={atCap ? `Limit of ${MAX_SCENARIOS} scenarios reached` : undefined}
                  onClick={() => { setCreateOpen(true); setError(""); }}>
            New scenario
          </button>
        </div>
      </header>

      <input ref={fileRef} type="file" accept="application/json,.json"
             className="hidden" onChange={onFile} />

      {notice && <InfoText>{notice}</InfoText>}
      <ErrorText>{error}</ErrorText>

      {!scenarios ? (
        <Spinner />
      ) : scenarios.length === 0 ? (
        <Card><Empty>No scenarios yet.</Empty></Card>
      ) : (
        <ul className="space-y-3">
          {scenarios.map((s) => {
            const isActive = s.id === (active?.id ?? "");
            return (
              <li key={s.id} className={`card ${isActive ? "border-emerald-700" : ""}`}>
                <div className="flex flex-wrap items-start justify-between gap-4">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-lg font-semibold text-slate-100">{s.name}</span>
                      {isActive && (
                        <span className="rounded-full border border-emerald-900 bg-emerald-950/40 px-2 py-0.5 text-[10px] text-emerald-300">
                          viewing
                        </span>
                      )}
                      {s.is_default && (
                        <span className="rounded-full border border-slate-700 bg-slate-800 px-2 py-0.5 text-[10px] text-slate-300">
                          default
                        </span>
                      )}
                      {s.backdated_fills > 0 && (
                        <span className="rounded-full border border-amber-900 bg-amber-950/40 px-2 py-0.5 text-[10px] text-amber-300"
                              title="Contains trades entered after the date they filled on — placed with the outcome already known">
                          {s.backdated_fills} past-dated fill{s.backdated_fills === 1 ? "" : "s"}
                        </span>
                      )}
                    </div>
                    {s.description && (
                      <p className="mt-1 text-sm text-slate-400">{s.description}</p>
                    )}
                    <p className="mt-1 text-xs text-slate-500">
                      {s.account_count} account{s.account_count === 1 ? "" : "s"} · created{" "}
                      {shortDate(s.created_at)}
                      {s.copied_from_id && " · copied from another scenario"}
                    </p>
                    <label className="mt-2 flex items-start gap-2 text-xs text-slate-400">
                      <input type="checkbox" className="mt-0.5 accent-emerald-500"
                             checked={s.allow_backdated}
                             onChange={(e) => setBackdating(s, e.target.checked)} />
                      <span>
                        Allow past-dated trades
                        <span className="block text-slate-500">
                          Lets an order be placed for a date that has already happened — with
                          the outcome known. Every such fill is marked wherever its numbers
                          appear, including on statements.
                        </span>
                      </span>
                    </label>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    {!isActive && (
                      <button className="btn-ghost !py-1.5 text-xs" onClick={() => switchTo(s)}>
                        Switch to
                      </button>
                    )}
                    {!s.is_default && (
                      <button className="btn-ghost !py-1.5 text-xs" onClick={() => makeDefault(s)}>
                        Make default
                      </button>
                    )}
                    <button className="btn-ghost !py-1.5 text-xs"
                            onClick={() => { setRenaming(s); setDraftName(s.name); }}>
                      Rename
                    </button>
                    <button className="btn-ghost !py-1.5 text-xs" onClick={() => exportOne(s)}>
                      Export
                    </button>
                    <button className="btn-ghost !py-1.5 text-xs"
                            title="Replace everything in this scenario with an export file"
                            onClick={() => { setImporting(s); fileRef.current?.click(); }}>
                      Restore…
                    </button>
                    <button className="btn-danger !py-1.5 text-xs"
                            disabled={scenarios.length <= 1}
                            title={scenarios.length <= 1 ? "Your only scenario" : undefined}
                            onClick={() => { setError(""); setConfirmDelete(s); }}>
                      Delete
                    </button>
                  </div>
                </div>
              </li>
            );
          })}
        </ul>
      )}

      {deleted && deleted.length > 0 && (
        <Card
          title={`Recently deleted (${deleted.length})`}
          action={
            <button className="btn-danger !py-1.5 text-xs"
                    onClick={() => { setError(""); setConfirmPurgeAll(true); }}>
              Purge all
            </button>
          }
        >
          <p className="mb-3 text-xs text-slate-500">
            Deleted scenarios are kept for {deleted[0].retention_days} days so a mistaken
            click can be undone. They are frozen meanwhile — their auto-invest, dividends
            and settlement interest do not run — and are wiped automatically when the time
            runs out.
          </p>
          <ul className="divide-y divide-slate-800/60">
            {deleted.map((d) => (
              <li key={d.id} className="flex flex-wrap items-center justify-between gap-3 py-2.5">
                <div className="min-w-0">
                  <div className="text-sm font-medium text-slate-200">{d.name}</div>
                  <div className="text-xs text-slate-500">
                    {d.account_count} account{d.account_count === 1 ? "" : "s"} · deleted{" "}
                    {shortDate(d.deleted_at)}
                  </div>
                </div>
                <div className="flex items-center gap-3">
                  <span className={`text-xs tabular-nums ${
                    d.days_left <= 3 ? "text-amber-300" : "text-slate-400"
                  }`} title={`Wiped on ${shortDate(d.purges_at)}`}>
                    {timeLeft(d)} left
                  </span>
                  <button className="btn-ghost !py-1.5 text-xs" onClick={() => restore(d)}>
                    Restore
                  </button>
                  <button className="btn-danger !py-1.5 text-xs"
                          onClick={() => { setError(""); setConfirmPurge(d); }}>
                    Delete forever
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </Card>
      )}

      <p className="text-xs text-slate-500">
        Up to {MAX_SCENARIOS} scenarios. Exports contain accounts, holdings, the full ledger
        and auto-invest rules; statements are left out because they re-render from the ledger.
      </p>

      <ConfirmDialog
        open={confirmDelete !== null}
        title={`Delete “${confirmDelete?.name ?? ""}”?`}
        confirmLabel="Delete scenario"
        busy={busy}
        onClose={() => setConfirmDelete(null)}
        onConfirm={() => confirmDelete && remove(confirmDelete)}
      >
        <p>
          This moves the scenario and everything in it — {confirmDelete?.account_count ?? 0}{" "}
          account{confirmDelete?.account_count === 1 ? "" : "s"}, their holdings, ledger and
          auto-invest rules — into <span className="text-slate-100">Recently deleted</span>.
        </p>
        <p>
          It stays there for <span className="text-slate-100">30 days</span> and can be
          restored at any point. After 30 days it is wiped automatically and is gone forever.
        </p>
        {confirmDelete?.is_default && (
          <p className="text-amber-300">
            This is your default scenario — another one will take over as the default.
          </p>
        )}
      </ConfirmDialog>

      <ConfirmDialog
        open={confirmPurge !== null}
        title={`Permanently delete “${confirmPurge?.name ?? ""}”?`}
        confirmLabel="Delete forever"
        busy={busy}
        onClose={() => setConfirmPurge(null)}
        onConfirm={() => confirmPurge && purgeOne(confirmPurge)}
      >
        <p>
          This skips the remaining {confirmPurge ? timeLeft(confirmPurge) : ""} of the
          retention window and destroys the scenario now.
        </p>
        <p className="text-amber-300">
          It cannot be restored afterwards. If you might want it later, export it first.
        </p>
      </ConfirmDialog>

      <ConfirmDialog
        open={confirmPurgeAll}
        title="Permanently delete all deleted scenarios?"
        confirmLabel={`Delete all ${deleted?.length ?? 0} forever`}
        busy={busy}
        onClose={() => setConfirmPurgeAll(false)}
        onConfirm={purgeAll}
      >
        <p>
          This destroys {deleted?.length ?? 0} scenario
          {deleted?.length === 1 ? "" : "s"} and everything in them right now, without
          waiting out their retention windows.
        </p>
        <p className="text-amber-300">
          None of them can be restored afterwards.
        </p>
      </ConfirmDialog>

      <Dialog open={createOpen} title="New scenario" onClose={() => setCreateOpen(false)}>
        <form onSubmit={create} className="space-y-4">
          <div>
            <label className="label" htmlFor="sc-name">Name</label>
            <input id="sc-name" required maxLength={80} className="input"
                   placeholder="e.g. All-in on VOO"
                   value={name} onChange={(e) => setName(e.target.value)} />
          </div>
          <div>
            <label className="label" htmlFor="sc-desc">Description (optional)</label>
            <input id="sc-desc" maxLength={300} className="input"
                   value={description} onChange={(e) => setDescription(e.target.value)} />
          </div>
          <div>
            <label className="label" htmlFor="sc-copy">Start from</label>
            <select id="sc-copy" className="input" value={copyFrom}
                    onChange={(e) => setCopyFrom(e.target.value)}>
              <option value="">Empty — no accounts</option>
              {(scenarios ?? []).map((s) => (
                <option key={s.id} value={s.id}>Copy of {s.name}</option>
              ))}
            </select>
          </div>
          {copyFrom && (
            <fieldset className="rounded-lg border border-slate-800 p-3">
              <legend className="px-1 text-xs font-medium text-slate-300">How much to copy</legend>
              <label className="flex cursor-pointer gap-3 py-1.5">
                <input type="radio" name="copy-mode" className="mt-1" value="position"
                       checked={copyMode === "position"}
                       onChange={() => setCopyMode("position")} />
                <span className="text-sm">
                  <span className="font-medium text-slate-200">Position only</span>
                  <span className="mt-0.5 block text-xs text-slate-500">
                    Accounts, cash and holdings, re-priced at today&apos;s market. Trades,
                    dividends and auto-invest rules are left behind, so returns start at
                    zero and measure what happens from here.
                  </span>
                </span>
              </label>
              <label className="flex cursor-pointer gap-3 py-1.5">
                <input type="radio" name="copy-mode" className="mt-1" value="full"
                       checked={copyMode === "full"}
                       onChange={() => setCopyMode("full")} />
                <span className="text-sm">
                  <span className="font-medium text-slate-200">Everything, including history</span>
                  <span className="mt-0.5 block text-xs text-slate-500">
                    An exact duplicate — every order, transaction, tax lot, dividend and
                    contribution. Returns and the full history carry over, so the copy
                    performs identically until you change something.
                  </span>
                </span>
              </label>
            </fieldset>
          )}
          <ErrorText>{error}</ErrorText>
          <button type="submit" disabled={busy} className="btn-primary w-full">
            {busy ? "Creating…" : "Create scenario"}
          </button>
        </form>
      </Dialog>

      <Dialog open={renaming !== null} title="Rename scenario" onClose={() => setRenaming(null)}>
        <form onSubmit={rename} className="space-y-4">
          <div>
            <label className="label" htmlFor="sc-rename">Name</label>
            <input id="sc-rename" required maxLength={80} autoFocus className="input"
                   value={draftName} onChange={(e) => setDraftName(e.target.value)} />
          </div>
          <ErrorText>{error}</ErrorText>
          <button type="submit" disabled={busy} className="btn-primary w-full">
            {busy ? "Saving…" : "Save name"}
          </button>
        </form>
      </Dialog>
    </div>
  );
}
