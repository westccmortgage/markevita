"""Email delivery for production notifications.

The studio's only notification channel was a browser push subscription: it
needs a registered service worker, a granted permission and, on several
platforms, the browser still running. A producer who switched notifications on
and then closed the tab was told nothing, for hours, while an episode sat
stopped. Email reaches them where they actually are.

Two transports, whichever is configured: Resend's HTTP API, or any SMTP
server. Neither is required — with no transport the studio says so on the
Integrations screen rather than pretending to deliver.
"""
from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def sender_address() -> str:
    return _env("NOTIFICATION_FROM") or _env("STUDIO_ADMIN_EMAIL")


def transport() -> str:
    """Which way mail would go out, or '' when nothing is configured."""
    if _env("RESEND_API_KEY") and sender_address():
        return "resend"
    if _env("SMTP_HOST") and sender_address():
        return "smtp"
    return ""


def configuration_problem() -> str:
    if transport():
        return ""
    if not sender_address():
        return ("Email notifications need a from-address: set NOTIFICATION_FROM "
                "(or STUDIO_ADMIN_EMAIL) on the server.")
    return ("Email notifications need a transport: set RESEND_API_KEY, or SMTP_HOST "
            "with SMTP_PORT, SMTP_USER and SMTP_PASSWORD.")


def _resend(to: str, subject: str, body: str) -> None:
    import requests
    response = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": "Bearer " + _env("RESEND_API_KEY"),
                 "Content-Type": "application/json"},
        json={"from": sender_address(), "to": [to], "subject": subject, "text": body},
        timeout=30)
    response.raise_for_status()


def _smtp(to: str, subject: str, body: str) -> None:
    message = EmailMessage()
    message["From"] = sender_address()
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    port = int(_env("SMTP_PORT") or 587)
    host = _env("SMTP_HOST")
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=30, context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(host, port, timeout=30)
    with server:
        if port != 465:
            try:
                server.starttls(context=ssl.create_default_context())
            except smtplib.SMTPNotSupportedError:
                pass   # a local relay without TLS is still a valid transport
        if _env("SMTP_USER"):
            server.login(_env("SMTP_USER"), _env("SMTP_PASSWORD"))
        server.send_message(message)


def deliver(to: str, subject: str, body: str) -> None:
    """Send one message. Raises on failure so the caller can retry later."""
    how = transport()
    if not how:
        raise RuntimeError(configuration_problem())
    if not to:
        raise RuntimeError("No recipient for this notification.")
    (_resend if how == "resend" else _smtp)(to, subject, body)
