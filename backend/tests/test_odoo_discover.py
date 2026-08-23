from __future__ import annotations

import importlib.util
import json
import xmlrpc.client
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[2] / "scripts" / "odoo_discover.py"
SPEC = importlib.util.spec_from_file_location("odoo_discover", SCRIPT)
assert SPEC and SPEC.loader
odoo_discover = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(odoo_discover)


def field(name, field_type="char", label=None):
    return {"name": name, "type": field_type, "string": label or name.title()}


class FakeReader:
    def __init__(self):
        self.calls = []

    def authenticate(self):
        return {
            "server_version": "18.0",
            "server_version_info": [18, 0, 0, "final", 0],
            "protocol_version": 1,
        }, 7

    def call(self, model, method, args=None, kwargs=None):
        self.calls.append((model, method, args or [], kwargs or {}))
        if model == "ir.model" and method == "search_read":
            domain = (args or [[]])[0]
            if any(isinstance(item, tuple) and item[0] == "model" and item[1] == "in"
                   for item in domain):
                return [
                    {"model": "stock.warehouse", "name": "Warehouse"},
                    {"model": "pos.session", "name": "POS Session"},
                ]
            return [{"model": "x_retail_branch", "name": "Retail Branch"}]
        if method == "search_count":
            return 3 if model == "pos.session" else 27
        if method == "fields_get":
            if model == "stock.warehouse":
                return {
                    "name": {"type": "char", "string": "Name"},
                    "code": {"type": "char", "string": "Code"},
                }
            if model == "pos.session":
                return {
                    "name": {"type": "char", "string": "Name"},
                    "start_at": {"type": "datetime", "string": "Opening Date"},
                }
            if model == "x_retail_branch":
                return {
                    "name": {"type": "char", "string": "Name"},
                    "branch_code": {"type": "char", "string": "Branch Code"},
                }
        if method == "search_read" and model == "x_retail_branch":
            return [{"id": 9, "name": "Junction", "branch_code": "JNT"}]
        if method == "search_read" and model == "stock.warehouse":
            return [{"id": 2, "name": "Fallback Warehouse", "code": "WH"}]
        raise AssertionError(f"Unexpected call: {(model, method, args, kwargs)}")


def config(**overrides):
    value = {
        "url": "https://odoo.example.test",
        "db": "vivo",
        "user": "readonly@example.test",
        "api_key": "secret-value",
        "timeout": 20.0,
    }
    value.update(overrides)
    return value


def test_load_config_requires_all_credentials_without_exposing_values():
    with pytest.raises(odoo_discover.DiscoveryError, match="ODOO_API_KEY"):
        odoo_discover.load_config(
            {"ODOO_URL": "https://odoo.test", "ODOO_DB": "db", "ODOO_USER": "user"}
        )


@pytest.mark.parametrize(
    "url",
    ["odoo.test", "ftp://odoo.test", "https://user:password@odoo.test"],
)
def test_load_config_rejects_unsafe_or_invalid_urls(url):
    values = {
        "ODOO_URL": url,
        "ODOO_DB": "db",
        "ODOO_USER": "user",
        "ODOO_API_KEY": "key",
    }
    with pytest.raises(odoo_discover.DiscoveryError):
        odoo_discover.load_config(values)


def test_safe_error_redacts_all_connection_secrets():
    error = RuntimeError("key-123 failed for user@example.test at https://odoo.test")
    message = odoo_discover.safe_error(
        error, ("key-123", "user@example.test", "https://odoo.test")
    )
    assert "key-123" not in message
    assert "user@example.test" not in message
    assert "https://odoo.test" not in message
    assert message.count("<redacted>") == 3


def test_or_domain_builds_valid_prefix_or_expression():
    domain = odoo_discover.or_domain(("model", "name"), ("store", "branch"))
    assert domain[:3] == ["|", "|", "|"]
    assert len(domain[3:]) == 4
    assert ("model", "ilike", "store") in domain
    assert ("name", "ilike", "branch") in domain


def test_discovery_reads_only_metadata_counts_and_store_identifiers():
    reader = FakeReader()
    result = odoo_discover.run_discovery(reader, config())

    assert result["odoo"]["server_version"] == "18.0"
    assert result["models"]["stock.warehouse"]["count"] == 27
    assert result["models"]["res.company"]["exists"] is False
    assert result["custom_store_models"] == [
        {"model": "x_retail_branch", "name": "Retail Branch"}
    ]
    assert result["store_identifiers"]["rows"][0] == {
        "model": "x_retail_branch",
        "res_id": 9,
        "code": "JNT",
        "name": "Junction",
    }
    assert result["pos_usage"]["last_30_days_count"] == 3
    assert result["privacy"] == {
        "store_identifier_records_read": True,
        "transaction_records_read": False,
        "employee_records_read": False,
        "customer_records_read": False,
        "monetary_values_read": False,
    }

    allowed_methods = {"search_read", "search_count", "fields_get"}
    assert {method for _model, method, _args, _kwargs in reader.calls} <= allowed_methods
    forbidden_models = {"hr.employee", "pos.order", "account.move"}
    record_reads = [
        model
        for model, method, _args, _kwargs in reader.calls
        if method == "search_read" and model != "ir.model"
    ]
    assert not (set(record_reads) & forbidden_models)


def test_store_samples_are_capped_across_models():
    class ManyStores(FakeReader):
        def call(self, model, method, args=None, kwargs=None):
            if method == "search_read" and model in {"x_retail_branch", "stock.warehouse"}:
                limit = kwargs["limit"]
                code_field = "branch_code" if model == "x_retail_branch" else "code"
                return [
                    {"id": index, "name": f"Store {index}", code_field: f"S{index}"}
                    for index in range(limit)
                ]
            return super().call(model, method, args, kwargs)

    reader = ManyStores()
    models = {
        "stock.warehouse": {
            "exists": True,
            "fields": [field("name"), field("code")],
        }
    }
    custom = [{"model": "x_retail_branch", "name": "Retail Branch"}]
    result = odoo_discover.discover_store_identifiers(
        reader, models, custom, ("secret",)
    )
    assert len(result["rows"]) == odoo_discover.MAX_STORE_IDENTIFIERS
    assert {row["model"] for row in result["rows"]} == {"x_retail_branch"}


def test_model_access_fault_is_reported_without_stopping_discovery():
    class DeniedReader(FakeReader):
        def call(self, model, method, args=None, kwargs=None):
            if model == "stock.warehouse" and method == "search_count":
                raise xmlrpc.client.Fault(1, "Access denied for secret-value")
            return super().call(model, method, args, kwargs)

    models = odoo_discover.discover_models(DeniedReader(), ("secret-value",))
    assert models["stock.warehouse"]["exists"] is True
    assert models["stock.warehouse"]["count"] is None
    assert models["stock.warehouse"]["fields"]
    assert "secret-value" not in models["stock.warehouse"]["count_error"]
    assert "<redacted>" in models["stock.warehouse"]["count_error"]


def test_json_result_contains_no_credentials():
    result = odoo_discover.run_discovery(FakeReader(), config())
    rendered = json.dumps(result)
    assert "secret-value" not in rendered
    assert "readonly@example.test" not in rendered
    assert "https://odoo.example.test" not in rendered
