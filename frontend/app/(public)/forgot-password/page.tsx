"use client";

import Link from "next/link";
import { FormEvent, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { ErrorText } from "@/components/ui";

/** Request a password reset link.
 *
 *  The confirmation is deliberately the same whether or not the address is
 *  registered, and it says so plainly rather than implying a link is on its
 *  way: the server cannot tell the user which it was without turning this page
 *  into a way to test who has an account here.
 */
export default function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      await api("/auth/password/forgot", { method: "POST", body: { email } });
      setSent(true);
    } catch (err) {
      setError(
        err instanceof ApiError && err.status === 429
          ? "Too many requests. Wait a few minutes and try again."
          : "Something went wrong. Try again shortly."
      );
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
            {sent ? "Check your inbox" : "Reset your password"}
          </p>
        </div>

        {sent ? (
          <div className="card space-y-3 text-sm text-slate-300">
            <p>
              If <span className="text-slate-100">{email}</span> has an account, a reset
              link is on its way.
            </p>
            <p className="text-slate-400">
              The link works once and expires shortly. Nothing has changed yet — your
              current password still works, and signing in with it cancels the link.
            </p>
            <Link href="/login" className="btn-ghost w-full">Back to sign in</Link>
          </div>
        ) : (
          <form onSubmit={submit} className="card space-y-4">
            <div>
              <label className="label" htmlFor="email">Email</label>
              <input id="email" type="email" required autoComplete="username"
                     className="input" value={email} autoFocus
                     onChange={(e) => setEmail(e.target.value)} />
              <p className="mt-2 text-xs text-slate-500">
                We&apos;ll email a link that lets you choose a new password.
              </p>
            </div>
            <ErrorText>{error}</ErrorText>
            <button type="submit" disabled={busy} className="btn-primary w-full">
              {busy ? "Sending…" : "Email me a reset link"}
            </button>
          </form>
        )}

        <p className="mt-4 text-center text-sm text-slate-400">
          Remembered it?{" "}
          <Link href="/login" className="font-medium text-emerald-400 hover:text-emerald-300">
            Sign in
          </Link>
        </p>
      </div>
    </div>
  );
}
