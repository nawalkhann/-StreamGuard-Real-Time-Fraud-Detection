"""Streaming ingestion. The transport-independent core is ``process_message``; the Kafka loop is a thin adapter.

Partition your input topic by ``account_id`` so per-account ordering is preserved.
"""
from __future__ import annotations

import asyncio
import json
import logging

from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from .config import Settings
from .engine import Engine, SchemaError
from .schemas import Transaction

log = logging.getLogger(__name__)


def process_message(engine: Engine, raw: bytes) -> tuple[bool, bytes]:
    """Score one raw message. Returns (ok, payload): decision JSON, or a DLQ envelope on failure."""
    try:
        txn = Transaction.model_validate_json(raw)
        decision = engine.score(txn)
        return True, decision.model_dump_json().encode()
    except (ValidationError, SchemaError) as e:
        engine.metrics.error()
        return False, json.dumps({"error": str(e)[:1000], "raw": raw.decode("utf-8", "replace")[:4000]}).encode()


async def run_kafka(engine: Engine, s: Settings) -> None:  # pragma: no cover - needs a broker
    from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

    consumer = AIOKafkaConsumer(
        s.kafka_in_topic, bootstrap_servers=s.kafka_bootstrap, group_id=s.kafka_group,
        enable_auto_commit=False, auto_offset_reset="earliest")
    producer = AIOKafkaProducer(bootstrap_servers=s.kafka_bootstrap, acks="all", enable_idempotence=True)
    await consumer.start()
    await producer.start()
    log.info("Kafka ingest started: %s -> %s", s.kafka_in_topic, s.kafka_out_topic)
    try:
        async for msg in consumer:
            ok, out = await run_in_threadpool(process_message, engine, msg.value)
            await producer.send_and_wait(s.kafka_out_topic if ok else s.kafka_dlq_topic, out, key=msg.key)
            await consumer.commit()  # at-least-once; scoring is idempotent on transaction_id
    except asyncio.CancelledError:
        raise
    finally:
        await consumer.stop()
        await producer.stop()
