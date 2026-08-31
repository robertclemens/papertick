"use client";

import { api } from "@/lib/api";

function b64uToBuf(s: string): ArrayBuffer {
  const pad = "=".repeat((4 - (s.length % 4)) % 4);
  const raw = atob((s + pad).replace(/-/g, "+").replace(/_/g, "/"));
  const buf = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) buf[i] = raw.charCodeAt(i);
  return buf.buffer;
}

function bufToB64u(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf);
  let s = "";
  for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function passkeysSupported(): boolean {
  return typeof window !== "undefined" && !!window.PublicKeyCredential;
}

/** Adding a passkey re-proves the password (and a TOTP code when enrolled):
 *  a passkey is a second permanent way into the account that a password change
 *  does not revoke, so a merely-borrowed session must not be able to add one. */
export async function registerPasskey(
  nickname: string,
  currentPassword: string,
  code?: string
): Promise<void> {
  const options: any = await api("/auth/passkeys/register/options", {
    method: "POST",
    body: { current_password: currentPassword, code: code || undefined },
  });
  options.challenge = b64uToBuf(options.challenge);
  options.user.id = b64uToBuf(options.user.id);
  options.excludeCredentials = (options.excludeCredentials ?? []).map((c: any) => ({
    ...c,
    id: b64uToBuf(c.id),
  }));
  const cred = (await navigator.credentials.create({ publicKey: options })) as PublicKeyCredential;
  if (!cred) throw new Error("Passkey creation was cancelled");
  const resp = cred.response as AuthenticatorAttestationResponse;
  await api("/auth/passkeys/register/verify", {
    method: "POST",
    body: {
      nickname,
      credential: {
        id: cred.id,
        rawId: bufToB64u(cred.rawId),
        type: cred.type,
        clientExtensionResults: cred.getClientExtensionResults(),
        authenticatorAttachment: (cred as any).authenticatorAttachment ?? undefined,
        response: {
          clientDataJSON: bufToB64u(resp.clientDataJSON),
          attestationObject: bufToB64u(resp.attestationObject),
          transports: (resp as any).getTransports ? (resp as any).getTransports() : [],
        },
      },
    },
  });
}

export interface PasskeyLoginResult {
  mfa_required: boolean;
  mfa_token: string | null;
}

/** Resolves with `mfa_required` set when the account also has TOTP enrolled —
 *  a passkey proves possession of a registered authenticator, not the second
 *  factor the account was configured to demand. */
export async function signInWithPasskey(): Promise<PasskeyLoginResult> {
  const { flow_id, options } = await api<{ flow_id: string; options: any }>(
    "/auth/passkeys/login/options",
    { method: "POST" }
  );
  options.challenge = b64uToBuf(options.challenge);
  options.allowCredentials = (options.allowCredentials ?? []).map((c: any) => ({
    ...c,
    id: b64uToBuf(c.id),
  }));
  const cred = (await navigator.credentials.get({ publicKey: options })) as PublicKeyCredential;
  if (!cred) throw new Error("Passkey sign-in was cancelled");
  const resp = cred.response as AuthenticatorAssertionResponse;
  return await api<PasskeyLoginResult>("/auth/passkeys/login/verify", {
    method: "POST",
    body: {
      flow_id,
      credential: {
        id: cred.id,
        rawId: bufToB64u(cred.rawId),
        type: cred.type,
        clientExtensionResults: cred.getClientExtensionResults(),
        authenticatorAttachment: (cred as any).authenticatorAttachment ?? undefined,
        response: {
          clientDataJSON: bufToB64u(resp.clientDataJSON),
          authenticatorData: bufToB64u(resp.authenticatorData),
          signature: bufToB64u(resp.signature),
          userHandle: resp.userHandle ? bufToB64u(resp.userHandle) : undefined,
        },
      },
    },
  });
}
