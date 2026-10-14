import httpx
import pytest
from fastapi import HTTPException

from app.config import Settings
from app.data_sources import _read_api_records, _validate_data_source
from app.models import DataSourceRecord
from app.schemas import DataSourceCreate


class FakeClient:
    pages = {}
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, url, *, headers, params=None):
        self.calls.append((url, params))
        payload = self.pages[url]
        return httpx.Response(200, json=payload, request=httpx.Request("GET", url))


@pytest.mark.parametrize(
    ("kind", "provider", "source_format", "endpoint", "pages", "expected"),
    [
        (
            "crm",
            "hubspot",
            "json",
            "https://api.hubapi.com/crm/v3/objects/contacts",
            {
                "https://api.hubapi.com/crm/v3/objects/contacts": {
                    "results": [{"id": "1", "properties": {"name": "North"}}],
                    "paging": {
                        "next": {"link": "https://api.hubapi.com/crm/v3/objects/contacts?after=2"}
                    },
                },
                "https://api.hubapi.com/crm/v3/objects/contacts?after=2": {
                    "results": [{"id": "2", "properties": {"name": "South"}}]
                },
            },
            ["1", "2"],
        ),
        (
            "crm",
            "salesforce",
            "json",
            "https://acme.my.salesforce.com/services/data/v60.0/query",
            {
                "https://acme.my.salesforce.com/services/data/v60.0/query": {
                    "records": [{"Id": "001", "Name": "Account 1"}],
                    "nextRecordsUrl": "/services/data/v60.0/query/next/001",
                },
                "https://acme.my.salesforce.com/services/data/v60.0/query/next/001": {
                    "records": [{"Id": "002", "Name": "Account 2"}],
                    "done": True,
                },
            },
            ["001", "002"],
        ),
        (
            "erp",
            "sap_odata",
            "odata",
            "https://sap.example/odata/BusinessPartners",
            {
                "https://sap.example/odata/BusinessPartners": {
                    "value": [{"BusinessPartner": "BP1"}],
                    "@odata.nextLink": "https://sap.example/odata/BusinessPartners?$skiptoken=next",
                },
                "https://sap.example/odata/BusinessPartners?$skiptoken=next": {
                    "value": [{"BusinessPartner": "BP2"}]
                },
            },
            ["BP1", "BP2"],
        ),
    ],
)
def test_vendor_adapters_follow_provider_pagination(
    monkeypatch, kind, provider, source_format, endpoint, pages, expected
):
    FakeClient.pages = pages
    FakeClient.calls = []
    monkeypatch.setattr("app.data_sources.httpx.Client", FakeClient)
    source = DataSourceRecord(
        kind=kind,
        name=provider,
        endpoint=endpoint,
        scope="crm:read",
        format=source_format,
        source_options={"provider": provider},
        created_by="test",
    )
    config = Settings(_env_file=None, environment="test", auth_mode="dev")

    records = _read_api_records(source, config, source.source_options)

    id_key = (
        "id" if provider == "hubspot" else "Id" if provider == "salesforce" else "BusinessPartner"
    )
    assert [record[id_key] for record in records] == expected
    assert len(FakeClient.calls) == 2


def test_vendor_adapters_reject_kind_or_format_mismatch():
    config = Settings(_env_file=None, environment="test", auth_mode="dev")
    hubspot_as_erp = DataSourceCreate(
        kind="erp",
        name="wrong kind",
        endpoint="https://api.hubapi.com/crm/v3/objects/contacts",
        scope="commercial",
        source_options={"provider": "hubspot"},
    )
    with pytest.raises(HTTPException, match="requires a crm source"):
        _validate_data_source(hubspot_as_erp, config)

    sap_as_json = DataSourceCreate(
        kind="erp",
        name="wrong format",
        endpoint="https://sap.example/odata/BusinessPartners",
        format="json",
        scope="finance",
        source_options={"provider": "sap_odata"},
    )
    with pytest.raises(HTTPException, match="requires odata format"):
        _validate_data_source(sap_as_json, config)
