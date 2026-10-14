from datetime import UTC, datetime
from io import BytesIO

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.audit_archive import archive_audit_chain
from app.config import Settings
from app.database import Base
from app.event_bus import write_audit
from app.models import AuditRecord


class FakePaginator:
    def __init__(self, store):
        self.store = store

    def paginate(self, **_kwargs):
        yield {
            "Versions": [
                {"Key": key, "VersionId": version_id, "IsLatest": True}
                for key in sorted(self.store.versions)
                for version_id in self.store.versions[key]
            ],
            "DeleteMarkers": [
                {"Key": key, "VersionId": "delete-marker"} for key in self.store.delete_markers
            ],
        }


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.versions = {}
        self.delete_markers = []
        self.retention_mode = "COMPLIANCE"

    def get_object_lock_configuration(self, **_kwargs):
        return {
            "ObjectLockConfiguration": {
                "ObjectLockEnabled": "Enabled",
                "Rule": {"DefaultRetention": {"Mode": self.retention_mode, "Days": 2557}},
            }
        }

    def get_paginator(self, _name):
        return FakePaginator(self)

    def put_object(self, **kwargs):
        now = datetime.now(UTC)
        version_id = f"version-{len(self.versions.get(kwargs['Key'], [])) + 1}"
        self.versions.setdefault(kwargs["Key"], []).append(version_id)
        self.objects[kwargs["Key"]] = {
            "body": kwargs["Body"],
            "metadata": kwargs["Metadata"],
            "mode": kwargs["ObjectLockMode"],
            "retain": kwargs["ObjectLockRetainUntilDate"],
            "modified": now,
        }

    def head_object(self, *, Key, VersionId=None, **_kwargs):
        if VersionId is not None:
            assert VersionId in self.versions[Key]
        item = self.objects[Key]
        return {
            "Metadata": item["metadata"],
            "ObjectLockMode": item["mode"],
            "ObjectLockRetainUntilDate": item["retain"],
            "LastModified": item["modified"],
        }

    def get_object(self, *, Key, VersionId=None, **_kwargs):
        if VersionId is not None:
            assert VersionId in self.versions[Key]
        return {"Body": BytesIO(self.objects[Key]["body"])}


@pytest.fixture
def audit_store():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        write_audit(session, "test-user", "task.create", "task", "task-1")
        write_audit(session, "test-user", "task.approve", "task", "task-1")
        session.commit()
    yield factory
    engine.dispose()


def test_archive_is_idempotent_and_verify_only_checks_worm_objects(audit_store):
    s3 = FakeS3()
    config = Settings(
        _env_file=None,
        environment="test",
        audit_archive_bucket="immutable-audit",
    )

    first = archive_audit_chain(audit_store, s3, config)
    second = archive_audit_chain(audit_store, s3, config)
    verified = archive_audit_chain(audit_store, s3, config, verify_only=True)

    assert first == {"valid": True, "entries": 2, "archived": 2, "verified": 0}
    assert second == {"valid": True, "entries": 2, "archived": 0, "verified": 2}
    assert verified["valid"] is True
    assert len(s3.objects) == 2
    assert all(record["mode"] == "COMPLIANCE" for record in s3.objects.values())


def test_archive_rejects_modified_database_row(audit_store):
    s3 = FakeS3()
    config = Settings(_env_file=None, environment="test", audit_archive_bucket="immutable-audit")
    archive_audit_chain(audit_store, s3, config)
    with audit_store() as session:
        session.query(AuditRecord).filter_by(sequence=1).update({"action": "tampered"})
        session.commit()

    with pytest.raises(RuntimeError, match="entry hash mismatch"):
        archive_audit_chain(audit_store, s3, config)


def test_archive_requires_compliance_retention(audit_store):
    s3 = FakeS3()
    s3.retention_mode = "GOVERNANCE"
    config = Settings(_env_file=None, environment="test", audit_archive_bucket="immutable-audit")

    with pytest.raises(RuntimeError, match="COMPLIANCE Object Lock"):
        archive_audit_chain(audit_store, s3, config)


def test_archive_rejects_duplicate_versions_and_delete_markers(audit_store):
    s3 = FakeS3()
    config = Settings(_env_file=None, environment="test", audit_archive_bucket="immutable-audit")
    archive_audit_chain(audit_store, s3, config)

    s3.versions[next(iter(s3.versions))].append("unexpected-version")
    with pytest.raises(RuntimeError, match="unexpected versions"):
        archive_audit_chain(audit_store, s3, config, verify_only=True)

    s3.versions[next(iter(s3.versions))].pop()
    s3.delete_markers.append(next(iter(s3.versions)))
    with pytest.raises(RuntimeError, match="unexpected versions"):
        archive_audit_chain(audit_store, s3, config, verify_only=True)
