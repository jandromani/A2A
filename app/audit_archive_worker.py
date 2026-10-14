"""One-shot command for Kubernetes CronJob or an operator-run archive check."""

from __future__ import annotations

import argparse
import json
import logging

from app.audit_archive import archive_audit_chain, create_s3_client
from app.config import settings
from app.database import make_engine, make_session_factory
from app.secrets import resolve_secret

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def main() -> None:
    parser = argparse.ArgumentParser(description="Archive or verify the audit chain in S3 WORM.")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    database_url = resolve_secret(settings, settings.database_url)
    if not database_url:
        raise RuntimeError("DATABASE_URL is not configured.")
    engine = make_engine(database_url)
    try:
        result = archive_audit_chain(
            make_session_factory(engine),
            create_s3_client(settings),
            settings,
            verify_only=args.verify_only,
        )
        print(json.dumps(result, sort_keys=True))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
