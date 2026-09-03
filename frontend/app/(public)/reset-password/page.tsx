"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { FormEvent, Suspense, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { ErrorText, InfoText, Spinner } from "@/components/ui";

/** Choose a new password from an emailed link.
 *
 *  The token is never sent anywhere but the reset call, and the page does not
 *  sign the user in afterwards: they sign in with the new password, which is
 *  what proves the reset landed on the account they think it did.
 */
function ResetPasswordInner() {
  const params = useSearchParams();
  const token = params.get("token") ?? "";
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [done, setDone] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError("");
    if (password !== confirm) {
      setError("The two passwords don't match.");
      return;
    }
    setBusy(true);
    try {
      const res = await api<{ status: string; note: string | null }>(
        "/auth/password/reset",
        { method: "POST", body: { token, new_password: password } }
      );
      setNote(res.note);
      setDone(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Something went wrong");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-4 py-10">
      <div className="w-full max-w-sm">
        <div className="mb-8 text-center">
          <span className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-xl bg-emerald-500 text-xl font-bold text-slate-950">
            P
          </span>
          <h1 className="text-2xl font-semibold tracking-tight">
            Paper<span className="text-emerald-400">Tick</span>
          </h1>
          <p className="mt-1 text-sm text-slate-400">
            {done ? "Password updated" : "Choose a new password"}
          </p>
        </div>

        {!token ? (
          <div className="card space-y-3 text-center text-sm text-slate-300">
            <div className="text-3xl" aria-hidden>⚠️</div>
            <p>This link is missing its token. Request a new one.</p>
            <Link href="/forgot-password" className="btn-primary w-full">
              Send a new link
            </Link>
          </div>
        ) : done ? (
          <div className="card space-y-3 text-sm text-slate-300">
            <p>
              Your password has been changed. Every signed-in session and every
              remembered device was cleared, so you&apos;ll need to sign in again —
              on this device too.
            </p>
            {note && <InfoText>{note}</InfoText>}
            <Link href="/login" className="btn-primary w-full">Sign in</Link>
          </div>
        ) : (
          <form onSubmit={submit} className="card space-y-4">
            <div>
              <label className="label" htmlFor="password">New password</label>
              <input id="password" type="password" required autoFocus
                     autoComplete="new-password" className="input"
                     value={password} onChange={(e) => setPassword(e.target.value)} />
              <p className="mt-2 text-xs text-slate-500">
                At least 12 characters, with letters and digits.
              </p>
            </div>
            <div>
              <label className="label" htmlFor="confirm">Confirm new password</label>
              <input id="confirm" type="password" required autoComplete="new-password"
                     className="input" value={confirm}
                     onChange={(e) => setConfirm(e.target.value)} />
            </div>
            <ErrorText>{error}</ErrorText>
            <button type="submit" disabled={busy} className="btn-primary w-full">
              {busy ? "Saving…" : "Set new password"}
            </button>
            <p className="text-center text-xs text-slate-500">
              Links expire quickly and work once.{" "}
              <Link href="/forgot-password" className="text-emerald-400 hover:text-emerald-300">
                Request another
              </Link>
              .
            </p>
          </form>
        )}
      </div>
    </div>
  );
}

export default function ResetPasswordPage() {
  return (
    <Suspense fallback={<Spinner />}>
      <ResetPasswordInner />
    </Suspense>
  );
}
