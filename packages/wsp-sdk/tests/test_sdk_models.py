"""The SDK mirrors wsp_api.schemas by hand; these tests make drift impossible to miss."""

import inspect
from typing import Any

import pytest
from pydantic import BaseModel

from wsp_api import schemas as server
from wsp_sdk import models as sdk

SDK_ONLY = {"StreamedChange"}


def _public_models(module: object) -> dict[str, type[BaseModel]]:
    return {
        name: obj
        for name, obj in inspect.getmembers(module, inspect.isclass)
        if issubclass(obj, BaseModel) and not name.startswith("_") and obj.__module__ == module.__name__  # type: ignore[attr-defined]
    }


def _strip(node: Any) -> Any:
    """Descriptions are documentation, not contract: compare everything else."""
    if isinstance(node, dict):
        return {k: _strip(v) for k, v in node.items() if k != "description"}  # pyright: ignore[reportUnknownVariableType]
    if isinstance(node, list):
        return [_strip(v) for v in node]  # pyright: ignore[reportUnknownVariableType]
    return node


SERVER = _public_models(server)
CLIENT = _public_models(sdk)


def test_same_model_names() -> None:
    assert set(SERVER) == set(CLIENT) - SDK_ONLY


@pytest.mark.parametrize("name", sorted(SERVER))
@pytest.mark.parametrize("mode", ["validation", "serialization"])
def test_same_json_schema(name: str, mode: str) -> None:
    ours = _strip(CLIENT[name].model_json_schema(mode=mode))  # type: ignore[arg-type]
    theirs = _strip(SERVER[name].model_json_schema(mode=mode))  # type: ignore[arg-type]
    assert ours == theirs


def test_api_version_matches() -> None:
    assert sdk.Health(sources=0).api_version == server.API_VERSION


def test_unknown_fields_from_newer_servers_are_ignored() -> None:
    hit = sdk.Hit.model_validate({"doc_id": "wsp:a/b", "score": 1, "title": "t", "address": "x", "new": 1})
    assert hit.doc_id == "wsp:a/b"
