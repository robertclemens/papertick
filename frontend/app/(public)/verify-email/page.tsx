"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { Spinner } from "@/components/ui";

function VerifyEmailInner() {
  const params = useSearchParams();
  const token = params.get("token") ?? "";
  const [state, setState] = useState<"working" | "ok" | "changed" | "error">("working");
  const [message, setMessage] = useState("");

  useEffect(() => {
    if (!token) {
      setState("error");
      setMessage("Missing verification token.");
      return;
    }
    api<{ status: string; email: string }>("/auth/verify-email", {
      method: "POST",
      body: { token },
    })
      .then((res) => {
        setMessage(res.email);
        setState(res.status === "email_changed" ? "changed" : "ok");
      })
      .catch((err) => {
        setState("error");
        setMessage(err instanceof ApiError ? err.message : "Verification failed");
      });
  }, [token]);

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <div className="card w-full max-w-sm space-y-3 text-center">
        {state === "working" ? (
          <Spinner />
        ) : state === "error" ? (
          <>
            <div className="text-3xl" aria-hidden>⚠️</div>
            <h1 className="text-xl font-semibold">Verification failed</h1>
            <p className="text-sm text-slate-400">{message}</p>
            <Link href="/login" className="btn-ghost w-full">Back to sign in</Link>
          </>
        ) : (
          <>
            <div className="text-3xl" aria-hidden>✅</div>
            <h1 className="text-xl font-semibold">
              {state === "changed" ? "Email address updated" : "Email verified"}
            </h1>
            <p className="text-sm text-slate-400">
              {state === "changed"
                ? <>Your sign-in email is now <span className="text-slate-200">{message}</span>.</>
                : <><span className="text-slate-200">{message}</span> is confirmed — your account is active.</>}
            </p>
            <Link href="/login" className="btn-primary w-full">Sign in</Link>
          </>
        )}
      </div>
    </div>
  );
}

export default function VerifyEmailPage() {
  return (
    <Suspense fallback={<Spinner />}>
      <VerifyEmailInner />
    </Suspense>
  );
}
