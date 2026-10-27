"""Send replies to customers by email over SMTP."""

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.config import Settings


class MailError(RuntimeError):
    pass


class Mailer:
    def __init__(
        self,
        host: str,
        port: int,
        sender: str,
        username: str = "",
        password: str = "",
        starttls: bool = True,
        timeout: float = 15.0,
    ) -> None:
        self.host = host
        self.port = port
        self.sender = sender
        self.username = username
        self.password = password
        self.starttls = starttls
        self.timeout = timeout

    def build(self, to: str, subject: str, body: str, reference: str | None = None) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = to
        msg["Subject"] = subject
        msg["Message-ID"] = make_msgid(domain=self.sender.rpartition("@")[2] or None)
        if reference:
            # Lets mail clients thread follow-ups about the same ticket.
            msg["X-SupportFlow-Ticket"] = reference
        msg.set_content(body)
        return msg

    def send(self, to: str, subject: str, body: str, reference: str | None = None) -> str:
        """Send the email; returns its Message-ID. Raises MailError on any failure."""
        msg = self.build(to, subject, body, reference)
        try:
            if self.port == 465:
                smtp: smtplib.SMTP = smtplib.SMTP_SSL(
                    self.host, self.port, timeout=self.timeout, context=ssl.create_default_context()
                )
            else:
                smtp = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
            with smtp:
                if self.starttls and self.port != 465:
                    smtp.starttls(context=ssl.create_default_context())
                if self.username:
                    smtp.login(self.username, self.password)
                smtp.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            raise MailError(f"could not send email to {to}: {exc}") from exc
        return msg["Message-ID"]


def build_mailer(settings: "Settings") -> Mailer | None:
    if not (settings.smtp_host and settings.smtp_from):
        return None
    return Mailer(
        host=settings.smtp_host,
        port=settings.smtp_port,
        sender=settings.smtp_from,
        username=settings.smtp_username,
        password=settings.smtp_password,
        starttls=settings.smtp_starttls,
    )
