"""Switchable local MQTT transport for Nimbus.

Supports the five canonical topics defined in contracts/topics.md:
  nimbus/{cluster_id}/lease       (cloud -> edge)
  nimbus/{cluster_id}/renewal     (cloud -> edge)
  nimbus/{cluster_id}/probe       (cloud -> edge)
  nimbus/{cluster_id}/escalation  (edge -> cloud)
  nimbus/{cluster_id}/checkin     (edge -> cloud)

Can connect to a local broker (e.g. Mosquitto via paho-mqtt) or use an
in-process stand-in broker when paho is missing or NIMBUS_MQTT_STANDIN=1 is set.
"""

from __future__ import annotations

import collections
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("nimbus.mqtt")

CANONICAL_MESSAGE_TYPES = ("lease", "escalation", "checkin", "renewal", "probe", "notice")


def canonical_topic(cluster_id: str, message_type: str) -> str:
    """Format one of the five canonical topic names."""
    if message_type not in CANONICAL_MESSAGE_TYPES:
        raise ValueError(f"unknown canonical message type: {message_type}")
    return f"nimbus/{cluster_id}/{message_type}"


def topic_matches(pattern: str, topic: str) -> bool:
    """MQTT topic matching with single-level wildcard '+'."""
    pattern_parts = pattern.split("/")
    topic_parts = topic.split("/")
    if len(pattern_parts) != len(topic_parts):
        return False
    for p, t in zip(pattern_parts, topic_parts):
        if p == "+":
            continue
        if p != t:
            return False
    return True


class SpoolManager:
    """Manages disk-backed FIFO message spooling for offline persistence."""

    def __init__(self, spool_path: str | Path | None = None):
        self.path = Path(spool_path) if spool_path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> list[tuple[str, bytes]]:
        if not self.path or not self.path.exists():
            return []
        items = []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    entry = json.loads(line)
                    raw_payload = entry.get("payload", "")
                    items.append((
                        entry["topic"],
                        raw_payload.encode("utf-8") if isinstance(raw_payload, str) else bytes(raw_payload)
                    ))
        except Exception as exc:
            logger.warning("Failed to load MQTT spool file %s: %s", self.path, exc)
        return items

    def append(self, topic: str, payload_bytes: bytes) -> None:
        if not self.path:
            return
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                payload_str = payload_bytes.decode("utf-8", errors="replace")
                f.write(json.dumps({"topic": topic, "payload": payload_str}) + "\n")
        except Exception as exc:
            logger.warning("Failed to write to MQTT spool %s: %s", self.path, exc)

    def sync(self, items: list[tuple[str, bytes]]) -> None:
        if not self.path:
            return
        try:
            if not items:
                if self.path.exists():
                    self.path.unlink(missing_ok=True)
                return
            tmp_path = self.path.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                for topic, raw in items:
                    payload_str = raw.decode("utf-8", errors="replace")
                    f.write(json.dumps({"topic": topic, "payload": payload_str}) + "\n")
            tmp_path.replace(self.path)
        except Exception as exc:
            logger.warning("Failed to sync MQTT spool %s: %s", self.path, exc)


class InMemoryBroker:
    """Thread-safe in-process stand-in broker for zero-dependency local runs and tests."""

    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers: list[tuple[str, Callable[[str, bytes], None]]] = []

    def subscribe(self, pattern: str, callback: Callable[[str, bytes], None]) -> None:
        with self._lock:
            self._subscribers.append((pattern, callback))

    def unsubscribe(self, callback: Callable[[str, bytes], None]) -> None:
        with self._lock:
            self._subscribers = [s for s in self._subscribers if s[1] != callback]

    def publish(self, topic: str, payload: bytes) -> None:
        with self._lock:
            targets = [cb for pat, cb in self._subscribers if topic_matches(pat, topic)]
        for cb in targets:
            try:
                cb(topic, payload)
            except Exception as exc:
                logger.warning("Error in stand-in MQTT subscriber for %s: %s", topic, exc)

    def reset(self) -> None:
        with self._lock:
            self._subscribers.clear()


_GLOBAL_STANDIN_BROKER = InMemoryBroker()


class StandinMQTTClient:
    """Client backed by the in-memory broker with durable buffering and deduplication."""

    def __init__(
        self,
        broker: InMemoryBroker | None = None,
        max_pending: int = 10000,
        spool_path: str | Path | None = None,
    ):
        self.broker = broker or _GLOBAL_STANDIN_BROKER
        self.max_pending = max_pending
        self._spool = SpoolManager(spool_path or os.environ.get("NIMBUS_MQTT_SPOOL_PATH"))
        self._subscriptions: list[tuple[str, Callable[[str, dict], None]]] = []
        self._subs_lock = threading.Lock()
        self._running = False
        self.connected = True

        self._pending_lock = threading.RLock()
        self._pending_messages: list[tuple[str, bytes]] = self._spool.load()

        # Observable telemetry
        self._overflow_drops = 0
        self._publish_failures = 0
        self._messages_published = 0
        self._messages_received = 0
        self._duplicates_suppressed = 0

        # Message deduplication state
        self._seen_message_ids: set[str] = set()
        self._seen_order: collections.deque[str] = collections.deque(maxlen=10000)

    def subscribe(self, pattern: str, handler: Callable[[str, dict], None]) -> None:
        with self._subs_lock:
            self._subscriptions.append((pattern, handler))
        if self._running and self.connected:
            self._register(pattern, handler)

    def _register(self, pattern: str, handler: Callable[[str, dict], None]) -> None:
        def _wrapper(topic: str, payload_bytes: bytes) -> None:
            try:
                data = json.loads(payload_bytes.decode("utf-8"))
            except Exception as e:
                logger.warning("Failed to parse JSON on topic %s: %s", topic, e)
                return

            if isinstance(data, dict):
                msg_id = data.get("message_id")
                if msg_id:
                    with self._pending_lock:
                        if msg_id in self._seen_message_ids:
                            self._duplicates_suppressed += 1
                            logger.info("Duplicate message %s suppressed on %s", msg_id, topic)
                            return
                        if len(self._seen_order) >= 10000:
                            oldest = self._seen_order.popleft()
                            self._seen_message_ids.discard(oldest)
                        self._seen_message_ids.add(msg_id)
                        self._seen_order.append(msg_id)

            with self._pending_lock:
                self._messages_received += 1

            handler(topic, data)

        self.broker.subscribe(pattern, _wrapper)

    def publish(self, topic: str, payload: dict | str | bytes) -> None:
        if isinstance(payload, dict):
            raw = json.dumps(payload).encode("utf-8")
        elif isinstance(payload, str):
            raw = payload.encode("utf-8")
        else:
            raw = payload

        if not self.connected:
            with self._pending_lock:
                if len(self._pending_messages) >= self.max_pending:
                    self._overflow_drops += 1
                    logger.error("MQTT pending queue full (%d items); dropping message on %s", self.max_pending, topic)
                    raise BufferError(f"MQTT pending queue overflow (limit {self.max_pending})")
                self._pending_messages.append((topic, raw))
                self._spool.append(topic, raw)
            logger.info("Standin broker disconnected; buffered message for %s (total: %d)", topic, len(self._pending_messages))
            return

        self.broker.publish(topic, raw)
        with self._pending_lock:
            self._messages_published += 1

    def disconnect_broker(self) -> None:
        """Simulate broker disconnection for resilience testing."""
        self.connected = False

    def reconnect_broker(self) -> None:
        """Simulate broker reconnection and flush buffered messages safely."""
        self.connected = True
        while True:
            with self._pending_lock:
                if not self._pending_messages:
                    break
                topic, raw = self._pending_messages[0]
            try:
                self.broker.publish(topic, raw)
                with self._pending_lock:
                    self._messages_published += 1
                    if self._pending_messages and self._pending_messages[0] == (topic, raw):
                        self._pending_messages.pop(0)
            except Exception as exc:
                with self._pending_lock:
                    self._publish_failures += 1
                logger.warning("Error replaying stand-in message on %s: %s", topic, exc)
                break
        with self._pending_lock:
            self._spool.sync(self._pending_messages)

    def metrics_snapshot(self) -> dict[str, Any]:
        """Return snapshot of transport metrics."""
        with self._pending_lock:
            return {
                "connected": self.connected,
                "pending_count": len(self._pending_messages),
                "max_pending": self.max_pending,
                "overflow_drops": self._overflow_drops,
                "publish_failures": self._publish_failures,
                "messages_published": self._messages_published,
                "messages_received": self._messages_received,
                "duplicates_suppressed": self._duplicates_suppressed,
            }

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        for pattern, handler in self._subscriptions:
            self._register(pattern, handler)

    def stop(self) -> None:
        self._running = False


class PahoMQTTClient:
    """Client wrapping paho-mqtt for connection to real local Mosquitto."""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 1883,
        max_pending: int = 10000,
        spool_path: str | Path | None = None,
    ):
        import paho.mqtt.client as paho_mqtt

        self.host = host
        self.port = port
        self.max_pending = max_pending
        self._spool = SpoolManager(spool_path or os.environ.get("NIMBUS_MQTT_SPOOL_PATH"))

        if hasattr(paho_mqtt, "CallbackAPIVersion"):
            self._client = paho_mqtt.Client(paho_mqtt.CallbackAPIVersion.VERSION2)
        else:
            self._client = paho_mqtt.Client()

        self._handlers: list[tuple[str, Callable[[str, dict], None]]] = []
        self._handlers_lock = threading.Lock()
        self._pending_lock = threading.RLock()
        self._pending_messages: list[tuple[str, bytes]] = self._spool.load()
        self._stopping = False
        self._reconnect_thread: threading.Thread | None = None

        # Observable telemetry
        self._overflow_drops = 0
        self._publish_failures = 0
        self._messages_published = 0
        self._messages_received = 0
        self._duplicates_suppressed = 0

        # Deduplication state
        self._seen_message_ids: set[str] = set()
        self._seen_order: collections.deque[str] = collections.deque(maxlen=10000)

        self._client.on_message = self._on_message
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self.connected = False

    def _on_connect(self, client, userdata, flags, rc, properties=None) -> None:
        is_ok = (rc == 0) if isinstance(rc, int) else (not getattr(rc, "is_failure", True))
        if is_ok:
            self.connected = True
            logger.info("Connected to MQTT broker at %s:%s", self.host, self.port)
            with self._handlers_lock:
                for pattern, _ in self._handlers:
                    try:
                        self._client.subscribe(pattern, qos=1)
                    except Exception as e:
                        logger.warning("Failed subscribing to %s: %s", pattern, e)

            # Flush buffered pending messages safely one-by-one upon reconnection
            while True:
                with self._pending_lock:
                    if not self._pending_messages:
                        break
                    topic, raw = self._pending_messages[0]
                try:
                    info = self._client.publish(topic, raw, qos=1)
                    if hasattr(info, "rc") and info.rc != 0:
                        with self._pending_lock:
                            self._publish_failures += 1
                        logger.warning("Error publishing buffered message to %s (rc=%d)", topic, info.rc)
                        break
                    with self._pending_lock:
                        self._messages_published += 1
                        if self._pending_messages and self._pending_messages[0] == (topic, raw):
                            self._pending_messages.pop(0)
                except Exception as exc:
                    with self._pending_lock:
                        self._publish_failures += 1
                    logger.warning("Error flushing pending message to %s: %s", topic, exc)
                    break
            with self._pending_lock:
                self._spool.sync(self._pending_messages)

    def _on_disconnect(self, client, userdata, rc, properties=None) -> None:
        self.connected = False
        if not self._stopping:
            logger.warning("Disconnected from MQTT broker (%s:%s); auto-reconnect active", self.host, self.port)

    def _on_message(self, client, userdata, msg) -> None:
        topic = msg.topic
        try:
            data = json.loads(msg.payload.decode("utf-8"))
        except Exception as e:
            logger.warning("Paho MQTT payload parse error on %s: %s", topic, e)
            return

        if isinstance(data, dict):
            msg_id = data.get("message_id")
            if msg_id:
                with self._pending_lock:
                    if msg_id in self._seen_message_ids:
                        self._duplicates_suppressed += 1
                        logger.info("Duplicate message %s suppressed on %s", msg_id, topic)
                        return
                    if len(self._seen_order) >= 10000:
                        oldest = self._seen_order.popleft()
                        self._seen_message_ids.discard(oldest)
                    self._seen_message_ids.add(msg_id)
                    self._seen_order.append(msg_id)

        with self._pending_lock:
            self._messages_received += 1

        with self._handlers_lock:
            handlers = list(self._handlers)

        for pattern, handler in handlers:
            if topic_matches(pattern, topic):
                try:
                    handler(topic, data)
                except Exception as ex:
                    logger.warning("Handler error on %s: %s", topic, ex)

    def subscribe(self, pattern: str, handler: Callable[[str, dict], None]) -> None:
        with self._handlers_lock:
            self._handlers.append((pattern, handler))
        if self.connected:
            try:
                self._client.subscribe(pattern, qos=1)
            except Exception as e:
                logger.warning("Failed subscribing to %s: %s", pattern, e)

    def publish(self, topic: str, payload: dict | str | bytes) -> None:
        if isinstance(payload, dict):
            raw = json.dumps(payload).encode("utf-8")
        elif isinstance(payload, str):
            raw = payload.encode("utf-8")
        else:
            raw = payload

        if not self.connected:
            with self._pending_lock:
                if len(self._pending_messages) >= self.max_pending:
                    self._overflow_drops += 1
                    logger.error("MQTT pending queue full (%d items); dropping message on %s", self.max_pending, topic)
                    raise BufferError(f"MQTT pending queue overflow (limit {self.max_pending})")
                self._pending_messages.append((topic, raw))
                self._spool.append(topic, raw)
            logger.info("MQTT disconnected; buffered message on %s (pending: %d)", topic, len(self._pending_messages))
            return

        try:
            info = self._client.publish(topic, raw, qos=1)
            if hasattr(info, "rc") and info.rc != 0:
                raise RuntimeError(f"MQTT publish error rc={info.rc}")
            with self._pending_lock:
                self._messages_published += 1
        except Exception as exc:
            self._publish_failures += 1
            logger.warning("Failed to publish to MQTT topic %s: %s; buffering", topic, exc)
            with self._pending_lock:
                if len(self._pending_messages) >= self.max_pending:
                    self._overflow_drops += 1
                    raise BufferError(f"MQTT pending queue overflow (limit {self.max_pending})")
                self._pending_messages.append((topic, raw))
                self._spool.append(topic, raw)

    def metrics_snapshot(self) -> dict[str, Any]:
        """Return snapshot of transport metrics."""
        with self._pending_lock:
            return {
                "connected": self.connected,
                "pending_count": len(self._pending_messages),
                "max_pending": self.max_pending,
                "overflow_drops": self._overflow_drops,
                "publish_failures": self._publish_failures,
                "messages_published": self._messages_published,
                "messages_received": self._messages_received,
                "duplicates_suppressed": self._duplicates_suppressed,
            }

    def start(self) -> None:
        self._stopping = False
        try:
            self._client.connect_async(self.host, self.port, 60)
            self._client.loop_start()
            logger.info("Paho MQTT background loop started for %s:%s", self.host, self.port)
        except Exception as exc:
            self.connected = False
            logger.warning(
                "Initial connection to MQTT broker at %s:%s failed (%s); starting background reconnect",
                self.host, self.port, exc
            )
            self._start_reconnect_loop()

    def _start_reconnect_loop(self) -> None:
        if self._reconnect_thread and self._reconnect_thread.is_alive():
            return

        def _loop():
            import time
            delay = 1.0
            while not self._stopping and not self.connected:
                time.sleep(delay)
                try:
                    self._client.connect(self.host, self.port, 60)
                    self._client.loop_start()
                    self.connected = True
                    break
                except Exception:
                    delay = min(10.0, delay * 1.5)

        self._reconnect_thread = threading.Thread(target=_loop, daemon=True, name="MQTT-Reconnect")
        self._reconnect_thread.start()

    def stop(self) -> None:
        self._stopping = True
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:
            pass


class MQTTTransport:
    """Unified MQTT transport providing pub/sub across the five canonical topics."""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 1883,
        force_standin: bool = False,
        broker: InMemoryBroker | None = None,
        max_pending: int = 10000,
        spool_path: str | Path | None = None,
    ):
        self.host = host
        self.port = port
        self.is_standin = force_standin
        self.client: StandinMQTTClient | PahoMQTTClient

        if not force_standin:
            try:
                self.client = PahoMQTTClient(host=host, port=port, max_pending=max_pending, spool_path=spool_path)
            except Exception as e:
                logger.info("paho-mqtt unavailable or failed to initialize (%s); using in-process stand-in", e)
                self.is_standin = True
                self.client = StandinMQTTClient(broker=broker, max_pending=max_pending, spool_path=spool_path)
        else:
            self.client = StandinMQTTClient(broker=broker, max_pending=max_pending, spool_path=spool_path)

    def subscribe(self, pattern: str, handler: Callable[[str, dict], None]) -> None:
        self.client.subscribe(pattern, handler)

    def publish(self, topic: str, payload: dict | str | bytes) -> None:
        self.client.publish(topic, payload)

    def start(self) -> None:
        self.client.start()

    @property
    def is_connected(self) -> bool:
        return getattr(self.client, "connected", False)

    def metrics_snapshot(self) -> dict[str, Any]:
        return self.client.metrics_snapshot()

    def stop(self) -> None:
        self.client.stop()


def is_mqtt_enabled() -> bool:
    """Returns True if NIMBUS_TRANSPORT is explicitly set to 'mqtt'."""
    return os.environ.get("NIMBUS_TRANSPORT", "http").lower() == "mqtt"


def make_mqtt_transport_from_env(broker: InMemoryBroker | None = None) -> MQTTTransport:
    """Create transport using environment configuration."""
    host = os.environ.get("NIMBUS_MQTT_HOST", "localhost")
    port = int(os.environ.get("NIMBUS_MQTT_PORT", "1883"))
    force_standin = os.environ.get("NIMBUS_MQTT_STANDIN", "0").lower() in ("1", "true", "yes")
    max_pending = int(os.environ.get("NIMBUS_MQTT_MAX_PENDING", "10000"))
    spool_path = os.environ.get("NIMBUS_MQTT_SPOOL_PATH")
    return MQTTTransport(
        host=host,
        port=port,
        force_standin=force_standin,
        broker=broker,
        max_pending=max_pending,
        spool_path=spool_path,
    )
