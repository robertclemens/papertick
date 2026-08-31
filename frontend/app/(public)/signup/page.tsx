"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { FormEvent, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { ErrorText } from "@/components/ui";

export default function SignupPage() {
  const router = useRouter();
  const [firstName, setFirstName] = useState("");
  const [lastName, setLastName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [dob, setDob] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [pendingVerification, setPendingVerification] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError("");
    if (password !== confirm) {
      setError("Passwords do not match");
      return;
    }
    setBusy(true);
    try {
      const res = await api<{ verification_required: boolean }>("/auth/signup", {
        method: "POST",
        body: {
          email,
          password,
          date_of_birth: dob,
          first_name: firstName,
          last_name: lastName,
        },
      });
      if (res.verification_required) {
        setPendingVerification(true);
        setBusy(false);
        return;
      }
      router.push("/");
      router.refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Something went wrong");
      setBusy(false);
    }
  }

  if (pendingVerification) {
    return (
      <div className="flex min-h-screen items-center justify-center px-4">
        <div className="card w-full max-w-sm space-y-3 text-center">
          <div className="text-3xl" aria-hidden>📬</div>
          <h1 className="text-xl font-semibold">Check your email</h1>
          <p className="text-sm text-slate-400">
            We sent a confirmation link to <span className="text-slate-200">{email}</span>.
            Open it to activate your account, then sign in.
          </p>
          <Link href="/login" className="btn-primary w-full">Go to sign in</Link>
        </div>
      </div>
    );
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <div className="w-full max-w-sm">
        <div className="mb-8 text-center">
          <h1 className="text-2xl font-semibold tracking-tight">Create your account</h1>
          <p className="mt-1 text-sm text-slate-400">
            Paper money only — no real funds, real market discipline.
          </p>
        </div>
        <form onSubmit={submit} className="card space-y-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <label className="label" htmlFor="first-name">First name</label>
              <input id="first-name" maxLength={60} autoComplete="given-name" className="input"
                     value={firstName} onChange={(e) => setFirstName(e.target.value)} />
            </div>
            <div>
              <label className="label" htmlFor="last-name">Last name</label>
              <input id="last-name" maxLength={60} autoComplete="family-name" className="input"
                     value={lastName} onChange={(e) => setLastName(e.target.value)} />
            </div>
          </div>
          <div>
            <label className="label" htmlFor="email">Email</label>
            <input id="email" type="email" required autoComplete="email" className="input"
                   value={email} onChange={(e) => setEmail(e.target.value)} />
          </div>
          <div>
            <label className="label" htmlFor="password">Password</label>
            <input id="password" type="password" required autoComplete="new-password" className="input"
                   value={password} onChange={(e) => setPassword(e.target.value)} />
            <p className="mt-1 text-xs text-slate-500">At least 12 characters with letters and digits.</p>
          </div>
          <div>
            <label className="label" htmlFor="confirm">Confirm password</label>
            <input id="confirm" type="password" required autoComplete="new-password" className="input"
                   value={confirm} onChange={(e) => setConfirm(e.target.value)} />
          </div>
          <div>
            <label className="label" htmlFor="dob">Date of birth</label>
            <input id="dob" type="date" required className="input"
                   value={dob} onChange={(e) => setDob(e.target.value)} />
            <p className="mt-1 text-xs text-slate-500">
              Used for IRA catch-up contribution and withdrawal rules.
            </p>
          </div>
          <ErrorText>{error}</ErrorText>
          <button type="submit" disabled={busy} className="btn-primary w-full">
            {busy ? "Creating…" : "Create account"}
          </button>
        </form>
        <p className="mt-4 text-center text-sm text-slate-400">
          Already registered?{" "}
          <Link href="/login" className="font-medium text-emerald-400 hover:text-emerald-300">
            Sign in
          </Link>
        </p>
      </div>
    </div>
  );
}
