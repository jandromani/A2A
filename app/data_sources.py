"""Configurable REST, CRM, ERP and data-lake ingestion adapters."""

import csv
import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import httpx
from fastapi import HTTPException
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.embeddings import embedding_for
from app.event_bus import publish_event, write_audit
from app.models import DataSourceRecord, KnowledgeDocument
from app.policy import authorize
from app.schemas import DataSourceCreate, DataSourceEventBatch, Principal
from app.secrets import resolve_named_secret

_OPTION_KEYS = {
    "provider",
    "records_path",
    "title_field",
    "id_field",
    "content_fields",
    "next_field",
    "max_pages",
    "max_records",
    "exclude_fields",
    "query_params",
}
_SOURCE_PROVIDERS = {
    "generic": {"kind": None, "format": None, "records_path": None, "next_field": None},
    "hubspot": {
        "kind": "crm",
        "format": "json",
        "records_path": "results",
        "next_field": "paging.next.link",
    },
    "salesforce": {
        "kind": "crm",
        "format": "json",
        "records_path": "records",
        "next_field": "nextRecordsUrl",
    },
    "sap_odata": {
        "kind": "erp",
        "format": "odata",
        "records_path": "value",
        "next_field": "@odata.nextLink",
    },
}


def serialize_data_source(source: DataSourceRecord, config: Settings) -> dict[str, Any]:
    auth_configured = False
    if source.auth_env:
        try:
            auth_configured = bool(resolve_named_secret(config, source.auth_env))
        except (RuntimeError, ValueError, httpx.HTTPError):
            auth_configured = False
    return {
        "id": source.id,
        "kind": source.kind,
        "name": source.name,
        "endpoint": source.endpoint,
        "auth_env": source.auth_env,
        "auth_configured": auth_configured,
        "scope": source.scope,
        "format": source.format,
        "source_options": source.source_options,
        "enabled": source.enabled,
        "last_sync_at": source.last_sync_at.isoformat() if source.last_sync_at else None,
        "last_sync_status": source.last_sync_status,
        "last_sync_error": source.last_sync_error,
    }


def _require_allowed(
    config: Settings,
    principal: Principal,
    action: str,
    resource: dict[str, Any] | None = None,
) -> None:
    allowed, reason = authorize(principal, action, resource or {}, config)
    if not allowed:
        raise HTTPException(status_code=403, detail=reason)


def _validate_data_source(source: DataSourceCreate, config: Settings) -> None:
    if source.kind == "data_lake":
        if source.source_options.get("provider", "generic") != "generic":
            raise HTTPException(
                status_code=422, detail="Data-lake sources do not use CRM adapters."
            )
        if not source.endpoint:
            raise HTTPException(
                status_code=422, detail="A relative data-lake file path is required."
            )
        if source.format not in {"csv", "parquet"}:
            raise HTTPException(status_code=422, detail="Data-lake files must be CSV or Parquet.")
        candidate = (config.data_lake_root / source.endpoint).resolve()
        root = config.data_lake_root.resolve()
        if candidate == root or root not in candidate.parents:
            raise HTTPException(
                status_code=422, detail="Data-lake path must remain inside DATA_LAKE_ROOT."
            )
        if candidate.suffix.lower() != f".{source.format}":
            raise HTTPException(
                status_code=422, detail="File extension does not match the selected format."
            )
        return
    if not source.endpoint:
        raise HTTPException(status_code=422, detail="An absolute source API URL is required.")
    if source.format not in {"json", "odata"}:
        raise HTTPException(status_code=422, detail="REST, CRM and ERP sources must return JSON.")
    options = source.source_options
    provider = options.get("provider", "generic")
    if provider not in _SOURCE_PROVIDERS:
        raise HTTPException(status_code=422, detail="Unsupported CRM or ERP provider adapter.")
    defaults = _SOURCE_PROVIDERS[provider]
    if defaults["kind"] and source.kind != defaults["kind"]:
        raise HTTPException(
            status_code=422,
            detail=f"The {provider} adapter requires a {defaults['kind']} source.",
        )
    if defaults["format"] and source.format != defaults["format"]:
        raise HTTPException(
            status_code=422,
            detail=f"The {provider} adapter requires {defaults['format']} format.",
        )
    _validate_source_url(source.endpoint, config, expected_host=None)


def _validate_source_url(url: str, config: Settings, expected_host: str | None) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        raise HTTPException(status_code=422, detail="Invalid outbound data-source URL.")
    if parsed.fragment:
        raise HTTPException(status_code=422, detail="Data-source URLs may not contain fragments.")
    if expected_host and (parsed.scheme != "https" or host != expected_host):
        raise HTTPException(
            status_code=422, detail="Pagination link left the configured source host."
        )
    if config.environment == "production":
        allowed_hosts = {
            item.strip().lower()
            for item in config.connector_allowed_hosts.split(",")
            if item.strip()
        }
        if parsed.scheme != "https" or host not in allowed_hosts:
            raise HTTPException(
                status_code=422, detail="Data-source host is not approved for production."
            )


def list_data_sources(session: Session, config: Settings) -> list[dict[str, Any]]:
    items = session.scalars(
        select(DataSourceRecord).order_by(desc(DataSourceRecord.created_at))
    ).all()
    return [serialize_data_source(item, config) for item in items]


def register_data_source(
    session: Session,
    config: Settings,
    body: DataSourceCreate,
    principal: Principal,
) -> dict[str, Any]:
    _require_allowed(config, principal, "data.source.manage")
    _validate_data_source(body, config)
    options = dict(body.source_options)
    if body.kind == "data_lake" and not options.get("content_fields"):
        options["content_fields"] = []
    record = DataSourceRecord(
        kind=body.kind,
        name=body.name,
        endpoint=body.endpoint,
        auth_env=body.auth_env,
        scope=body.scope,
        format=body.format,
        source_options=options,
        created_by=principal.subject,
    )
    session.add(record)
    session.flush()
    write_audit(
        session,
        principal.subject,
        "data_source.register",
        "data_source",
        record.id,
        details={"kind": record.kind, "format": record.format, "scope": record.scope},
    )
    session.commit()
    return serialize_data_source(record, config)


def _field(value: Any, path: str | None) -> Any:
    if not path:
        return None
    if isinstance(value, dict) and path in value:
        return value[path]
    current = value
    for name in path.split("."):
        if isinstance(current, dict):
            current = current.get(name)
        elif isinstance(current, list) and name.isdigit():
            index = int(name)
            current = current[index] if index < len(current) else None
        else:
            return None
    return current


def _records_from_page(
    payload: Any, options: dict[str, Any], source_format: str
) -> list[dict[str, Any]]:
    configured_path = options.get("records_path")
    provider_defaults = _SOURCE_PROVIDERS.get(options.get("provider", "generic"), {})
    candidates = (
        [configured_path]
        if configured_path
        else (
            [provider_defaults["records_path"]]
            if provider_defaults.get("records_path")
            else (["value"] if source_format == "odata" else ["records", "items", "data"])
        )
    )
    records = payload if isinstance(payload, list) else None
    for path in candidates:
        value = _field(payload, path)
        if isinstance(value, list):
            records = value
            break
    if records is None and isinstance(payload, dict):
        records = [payload]
    if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
        raise HTTPException(
            status_code=502, detail="Source response does not contain a list of records."
        )
    return records


def _read_api_records(
    source: DataSourceRecord, config: Settings, options: dict[str, Any]
) -> list[dict[str, Any]]:
    base_url = source.endpoint or ""
    _validate_source_url(base_url, config, expected_host=None)
    base_host = (urlparse(base_url).hostname or "").lower()
    headers = {"Accept": "application/json"}
    if source.auth_env:
        token = resolve_named_secret(config, source.auth_env)
        if not token:
            raise HTTPException(
                status_code=424, detail=f"Data-source secret {source.auth_env} is missing."
            )
        headers["Authorization"] = f"Bearer {token}"
    provider = options.get("provider", "generic")
    max_pages = int(options.get("max_pages", 10 if provider != "generic" else 1))
    max_records = int(options.get("max_records", 500))
    query_params = options.get("query_params", {})
    if not isinstance(query_params, dict):
        raise HTTPException(status_code=422, detail="query_params must be an object.")
    records: list[dict[str, Any]] = []
    next_url: str | None = base_url
    try:
        with httpx.Client(timeout=20.0, follow_redirects=False) as client:
            for page_index in range(max_pages):
                if not next_url:
                    break
                _validate_source_url(
                    next_url, config, expected_host=base_host if page_index else None
                )
                response = client.get(
                    next_url,
                    headers=headers,
                    params=query_params if page_index == 0 else None,
                )
                response.raise_for_status()
                page = response.json()
                records.extend(_records_from_page(page, options, source.format))
                records = records[:max_records]
                if len(records) >= max_records:
                    break
                provider_defaults = _SOURCE_PROVIDERS.get(provider, {})
                next_field = (
                    options.get("next_field")
                    or provider_defaults.get("next_field")
                    or ("@odata.nextLink" if source.format == "odata" else None)
                )
                next_value = _field(page, next_field) if next_field else None
                if next_value is not None and not isinstance(next_value, str):
                    raise HTTPException(
                        status_code=502, detail="Pagination link must be a URL string."
                    )
                next_url = urljoin(base_url, next_value) if next_value else None
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=502, detail="Data-source request or JSON parsing failed."
        ) from exc
    return records


def _read_lake_records(
    source: DataSourceRecord, config: Settings, options: dict[str, Any]
) -> list[dict[str, Any]]:
    if not source.endpoint:
        raise HTTPException(status_code=422, detail="Data-lake path is required.")
    root = config.data_lake_root.resolve()
    path = (root / source.endpoint).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(
            status_code=404, detail="Data-lake file is missing or outside the configured root."
        )
    limit = int(options.get("max_records", 500))
    if source.format == "csv":
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as stream:
                return [dict(row) for row in list(csv.DictReader(stream))[:limit]]
        except (OSError, UnicodeError, csv.Error) as exc:
            raise HTTPException(
                status_code=422, detail="CSV data-lake file could not be read."
            ) from exc
    try:
        import duckdb

        connection = duckdb.connect(database=":memory:", read_only=False)
        try:
            result = connection.execute("SELECT * FROM read_parquet(?) LIMIT ?", [str(path), limit])
            columns = [item[0] for item in result.description]
            return [dict(zip(columns, row, strict=True)) for row in result.fetchall()]
        finally:
            connection.close()
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="Parquet ingestion requires DuckDB.") from exc
    except Exception as exc:
        raise HTTPException(
            status_code=422, detail="Parquet data-lake file could not be read."
        ) from exc


def _document_for_record(
    source: DataSourceRecord,
    record: dict[str, Any],
    index: int,
) -> tuple[str, str, str]:
    options = source.source_options or {}
    excluded = set(options.get("exclude_fields", []))
    id_field = options.get("id_field")
    identifier = _field(record, id_field) if id_field else None
    if identifier is None:
        identifier = record.get("id") or record.get("key") or index
    title = _field(record, options.get("title_field")) if options.get("title_field") else None
    if title is None:
        title = (
            record.get("title")
            or record.get("name")
            or record.get("subject")
            or f"{source.name} · {identifier}"
        )
    fields = options.get("content_fields") or [key for key in record if key not in excluded]
    content_parts = []
    for field_name in fields:
        if field_name in excluded:
            continue
        value = _field(record, field_name)
        if value is None:
            continue
        rendered = (
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        )
        if rendered.strip():
            content_parts.append(f"{field_name}: {rendered[:8_000]}")
    content = "\n".join(content_parts) or json.dumps(
        {key: value for key, value in record.items() if key not in excluded},
        ensure_ascii=False,
        default=str,
    )
    return str(title)[:240], content[:100_000], quote(str(identifier), safe="-_.")[:300]


def _upsert_records(
    session: Session,
    config: Settings,
    source: DataSourceRecord,
    records: list[dict[str, Any]],
) -> int:
    changed = 0
    for index, record in enumerate(records):
        title, content, record_id = _document_for_record(source, record, index)
        source_uri = f"source://{source.id}/{record_id}"
        document = session.scalar(
            select(KnowledgeDocument).where(KnowledgeDocument.source_uri == source_uri)
        )
        if document is None:
            document = KnowledgeDocument(
                title=title,
                content=content,
                scope=source.scope,
                source_uri=source_uri,
                tags=[source.kind, source.id],
                embedding=embedding_for(f"{title}\n{content}", config),
                enabled=True,
            )
            session.add(document)
        else:
            document.title = title
            document.content = content
            document.scope = source.scope
            document.embedding = embedding_for(f"{title}\n{content}", config)
            document.enabled = True
            document.updated_at = datetime.now(UTC)
        changed += 1
    return changed


def sync_data_source(
    session: Session,
    config: Settings,
    source_id: str,
    principal: Principal,
    *,
    records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    source = session.get(DataSourceRecord, source_id)
    if not source or not source.enabled:
        raise HTTPException(status_code=404, detail="Data source not found or disabled.")
    _require_allowed(
        config, principal, "data.source.sync", {"scope": source.scope, "kind": source.kind}
    )
    try:
        loaded = (
            records
            if records is not None
            else (
                _read_lake_records(source, config, source.source_options or {})
                if source.kind == "data_lake"
                else _read_api_records(source, config, source.source_options or {})
            )
        )
        count = _upsert_records(session, config, source, loaded)
        source_prefix = f"source://{source.id}/"
        active_uris = {
            f"{source_prefix}{_document_for_record(source, item, index)[2]}"
            for index, item in enumerate(loaded)
        }
        stale_documents = session.scalars(
            select(KnowledgeDocument).where(
                KnowledgeDocument.source_uri.like(f"{source_prefix}%"),
                KnowledgeDocument.enabled.is_(True),
            )
        ).all()
        for document in stale_documents:
            if document.source_uri not in active_uris:
                document.enabled = False
                document.updated_at = datetime.now(UTC)
        source.last_sync_at = datetime.now(UTC)
        source.last_sync_status = "completed"
        source.last_sync_error = None
        write_audit(
            session,
            principal.subject,
            "data_source.sync",
            "data_source",
            source.id,
            details={"records": count, "kind": source.kind},
        )
        publish_event(
            session,
            config,
            aggregate_id=source.id,
            event_type="data.source.synced",
            message=f"{source.name}: {count} records indexed.",
            payload={"source_id": source.id, "records": count},
        )
        session.commit()
        return {
            "source": serialize_data_source(source, config),
            "records_received": len(loaded),
            "documents_updated": count,
        }
    except Exception as exc:
        session.rollback()
        failed = session.get(DataSourceRecord, source_id)
        if failed:
            failed.last_sync_at = datetime.now(UTC)
            failed.last_sync_status = "failed"
            failed.last_sync_error = str(exc)[:500]
            session.commit()
        raise


def ingest_event_batch(
    session: Session,
    config: Settings,
    source_id: str,
    body: DataSourceEventBatch,
    principal: Principal,
) -> dict[str, Any]:
    source = session.get(DataSourceRecord, source_id)
    if not source or not source.enabled:
        raise HTTPException(status_code=404, detail="Data source not found or disabled.")
    _require_allowed(
        config, principal, "data.source.write", {"scope": source.scope, "kind": source.kind}
    )
    if config.event_worker_enabled or config.task_execution_mode == "queue":
        publish_event(
            session,
            config,
            aggregate_id=source.id,
            event_type="data.source.records_received",
            message=f"{source.name}: accepted {len(body.records)} pipeline records.",
            payload={
                "source_id": source.id,
                "records": body.records,
                "event_id": body.event_id,
                "actor": principal.subject,
                "roles": sorted(principal.roles),
                "scopes": sorted(principal.scopes),
            },
        )
        source.last_sync_status = "queued"
        count = len(body.records)
    else:
        count = _upsert_records(session, config, source, body.records)
        source.last_sync_at = datetime.now(UTC)
        source.last_sync_status = "streaming"
        source.last_sync_error = None
    write_audit(
        session,
        principal.subject,
        "data_source.event_batch",
        "data_source",
        source.id,
        details={"records": count, "event_id": body.event_id},
    )
    if not (config.event_worker_enabled or config.task_execution_mode == "queue"):
        publish_event(
            session,
            config,
            aggregate_id=source.id,
            event_type="data.source.batch_indexed",
            message=f"{source.name}: received {count} pipeline records.",
            payload={"source_id": source.id, "records": count, "event_id": body.event_id},
        )
    session.commit()
    return {
        "source_id": source.id,
        "event_id": body.event_id,
        "records_received": count,
        "status": source.last_sync_status,
    }


def queue_data_source_sync(
    session: Session,
    config: Settings,
    source_id: str,
    principal: Principal,
) -> dict[str, Any]:
    source = session.get(DataSourceRecord, source_id)
    if not source or not source.enabled:
        raise HTTPException(status_code=404, detail="Data source not found or disabled.")
    _require_allowed(
        config, principal, "data.source.sync", {"scope": source.scope, "kind": source.kind}
    )
    publish_event(
        session,
        config,
        aggregate_id=source.id,
        event_type="data.source.sync.requested",
        message=f"Sync requested for {source.name}.",
        payload={
            "source_id": source.id,
            "actor": principal.subject,
            "roles": sorted(principal.roles),
            "scopes": sorted(principal.scopes),
        },
    )
    source.last_sync_status = "queued"
    source.last_sync_error = None
    session.commit()
    return {"source": serialize_data_source(source, config), "status": "queued"}


def disable_data_source(
    session: Session,
    config: Settings,
    source_id: str,
    principal: Principal,
) -> None:
    _require_allowed(config, principal, "data.source.manage")
    source = session.get(DataSourceRecord, source_id)
    if not source:
        raise HTTPException(status_code=404, detail="Data source not found.")
    source.enabled = False
    write_audit(session, principal.subject, "data_source.disable", "data_source", source.id)
    session.commit()
