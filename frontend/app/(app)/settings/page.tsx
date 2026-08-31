"use client";

import { FormEvent, useEffect, useState } from "react";
import { api, ApiError, MeT, RANGE_LABEL, RANGES, RangeT, ScenarioT } from "@/lib/api";
import { clearRangeCache } from "@/lib/prefs";
import { useScenarios } from "@/components/scenario-context";
import { dateTime, shortDate } from "@/lib/format";
import { passkeysSupported, registerPasskey } from "@/lib/webauthn";
import { Badge, Card, Dialog, Empty, ErrorText, InfoText, Spinner } from "@/components/ui";

interface PasskeyT {
  id: string;
  nickname: string;
  transports: string | null;
  created_at: string;
  last_used_at: string | null;
}

/** base64 for a data: URI, unicode-safe (btoa alone throws on non-Latin-1). */
function qrDataUri(svg: string): string {
  const bytes = new TextEncoder().encode(svg);
  let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin);
}

export default function SettingsPage() {
  // shared with the sidebar switcher, so a default change shows up there at once
  const { scenarios, refresh: refreshScenarios } = useScenarios();
  const [me, setMe] = useState<MeT | null>(null);
  const [passkeys, setPasskeys] = useState<PasskeyT[] | null>(null);

  const [notice, setNotice] = useState("");

  // MFA state
  const [setup, setSetup] = useState<{ otpauth_uri: string; secret: string; qr_svg: string } | null>(null);
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [disabling, setDisabling] = useState(false);
  const [mfaError, setMfaError] = useState("");

  // profile dialogs
  const [emailDialog, setEmailDialog] = useState(false);
  const [newEmail, setNewEmail] = useState("");
  const [emailPassword, setEmailPassword] = useState("");
  const [pwDialog, setPwDialog] = useState(false);
  const [currentPw, setCurrentPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [confirmPw, setConfirmPw] = useState("");
  const [pwError, setPwError] = useState("");
  const [nameDialog, setNameDialog] = useState(false);
  const [firstName, setFirstName] = useState("");
  const [lastName, setLastName] = useState("");
  const [dobDialog, setDobDialog] = useState(false);
  const [newDob, setNewDob] = useState("");
  const [dobWarnings, setDobWarnings] = useState<string[] | null>(null);
  const [profileError, setProfileError] = useState("");

  // passkeys
  const [pkDialog, setPkDialog] = useState(false);
  const [pkNickname, setPkNickname] = useState("");
  const [pkPassword, setPkPassword] = useState("");
  const [pkCode, setPkCode] = useState("");
  const [pkError, setPkError] = useState("");
  const [mfaPassword, setMfaPassword] = useState("");

  const [busy, setBusy] = useState(false);

  function load() {
    api<MeT>("/auth/me").then(setMe).catch(() => {});
    api<PasskeyT[]>("/auth/passkeys").then(setPasskeys).catch(() => setPasskeys([]));
  }
  useEffect(load, []);

  // ------------------------------------------------------------ profile

  async function changeEmail(e: FormEvent) {
    e.preventDefault();
    setProfileError("");
    setBusy(true);
    try {
      const res = await api<{ email_change: string }>("/auth/profile", {
        method: "PATCH",
        body: { email: newEmail, current_password: emailPassword },
      });
      setEmailDialog(false);
      setEmailPassword("");
      setNotice(
        res.email_change === "verification_sent"
          ? `A confirmation link was sent to ${newEmail} — your email changes once you click it.`
          : `Sign-in email changed to ${newEmail}.`
      );
      load();
    } catch (err) {
      setProfileError(err instanceof ApiError ? err.message : "Failed to change email");
    } finally {
      setBusy(false);
    }
  }

  async function changeName(e: FormEvent) {
    e.preventDefault();
    setProfileError("");
    setBusy(true);
    try {
      await api("/auth/profile", {
        method: "PATCH",
        body: { first_name: firstName, last_name: lastName },
      });
      setNameDialog(false);
      setNotice("Name updated.");
      load();
    } catch (err) {
      setProfileError(err instanceof ApiError ? err.message : "Failed to update name");
    } finally {
      setBusy(false);
    }
  }

  async function changeDefaultScenario(next: string) {
    setMe((cur) => (cur ? { ...cur, default_scenario_id: next } : cur));
    try {
      await api(`/scenarios/${next}`, { method: "PATCH", body: { is_default: true } });
      setNotice("That scenario now opens by default when you sign in.");
      refreshScenarios();
      load();
    } catch {
      load();
    }
  }

  async function changeDefaultRange(next: RangeT) {
    setMe((cur) => (cur ? { ...cur, default_range: next } : cur));
    try {
      await api("/auth/profile", { method: "PATCH", body: { default_range: next } });
      clearRangeCache();   // pickers pick it up on their next mount
      setNotice(`Performance now opens on ${RANGE_LABEL[next].toLowerCase()}.`);
    } catch {
      load();
    }
  }

  async function changePassword(e: FormEvent) {
    e.preventDefault();
    setPwError("");
    if (newPw !== confirmPw) {
      setPwError("The new passwords do not match");
      return;
    }
    setBusy(true);
    try {
      await api("/auth/password", {
        method: "POST",
        body: { current_password: currentPw, new_password: newPw },
      });
      setPwDialog(false);
      setCurrentPw("");
      setNewPw("");
      setConfirmPw("");
      setNotice("Password changed — any other signed-in devices have been signed out.");
    } catch (err) {
      setPwError(err instanceof ApiError ? err.message : "Failed to change password");
    } finally {
      setBusy(false);
    }
  }

  async function checkDob(value: string) {
    setNewDob(value);
    setDobWarnings(null);
    if (!value) return;
    try {
      const res = await api<{ warnings: string[] }>(`/auth/profile/dob-impact?date_of_birth=${value}`);
      setDobWarnings(res.warnings);
    } catch { /* validation shows on submit */ }
  }

  async function changeDob(e: FormEvent) {
    e.preventDefault();
    setProfileError("");
    setBusy(true);
    try {
      await api("/auth/profile", {
        method: "PATCH",
        body: { date_of_birth: newDob, confirm_impacts: true },
      });
      setDobDialog(false);
      setNotice("Birthdate updated.");
      load();
    } catch (err) {
      setProfileError(err instanceof ApiError ? err.message : "Failed to update birthdate");
    } finally {
      setBusy(false);
    }
  }

  // ------------------------------------------------------------ passkeys

  async function addPasskey(e: FormEvent) {
    e.preventDefault();
    setPkError("");
    setBusy(true);
    try {
      await registerPasskey(pkNickname || "Passkey", pkPassword, pkCode);
      setPkPassword("");
      setPkCode("");
      setPkDialog(false);
      setPkNickname("");
      setNotice("Passkey added — you can now sign in without a password.");
      load();
    } catch (err) {
      if (err instanceof DOMException && err.name === "NotAllowedError") {
        setPkError("Passkey creation was cancelled.");
      } else {
        setPkError(err instanceof ApiError ? err.message : "Failed to add passkey");
      }
    } finally {
      setBusy(false);
    }
  }

  async function removePasskey(id: string) {
    await api(`/auth/passkeys/${id}`, { method: "DELETE" }).catch(() => {});
    load();
  }

  // ------------------------------------------------------------ MFA

  async function startSetup() {
    setMfaError("");
    try {
      setSetup(await api("/auth/mfa/setup", {
        method: "POST",
        body: { current_password: mfaPassword },
      }));
      setMfaPassword("");
    } catch (err) {
      setMfaError(err instanceof ApiError ? err.message : "Failed to start MFA setup");
    }
  }

  async function enableMfa(e: FormEvent) {
    e.preventDefault();
    setMfaError("");
    setBusy(true);
    try {
      await api("/auth/mfa/enable", { method: "POST", body: { code } });
      setSetup(null);
      setCode("");
      setNotice("Authenticator app enabled — codes are now required at sign-in.");
      load();
    } catch (err) {
      setMfaError(err instanceof ApiError ? err.message : "Invalid code");
    } finally {
      setBusy(false);
    }
  }

  async function disableMfa(e: FormEvent) {
    e.preventDefault();
    setMfaError("");
    setBusy(true);
    try {
      await api("/auth/mfa/disable", { method: "POST", body: { code, password } });
      setDisabling(false);
      setCode("");
      setPassword("");
      setNotice("Authenticator app disabled.");
      load();
    } catch (err) {
      setMfaError(err instanceof ApiError ? err.message : "Failed to disable MFA");
    } finally {
      setBusy(false);
    }
  }

  if (!me) return <Spinner />;

  return (
    <div className="max-w-2xl space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Settings</h1>
        <p className="mt-1 text-sm text-slate-400">
          Profile and sign-in security. MFA is optional — add a passkey, an authenticator app, both, or neither.
        </p>
      </header>

      {notice && <InfoText>{notice}</InfoText>}

      <Card title="Profile">
        <dl className="space-y-2.5 text-sm">
          <div className="flex items-center justify-between">
            <dt className="text-slate-400">Name</dt>
            <dd className="flex items-center gap-3">
              <span className={me.first_name || me.last_name ? "" : "text-slate-500"}>
                {me.first_name || me.last_name ? me.full_name : "Not set"}
              </span>
              <button className="text-xs text-emerald-400 hover:text-emerald-300"
                      onClick={() => {
                        setFirstName(me.first_name ?? "");
                        setLastName(me.last_name ?? "");
                        setProfileError("");
                        setNameDialog(true);
                      }}>
                Change
              </button>
            </dd>
          </div>
          <div className="flex items-center justify-between">
            <dt className="text-slate-400">Email</dt>
            <dd className="flex items-center gap-3">
              {me.email}
              {!me.email_verified && <Badge value="PENDING" />}
              <button className="text-xs text-emerald-400 hover:text-emerald-300"
                      onClick={() => { setEmailDialog(true); setNewEmail(""); setProfileError(""); }}>
                Change
              </button>
            </dd>
          </div>
          {(scenarios?.length ?? 0) > 0 && (
            <div className="flex items-center justify-between gap-3">
              <dt className="text-slate-400">
                Default scenario
                <span className="mt-0.5 block text-xs text-slate-500">
                  The track you land in when you sign in.
                </span>
              </dt>
              <dd>
                <select
                  aria-label="Default scenario"
                  className="input w-44"
                  value={me.default_scenario_id ?? scenarios?.[0]?.id ?? ""}
                  onChange={(e) => changeDefaultScenario(e.target.value)}
                >
                  {(scenarios ?? []).map((s) => (
                    <option key={s.id} value={s.id}>{s.name}</option>
                  ))}
                </select>
              </dd>
            </div>
          )}
          <div className="flex items-center justify-between gap-3">
            <dt className="text-slate-400">
              Default performance timeframe
              <span className="mt-0.5 block text-xs text-slate-500">
                The window the dashboard, accounts and history open on.
              </span>
            </dt>
            <dd>
              <select
                aria-label="Default performance timeframe"
                className="input w-44"
                value={me.default_range}
                onChange={(e) => changeDefaultRange(e.target.value as RangeT)}
              >
                {RANGES.map((r) => (
                  <option key={r} value={r}>{RANGE_LABEL[r]}</option>
                ))}
              </select>
            </dd>
          </div>
          <div className="flex items-center justify-between">
            <dt className="text-slate-400">Password</dt>
            <dd className="flex items-center gap-3">
              <span className="text-slate-500">••••••••••••</span>
              <button className="text-xs text-emerald-400 hover:text-emerald-300"
                      onClick={() => {
                        setCurrentPw("");
                        setNewPw("");
                        setConfirmPw("");
                        setPwError("");
                        setPwDialog(true);
                      }}>
                Change
              </button>
            </dd>
          </div>
          <div className="flex items-center justify-between">
            <dt className="text-slate-400">Date of birth</dt>
            <dd className="flex items-center gap-3">
              {shortDate(me.date_of_birth)}
              <button className="text-xs text-emerald-400 hover:text-emerald-300"
                      onClick={() => { setDobDialog(true); setNewDob(me.date_of_birth); setDobWarnings(null); setProfileError(""); }}>
                Change
              </button>
            </dd>
          </div>
          <div className="flex justify-between"><dt className="text-slate-400">Member since</dt><dd>{shortDate(me.created_at)}</dd></div>
        </dl>
      </Card>

      <Card title="Passkeys" action={passkeys && passkeys.length > 0 ? <Badge value="ACTIVE" /> : undefined}>
        <p className="mb-3 text-sm text-slate-400">
          A passkey signs you in with your device&apos;s fingerprint, face, or PIN — no password,
          phishing-resistant, and multi-factor by design.
        </p>
        {!passkeysSupported() ? (
          <InfoText>This browser does not support passkeys.</InfoText>
        ) : (
          <>
            {!passkeys ? (
              <Spinner />
            ) : passkeys.length === 0 ? (
              <Empty>No passkeys yet.</Empty>
            ) : (
              <ul className="mb-3 divide-y divide-slate-800/60">
                {passkeys.map((p) => (
                  <li key={p.id} className="flex items-center justify-between py-2">
                    <div>
                      <div className="text-sm font-medium text-slate-100">
                        <span aria-hidden>🔑</span> {p.nickname}
                      </div>
                      <div className="text-xs text-slate-500">
                        added {shortDate(p.created_at)} · last used {p.last_used_at ? dateTime(p.last_used_at) : "never"}
                      </div>
                    </div>
                    <button className="text-xs text-red-400 hover:text-red-300"
                            onClick={() => removePasskey(p.id)}>
                      Remove
                    </button>
                  </li>
                ))}
              </ul>
            )}
            <button className="btn-primary" onClick={() => { setPkDialog(true); setPkError(""); }}>
              Add passkey
            </button>
          </>
        )}
      </Card>

      <Card
        title="Authenticator app (TOTP)"
        action={me.mfa_enabled ? <Badge value="ACTIVE" /> : undefined}
      >
        {!me.mfa_enabled && !setup && (
          <form className="space-y-3" onSubmit={(e) => { e.preventDefault(); startSetup(); }}>
            <p className="text-sm text-slate-400">
              Optionally require a 6-digit code from an authenticator app when signing in with your password.
            </p>
            <div>
              <label className="label" htmlFor="mfa-pw">Confirm your password</label>
              <input id="mfa-pw" type="password" required autoComplete="current-password"
                     className="input max-w-xs" value={mfaPassword}
                     onChange={(e) => setMfaPassword(e.target.value)} />
            </div>
            <button type="submit" className="btn-ghost">Set up authenticator</button>
          </form>
        )}
        {setup && (
          <form onSubmit={enableMfa} className="space-y-4">
            <p className="text-sm text-slate-400">
              Scan the QR code with your authenticator app, then enter the 6-digit code to confirm.
            </p>
            <div className="flex items-start gap-4">
              {/* Rendered as an image, not injected as markup: an <img> with an
                SVG source cannot run script, so this stays inert even if the
                upstream generator ever changes. */}
            <img className="shrink-0 rounded-lg bg-white p-2"
                 width={160} height={160}
                 src={`data:image/svg+xml;base64,${qrDataUri(setup.qr_svg)}`}
                 alt="MFA enrollment QR code" />
              <div className="min-w-0 text-xs text-slate-400">
                <div className="label">Manual entry secret</div>
                <code className="break-all text-emerald-300">{setup.secret}</code>
              </div>
            </div>
            <div>
              <label className="label" htmlFor="mfa-code">Verification code</label>
              <input id="mfa-code" inputMode="numeric" maxLength={8} required
                     className="input max-w-40 text-center tracking-[0.3em]"
                     value={code} onChange={(e) => setCode(e.target.value)} />
            </div>
            <ErrorText>{mfaError}</ErrorText>
            <div className="flex gap-2">
              <button type="submit" disabled={busy} className="btn-primary">
                {busy ? "Verifying…" : "Confirm & enable"}
              </button>
              <button type="button" className="btn-ghost" onClick={() => setSetup(null)}>Cancel</button>
            </div>
          </form>
        )}
        {me.mfa_enabled && (
          <div className="space-y-3">
            <p className="text-sm text-slate-400">
              A code from your authenticator app is required at every password sign-in.
            </p>
            <button className="btn-danger" onClick={() => { setDisabling(true); setMfaError(""); }}>
              Disable authenticator
            </button>
          </div>
        )}
        {!setup && !me.mfa_enabled && <ErrorText>{mfaError}</ErrorText>}
      </Card>

      {/* ------------------------------------------------ dialogs */}

      <Dialog open={pwDialog} title="Change password" onClose={() => setPwDialog(false)}>
        <form onSubmit={changePassword} className="space-y-4">
          <div>
            <label className="label" htmlFor="pw-current">Current password</label>
            <input id="pw-current" type="password" required autoComplete="current-password"
                   className="input" value={currentPw}
                   onChange={(e) => setCurrentPw(e.target.value)} />
          </div>
          <div>
            <label className="label" htmlFor="pw-new">New password</label>
            <input id="pw-new" type="password" required autoComplete="new-password"
                   className="input" value={newPw} onChange={(e) => setNewPw(e.target.value)} />
            <p className="mt-1 text-xs text-slate-500">At least 12 characters with letters and digits.</p>
          </div>
          <div>
            <label className="label" htmlFor="pw-confirm">Confirm new password</label>
            <input id="pw-confirm" type="password" required autoComplete="new-password"
                   className="input" value={confirmPw}
                   onChange={(e) => setConfirmPw(e.target.value)} />
          </div>
          <p className="text-xs text-slate-500">
            Changing your password signs out every other device; this one stays signed in.
          </p>
          <ErrorText>{pwError}</ErrorText>
          <button type="submit" disabled={busy} className="btn-primary w-full">
            {busy ? "Saving…" : "Change password"}
          </button>
        </form>
      </Dialog>

      <Dialog open={nameDialog} title="Change your name" onClose={() => setNameDialog(false)}>
        <form onSubmit={changeName} className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <label className="label" htmlFor="pn-first">First name</label>
              <input id="pn-first" maxLength={60} className="input" autoComplete="given-name"
                     value={firstName} onChange={(e) => setFirstName(e.target.value)} />
            </div>
            <div>
              <label className="label" htmlFor="pn-last">Last name</label>
              <input id="pn-last" maxLength={60} className="input" autoComplete="family-name"
                     value={lastName} onChange={(e) => setLastName(e.target.value)} />
            </div>
          </div>
          <p className="text-xs text-slate-500">
            Shown in the sidebar above Sign out. Leave both blank to fall back to your email.
          </p>
          <ErrorText>{profileError}</ErrorText>
          <button type="submit" disabled={busy} className="btn-primary w-full">
            {busy ? "Saving…" : "Save name"}
          </button>
        </form>
      </Dialog>

      <Dialog open={emailDialog} title="Change email address" onClose={() => setEmailDialog(false)}>
        <form onSubmit={changeEmail} className="space-y-4">
          <div>
            <label className="label" htmlFor="pe-email">New email address</label>
            <input id="pe-email" type="email" required className="input"
                   value={newEmail} onChange={(e) => setNewEmail(e.target.value)} />
          </div>
          <div>
            <label className="label" htmlFor="pe-pass">Current password</label>
            <input id="pe-pass" type="password" required autoComplete="current-password" className="input"
                   value={emailPassword} onChange={(e) => setEmailPassword(e.target.value)} />
          </div>
          <p className="text-xs text-slate-500">
            In production a confirmation link is sent to the new address; the change applies once clicked.
          </p>
          <ErrorText>{profileError}</ErrorText>
          <button type="submit" disabled={busy} className="btn-primary w-full">
            {busy ? "Saving…" : "Change email"}
          </button>
        </form>
      </Dialog>

      <Dialog open={dobDialog} title="Change date of birth" onClose={() => setDobDialog(false)}>
        <form onSubmit={changeDob} className="space-y-4">
          <div>
            <label className="label" htmlFor="pd-dob">Date of birth</label>
            <input id="pd-dob" type="date" required className="input"
                   value={newDob} onChange={(e) => checkDob(e.target.value)} />
          </div>
          {dobWarnings === null && newDob !== me.date_of_birth && newDob !== "" && (
            <p className="text-xs text-slate-500">Checking impact on your contribution history…</p>
          )}
          {dobWarnings && dobWarnings.length > 0 && (
            <div className="space-y-2">
              {dobWarnings.map((w) => <InfoText key={w}>{w}</InfoText>)}
            </div>
          )}
          {dobWarnings && dobWarnings.length === 0 && newDob !== me.date_of_birth && (
            <p className="text-xs text-slate-500">
              No impact on your IRA limits or contribution history.
            </p>
          )}
          <ErrorText>{profileError}</ErrorText>
          <button type="submit" disabled={busy || newDob === me.date_of_birth} className="btn-primary w-full">
            {busy ? "Saving…" : dobWarnings && dobWarnings.length > 0 ? "I understand — update birthdate" : "Update birthdate"}
          </button>
        </form>
      </Dialog>

      <Dialog open={pkDialog} title="Add a passkey" onClose={() => setPkDialog(false)}>
        <form onSubmit={addPasskey} className="space-y-4">
          <p className="text-sm text-slate-400">
            Your browser will ask you to confirm with this device&apos;s screen lock, security key,
            or a phone. Give the passkey a name so you can recognize it later.
          </p>
          <div>
            <label className="label" htmlFor="pk-name">Passkey name</label>
            <input id="pk-name" maxLength={100} className="input" placeholder="e.g. Work laptop"
                   value={pkNickname} onChange={(e) => setPkNickname(e.target.value)} />
          </div>
          <div>
            <label className="label" htmlFor="pk-pw">Confirm your password</label>
            <input id="pk-pw" type="password" required autoComplete="current-password"
                   className="input" value={pkPassword}
                   onChange={(e) => setPkPassword(e.target.value)} />
          </div>
          {me.mfa_enabled && (
            <div>
              <label className="label" htmlFor="pk-code">Authenticator code</label>
              <input id="pk-code" inputMode="numeric" maxLength={8} required className="input"
                     value={pkCode} onChange={(e) => setPkCode(e.target.value)} />
            </div>
          )}
          <ErrorText>{pkError}</ErrorText>
          <button type="submit" disabled={busy} className="btn-primary w-full">
            {busy ? "Follow your browser's prompt…" : "Create passkey"}
          </button>
        </form>
      </Dialog>

      <Dialog open={disabling} title="Disable authenticator app" onClose={() => setDisabling(false)}>
        <form onSubmit={disableMfa} className="space-y-4">
          <InfoText>Confirm with your password and a current code.</InfoText>
          <div>
            <label className="label" htmlFor="d-password">Password</label>
            <input id="d-password" type="password" required autoComplete="current-password" className="input"
                   value={password} onChange={(e) => setPassword(e.target.value)} />
          </div>
          <div>
            <label className="label" htmlFor="d-code">Authenticator code</label>
            <input id="d-code" inputMode="numeric" maxLength={8} required className="input"
                   value={code} onChange={(e) => setCode(e.target.value)} />
          </div>
          <ErrorText>{mfaError}</ErrorText>
          <button type="submit" disabled={busy} className="btn-danger w-full">
            {busy ? "Disabling…" : "Disable"}
          </button>
        </form>
      </Dialog>
    </div>
  );
}
