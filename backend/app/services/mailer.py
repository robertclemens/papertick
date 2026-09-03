"""Outbound email: a small branded template, and the messages built on it.

Every message is sent as multipart/alternative — a plain-text part that reads
properly on its own, and an HTML part for clients that want one. The HTML is
deliberately old-fashioned (tables, inline styles, no external assets, no web
fonts): that is what survives Gmail, Outlook and Apple Mail intact. Nothing is
loaded from the network, so opening a PaperTick email cannot report back that
it was opened.

Security messages are the reason this file is more than `smtplib`. A notice
that something changed is only useful if the reader can tell whether it was
them, so each one states *what* changed — both sides of it, for an email
change — along with when, from which IP, and on what kind of device. With no
SMTP_HOST configured, messages are logged (metadata only, never the body:
these carry action links that are bearer credentials).
"""

import logging
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage

from app.config import get_settings

log = logging.getLogger("papertick.mailer")

BRAND = "PaperTick"

# Palette, kept in step with the app's own. Light-ground: a dark email body is
# still a coin flip across clients, and a readable message beats a matching one.
INK = "#0f172a"
INK_MUTED = "#64748b"
ACCENT = "#059669"
ACCENT_INK = "#ffffff"
BORDER = "#e2e8f0"
PANEL = "#f8fafc"
ALERT = "#b91c1c"


def _esc(v: object) -> str:
    return (str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def utc_stamp(when: datetime | None = None) -> str:
    when = when or datetime.now(timezone.utc)
    return when.strftime("%d %b %Y at %H:%M UTC")


def render(
    *,
    heading: str,
    lede: str,
    rows: list[tuple[str, str]] | None = None,
    action: tuple[str, str] | None = None,
    action_note: str | None = None,
    closing: str | None = None,
    alert: bool = False,
) -> tuple[str, str]:
    """Build one message as (plain_text, html).

    `rows` are the facts of the event, rendered as a label/value table — the
    part a reader actually needs to decide whether to worry. `action` is
    (label, url); the URL is always printed in full in both parts, because a
    button whose destination cannot be read is exactly what a phishing email
    looks like.
    """
    rows = rows or []
    tone = ALERT if alert else ACCENT

    # ------------------------------------------------------------ plain text
    text = [heading, "=" * len(heading), "", lede, ""]
    if rows:
        width = max(len(label) for label, _ in rows)
        text += [f"{label.ljust(width)}  {value}" for label, value in rows]
        text.append("")
    if action:
        label, url = action
        text += [f"{label}:", url, ""]
        if action_note:
            text += [action_note, ""]
    if closing:
        text += [closing, ""]
    text.append(f"— {BRAND}")
    plain = "\n".join(text)

    # ------------------------------------------------------------------ html
    row_html = "".join(
        f'<tr>'
        f'<td style="padding:6px 16px 6px 0;color:{INK_MUTED};font-size:13px;'
        f'white-space:nowrap;vertical-align:top">{_esc(label)}</td>'
        f'<td style="padding:6px 0;color:{INK};font-size:13px;font-weight:600;'
        f'vertical-align:top">{_esc(value)}</td>'
        f'</tr>'
        for label, value in rows
    )
    facts = (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'style="width:100%;margin:0 0 20px;background:{PANEL};border:1px solid {BORDER};'
        f'border-radius:8px;padding:10px 14px">{row_html}</table>'
        if rows else ""
    )

    button = ""
    if action:
        label, url = action
        button = (
            f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
            f'style="margin:0 0 12px"><tr><td style="border-radius:8px;background:{tone}">'
            f'<a href="{_esc(url)}" style="display:inline-block;padding:11px 22px;'
            f'font-size:14px;font-weight:600;color:{ACCENT_INK};text-decoration:none;'
            f'border-radius:8px">{_esc(label)}</a></td></tr></table>'
            f'<p style="margin:0 0 20px;font-size:12px;line-height:1.5;color:{INK_MUTED}">'
            f'Or paste this into your browser:<br>'
            f'<span style="word-break:break-all;color:{INK}">{_esc(url)}</span></p>'
        )
        if action_note:
            button += (f'<p style="margin:0 0 20px;font-size:13px;line-height:1.6;'
                       f'color:{INK_MUTED}">{_esc(action_note)}</p>')

    closing_html = (
        f'<p style="margin:0 0 8px;font-size:13px;line-height:1.6;color:{INK_MUTED}">'
        f'{_esc(closing)}</p>' if closing else ""
    )

    html = f"""<!doctype html>
<html><body style="margin:0;padding:0;background:#f1f5f9">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width:100%;background:#f1f5f9">
<tr><td align="center" style="padding:28px 12px">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width:100%;max-width:520px;background:#ffffff;border:1px solid {BORDER};border-radius:12px">
<tr><td style="padding:22px 28px 0">
  <span style="display:inline-block;width:26px;height:26px;line-height:26px;text-align:center;background:{ACCENT};color:#ffffff;border-radius:7px;font:700 14px/26px -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif">P</span>
  <span style="margin-left:8px;font:600 16px/26px -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:{INK};vertical-align:top">Paper<span style="color:{ACCENT}">Tick</span></span>
</td></tr>
<tr><td style="padding:16px 28px 26px;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif">
  <h1 style="margin:0 0 10px;font-size:18px;line-height:1.35;color:{INK}">{_esc(heading)}</h1>
  <p style="margin:0 0 18px;font-size:14px;line-height:1.6;color:{INK}">{_esc(lede)}</p>
  {facts}
  {button}
  {closing_html}
</td></tr>
<tr><td style="padding:0 28px 22px;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif">
  <p style="margin:0;padding-top:14px;border-top:1px solid {BORDER};font-size:11px;line-height:1.6;color:{INK_MUTED}">
    {BRAND} is a paper-trading simulator. No real money, securities or orders are involved.<br>
    This message was sent because of activity on your account. We never ask for your password by email.
  </p>
</td></tr>
</table></td></tr></table></body></html>"""
    return plain, html


def send_email(to: str, subject: str, body: str, html: str | None = None) -> bool:
    s = get_settings()
    if not s.smtp_host:
        # The body carries an action link, and that link is a bearer credential
        # that can verify an account, reset its password or rewrite its sign-in
        # address. Logging it hands account takeover to anyone who can read the
        # container logs, so only metadata is recorded — and in production a
        # missing relay is an error, not a fallback.
        if s.is_production:
            log.error("SMTP is not configured — could not deliver %r to %s", subject, to)
        else:
            log.info("EMAIL (dev, SMTP not configured) to=%s subject=%r "
                     "[body withheld: contains an action token]", to, subject)
        return False
    msg = EmailMessage()
    msg["From"] = s.smtp_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    try:
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=10) as server:
            if s.smtp_starttls:
                # `starttls()` with no context uses ssl._create_stdlib_context(),
                # which sets check_hostname=False and verify_mode=CERT_NONE — an
                # encrypted channel to whoever answers. Anyone in path could
                # otherwise read the SMTP password and every verification link.
                server.starttls(context=ssl.create_default_context())
            if s.smtp_user:
                server.login(s.smtp_user, s.smtp_password)
            server.send_message(msg)
        return True
    except (smtplib.SMTPException, ssl.SSLError, OSError) as exc:
        # Fail closed and stay quiet about the cause beyond the class of error.
        log.error("failed to send email to %s: %s", to, exc.__class__.__name__)
        return False


def send_rendered(to: str, subject: str, **kwargs) -> bool:
    plain, html = render(**kwargs)
    return send_email(to, subject, plain, html)


# --------------------------------------------------------------- account setup

def send_verification_email(to: str, token: str) -> bool:
    url = f"{get_settings().app_url}/verify-email?token={token}"
    return send_rendered(
        to, f"Verify your {BRAND} account",
        heading="Confirm your email address",
        lede="Your account is ready as soon as you confirm this address.",
        action=("Verify my email", url),
        action_note="This link expires in 24 hours.",
        closing="If you didn't create this account, ignore this message — nothing happens until the link is used.",
    )


def send_email_change_email(to: str, token: str, old_email: str) -> bool:
    """Sent to the *new* address: it is the one that has to prove it is real."""
    url = f"{get_settings().app_url}/verify-email?token={token}"
    return send_rendered(
        to, f"Confirm your new {BRAND} email address",
        heading="Confirm your new email address",
        lede=f"A request was made to change the {BRAND} sign-in email to this address.",
        rows=[("Current address", old_email), ("New address", to), ("Requested", utc_stamp())],
        action=("Confirm the change", url),
        action_note="This link expires in 24 hours. Your sign-in address does not change until it is used.",
        closing="If you didn't request this, ignore this message and consider changing your password.",
    )


# ------------------------------------------------------------- password reset

def send_password_reset_email(to: str, token: str, ip: str, device: str,
                              minutes: int) -> bool:
    url = f"{get_settings().app_url}/reset-password?token={token}"
    return send_rendered(
        to, f"Reset your {BRAND} password",
        heading="Reset your password",
        lede=("Someone asked to reset the password on this account. Use the button "
              "below to choose a new one."),
        rows=[("Requested", utc_stamp()), ("IP address", ip), ("Device", device)],
        action=("Choose a new password", url),
        action_note=(f"This link works once and expires in {minutes} minutes. Signing in "
                     "with your existing password also cancels it."),
        closing=("If you didn't ask for this, no action is needed — your password has not "
                 "changed and this link can only be used from your inbox."),
    )
