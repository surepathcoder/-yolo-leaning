"""Notification channel abstractions.

No provider is hardcoded. Each channel is an interface with a local
implementation that records what *would* have been sent, so the whole alert
path is exercisable end to end before any provider contract is signed — which
matters in this market, where the SMS and WhatsApp providers differ per
country and per customer.

A recorded delivery is **not** a pretended one: :class:`LocalNotifier` writes a
``Notification`` row with ``status=SENT`` and a provider id of ``local:...``,
and the dashboard shows the channel as locally recorded. Nothing claims an SMS
reached a phone.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.core.logging import get_logger
from app.models.enums import NotificationChannel

log = get_logger(__name__)


@dataclass(slots=True)
class NotificationMessage:
    """What to send, independent of how."""

    subject: str
    body: str
    #: Email address, phone number or webhook URL.
    destination: str
    severity: str = "INFO"
    #: Structured payload for webhooks and rich clients.
    data: dict = field(default_factory=dict)
    alert_id: uuid.UUID | None = None
    organization_id: uuid.UUID | None = None


@dataclass(slots=True)
class DeliveryResult:
    """Outcome of one delivery attempt."""

    success: bool
    provider_message_id: str | None = None
    error: str | None = None
    #: True when the failure may succeed on a retry (timeout, 5xx) rather than
    #: being permanent (malformed address). Permanent failures must not be
    #: retried -- that just burns quota and delays the next real alert.
    retryable: bool = False


class Notifier(ABC):
    """One delivery channel."""

    channel: NotificationChannel

    @abstractmethod
    def send(self, message: NotificationMessage) -> DeliveryResult:
        """Attempt delivery. Must not raise for an ordinary failure."""

    def is_configured(self) -> bool:
        """Whether this channel has what it needs to actually deliver."""
        return True

    def validate_destination(self, destination: str) -> bool:
        return bool(destination)


class LocalNotifier(Notifier):
    """Records deliveries locally instead of contacting a provider.

    The default in development and wherever a real provider is not configured.
    It is honest about what it is: the resulting notification row is marked
    ``local:``, so nobody can mistake a recorded message for a delivered one.
    """

    def __init__(self, channel: NotificationChannel) -> None:
        self.channel = channel
        self.sent: list[NotificationMessage] = []

    def send(self, message: NotificationMessage) -> DeliveryResult:
        self.sent.append(message)
        log.info(
            "notification_recorded_locally",
            channel=self.channel.value,
            destination=message.destination,
            severity=message.severity,
            subject=message.subject[:80],
            detail="no provider configured for this channel",
        )
        return DeliveryResult(
            success=True,
            provider_message_id=f"local:{uuid.uuid4().hex[:12]}",
        )

    def is_configured(self) -> bool:
        # Deliberately reports False: the alert service uses this to tell the
        # dashboard that the channel is not really wired up.
        return False


class EmailNotifier(Notifier):
    """SMTP email.

    Not wired to a provider yet. Left as an explicit, documented placeholder
    rather than a fake success: it reports ``is_configured() == False`` and the
    alert service falls back to local recording, so the alert still reaches the
    dashboard and nothing silently disappears.
    """

    channel = NotificationChannel.EMAIL

    def __init__(
        self,
        *,
        host: str = "",
        port: int = 587,
        username: str = "",
        password: str = "",
        from_address: str = "",
        use_tls: bool = True,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self._password = password
        self.from_address = from_address
        self.use_tls = use_tls

    def is_configured(self) -> bool:
        return bool(self.host and self.from_address)

    def validate_destination(self, destination: str) -> bool:
        return "@" in destination and "." in destination.split("@")[-1]

    def send(self, message: NotificationMessage) -> DeliveryResult:
        if not self.is_configured():
            return DeliveryResult(
                success=False,
                error="SMTP is not configured",
                retryable=False,
            )
        if not self.validate_destination(message.destination):
            return DeliveryResult(success=False, error="invalid email address", retryable=False)

        import smtplib
        from email.message import EmailMessage

        email = EmailMessage()
        email["Subject"] = message.subject
        email["From"] = self.from_address
        email["To"] = message.destination
        email.set_content(message.body)

        try:
            with smtplib.SMTP(self.host, self.port, timeout=15) as smtp:
                if self.use_tls:
                    smtp.starttls()
                if self.username:
                    smtp.login(self.username, self._password)
                smtp.send_message(email)
        except smtplib.SMTPRecipientsRefused as exc:
            return DeliveryResult(success=False, error=str(exc)[:200], retryable=False)
        except Exception as exc:
            # Network and server errors are worth retrying; the caller decides.
            log.warning("email_delivery_failed", error=str(exc)[:200])
            return DeliveryResult(success=False, error=str(exc)[:200], retryable=True)

        return DeliveryResult(success=True, provider_message_id=email["Message-ID"])


class SMSNotifier(Notifier):
    """SMS via an HTTP gateway.

    Deliberately generic. Tanzanian deployments use several gateways
    (Beem, NextSMS, Africa's Talking, an operator's own API) and hardcoding
    one would make the product unsellable to customers using another.
    """

    channel = NotificationChannel.SMS

    def __init__(
        self, *, api_url: str = "", api_key: str = "", sender_id: str = "VisionGuard"
    ) -> None:
        self.api_url = api_url
        self._api_key = api_key
        self.sender_id = sender_id

    def is_configured(self) -> bool:
        return bool(self.api_url and self._api_key)

    def validate_destination(self, destination: str) -> bool:
        digits = destination.lstrip("+").replace(" ", "")
        return digits.isdigit() and 9 <= len(digits) <= 15

    def send(self, message: NotificationMessage) -> DeliveryResult:
        if not self.is_configured():
            return DeliveryResult(
                success=False, error="SMS gateway is not configured", retryable=False
            )
        if not self.validate_destination(message.destination):
            return DeliveryResult(success=False, error="invalid phone number", retryable=False)

        import httpx

        # SMS is charged per 160-character segment. Alert bodies are trimmed so
        # a verbose event description cannot quietly triple a customer's bill.
        text = f"{message.subject}\n{message.body}"[:320]
        try:
            response = httpx.post(
                self.api_url,
                json={
                    "to": message.destination,
                    "from": self.sender_id,
                    "message": text,
                },
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=15,
            )
            if response.status_code >= 500:
                return DeliveryResult(
                    success=False, error=f"gateway {response.status_code}", retryable=True
                )
            if response.status_code >= 400:
                return DeliveryResult(
                    success=False, error=f"gateway {response.status_code}", retryable=False
                )
            payload = response.json() if response.content else {}
            return DeliveryResult(
                success=True, provider_message_id=str(payload.get("id", ""))[:200] or None
            )
        except Exception as exc:
            log.warning("sms_delivery_failed", error=str(exc)[:200])
            return DeliveryResult(success=False, error=str(exc)[:200], retryable=True)


class WhatsAppNotifier(SMSNotifier):
    """WhatsApp Business API.

    Shares the SMS shape because the gateways in this market usually expose
    both behind one HTTP contract. WhatsApp matters here specifically: it is
    the messaging default across East Africa, and an alert on WhatsApp gets
    read where an email does not.
    """

    channel = NotificationChannel.WHATSAPP


class WebhookNotifier(Notifier):
    """POSTs the structured event to a customer's endpoint.

    The most useful channel for integrators: it carries the full event payload
    rather than prose, so a customer can drive their own systems from it.
    """

    channel = NotificationChannel.WEBHOOK

    def __init__(self, *, timeout: float = 10.0, secret: str = "") -> None:
        self.timeout = timeout
        self._secret = secret

    def validate_destination(self, destination: str) -> bool:
        return destination.startswith(("http://", "https://"))

    def send(self, message: NotificationMessage) -> DeliveryResult:
        if not self.validate_destination(message.destination):
            return DeliveryResult(success=False, error="invalid webhook URL", retryable=False)

        import hashlib
        import hmac
        import json

        import httpx

        payload = {
            "subject": message.subject,
            "body": message.body,
            "severity": message.severity,
            "alert_id": str(message.alert_id) if message.alert_id else None,
            "sent_at": datetime.now(UTC).isoformat(),
            "data": message.data,
        }
        body = json.dumps(payload, separators=(",", ":"))
        headers = {"Content-Type": "application/json"}
        if self._secret:
            # Lets the receiver verify the call really came from VisionGuard
            # rather than anyone who guessed the URL.
            headers["X-VisionGuard-Signature"] = hmac.new(
                self._secret.encode(), body.encode(), hashlib.sha256
            ).hexdigest()

        try:
            response = httpx.post(
                message.destination, content=body, headers=headers, timeout=self.timeout
            )
            if response.status_code >= 500:
                return DeliveryResult(
                    success=False, error=f"HTTP {response.status_code}", retryable=True
                )
            if response.status_code >= 400:
                return DeliveryResult(
                    success=False, error=f"HTTP {response.status_code}", retryable=False
                )
            return DeliveryResult(success=True, provider_message_id=str(response.status_code))
        except Exception as exc:
            log.warning("webhook_delivery_failed", error=str(exc)[:200])
            return DeliveryResult(success=False, error=str(exc)[:200], retryable=True)


class NotifierRegistry:
    """Resolves a channel to its notifier.

    Falls back to :class:`LocalNotifier` for any channel with no configured
    provider, so an alert never vanishes because a channel is unwired.
    """

    def __init__(self) -> None:
        self._notifiers: dict[NotificationChannel, Notifier] = {}
        #: Cached local recorders, so repeated sends on an unwired channel
        #: accumulate in one place instead of creating a new recorder each time.
        self._fallbacks: dict[NotificationChannel, LocalNotifier] = {}

    def register(self, notifier: Notifier) -> None:
        self._notifiers[notifier.channel] = notifier

    def get(self, channel: NotificationChannel) -> Notifier:
        """The notifier for a channel, or a local recorder if none works.

        An unconfigured provider is treated the same as a missing one: the
        alert is recorded locally and still reaches the dashboard, rather than
        being lost because someone forgot an API key.
        """
        notifier = self._notifiers.get(channel)
        if notifier is not None and notifier.is_configured():
            return notifier
        return self._fallbacks.setdefault(channel, LocalNotifier(channel))

    def configured_channels(self) -> list[NotificationChannel]:
        return [
            channel for channel, notifier in self._notifiers.items() if notifier.is_configured()
        ]


def build_default_registry() -> NotifierRegistry:
    """The registry a deployment starts with.

    Dashboard and WebSocket are always real (they are internal). The external
    channels start as local recorders and are replaced when a customer's
    provider credentials are configured.
    """
    registry = NotifierRegistry()
    for channel in NotificationChannel:
        registry.register(LocalNotifier(channel))
    return registry
