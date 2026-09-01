"""Outbound email. With no SMTP_HOST configured, messages (and their action
links) are written to the backend log instead — handy for development."""

import logging
import smtplib
import ssl
from email.message import EmailMessage

from app.config import get_settings

log = logging.getLogger("papertick.mailer")


def send_email(to: str, subject: str, body: str) -> bool:
    s = get_settings()
    if not s.smtp_host:
        # The body carries a verification link, and that link is a 24-hour
        # bearer credential that can verify an account or rewrite its sign-in
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


def send_verification_email(to: str, token: str) -> bool:
    url = f"{get_settings().app_url}/verify-email?token={token}"
    return send_email(
        to,
        "Verify your PaperTick account",
        "Welcome to PaperTick!\n\n"
        f"Confirm your email address to activate your account:\n\n{url}\n\n"
        "This link expires in 24 hours. If you didn't create this account, ignore this message.",
    )


def send_email_change_email(to: str, token: str) -> bool:
    url = f"{get_settings().app_url}/verify-email?token={token}"
    return send_email(
        to,
        "Confirm your new PaperTick email address",
        "A request was made to change your PaperTick sign-in email to this address.\n\n"
        f"Confirm the change:\n\n{url}\n\n"
        "This link expires in 24 hours. If you didn't request this, ignore this message.",
    )
