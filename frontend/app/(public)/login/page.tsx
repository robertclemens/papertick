"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { FormEvent, useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { passkeysSupported, signInWithPasskey } from "@/lib/webauthn";
import { ErrorText, InfoText } from "@/components/ui";

/** Sign-in runs in stages: identify, then prove it.
 *
 *  1. email
 *  2. password — or a passkey, which replaces the password entirely
 *  3. authenticator code, whenever the account has one enrolled. Both the
 *     password and the passkey path go through it: a passkey proves possession
 *     of a registered authenticator, which is not a substitute for the second
 *     factor the user deliberately turned on.
 *
 *  The email step deliberately does NOT ask the server which methods an
 *  account has: an endpoint that answers "this address has a passkey" is an
 *  account-enumeration oracle. Passkeys here are discoverable credentials, so
 *  the browser can offer them without the server naming them first. */
type Step = "email" | "credential" | "mfa";

export default function LoginPage() {
  const router = useRouter();
  const [step, setStep] = useState<Step>("email");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [mfaToken, setMfaToken] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [showResend, setShowResend] = useState(false);
  const [busy, setBusy] = useState(false);
  const focusRef = useRef<HTMLInputElement>(null);

  useEffect(() => { focusRef.current?.focus(); }, [step]);

  function backToEmail() {
    setStep("email");
    setPassword("");
    setCode("");
    setMfaToken(null);
    setError("");
    setShowResend(false);
  }

  async function passkeyLogin() {
    setError("");
    setBusy(true);
    try {
      const res = await signInWithPasskey();
      if (res.mfa_required) {
        // The account has an authenticator enrolled, so the passkey is the
        // first factor here, not both.
        setMfaToken(res.mfa_token);
        setStep("mfa");
        setBusy(false);
        return;
      }
      router.push("/");
      router.refresh();
    } catch (err) {
      if (!(err instanceof DOMException && err.name === "NotAllowedError")) {
        setError(err instanceof ApiError ? err.message : "Passkey sign-in failed");
      }
      setBusy(false);
    }
  }

  async function resend() {
    try {
      await api("/auth/resend-verification", { method: "POST", body: { email } });
      setNotice("If that account is pending verification, a new link is on its way.");
      setShowResend(false);
      setError("");
    } catch { /* rate limited */ }
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError("");

    if (step === "email") {
      if (!email.trim()) return;
      setNotice("");
      setStep("credential");
      return;
    }

    setBusy(true);
    try {
      if (step === "mfa") {
        await api("/auth/login/mfa", { method: "POST", body: { mfa_token: mfaToken, code } });
      } else {
        const res = await api<{ mfa_required: boolean; mfa_token: string | null }>("/auth/login", {
          method: "POST",
          body: { email, password },
        });
        if (res.mfa_required) {
          setMfaToken(res.mfa_token);
          setStep("mfa");
          setBusy(false);
          return;
        }
      }
      router.push("/");
      router.refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Something went wrong");
      setShowResend(err instanceof ApiError && err.status === 403);
      setBusy(false);
    }
  }

  const heading = {
    email: "Sign in to your simulation desk",
    credential: "Confirm it's you",
    mfa: "Two-factor verification",
  }[step];

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <div className="w-full max-w-sm">
        <div className="mb-8 text-center">
          <span className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-xl bg-emerald-500 text-xl font-bold text-slate-950">
            P
          </span>
          <h1 className="text-2xl font-semibold tracking-tight">
            Paper<span className="text-emerald-400">Tick</span>
          </h1>
          <p className="mt-1 text-sm text-slate-400">{heading}</p>
        </div>

        <form onSubmit={submit} className="card space-y-4">
          {step !== "email" && (
            <div className="flex items-center justify-between gap-3 rounded-lg border border-slate-800 bg-slate-950/60 px-3 py-2">
              <span className="min-w-0 truncate text-sm text-slate-300" title={email}>{email}</span>
              <button type="button" onClick={backToEmail}
                      className="shrink-0 text-xs text-emerald-400 hover:text-emerald-300">
                Change
              </button>
            </div>
          )}

          {step === "email" && (
            <div>
              <label className="label" htmlFor="email">Email</label>
              <input ref={focusRef} id="email" type="email" required autoComplete="username"
                     className="input" value={email}
                     onChange={(e) => setEmail(e.target.value)} />
            </div>
          )}

          {step === "credential" && (
            <div>
              {/* keeps password managers able to pair the saved username with
                  the password field now that they are on separate steps */}
              <input type="email" value={email} readOnly autoComplete="username"
                     tabIndex={-1} aria-hidden className="sr-only" />
              <label className="label" htmlFor="password">Password</label>
              <input ref={focusRef} id="password" type="password" required
                     autoComplete="current-password" className="input"
                     value={password} onChange={(e) => setPassword(e.target.value)} />
            </div>
          )}

          {step === "mfa" && (
            <div>
              <label className="label" htmlFor="code">Authenticator code</label>
              <input ref={focusRef} id="code" inputMode="numeric" pattern="[0-9]*" maxLength={8}
                     required autoComplete="one-time-code"
                     className="input text-center text-lg tracking-[0.4em]"
                     value={code} onChange={(e) => setCode(e.target.value)} />
              <p className="mt-2 text-xs text-slate-500">
                Enter the 6-digit code from your authenticator app.
              </p>
            </div>
          )}

          <ErrorText>{error}</ErrorText>
          {notice && <InfoText>{notice}</InfoText>}
          {showResend && (
            <button type="button" onClick={resend} className="btn-ghost w-full">
              Resend verification email
            </button>
          )}

          <button type="submit" disabled={busy} className="btn-primary w-full">
            {busy
              ? "Signing in…"
              : step === "email" ? "Continue"
              : step === "mfa" ? "Verify code"
              : "Sign in"}
          </button>

          {step === "credential" && passkeysSupported() && (
            <>
              <div className="flex items-center gap-3 text-xs text-slate-500">
                <span className="h-px flex-1 bg-slate-800" />or<span className="h-px flex-1 bg-slate-800" />
              </div>
              <button type="button" onClick={passkeyLogin} disabled={busy} className="btn-ghost w-full">
                <span aria-hidden>🔑</span> Use a passkey instead
              </button>
              <p className="text-center text-xs text-slate-500">
                A passkey signs you in on its own — no password, no code.
              </p>
            </>
          )}
        </form>

        <p className="mt-4 text-center text-sm text-slate-400">
          No account?{" "}
          <Link href="/signup" className="font-medium text-emerald-400 hover:text-emerald-300">
            Create one
          </Link>
        </p>
      </div>
    </div>
  );
}
