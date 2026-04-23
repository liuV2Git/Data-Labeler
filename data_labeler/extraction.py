"""Document extraction helpers for Story 6."""

from __future__ import annotations

import base64
import http.client
import json
import mimetypes
import os
import socket
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, request

LOCAL_RESPONSES_URL = "http://127.0.0.1:9755/v1/responses"
INTERNAL_RESPONSES_URL = (
    "https://code-internal.aiservice.us-chicago-1.oci.oraclecloud.com/"
    "20250206/app/litellm/responses"
)
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
DEFAULT_INTERNAL_MODEL = "gpt-5.4"
DEFAULT_OPENAI_MODEL = "gpt-5-mini"
SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
MAX_IMAGE_UPLOAD_BYTES = 1_200_000
MAX_IMAGE_DIMENSION = 1600


class ExtractionError(RuntimeError):
    """Raised when document extraction cannot be completed."""


@dataclass
class ExtractionResult:
    """Structured result from a document extraction attempt."""

    field_values: dict[str, str]
    mode: str
    model: str | None = None
    provider: str | None = None


@dataclass
class ResponsesTarget:
    """Connection details for a Responses-compatible endpoint."""

    url: str
    model: str
    provider: str
    bearer_token: str | None = None


def extract_document_fields(
    document_path: Path,
    schema_path: Path,
) -> ExtractionResult:
    """Extracts schema field values for a single document."""

    schema = _load_schema(schema_path)
    fields = schema.get("fields", [])
    if not isinstance(fields, list) or not fields:
        raise ExtractionError("The active schema has no fields to extract.")

    responses_target = _resolve_responses_target()
    if responses_target is not None:
        return _extract_with_responses_api(document_path, fields, responses_target)

    if os.getenv("OPENAI_API_KEY"):
        return _extract_with_openai(document_path, fields)

    return _extract_with_stub(document_path, fields)


def _load_schema(schema_path: Path) -> dict[str, Any]:
    """Loads a schema file from disk."""

    try:
        return json.loads(schema_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ExtractionError(f"Schema not found: {schema_path.name}") from exc
    except json.JSONDecodeError as exc:
        raise ExtractionError(f"Schema is not valid JSON: {exc.msg}") from exc
    except OSError as exc:
        raise ExtractionError(f"Could not read schema: {exc}") from exc


def _extract_with_stub(
    document_path: Path,
    fields: list[dict[str, Any]],
) -> ExtractionResult:
    """Builds blank extraction values for local demos and fallback mode."""

    values: dict[str, str] = {}
    for field in fields:
        field_name = str(field.get("name", "")).strip()
        if not field_name:
            continue

        values[field_name] = ""

    return ExtractionResult(
        field_values=values,
        mode="stub",
        model=None,
        provider="stub",
    )


def _resolve_responses_target() -> ResponsesTarget | None:
    """Chooses the preferred Responses-compatible endpoint for extraction."""

    explicit_url = os.getenv("DATA_LABELER_RESPONSES_URL")
    explicit_model = os.getenv("DATA_LABELER_MODEL") or os.getenv("OPENAI_MODEL")
    if explicit_url:
        return ResponsesTarget(
            url=explicit_url,
            model=explicit_model or DEFAULT_INTERNAL_MODEL,
            provider="configured-responses",
            bearer_token=_read_internal_token(optional=True),
        )

    if _is_local_ocat_available():
        return ResponsesTarget(
            url=LOCAL_RESPONSES_URL,
            model=os.getenv("DATA_LABELER_MODEL", DEFAULT_INTERNAL_MODEL),
            provider="ocat-local",
            bearer_token=None,
        )

    token = _read_internal_token(optional=True)
    if token:
        return ResponsesTarget(
            url=INTERNAL_RESPONSES_URL,
            model=os.getenv("DATA_LABELER_MODEL", DEFAULT_INTERNAL_MODEL),
            provider="oci-internal",
            bearer_token=token,
        )

    return None


def _extract_with_responses_api(
    document_path: Path,
    fields: list[dict[str, Any]],
    target: ResponsesTarget,
) -> ExtractionResult:
    """Calls a Responses-compatible API for one-document extraction."""

    prompt = _build_extraction_prompt(fields)
    schema = _build_output_schema(fields)

    payload = {
        "model": target.model,
        "input": [
            {
                "role": "system",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "You extract structured values from a single business "
                            "document. Return only values that are directly supported "
                            "by the document. Use empty strings when a value is missing "
                            "or uncertain."
                        ),
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    _build_document_input(document_path),
                ],
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "document_extraction",
                "schema": schema,
                "strict": True,
            }
        },
    }

    request_body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if target.bearer_token:
        headers["Authorization"] = f"Bearer {target.bearer_token}"

    api_request = request.Request(
        target.url,
        data=request_body,
        headers=headers,
        method="POST",
    )

    try:
        with request.urlopen(api_request, timeout=120) as response:
            raw_response = response.read().decode("utf-8", errors="replace")
            content_type = response.headers.get("Content-Type", "unknown")
            if "text/event-stream" in content_type:
                raw_response = _extract_json_from_event_stream(raw_response)
            try:
                response_json = json.loads(raw_response)
            except json.JSONDecodeError as exc:
                snippet = raw_response[:500].strip() or "<empty response body>"
                raise ExtractionError(
                    f"{target.provider} returned non-JSON content "
                    f"(content-type: {content_type}): {snippet}"
                ) from exc
    except error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        message = _extract_error_message(error_body) or exc.reason
        raise ExtractionError(f"{target.provider} request failed: {message}") from exc
    except error.URLError as exc:
        raise ExtractionError(f"Could not reach {target.provider}: {exc.reason}") from exc
    except http.client.RemoteDisconnected as exc:
        raise ExtractionError(
            f"{target.provider} connection closed unexpectedly. "
            "This can happen because of a network/proxy issue or a rejected request."
        ) from exc
    except ExtractionError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ExtractionError(
            f"Unexpected extraction error: {exc.__class__.__name__}: {exc}"
        ) from exc

    response_text = _extract_response_text(response_json)
    if not response_text:
        raise ExtractionError(f"{target.provider} returned an empty extraction response.")

    try:
        parsed = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise ExtractionError(
            f"{target.provider} returned invalid JSON: {exc.msg}"
        ) from exc

    field_values = {
        str(field.get("name", "")): _normalize_value(parsed.get(field.get("name", "")))
        for field in fields
        if field.get("name")
    }
    return ExtractionResult(
        field_values=field_values,
        mode="responses-api",
        model=target.model,
        provider=target.provider,
    )


def _extract_with_openai(
    document_path: Path,
    fields: list[dict[str, Any]],
) -> ExtractionResult:
    """Calls the OpenAI Responses API for one-document extraction."""

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ExtractionError("OPENAI_API_KEY is not set.")

    target = ResponsesTarget(
        url=OPENAI_RESPONSES_URL,
        model=os.getenv("OPENAI_MODEL", DEFAULT_OPENAI_MODEL),
        provider="openai",
        bearer_token=api_key,
    )
    return _extract_with_responses_api(document_path, fields, target)


def _build_extraction_prompt(fields: list[dict[str, Any]]) -> str:
    """Builds a compact instruction block for the document extraction request."""

    field_lines = []
    for field in fields:
        field_name = str(field.get("name", "")).strip()
        if not field_name:
            continue

        label = str(field.get("label") or field_name)
        field_type = str(field.get("type", "string"))
        description = str(field.get("description", "")).strip()
        description_suffix = f" Description: {description}" if description else ""
        field_lines.append(
            f"- {field_name}: label={label}; type={field_type}.{description_suffix}"
        )

    joined_fields = "\n".join(field_lines)
    return (
        "Extract values for the following schema fields from the attached document.\n"
        "Return only one JSON object that matches the provided schema.\n"
        "Prefer a best-effort extraction when the document shows a likely value.\n"
        "Use empty strings only when a value is truly absent from the document.\n"
        "When the schema includes firstname, middlename, and lastname, split a full "
        "person name across those fields whenever the card makes that possible. Do "
        "not put the entire name into only one of those fields unless the others are "
        "truly unclear.\n"
        "If a full person name is visible, infer the most likely first, middle, and "
        "last name split from the printed text.\n"
        "For phone and email, copy the exact visible contact value from the card "
        "without adding commentary.\n"
        f"{joined_fields}"
    )


def _build_output_schema(fields: list[dict[str, Any]]) -> dict[str, Any]:
    """Builds a JSON schema for structured extraction output."""

    properties: dict[str, Any] = {}
    required: list[str] = []

    for field in fields:
        field_name = str(field.get("name", "")).strip()
        if not field_name:
            continue

        field_type = str(field.get("type", "string")).lower()
        description = str(field.get("description", "")).strip()
        label = str(field.get("label") or field_name)

        if field_type == "boolean":
            schema_type: str | list[str] = ["boolean", "string"]
        elif field_type == "number":
            schema_type = ["number", "string"]
        else:
            schema_type = "string"

        properties[field_name] = {
            "type": schema_type,
            "description": description or f"Extract the value for {label}.",
        }
        required.append(field_name)

    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _build_document_input(document_path: Path) -> dict[str, Any]:
    """Builds a Responses API content item for a local document."""

    suffix = document_path.suffix.lower()
    payload_path = document_path
    if suffix in SUPPORTED_IMAGE_EXTENSIONS:
        payload_path = _prepare_image_payload(document_path)

    mime_type = (
        mimetypes.guess_type(payload_path.name)[0] or "application/octet-stream"
    )
    file_bytes = payload_path.read_bytes()
    encoded = base64.b64encode(file_bytes).decode("ascii")
    data_url = f"data:{mime_type};base64,{encoded}"

    if suffix in SUPPORTED_IMAGE_EXTENSIONS:
        return {
            "type": "input_image",
            "image_url": data_url,
        }

    return {
        "type": "input_file",
        "filename": document_path.name,
        "file_data": data_url,
    }


def _prepare_image_payload(document_path: Path) -> Path:
    """Downsizes large images to reduce request size for API calls."""

    try:
        file_size = document_path.stat().st_size
    except OSError:
        return document_path

    if file_size <= MAX_IMAGE_UPLOAD_BYTES:
        return document_path

    temp_dir = Path(tempfile.mkdtemp(prefix="data-labeler-image-"))
    output_path = temp_dir / f"{document_path.stem}.jpg"

    command = [
        "sips",
        "-Z",
        str(MAX_IMAGE_DIMENSION),
        "--setProperty",
        "format",
        "jpeg",
        "--setProperty",
        "formatOptions",
        "75",
        str(document_path),
        "--out",
        str(output_path),
    ]

    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0 or not output_path.exists():
        return document_path

    return output_path


def _is_local_ocat_available() -> bool:
    """Returns whether the local OCAT Responses endpoint appears reachable."""

    try:
        with socket.create_connection(("127.0.0.1", 9755), timeout=0.2):
            return True
    except OSError:
        return False


def _read_internal_token(optional: bool = False) -> str | None:
    """Reads an internal bearer token from env or a file path."""

    env_token = os.getenv("OCA_TOKEN") or os.getenv("DATA_LABELER_TOKEN")
    if env_token:
        return env_token.strip()

    token_file = (
        os.getenv("OCA_TOKEN_FILE")
        or os.getenv("DATA_LABELER_TOKEN_FILE")
        or os.getenv("TOKEN_FILE")
    )
    if token_file:
        try:
            token = Path(token_file).expanduser().read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ExtractionError(f"Could not read token file: {exc}") from exc

        if token:
            return token

        raise ExtractionError("Token file is empty.")

    if optional:
        return None

    raise ExtractionError(
        "No internal bearer token found. Set OCA_TOKEN_FILE to a file containing the token."
    )


def _extract_response_text(response_json: dict[str, Any]) -> str:
    """Pulls text content out of a Responses API payload."""

    output_text = response_json.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    outputs = response_json.get("output", [])
    if not isinstance(outputs, list):
        return ""

    chunks: list[str] = []
    for output_item in outputs:
        if not isinstance(output_item, dict):
            continue

        content_items = output_item.get("content", [])
        if not isinstance(content_items, list):
            continue

        for content_item in content_items:
            if not isinstance(content_item, dict):
                continue

            text = content_item.get("text")
            if isinstance(text, str) and text.strip():
                chunks.append(text)

    return "\n".join(chunks).strip()


def _extract_error_message(error_body: str) -> str | None:
    """Extracts a readable error message from an API error payload."""

    if not error_body:
        return None

    try:
        parsed = json.loads(error_body)
    except json.JSONDecodeError:
        return error_body.strip()

    error_value = parsed.get("error")
    if isinstance(error_value, dict):
        message = error_value.get("message")
        if isinstance(message, str):
            return message

    return error_body.strip()


def _extract_json_from_event_stream(event_stream_text: str) -> str:
    """Extracts the final JSON payload from a text/event-stream response."""

    data_lines: list[str] = []
    for raw_line in event_stream_text.splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue

        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue

        data_lines.append(payload)

    if not data_lines:
        return event_stream_text

    return data_lines[-1]


def _normalize_value(value: Any) -> str:
    """Normalizes extracted values for text inputs in the current UI."""

    if value is None:
        return ""

    if isinstance(value, bool):
        return "true" if value else "false"

    if isinstance(value, (int, float)):
        return str(value)

    if isinstance(value, str):
        return value

    return json.dumps(value, ensure_ascii=True)
