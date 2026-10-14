from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from deploy.backup.restore import latest_backup_key, verify_checksum


class FakePaginator:
    def paginate(self, **kwargs):
        assert kwargs == {
            "Bucket": "backups",
            "Prefix": "a2a/production/",
        }
        now = datetime.now(UTC)
        return [
            {
                "Contents": [
                    {
                        "Key": "a2a/production/20261001T020000Z.dump",
                        "LastModified": now - timedelta(days=1),
                    },
                    {
                        "Key": "a2a/production/20261002T020000Z.dump",
                        "LastModified": now,
                    },
                    {
                        "Key": "a2a/production/data-lake/latest.tar.gz",
                        "LastModified": now + timedelta(days=1),
                    },
                ]
            }
        ]


class FakeS3:
    def get_paginator(self, operation):
        assert operation == "list_objects_v2"
        return FakePaginator()


def test_latest_backup_ignores_data_lake_archives_and_selects_newest_dump():
    assert latest_backup_key(FakeS3(), "backups", "/a2a/production/") == (
        "a2a/production/20261002T020000Z.dump"
    )


def test_backup_checksum_requires_matching_digest_and_filename(tmp_path):
    archive = tmp_path / "database.dump"
    archive.write_bytes(b"verified archive")
    digest = sha256(archive.read_bytes()).hexdigest()

    verify_checksum(
        archive,
        f"{digest}  20261002T020000Z.dump\n",
        "a2a/production/20261002T020000Z.dump",
    )
    with pytest.raises(RuntimeError, match="checksum does not match"):
        verify_checksum(
            archive,
            f"{'0' * 64}  20261002T020000Z.dump",
            "a2a/production/20261002T020000Z.dump",
        )


def test_backup_checksum_rejects_wrong_sidecar_filename(tmp_path):
    archive = tmp_path / "database.dump"
    archive.write_bytes(b"verified archive")
    digest = sha256(archive.read_bytes()).hexdigest()
    with pytest.raises(RuntimeError, match="malformed"):
        verify_checksum(archive, f"{digest}  another.dump", "a2a/production/backup.dump")
