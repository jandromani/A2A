"""MQTT QoS ingress that durably stages IoT events in the SQL outbox."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import ssl
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.event_bus import publish_event, write_audit
from app.models import OutboxEvent
from app.secrets import resolve_secret

logger = logging.getLogger("agentic.mqtt")
_DEVICE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,120}$")


def _device_from_topic(topic: str, topic_filter: str) -> str:
    filter_parts = topic_filter.split("/")
    topic_parts = topic.split("/")
    if len(filter_parts) != len(topic_parts):
        raise ValueError("topic_shape_mismatch")
    for expected, actual in zip(filter_parts, topic_parts, strict=True):
        if expected not in {"+", "#"} and expected != actual:
            raise ValueError("topic_filter_mismatch")
    try:
        index = filter_parts.index("+")
    except ValueError as exc:
        raise ValueError("topic_filter_must_include_device_wildcard") from exc
    device_id = topic_parts[index]
    if not _DEVICE_ID.fullmatch(device_id):
        raise ValueError("invalid_device_id")
    return device_id


def persist_mqtt_event(
    factory: sessionmaker[Session], config: Settings, topic: str, payload: bytes
) -> str:
    if len(payload) > config.mqtt_max_message_bytes:
        raise ValueError("message_too_large")
    device_id = _device_from_topic(topic, config.mqtt_topic_filter)
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("payload_must_be_utf8_json") from exc
    if not isinstance(decoded, dict):
        raise ValueError("payload_must_be_json_object")
    supplied_id = decoded.get("event_id")
    event_id = str(supplied_id)[:160] if isinstance(supplied_id, str) and supplied_id else None
    external_id = f"mqtt:{device_id}:{event_id}" if event_id else None
    try:
        with factory() as session:
            if external_id:
                existing = session.scalar(
                    select(OutboxEvent).where(OutboxEvent.external_id == external_id)
                )
                if existing:
                    return existing.task_event_id
            digest = hashlib.sha256(payload).hexdigest()
            event = publish_event(
                session,
                config,
                task_id=None,
                event_type="iot.telemetry",
                message=f"Telemetry received from device {device_id}.",
                payload={
                    "device_id": device_id,
                    "source_topic": topic,
                    "event_id": event_id,
                    "payload_sha256": digest,
                    "data": decoded,
                },
                aggregate_id=device_id,
                external_id=external_id,
            )
            write_audit(
                session,
                f"mqtt:{device_id}",
                "iot.telemetry.ingest",
                "device",
                device_id,
                details={"event_id": event_id, "payload_sha256": digest, "outbox_id": event.id},
            )
            session.commit()
            return event.id
    except IntegrityError:
        if not external_id:
            raise
        with factory() as session:
            existing = session.scalar(
                select(OutboxEvent).where(OutboxEvent.external_id == external_id)
            )
            if existing:
                return existing.task_event_id
        raise


def record_mqtt_rejection(
    factory: sessionmaker[Session], config: Settings, topic: str, reason: str
) -> None:
    try:
        device_id = _device_from_topic(topic, config.mqtt_topic_filter)
    except ValueError:
        device_id = "unknown"
    with factory() as session:
        write_audit(
            session,
            f"mqtt:{device_id}",
            "iot.telemetry.reject",
            "device",
            device_id if device_id != "unknown" else None,
            decision="rejected",
            details={"reason": reason[:80], "topic": topic[:300]},
        )
        session.commit()


class MQTTIngress:
    def __init__(self, factory: sessionmaker[Session], config: Settings):
        if not config.mqtt_broker_url:
            raise ValueError("MQTT_BROKER_URL is required to start MQTT ingress.")
        self.factory = factory
        self.config = config
        self.client: Any | None = None

    def start(self) -> None:
        try:
            from paho.mqtt import client as mqtt
        except ImportError as exc:
            raise RuntimeError(
                "MQTT is configured but paho-mqtt is not installed. Install project dependencies."
            ) from exc
        parsed = urlparse(self.config.mqtt_broker_url or "")
        if parsed.scheme not in {"mqtt", "mqtts"} or not parsed.hostname:
            raise ValueError(
                "MQTT_BROKER_URL must be mqtt:// or mqtts:// followed by a broker host."
            )
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("MQTT_BROKER_URL must not contain credentials, query, or fragment.")
        username = resolve_secret(self.config, self.config.mqtt_username)
        password = resolve_secret(self.config, self.config.mqtt_password)
        if bool(username) != bool(password):
            raise ValueError("MQTT_USERNAME and MQTT_PASSWORD must be configured together.")

        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=self.config.mqtt_client_id,
            protocol=mqtt.MQTTv5,
            manual_ack=True,
        )
        if username and password:
            client.username_pw_set(username, password)
        if parsed.scheme == "mqtts":
            client.tls_set(
                ca_certs=str(self.config.mqtt_ca_file) if self.config.mqtt_ca_file else None,
                cert_reqs=ssl.CERT_REQUIRED,
            )

        def on_connect(client, _userdata, _flags, reason_code, _properties):
            if getattr(reason_code, "is_failure", False):
                logger.error("MQTT broker rejected connection (reason=%s).", reason_code)
                return
            result, message_id = client.subscribe(self.config.mqtt_topic_filter, qos=1)
            if result != mqtt.MQTT_ERR_SUCCESS:
                logger.error("MQTT topic subscription failed (code=%s).", result)
            else:
                logger.info("MQTT ingress subscribed (mid=%s).", message_id)

        def on_message(client, _userdata, message):
            try:
                persist_mqtt_event(self.factory, self.config, message.topic, message.payload)
            except ValueError as exc:
                # Invalid records are acknowledged and discarded; valid records remain retryable
                # until their SQL outbox transaction commits.
                logger.warning(
                    "Rejected MQTT message (topic=%s reason=%s).", message.topic, str(exc)
                )
                try:
                    record_mqtt_rejection(self.factory, self.config, message.topic, str(exc))
                except Exception:
                    logger.exception("Could not persist MQTT rejection audit event.")
                    return
            except Exception:
                logger.exception("Could not persist MQTT message; broker delivery remains pending.")
                return
            if message.qos:
                client.ack(message.mid, message.qos)

        client.on_connect = on_connect
        client.on_message = on_message
        client.connect_async(
            parsed.hostname, parsed.port or (8883 if parsed.scheme == "mqtts" else 1883)
        )
        client.loop_start()
        self.client = client

    def stop(self) -> None:
        if self.client is not None:
            self.client.disconnect()
            self.client.loop_stop()
            self.client = None
