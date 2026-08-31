"""Outbound email. With no SMTP_HOST configured, messages (and their action
links) are written to the backend log instead — handy for development."""

import logging
import smtplib
from email.message import EmailMessage

from app.config import get_settings

log = logging.getLogger("papertick.mailer")


def send_email(to: str, subject: str, body: str) -> bool:
    s = get_settings()
    if not s.smtp_host:
        log.info("EMAIL (SMTP not configured) to=%s subject=%r\n%s", to, subject, body)
        return False
    msg = EmailMessage()
    msg["From"] = s.smtp_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=10) as server:
            if s.smtp_starttls:
                server.starttls()
            if s.smtp_user:
                server.login(s.smtp_user, s.smtp_password)
            server.send_message(msg)
        return True
    except (smtplib.SMTPException, OSError) as exc:
        log.error("failed to send email to %s: %s", to, exc)
        return False


def send_verification_email(to: str, token: str) -> bool:
    url = f"{get_settings().frontend_origin}/verify-email?token={token}"
    return send_email(
        to,
        "Verify your PaperTick account",
        "Welcome to PaperTick!\n\n"
        f"Confirm your email address to activate your account:\n\n{url}\n\n"
        "This link expires in 24 hours. If you didn't create this account, ignore this message.",
    )


def send_email_change_email(to: str, token: str) -> bool:
    url = f"{get_settings().frontend_origin}/verify-email?token={token}"
    return send_email(
        to,
        "Confirm your new PaperTick email address",
        "A request was made to change your PaperTick sign-in email to this address.\n\n"
        f"Confirm the change:\n\n{url}\n\n"
        "This link expires in 24 hours. If you didn't request this, ignore this message.",
    )
