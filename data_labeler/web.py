"""Local web application for Story 3 review workflow."""

from __future__ import annotations

import csv
import io
import json
import mimetypes
import subprocess
import threading
import traceback
import webbrowser
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from data_labeler.extraction import extract_document_fields
from data_labeler.models import ScanResult
from data_labeler.scanner import (
    format_relative_paths,
    get_default_active_schema,
    scan_folder,
)

HOST = "127.0.0.1"
PORT = 8765


@dataclass
class AppState:
    """Stores mutable application state for the local web app.

    Attributes:
        selected_folder: Folder currently selected by the user.
        scan_result: Most recent scan result.
        active_schema: Schema currently selected by the user.
        active_document: Document currently selected in the review workspace.
        review_data: In-memory review values keyed by absolute document path.
        reviewed_documents: Absolute document paths that have been saved.
        status_message: Short UI status text.
    """

    selected_folder: Path | None = None
    scan_result: ScanResult | None = None
    active_schema: Path | None = None
    active_document: Path | None = None
    review_data: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    selected_documents: set[str] = field(default_factory=set)
    extracted_documents: set[str] = field(default_factory=set)
    failed_documents: set[str] = field(default_factory=set)
    reviewed_documents: set[str] = field(default_factory=set)
    batch_extract_running: bool = False
    batch_extract_total: int = 0
    batch_extract_completed: int = 0
    batch_extract_extracted: int = 0
    batch_extract_failed: int = 0
    batch_extract_skipped: int = 0
    batch_extract_cancel_requested: bool = False
    batch_extract_status: str | None = None
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    status_message: str = (
        "Choose a folder to discover documents, select a schema, and begin review."
    )


def _serialize_scan_result(result: ScanResult | None) -> dict[str, Any]:
    """Converts a scan result into JSON-safe browser data.

    Args:
        result: Current scan result or ``None`` before a folder is chosen.

    Returns:
        A dictionary that can be encoded as JSON for the browser UI.
    """

    if result is None:
        return {
            "selectedFolder": None,
            "supportFolder": None,
            "documents": [],
            "schemaFiles": [],
            "categoriesFile": None,
            "labelsFile": None,
            "warnings": [],
            "documentCount": 0,
            "schemaCount": 0,
        }

    support_root = result.support_root or result.root_path
    return {
        "selectedFolder": str(result.root_path),
        "supportFolder": str(support_root),
        "documents": format_relative_paths(result.documents, result.root_path),
        "schemaFiles": format_relative_paths(result.schema_files, support_root),
        "categoriesFile": (
            str(result.categories_file.relative_to(support_root))
            if result.categories_file
            else None
        ),
        "labelsFile": (
            str(result.labels_file.relative_to(result.root_path))
            if result.labels_file
            else None
        ),
        "warnings": result.warnings,
        "documentCount": result.document_count,
        "schemaCount": result.schema_count,
    }


def _read_json_file(file_path: Path | None) -> tuple[dict[str, Any] | None, str | None]:
    """Reads a JSON file and reports parse errors without raising.

    Args:
        file_path: File that should be read.

    Returns:
        A tuple of parsed JSON content and an optional error string.
    """

    if file_path is None:
        return None, None

    try:
        contents = file_path.read_text(encoding="utf-8")
        return json.loads(contents), None
    except Exception as error:  # noqa: BLE001
        return None, str(error)


def _load_schema_definition(schema_path: Path | None) -> tuple[list[dict[str, Any]], str | None]:
    """Loads normalized schema fields from the active schema file.

    Args:
        schema_path: Active schema path.

    Returns:
        A tuple of normalized field definitions and an optional error string.
    """

    schema_json, error = _read_json_file(schema_path)
    if schema_json is None:
        return [], error

    raw_fields = schema_json.get("fields", [])
    normalized_fields: list[dict[str, Any]] = []
    for field in raw_fields:
        normalized_fields.append(
            {
                "name": field.get("name", ""),
                "label": field.get("label") or field.get("name", ""),
                "type": field.get("type", "string"),
                "description": field.get("description", ""),
            }
        )
    return normalized_fields, None


def _load_categories_definition(
    categories_path: Path | None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Loads normalized category definitions from the categories config.

    Args:
        categories_path: Categories config path.

    Returns:
        A tuple of normalized category definitions and an optional error string.
    """

    categories_json, error = _read_json_file(categories_path)
    if categories_json is None:
        return [], error

    raw_categories = categories_json.get("categories", [])
    normalized_categories: list[dict[str, Any]] = []
    for category in raw_categories:
        options = category.get("options", [])
        normalized_categories.append(
            {
                "name": category.get("name", ""),
                "description": category.get("description", ""),
                "options": options,
                "kind": "select" if options else "boolean",
            }
        )
    return normalized_categories, None


def _read_schema_preview(schema_path: Path | None) -> dict[str, Any]:
    """Reads the active schema into preview-friendly browser data.

    Args:
        schema_path: Currently active schema path.

    Returns:
        A dictionary with preview text and optional preview error.
    """

    if schema_path is None:
        return {
            "activeSchema": None,
            "activeSchemaPath": None,
            "schemaPreview": "No active schema selected.",
            "schemaPreviewError": None,
        }

    parsed, error = _read_json_file(schema_path)
    if parsed is None:
        return {
            "activeSchema": schema_path.name,
            "activeSchemaPath": str(schema_path),
            "schemaPreview": "",
            "schemaPreviewError": error,
        }

    return {
        "activeSchema": schema_path.name,
        "activeSchemaPath": str(schema_path),
        "schemaPreview": json.dumps(parsed, indent=2, ensure_ascii=True),
        "schemaPreviewError": None,
    }


def _relative_path(path: Path | None, root_path: Path | None) -> str | None:
    """Builds a relative display path.

    Args:
        path: Absolute path to convert.
        root_path: Root folder for relative formatting.

    Returns:
        Relative path text if possible, otherwise the absolute path string.
    """

    if path is None:
        return None

    if root_path is None:
        return str(path)

    try:
        return str(path.relative_to(root_path))
    except ValueError:
        return str(path)

def _document_key(document_path: Path) -> str:
    """Builds a stable in-memory key for a document path."""

    return str(document_path)
def _find_document_by_relative_path(state: AppState, document_relative_path: str) -> Path:
    """Finds a scanned document by its review-queue relative path."""

    if state.scan_result is None:
        raise RuntimeError("Choose a folder before selecting a document.")

    for document_path in state.scan_result.documents:
        relative_path = _relative_path(document_path, state.scan_result.root_path)
        if relative_path == document_relative_path:
            return document_path

    raise ValueError(f"Document not found: {document_relative_path}")

def _sync_document_state(state: AppState) -> None:
    """Removes stale in-memory data after scanning or refreshing a folder."""

    if state.scan_result is None:
        state.review_data.clear()
        state.selected_documents.clear()
        state.extracted_documents.clear()
        state.failed_documents.clear()
        state.reviewed_documents.clear()
        return

    current_keys = {_document_key(document) for document in state.scan_result.documents}
    state.review_data = {
        key: value for key, value in state.review_data.items() if key in current_keys
    }
    state.selected_documents &= current_keys
    state.extracted_documents &= current_keys
    state.failed_documents &= current_keys
    state.reviewed_documents &= current_keys


def _reset_batch_extract_progress(state: AppState) -> None:
    """Clears batch extraction progress metadata."""

    state.batch_extract_running = False
    state.batch_extract_total = 0
    state.batch_extract_completed = 0
    state.batch_extract_extracted = 0
    state.batch_extract_failed = 0
    state.batch_extract_skipped = 0
    state.batch_extract_cancel_requested = False
    state.batch_extract_status = None


def _batch_extract_payload(state: AppState) -> dict[str, Any]:
    """Builds browser-facing progress data for batch extraction."""

    if state.batch_extract_total == 0 and state.batch_extract_status is None:
        return {
            "running": False,
            "total": 0,
            "completed": 0,
            "extracted": 0,
            "failed": 0,
            "skipped": 0,
            "cancelRequested": False,
            "status": None,
            "summaryText": None,
        }

    if state.batch_extract_running:
        summary_text = (
            f"{state.batch_extract_completed}/{state.batch_extract_total} documents processed. "
            f"{state.batch_extract_extracted} extracted, "
            f"{state.batch_extract_failed} errors, "
            f"{state.batch_extract_skipped} skipped."
        )
    else:
        summary_text = state.batch_extract_status

    return {
        "running": state.batch_extract_running,
        "total": state.batch_extract_total,
        "completed": state.batch_extract_completed,
        "extracted": state.batch_extract_extracted,
        "failed": state.batch_extract_failed,
        "skipped": state.batch_extract_skipped,
        "cancelRequested": state.batch_extract_cancel_requested,
        "status": state.batch_extract_status,
        "summaryText": summary_text,
    }


def _document_preview_payload(state: AppState) -> dict[str, Any]:
    """Builds preview metadata for the active document."""

    if state.active_document is None or state.scan_result is None:
        return {
            "previewUrl": None,
            "previewKind": None,
            "previewTitle": "No document selected.",
        }

    suffix = state.active_document.suffix.lower()
    if suffix == ".pdf":
        preview_kind = "pdf"
    elif suffix in {".jpg", ".jpeg", ".png"}:
        preview_kind = "image"
    else:
        preview_kind = "unsupported"

    relative_path = _relative_path(state.active_document, state.scan_result.root_path)
    return {
        "previewUrl": f"/api/document-file?path={quote(relative_path or '')}",
        "previewKind": preview_kind,
        "previewTitle": state.active_document.name,
    }


def _ensure_active_document(state: AppState) -> None:
    """Ensures there is a sensible active document after a scan or refresh.

    Args:
        state: Shared application state.
    """

    if state.scan_result is None or not state.scan_result.documents:
        state.active_document = None
        return

    available_documents = set(state.scan_result.documents)
    if state.active_document not in available_documents:
        state.active_document = state.scan_result.documents[0]


def _ensure_review_record(state: AppState, document_path: Path | None) -> None:
    """Initializes or syncs in-memory review data for a document.

    Args:
        state: Shared application state.
        document_path: Document that should have an editable review record.
    """

    if document_path is None:
        return

    schema_fields, _ = _load_schema_definition(state.active_schema)
    categories_path = state.scan_result.categories_file if state.scan_result else None
    categories, _ = _load_categories_definition(categories_path)

    record = state.review_data.setdefault(
        str(document_path),
        {"fields": {}, "categories": {}, "meta": {}},
    )

    for field in schema_fields:
        record["fields"].setdefault(field["name"], "")

    for category in categories:
        default_value: Any = "" if category["kind"] == "select" else False
        record["categories"].setdefault(category["name"], default_value)


def _build_review_payload(state: AppState) -> dict[str, Any]:
    """Builds review workspace data for the selected document.

    Args:
        state: Shared application state.

    Returns:
        Review workspace data for browser rendering.
    """

    schema_fields, schema_error = _load_schema_definition(state.active_schema)
    categories_path = state.scan_result.categories_file if state.scan_result else None
    category_definitions, categories_error = _load_categories_definition(categories_path)

    _ensure_review_record(state, state.active_document)

    record = state.review_data.get(str(state.active_document), {"fields": {}, "categories": {}})
    fields_payload = [
        {
            **field,
            "value": record["fields"].get(field["name"], ""),
        }
        for field in schema_fields
    ]
    categories_payload = [
        {
            **category,
            "value": record["categories"].get(
                category["name"],
                "" if category["kind"] == "select" else False,
            ),
        }
        for category in category_definitions
    ]

    active_document_relative = _relative_path(
        state.active_document,
        state.scan_result.root_path if state.scan_result else None,
    )
    document_queue = []
    if state.scan_result is not None:
        for document_path in state.scan_result.documents:
            document_relative = _relative_path(document_path, state.scan_result.root_path)
            document_key = _document_key(document_path)
            document_queue.append(
                {
                    "path": document_relative,
                    "selected": document_key in state.selected_documents,
                    "reviewed": document_key in state.reviewed_documents,
                    "extracted": document_key in state.extracted_documents,
                    "failed": document_key in state.failed_documents,
                    "active": document_path == state.active_document,
                }
            )

    return {
        "activeDocument": active_document_relative,
        "reviewFields": fields_payload,
        "reviewCategories": categories_payload,
        "reviewSchemaError": schema_error,
        "reviewCategoriesError": categories_error,
        "documentQueue": document_queue,
        "selectedDocumentCount": len(state.selected_documents),
        "extractableDocumentCount": sum(
            1 for item in document_queue if item["selected"] and not item["reviewed"]
        ),
        "reviewedDocuments": [
            _relative_path(Path(document_key), state.scan_result.root_path)
            for document_key in sorted(state.reviewed_documents)
        ] if state.scan_result else [],
        **_document_preview_payload(state),
    }


def _serialize_state(state: AppState) -> dict[str, Any]:
    """Converts full application state into JSON-safe browser data.

    Args:
        state: Shared application state.

    Returns:
        Serialized application state for browser rendering.
    """

    return {
        "statusMessage": state.status_message,
        "batchExtraction": _batch_extract_payload(state),
        **_serialize_scan_result(state.scan_result),
        **_read_schema_preview(state.active_schema),
        **_build_review_payload(state),
    }


def _choose_folder_with_osascript() -> Path | None:
    """Prompts the user to pick a folder using macOS AppleScript.

    Returns:
        The selected folder path, or ``None`` if the picker was canceled.
    """

    command = [
        "osascript",
        "-e",
        'POSIX path of (choose folder with prompt "Choose document root folder")',
    ]

    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )

    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        if "User canceled" in stderr:
            return None
        raise RuntimeError(stderr or "Folder picker failed.")

    selected = completed.stdout.strip()
    return Path(selected) if selected else None


def _scan_selected_folder(state: AppState, folder: Path) -> None:
    """Scans a selected folder and updates application state.

    Args:
        state: Shared application state.
        folder: Folder that should be scanned.
    """

    state.selected_folder = folder
    state.scan_result = scan_folder(folder)
    _sync_document_state(state)
    _reset_batch_extract_progress(state)

    available_schemas = set(state.scan_result.schema_files)
    if state.active_schema not in available_schemas:
        state.active_schema = get_default_active_schema(state.scan_result.schema_files)

    _ensure_active_document(state)
    _load_saved_reviews(state)
    _ensure_review_record(state, state.active_document)

    document_count = state.scan_result.document_count
    if document_count == 0:
        state.status_message = (
            "Scan complete. No supported documents were found in the selected folder."
        )
    else:
        state.status_message = (
            f"Scan complete. Found {document_count} supported documents."
        )


def _labels_path_for_state(state: AppState) -> Path:
    """Returns the labels.json path for the current scan context.

    Args:
        state: Shared application state.

    Returns:
        Path to the output labels file.

    Raises:
        RuntimeError: If no folder has been scanned yet.
    """

    if state.scan_result is None:
        raise RuntimeError("Choose a folder before saving reviews.")

    if state.scan_result.labels_file is not None:
        return state.scan_result.labels_file

    labels_path = state.scan_result.root_path / "labels.json"
    state.scan_result.labels_file = labels_path
    return labels_path


def _load_saved_reviews(state: AppState) -> None:
    """Loads saved reviews from labels.json into in-memory state.

    Args:
        state: Shared application state.
    """

    state.reviewed_documents.clear()
    state.extracted_documents.clear()
    state.failed_documents.clear()

    if state.scan_result is None or state.scan_result.labels_file is None:
        return

    labels_json, error = _read_json_file(state.scan_result.labels_file)
    if labels_json is None:
        if error:
            state.scan_result.warnings.append(
                f"Could not read {state.scan_result.labels_file.name}: {error}"
            )
        return

    records = labels_json.get("documents", labels_json)
    if not isinstance(records, list):
        state.scan_result.warnings.append("labels.json has an unexpected format.")
        return

    current_documents = {
        str(document.resolve()): document for document in state.scan_result.documents
    }

    for record in records:
        if not isinstance(record, dict):
            continue

        document_id = record.get("document_id")
        file_path = record.get("file_path")
        resolved_path: Path | None = None

        if isinstance(document_id, str) and document_id in current_documents:
            resolved_path = current_documents[document_id]
        elif isinstance(file_path, str):
            candidate = state.scan_result.root_path / file_path
            candidate_resolved = candidate.resolve()
            resolved_path = current_documents.get(str(candidate_resolved))

        if resolved_path is None:
            continue

        document_key = str(resolved_path)
        review_record = state.review_data.setdefault(
            document_key,
            {"fields": {}, "categories": {}, "meta": {}},
        )
        review_record["fields"].update(record.get("fields", {}))
        review_record["categories"].update(record.get("categories", {}))
        review_record["meta"].update(
            {
                "schema_file": record.get("schema_file"),
                "schema_path": record.get("schema_path"),
                "extracted": True,
            }
        )
        state.reviewed_documents.add(document_key)
        state.extracted_documents.add(document_key)


def _build_labels_records(state: AppState) -> list[dict[str, Any]]:
    """Builds labels.json records from the in-memory review state.

    Args:
        state: Shared application state.

    Returns:
        List of review records that should be written to labels.json.
    """

    if state.scan_result is None:
        return []

    support_root = state.scan_result.support_root or state.scan_result.root_path
    records: list[dict[str, Any]] = []

    for document_path in state.scan_result.documents:
        document_key = str(document_path)
        if document_key not in state.reviewed_documents:
            continue

        review_record = state.review_data.get(
            document_key, {"fields": {}, "categories": {}, "meta": {}}
        )
        records.append(
            {
                "document_id": str(document_path.resolve()),
                "file_name": document_path.name,
                "file_path": _relative_path(document_path, state.scan_result.root_path),
                "schema_file": review_record.get("meta", {}).get("schema_file"),
                "schema_path": review_record.get("meta", {}).get("schema_path"),
                "status": "reviewed",
                "fields": review_record.get("fields", {}),
                "categories": review_record.get("categories", {}),
            }
        )

    return records


def _csv_value(value: Any) -> str:
    """Normalizes a value for CSV export."""

    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _build_export_csv(state: AppState) -> tuple[str, str]:
    """Builds a one-row-per-document CSV export for reviewed ground truth."""

    if state.scan_result is None:
        raise RuntimeError("Choose a folder before exporting CSV.")

    records = _build_labels_records(state)
    if not records:
        raise RuntimeError("Save at least one reviewed document before exporting CSV.")

    schema_fields, _ = _load_schema_definition(state.active_schema)
    categories_path = state.scan_result.categories_file if state.scan_result else None
    category_definitions, _ = _load_categories_definition(categories_path)

    field_columns = [field["name"] for field in schema_fields if field.get("name")]
    category_columns = [
        f"category_{category['name']}"
        for category in category_definitions
        if category.get("name")
    ]

    extra_field_columns = sorted(
        {
            field_name
            for record in records
            for field_name in record.get("fields", {})
            if field_name not in field_columns
        }
    )
    extra_category_columns = sorted(
        {
            f"category_{category_name}"
            for record in records
            for category_name in record.get("categories", {})
            if f"category_{category_name}" not in category_columns
        }
    )

    headers = [
        "document_id",
        "file_name",
        "file_path",
        "schema_file",
        "schema_path",
        "status",
        *field_columns,
        *extra_field_columns,
        *category_columns,
        *extra_category_columns,
    ]

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()

    for record in records:
        row = {
            "document_id": _csv_value(record.get("document_id")),
            "file_name": _csv_value(record.get("file_name")),
            "file_path": _csv_value(record.get("file_path")),
            "schema_file": _csv_value(record.get("schema_file")),
            "schema_path": _csv_value(record.get("schema_path")),
            "status": _csv_value(record.get("status")),
        }

        for field_name, value in record.get("fields", {}).items():
            row[field_name] = _csv_value(value)

        for category_name, value in record.get("categories", {}).items():
            row[f"category_{category_name}"] = _csv_value(value)

        writer.writerow(row)

    folder_name = state.scan_result.root_path.name.replace(" ", "_") or "documents"
    file_name = f"{folder_name}_ground_truth.csv"
    return output.getvalue(), file_name


def _save_active_review(state: AppState) -> None:
    """Persists the active document review into labels.json.

    Args:
        state: Shared application state.

    Raises:
        RuntimeError: If no document is selected.
    """

    if state.active_document is None:
        raise RuntimeError("Select a document before saving.")

    _ensure_review_record(state, state.active_document)
    active_record = state.review_data[str(state.active_document)]
    active_record.setdefault("meta", {})
    active_record["meta"]["schema_file"] = (
        state.active_schema.name if state.active_schema else None
    )
    active_record["meta"]["schema_path"] = (
        _relative_path(
            state.active_schema,
            state.scan_result.support_root or state.scan_result.root_path,
        )
        if state.scan_result and state.active_schema
        else None
    )

    labels_path = _labels_path_for_state(state)
    active_key = _document_key(state.active_document)
    state.reviewed_documents.add(active_key)
    state.extracted_documents.add(active_key)
    state.failed_documents.discard(active_key)
    state.selected_documents.discard(active_key)
    payload = {"documents": _build_labels_records(state)}

    labels_path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(labels_path.parent),
        delete=False,
    ) as temporary_file:
        json.dump(payload, temporary_file, indent=2, ensure_ascii=True)
        temporary_file.write("\n")
        temp_path = Path(temporary_file.name)

    temp_path.replace(labels_path)
    state.status_message = f"Saved review for {state.active_document.name}."


def _extract_active_document(state: AppState) -> None:
    """Runs field extraction for the selected document and active schema.

    Args:
        state: Shared application state.

    Raises:
        RuntimeError: If the current review context is incomplete.
    """

    if state.active_document is None:
        raise RuntimeError("Select a document before extracting.")

    if state.active_schema is None:
        raise RuntimeError("Select an active schema before extracting.")

    _ensure_review_record(state, state.active_document)
    active_key = _document_key(state.active_document)

    result = extract_document_fields(state.active_document, state.active_schema)

    review_record = state.review_data[active_key]
    for field_name, value in result.field_values.items():
        review_record["fields"][field_name] = value
    review_record.setdefault("meta", {})
    review_record["meta"]["extracted"] = True
    state.extracted_documents.add(active_key)
    state.failed_documents.discard(active_key)

    if result.mode == "openai":
        state.status_message = (
            f"Extraction complete for {state.active_document.name} using {result.model}."
        )
        return

    if result.mode == "responses-api":
        provider = result.provider or "responses-api"
        state.status_message = (
            f"Extraction complete for {state.active_document.name} using "
            f"{provider} ({result.model})."
        )
        return

    state.status_message = (
        "Demo extraction complete. Start OCAT locally or configure OCA_TOKEN_FILE "
        "for live extraction."
    )


def _select_all_documents(state: AppState) -> None:
    """Selects all unsaved documents in the current review queue."""

    if state.scan_result is None:
        raise RuntimeError("Choose a folder before selecting documents.")
    if state.batch_extract_running:
        raise RuntimeError("Wait for batch extraction to finish before changing selection.")

    state.selected_documents = {
        _document_key(document_path)
        for document_path in state.scan_result.documents
        if _document_key(document_path) not in state.reviewed_documents
    }
    state.status_message = f"Selected {len(state.selected_documents)} documents for extraction."


def _clear_document_selection(state: AppState) -> None:
    """Clears all review-queue selections."""

    if state.batch_extract_running:
        raise RuntimeError("Wait for batch extraction to finish before changing selection.")
    state.selected_documents.clear()
    state.status_message = "Cleared document selection."


def _toggle_document_selection(state: AppState, document_relative_path: str) -> None:
    """Toggles whether a review-queue document is selected for batch extraction."""

    if state.batch_extract_running:
        raise RuntimeError("Wait for batch extraction to finish before changing selection.")

    document_path = _find_document_by_relative_path(state, document_relative_path)
    document_key = _document_key(document_path)

    if document_key in state.reviewed_documents:
        state.status_message = f"{document_path.name} is already saved and will be skipped."
        return

    if document_key in state.selected_documents:
        state.selected_documents.remove(document_key)
        state.status_message = f"Removed {document_relative_path} from selection."
        return

    state.selected_documents.add(document_key)
    state.status_message = f"Selected {document_relative_path} for extraction."


def _run_batch_extraction(state: AppState, selected_paths: list[Path]) -> None:
    """Runs extraction for selected documents on a background thread."""

    selected_keys = {_document_key(document_path) for document_path in selected_paths}
    canceled = False

    for document_path in selected_paths:
        document_key = _document_key(document_path)

        with state.lock:
            if state.batch_extract_cancel_requested:
                canceled = True
                break
            _ensure_review_record(state, document_path)
            if document_key in state.reviewed_documents:
                state.batch_extract_completed += 1
                state.batch_extract_skipped += 1
                state.batch_extract_status = (
                    f"Processing {state.batch_extract_completed}/{state.batch_extract_total} selected documents..."
                )
                continue

        try:
            result = extract_document_fields(document_path, state.active_schema)
        except Exception:
            with state.lock:
                state.failed_documents.add(document_key)
                state.batch_extract_completed += 1
                state.batch_extract_failed += 1
                state.batch_extract_status = (
                    f"Processing {state.batch_extract_completed}/{state.batch_extract_total} selected documents..."
                )
            continue

        with state.lock:
            review_record = state.review_data[document_key]
            for field_name, value in result.field_values.items():
                review_record["fields"][field_name] = value
            review_record.setdefault("meta", {})
            review_record["meta"]["extracted"] = True
            state.extracted_documents.add(document_key)
            state.failed_documents.discard(document_key)
            state.batch_extract_completed += 1
            state.batch_extract_extracted += 1
            state.batch_extract_status = (
                f"Processing {state.batch_extract_completed}/{state.batch_extract_total} selected documents..."
            )

    with state.lock:
        state.selected_documents = {
            key
            for key in state.selected_documents
            if key not in state.reviewed_documents and key in selected_keys
        }
        state.batch_extract_running = False
        if canceled:
            state.batch_extract_status = (
                f"Extraction canceled at {state.batch_extract_completed}/{state.batch_extract_total}. "
                f"Extracted {state.batch_extract_extracted}, "
                f"errors on {state.batch_extract_failed}, "
                f"skipped {state.batch_extract_skipped} already saved."
            )
        else:
            state.batch_extract_status = (
                f"Extract All complete. Extracted {state.batch_extract_extracted}, "
                f"errors on {state.batch_extract_failed}, "
                f"skipped {state.batch_extract_skipped} already saved."
            )
        state.status_message = state.batch_extract_status


def _extract_selected_documents(state: AppState) -> None:
    """Starts background extraction for all currently selected unsaved documents."""

    if state.scan_result is None:
        raise RuntimeError("Choose a folder before extracting documents.")

    if state.active_schema is None:
        raise RuntimeError("Select an active schema before extracting.")

    if state.batch_extract_running:
        raise RuntimeError("Batch extraction is already running.")

    selected_paths = [
        document_path
        for document_path in state.scan_result.documents
        if _document_key(document_path) in state.selected_documents
    ]
    if not selected_paths:
        raise RuntimeError("Select at least one document before running Extract All.")

    state.batch_extract_running = True
    state.batch_extract_total = len(selected_paths)
    state.batch_extract_completed = 0
    state.batch_extract_extracted = 0
    state.batch_extract_failed = 0
    state.batch_extract_skipped = 0
    state.batch_extract_cancel_requested = False
    state.batch_extract_status = (
        f"Starting extraction for {state.batch_extract_total} selected documents..."
    )
    state.status_message = state.batch_extract_status

    worker = threading.Thread(
        target=_run_batch_extraction,
        args=(state, selected_paths),
        daemon=True,
    )
    worker.start()


def _cancel_batch_extraction(state: AppState) -> None:
    """Requests cancellation of a running batch extraction."""

    if not state.batch_extract_running:
        raise RuntimeError("No batch extraction is currently running.")

    state.batch_extract_cancel_requested = True
    state.batch_extract_status = (
        f"Cancel requested. Finishing current document before stopping at "
        f"{state.batch_extract_completed}/{state.batch_extract_total}."
    )
    state.status_message = state.batch_extract_status


def _set_active_schema(state: AppState, schema_name: str) -> None:
    """Sets the active schema from the current scan result.

    Args:
        state: Shared application state.
        schema_name: Name of the schema selected in the UI.

    Raises:
        RuntimeError: If no folder has been scanned yet.
        ValueError: If the requested schema name does not exist.
    """

    if state.scan_result is None:
        raise RuntimeError("Choose a folder before selecting a schema.")

    for schema_path in state.scan_result.schema_files:
        if schema_path.name == schema_name:
            state.active_schema = schema_path
            _ensure_review_record(state, state.active_document)
            state.status_message = f"Active schema set to {schema_name}."
            return

    raise ValueError(f"Schema not found: {schema_name}")


def _set_active_document(state: AppState, document_relative_path: str) -> None:
    """Sets the active document from the current scan result.

    Args:
        state: Shared application state.
        document_relative_path: Relative path selected in the UI.

    Raises:
        RuntimeError: If no folder has been scanned yet.
        ValueError: If the requested document does not exist.
    """

    document_path = _find_document_by_relative_path(state, document_relative_path)
    state.active_document = document_path
    _ensure_review_record(state, state.active_document)
    state.status_message = f"Selected document: {document_relative_path}"


def _update_field_value(state: AppState, field_name: str, value: str) -> None:
    """Updates an in-memory field value for the active document.

    Args:
        state: Shared application state.
        field_name: Field name being edited.
        value: New field value.
    """

    _ensure_review_record(state, state.active_document)
    if state.active_document is None:
        raise RuntimeError("Select a document before editing fields.")

    state.review_data[str(state.active_document)]["fields"][field_name] = value
    state.status_message = f"Updated field: {field_name}"


def _update_category_value(state: AppState, category_name: str, value: Any) -> None:
    """Updates an in-memory category value for the active document.

    Args:
        state: Shared application state.
        category_name: Category being edited.
        value: New category value.
    """

    _ensure_review_record(state, state.active_document)
    if state.active_document is None:
        raise RuntimeError("Select a document before editing categories.")

    state.review_data[str(state.active_document)]["categories"][category_name] = value
    state.status_message = f"Updated category: {category_name}"


def _open_in_system_editor(file_path: Path) -> None:
    """Opens a file using the macOS default editor.

    Args:
        file_path: File that should be opened.
    """

    subprocess.run(["open", str(file_path)], check=True)


def _open_in_system_viewer(file_path: Path) -> None:
    """Opens a document using the macOS default viewer.

    Args:
        file_path: Document file that should be opened.
    """

    subprocess.run(["open", str(file_path)], check=True)


class DataLabelerHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the local Story 3 web app."""

    app_state: AppState

    def do_GET(self) -> None:  # noqa: N802
        """Handles GET requests for the UI and current state."""

        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                self._send_html(_build_html())
                return

            if parsed.path == "/api/state":
                self._send_json(_serialize_state(self.app_state))
                return

            if parsed.path == "/api/export-csv":
                csv_content, download_name = _build_export_csv(self.app_state)
                self._send_csv(csv_content, download_name)
                return

        except Exception as error:  # noqa: BLE001
            traceback.print_exc()
            self.app_state.status_message = f"Error: {error}"
            self.send_error(HTTPStatus.BAD_REQUEST, str(error))
            return

        if parsed.path == "/api/document-file":
            params = parse_qs(parsed.query)
            document_relative_path = params.get("path", [""])[0]
            document_path = _find_document_by_relative_path(
                self.app_state, document_relative_path
            )
            self._send_file(document_path)
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:  # noqa: N802
        """Handles POST requests for Story 3 actions."""

        parsed = urlparse(self.path)

        try:
            if parsed.path == "/api/choose-folder":
                folder = _choose_folder_with_osascript()
                if folder is None:
                    self.app_state.status_message = "Folder selection canceled."
                    self._send_json({"ok": True, **_serialize_state(self.app_state)})
                    return

                _scan_selected_folder(self.app_state, folder)
                self._send_json({"ok": True, **_serialize_state(self.app_state)})
                return

            if parsed.path == "/api/refresh":
                if self.app_state.selected_folder is None:
                    self.app_state.status_message = (
                        "Choose a folder before refreshing."
                    )
                    self._send_json(
                        {"ok": False, **_serialize_state(self.app_state)},
                        status=HTTPStatus.BAD_REQUEST,
                    )
                    return

                _scan_selected_folder(self.app_state, self.app_state.selected_folder)
                self._send_json({"ok": True, **_serialize_state(self.app_state)})
                return

            if parsed.path == "/api/set-active-schema":
                body = self._read_json_body()
                _set_active_schema(self.app_state, body.get("schemaName", ""))
                self._send_json({"ok": True, **_serialize_state(self.app_state)})
                return

            if parsed.path == "/api/open-active-schema":
                if self.app_state.active_schema is None:
                    raise RuntimeError("No active schema selected.")

                _open_in_system_editor(self.app_state.active_schema)
                self.app_state.status_message = (
                    f"Opened {self.app_state.active_schema.name} in the system editor."
                )
                self._send_json({"ok": True, **_serialize_state(self.app_state)})
                return

            if parsed.path == "/api/open-active-document":
                if self.app_state.active_document is None:
                    raise RuntimeError("No active document selected.")

                _open_in_system_viewer(self.app_state.active_document)
                self.app_state.status_message = (
                    f"Opened {self.app_state.active_document.name} in the system viewer."
                )
                self._send_json({"ok": True, **_serialize_state(self.app_state)})
                return

            if parsed.path == "/api/select-document":
                body = self._read_json_body()
                _set_active_document(self.app_state, body.get("documentPath", ""))
                self._send_json({"ok": True, **_serialize_state(self.app_state)})
                return

            if parsed.path == "/api/toggle-document-selection":
                body = self._read_json_body()
                _toggle_document_selection(
                    self.app_state, body.get("documentPath", "")
                )
                self._send_json({"ok": True, **_serialize_state(self.app_state)})
                return

            if parsed.path == "/api/select-all-documents":
                _select_all_documents(self.app_state)
                self._send_json({"ok": True, **_serialize_state(self.app_state)})
                return

            if parsed.path == "/api/clear-document-selection":
                _clear_document_selection(self.app_state)
                self._send_json({"ok": True, **_serialize_state(self.app_state)})
                return

            if parsed.path == "/api/update-field":
                body = self._read_json_body()
                _update_field_value(
                    self.app_state,
                    body.get("fieldName", ""),
                    body.get("value", ""),
                )
                self._send_json({"ok": True, **_serialize_state(self.app_state)})
                return

            if parsed.path == "/api/update-category":
                body = self._read_json_body()
                _update_category_value(
                    self.app_state,
                    body.get("categoryName", ""),
                    body.get("value"),
                )
                self._send_json({"ok": True, **_serialize_state(self.app_state)})
                return

            if parsed.path == "/api/extract-document":
                _extract_active_document(self.app_state)
                self._send_json({"ok": True, **_serialize_state(self.app_state)})
                return

            if parsed.path == "/api/extract-selected-documents":
                _extract_selected_documents(self.app_state)
                self._send_json({"ok": True, **_serialize_state(self.app_state)})
                return

            if parsed.path == "/api/cancel-extract-selected-documents":
                _cancel_batch_extraction(self.app_state)
                self._send_json({"ok": True, **_serialize_state(self.app_state)})
                return

            if parsed.path == "/api/save-review":
                _save_active_review(self.app_state)
                self._send_json({"ok": True, **_serialize_state(self.app_state)})
                return

        except Exception as error:  # noqa: BLE001
            traceback.print_exc()
            self.app_state.status_message = f"Error: {error}"
            self._send_json(
                {"ok": False, **_serialize_state(self.app_state)},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def log_message(self, format: str, *args: Any) -> None:
        """Suppresses default HTTP request logging noise."""

    def _read_json_body(self) -> dict[str, Any]:
        """Reads a JSON request body.

        Returns:
            Parsed JSON body.
        """

        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length) if content_length else b"{}"
        return json.loads(raw_body.decode("utf-8"))

    def _send_html(self, content: str) -> None:
        """Writes an HTML response.

        Args:
            content: HTML content to send.
        """

        encoded = content.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_json(
        self,
        payload: dict[str, Any],
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        """Writes a JSON response.

        Args:
            payload: Dictionary to encode as JSON.
            status: HTTP status code for the response.
        """

        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_csv(self, content: str, download_name: str) -> None:
        """Writes a downloadable CSV response."""

        payload = content.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{download_name}"',
        )
    def _send_file(self, file_path: Path) -> None:
        """Writes a document file response for in-app preview."""

        payload = file_path.read_bytes()
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _build_html() -> str:
    """Builds the single-page Story 3 user interface.

    Returns:
        The full HTML document used by the local browser UI.
    """

    return """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>D2R Data Labeler</title>
    <style>
      :root {
        color-scheme: light;
        --bg: #eef3f8;
        --panel: #ffffff;
        --panel-alt: #f7f9fc;
        --border: #dce4ee;
        --ink: #11253e;
        --muted: #6f8097;
        --blue: #5b8def;
        --blue-soft: #eaf1ff;
        --teal: #2f8f83;
        --teal-soft: #e9f7f4;
        --amber: #a56a00;
        --amber-soft: #fff3db;
      }

      * { box-sizing: border-box; }
      body {
        margin: 0;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        background: var(--bg);
        color: var(--ink);
      }
      .app {
        max-width: 1520px;
        margin: 0 auto;
        padding: 24px;
      }
      .header, .panel {
        background: var(--panel);
        border: 1px solid var(--border);
        border-radius: 20px;
      }
      .header {
        padding: 24px;
        margin-bottom: 20px;
      }
      .eyebrow {
        display: inline-block;
        margin: 0 0 12px;
        padding: 6px 12px;
        border-radius: 999px;
        background: var(--blue-soft);
        color: var(--blue);
        font-size: 13px;
        font-weight: 600;
      }
      h1, h2, h3 { margin: 0; }
      .subtitle {
        margin: 10px 0 0;
        color: var(--muted);
        max-width: 960px;
        line-height: 1.5;
      }
      .controls {
        display: flex;
        gap: 12px;
        flex-wrap: wrap;
        margin: 20px 0;
      }
      .top-nav {
        display: flex;
        gap: 12px;
        margin: 0 0 20px;
      }
      button {
        border: 1px solid #bcd0ff;
        border-radius: 14px;
        background: var(--blue-soft);
        color: var(--blue);
        padding: 10px 16px;
        font: inherit;
        font-weight: 600;
        cursor: pointer;
      }
      button.secondary {
        border-color: var(--border);
        background: #fff;
        color: var(--ink);
      }
      button.nav-active {
        background: var(--blue-soft);
        color: var(--blue);
      }
      button:disabled {
        opacity: 0.6;
        cursor: not-allowed;
      }
      .status {
        margin: 0 0 16px;
        padding: 14px 16px;
        border-radius: 14px;
        background: var(--panel-alt);
        border: 1px solid var(--border);
        color: var(--muted);
      }
      .grid-setup {
        display: grid;
        grid-template-columns: 360px 420px 1fr;
        gap: 20px;
      }
      .grid-review {
        display: grid;
        grid-template-columns: 360px 1fr;
        gap: 20px;
      }
      .review-shell {
        display: grid;
        grid-template-columns: minmax(360px, 0.95fr) minmax(420px, 1.05fr);
        gap: 20px;
        align-items: start;
      }
      .review-shell.preview-hidden {
        grid-template-columns: 1fr;
      }
      .panel { padding: 20px; }
      .panel-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        flex-wrap: wrap;
      }
      .summary-grid {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 12px;
      }
      .summary-card {
        background: var(--panel-alt);
        border: 1px solid var(--border);
        border-radius: 16px;
        padding: 14px;
      }
      .summary-label {
        margin: 0 0 6px;
        font-size: 12px;
        color: var(--muted);
      }
      .summary-value {
        margin: 0;
        font-size: 18px;
        font-weight: 700;
        word-break: break-word;
      }
      .badge {
        display: inline-block;
        margin-bottom: 8px;
        padding: 6px 12px;
        border-radius: 999px;
        font-size: 12px;
      }
      .badge.teal { background: var(--teal-soft); color: var(--teal); }
      .badge.blue { background: var(--blue-soft); color: var(--blue); }
      .badge.amber { background: var(--amber-soft); color: var(--amber); }
      .badge.reviewed { background: var(--teal-soft); color: var(--teal); }
      .list-shell {
        border: 1px solid var(--border);
        border-radius: 16px;
        overflow: hidden;
        background: var(--panel-alt);
      }
      .list-header {
        padding: 14px 16px;
        border-bottom: 1px solid var(--border);
        font-weight: 700;
      }
      .list-header.categories-header {
        background: var(--teal-soft);
        color: var(--teal);
      }
      .list-header.fields-header {
        background: var(--blue-soft);
        color: var(--blue);
      }
      .list-content {
        max-height: 560px;
        overflow: auto;
        background: #fff;
      }
      .row {
        padding: 12px 16px;
        border-bottom: 1px solid #edf2f7;
        font-size: 14px;
        line-height: 1.4;
        word-break: break-word;
      }
      .row:last-child { border-bottom: 0; }
      .empty { color: var(--muted); }
      .schema-title { font-weight: 700; }
      .schema-controls, .doc-controls {
        display: flex;
        gap: 8px;
        margin-top: 10px;
        align-items: center;
        flex-wrap: wrap;
      }
      .queue-toolbar {
        display: flex;
        gap: 10px;
        flex-wrap: wrap;
        margin-top: 18px;
      }
      .queue-summary {
        margin: 12px 0 0;
        color: var(--muted);
        font-size: 13px;
      }
      .batch-progress {
        margin-top: 14px;
        padding: 12px 14px;
        border: 1px solid var(--border);
        border-radius: 14px;
        background: #fff;
      }
      .batch-progress.hidden {
        display: none;
      }
      .batch-progress-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        flex-wrap: wrap;
      }
      .batch-progress-title {
        font-size: 13px;
        font-weight: 700;
      }
      .batch-progress-detail {
        margin: 8px 0 0;
        color: var(--muted);
        font-size: 13px;
      }
      .progress-track {
        margin-top: 10px;
        width: 100%;
        height: 10px;
        background: var(--panel-alt);
        border-radius: 999px;
        overflow: hidden;
      }
      .progress-fill {
        height: 100%;
        width: 0%;
        background: linear-gradient(90deg, var(--blue), #7ab2ff);
        border-radius: 999px;
        transition: width 180ms ease-out;
      }
      .doc-row-header {
        display: flex;
        gap: 10px;
        align-items: flex-start;
      }
      .doc-checkbox {
        margin-top: 3px;
      }
      .doc-title-group {
        flex: 1;
        min-width: 0;
      }
      .preview {
        max-height: 320px;
        overflow: auto;
        margin: 0;
        padding: 16px;
        background: #0f1d31;
        color: #f3f7ff;
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        font-size: 13px;
        line-height: 1.5;
        white-space: pre-wrap;
      }
      .document-preview-shell {
        min-height: 740px;
        background: #fff;
      }
      .review-shell.preview-hidden .document-preview-shell {
        display: none;
      }
      .document-preview-body {
        background: #f6f8fc;
        min-height: 700px;
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 18px;
        overflow: auto;
      }
      .document-preview-body img {
        display: block;
        max-width: 100%;
        max-height: 664px;
        width: auto;
        height: auto;
        object-fit: contain;
        border-radius: 14px;
        box-shadow: 0 8px 24px rgba(17, 37, 62, 0.08);
      }
      .document-preview-body iframe {
        width: 100%;
        height: 664px;
        border: 0;
        background: #fff;
        border-radius: 14px;
      }
      .document-preview-empty {
        padding: 28px;
        text-align: center;
        color: var(--muted);
      }
      .workspace {
        display: none;
      }
      .workspace.active {
        display: block;
      }
      .field-grid {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 16px;
        margin-top: 16px;
      }
      .field-card {
        background: var(--panel-alt);
        border: 1px solid var(--border);
        border-radius: 16px;
        padding: 14px;
      }
      .field-card label {
        display: block;
        font-size: 13px;
        font-weight: 700;
        margin-bottom: 8px;
      }
      .field-card .field-description {
        color: var(--muted);
        font-size: 12px;
        margin-bottom: 10px;
      }
      input[type="text"], input[type="url"], select, textarea {
        width: 100%;
        border: 1px solid var(--border);
        border-radius: 12px;
        padding: 10px 12px;
        font: inherit;
        background: #fff;
      }
      textarea {
        min-height: 96px;
        resize: vertical;
      }
      .category-grid {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 16px;
        margin-top: 16px;
      }
      .placeholder-actions {
        margin-top: 20px;
        display: flex;
        gap: 12px;
      }
      @media (max-width: 1180px) {
        .grid-setup, .grid-review, .review-shell, .summary-grid, .field-grid, .category-grid {
          grid-template-columns: 1fr;
        }
      }
    </style>
  </head>
  <body>
    <main class="app">
      <section class="header">
        <p class="eyebrow">Story 3</p>
        <h1>D2R Data Labeler</h1>
        <p class="subtitle">
          Choose a folder, select the active schema, and manually review
          document fields and categories in a dedicated workspace.
        </p>
      </section>

      <div class="top-nav">
        <button id="nav-setup" class="nav-active">Setup & Schema</button>
        <button id="nav-review" class="secondary">Workspace Review</button>
      </div>

      <p id="status" class="status">Loading application state...</p>

      <section id="workspace-setup" class="workspace active">
        <div class="controls">
          <button id="choose-folder">Choose Folder</button>
          <button id="refresh-folder" class="secondary" disabled>Refresh Folder</button>
        </div>

        <div class="grid-setup">
          <div class="panel">
            <h2>Folder Summary</h2>
            <p class="subtitle" id="selected-folder">No folder selected.</p>
            <p class="subtitle">
              Shared support files from:
              <strong id="support-folder">No support folder selected.</strong>
            </p>

            <div class="summary-grid" style="margin-top: 20px;">
              <article class="summary-card">
                <span class="badge amber">Documents</span>
                <p class="summary-label">Supported files</p>
                <p id="document-count" class="summary-value">0</p>
              </article>
              <article class="summary-card">
                <span class="badge blue">Schemas</span>
                <p class="summary-label">Detected schema files</p>
                <p id="schema-count" class="summary-value">0</p>
              </article>
              <article class="summary-card">
                <span class="badge teal">Categories</span>
                <p class="summary-label">Shared config</p>
                <p id="categories-file" class="summary-value">Not found</p>
              </article>
              <article class="summary-card">
                <span class="badge blue">Labels</span>
                <p class="summary-label">Existing output</p>
                <p id="labels-file" class="summary-value">Not found</p>
              </article>
            </div>

            <div class="list-shell" style="margin-top: 20px;">
              <div class="list-header">Warnings</div>
              <div id="warning-list" class="list-content"></div>
            </div>
          </div>

          <div class="panel">
            <h2>Schema List</h2>
            <p class="subtitle">
              Choose the active schema here. Editing still happens outside the app.
            </p>

            <div class="controls">
              <button id="refresh-schemas" class="secondary" disabled>Refresh Schemas</button>
              <button id="open-active-schema" class="secondary" disabled>Open Active Schema</button>
            </div>

            <div class="list-shell">
              <div class="list-header">Available Schemas</div>
              <div id="schema-list" class="list-content"></div>
            </div>
          </div>

          <div class="panel">
            <h2>Active Schema Preview</h2>
            <p class="subtitle">
              Read-only preview of the active schema and the documents available for review.
            </p>

            <div class="list-shell" style="margin-top: 20px;">
              <div class="list-header" id="active-schema-name">No active schema selected</div>
              <pre id="schema-preview" class="preview">No active schema selected.</pre>
            </div>
          </div>
        </div>
      </section>

      <section id="workspace-review" class="workspace">
        <div class="grid-review">
          <div class="panel">
            <h2>Documents</h2>
            <p class="subtitle">
              Select documents first, run Extract All, then review each document one by one.
            </p>

            <div class="queue-toolbar">
              <button id="select-all-documents" class="secondary">Select All</button>
              <button id="clear-document-selection" class="secondary">Clear Selection</button>
              <button id="extract-selected-documents">Extract All Selected</button>
              <button id="cancel-extract-selected-documents" class="secondary" disabled>Cancel Extraction</button>
            </div>
            <p id="queue-summary" class="queue-summary">
              No documents selected.
            </p>
            <div id="batch-progress" class="batch-progress hidden">
              <div class="batch-progress-header">
                <span class="batch-progress-title" id="batch-progress-title">Batch extraction</span>
                <span class="badge blue" id="batch-progress-count">0/0</span>
              </div>
              <div class="progress-track">
                <div id="batch-progress-fill" class="progress-fill"></div>
              </div>
              <p id="batch-progress-detail" class="batch-progress-detail"></p>
            </div>

            <div class="list-shell" style="margin-top: 20px;">
              <div class="list-header">Review Queue</div>
              <div id="document-review-list" class="list-content"></div>
            </div>
          </div>

          <div class="panel">
            <div class="panel-header">
              <h2>Review Workspace</h2>
              <div class="doc-controls" style="margin-top: 0;">
                <button id="toggle-preview" class="secondary">Hide Preview</button>
                <button id="open-active-document" class="secondary" disabled>Open in Viewer</button>
              </div>
            </div>
            <p class="subtitle" id="active-document-label">
              No document selected.
            </p>
            <div id="review-shell" class="review-shell" style="margin-top: 20px;">
              <div class="list-shell document-preview-shell">
                <div class="list-header" id="preview-title">Document Preview</div>
                <div id="document-preview-body" class="document-preview-body"></div>
              </div>

              <div>
                <div class="list-shell">
                  <div class="list-header categories-header">Categories</div>
                  <div style="padding: 16px; background: #fff;">
                    <p id="categories-error" class="subtitle" style="margin: 0;"></p>
                    <div id="category-grid" class="category-grid"></div>
                  </div>
                </div>

                <div class="list-shell" style="margin-top: 20px;">
                  <div class="list-header fields-header">Fields</div>
                  <div style="padding: 16px; background: #fff;">
                    <p id="fields-error" class="subtitle" style="margin: 0;"></p>
                    <div id="field-grid" class="field-grid"></div>
                  </div>
                </div>

              </div>
            </div>
            <div class="placeholder-actions">
              <button class="secondary" disabled>Save Draft</button>
              <button id="extract-document" class="secondary">Extract</button>
              <button id="save-review">Save Review</button>
              <button id="export-csv" class="secondary">Download CSV</button>
            </div>
          </div>
        </div>
      </section>
    </main>

    <script>
      const state = { activeView: "setup", batchPollTimer: null, previewVisible: true, lastPreviewPayload: null };

      const setupView = document.querySelector("#workspace-setup");
      const reviewView = document.querySelector("#workspace-review");
      const navSetupButton = document.querySelector("#nav-setup");
      const navReviewButton = document.querySelector("#nav-review");

      const statusEl = document.querySelector("#status");
      const selectedFolderEl = document.querySelector("#selected-folder");
      const supportFolderEl = document.querySelector("#support-folder");
      const documentCountEl = document.querySelector("#document-count");
      const schemaCountEl = document.querySelector("#schema-count");
      const categoriesFileEl = document.querySelector("#categories-file");
      const labelsFileEl = document.querySelector("#labels-file");
      const warningListEl = document.querySelector("#warning-list");
      const schemaListEl = document.querySelector("#schema-list");
      const activeSchemaNameEl = document.querySelector("#active-schema-name");
      const schemaPreviewEl = document.querySelector("#schema-preview");
      const documentReviewListEl = document.querySelector("#document-review-list");
      const activeDocumentLabelEl = document.querySelector("#active-document-label");
      const reviewShellEl = document.querySelector("#review-shell");
      const previewTitleEl = document.querySelector("#preview-title");
      const previewBodyEl = document.querySelector("#document-preview-body");
      const queueSummaryEl = document.querySelector("#queue-summary");
      const batchProgressEl = document.querySelector("#batch-progress");
      const batchProgressTitleEl = document.querySelector("#batch-progress-title");
      const batchProgressCountEl = document.querySelector("#batch-progress-count");
      const batchProgressFillEl = document.querySelector("#batch-progress-fill");
      const batchProgressDetailEl = document.querySelector("#batch-progress-detail");
      const categoryGridEl = document.querySelector("#category-grid");
      const fieldGridEl = document.querySelector("#field-grid");
      const categoriesErrorEl = document.querySelector("#categories-error");
      const fieldsErrorEl = document.querySelector("#fields-error");
      const openActiveDocumentButton = document.querySelector("#open-active-document");
      const togglePreviewButton = document.querySelector("#toggle-preview");
      const extractDocumentButton = document.querySelector("#extract-document");
      const saveReviewButton = document.querySelector("#save-review");
      const exportCsvButton = document.querySelector("#export-csv");
      const selectAllDocumentsButton = document.querySelector("#select-all-documents");
      const clearDocumentSelectionButton = document.querySelector("#clear-document-selection");
      const extractSelectedDocumentsButton = document.querySelector("#extract-selected-documents");
      const cancelExtractSelectedDocumentsButton = document.querySelector("#cancel-extract-selected-documents");

      const chooseFolderButton = document.querySelector("#choose-folder");
      const refreshFolderButton = document.querySelector("#refresh-folder");
      const refreshSchemasButton = document.querySelector("#refresh-schemas");
      const openActiveSchemaButton = document.querySelector("#open-active-schema");

      function stopBatchPolling() {
        if (state.batchPollTimer) {
          window.clearTimeout(state.batchPollTimer);
          state.batchPollTimer = null;
        }
      }

      async function pollBatchProgress() {
        stopBatchPolling();
        const payload = await requestJson("/api/state");
        renderState(payload);
        if (payload.batchExtraction && payload.batchExtraction.running) {
          state.batchPollTimer = window.setTimeout(pollBatchProgress, 800);
        }
      }

      function ensureBatchPolling(batchExtraction) {
        if (batchExtraction && batchExtraction.running) {
          if (!state.batchPollTimer) {
            state.batchPollTimer = window.setTimeout(pollBatchProgress, 800);
          }
          return;
        }
        stopBatchPolling();
      }

      function setActiveView(viewName) {
        state.activeView = viewName;
        const setupActive = viewName === "setup";
        setupView.classList.toggle("active", setupActive);
        reviewView.classList.toggle("active", !setupActive);
        navSetupButton.className = setupActive ? "nav-active" : "secondary";
        navReviewButton.className = setupActive ? "secondary" : "nav-active";
      }

      async function requestJson(url, options = {}) {
        const response = await fetch(url, options);
        return response.json();
      }

      function renderRows(container, items, emptyText) {
        container.innerHTML = "";
        if (!items.length) {
          const row = document.createElement("div");
          row.className = "row empty";
          row.textContent = emptyText;
          container.appendChild(row);
          return;
        }
        for (const item of items) {
          const row = document.createElement("div");
          row.className = "row";
          row.textContent = item;
          container.appendChild(row);
        }
      }

      function renderSchemaRows(payload) {
        schemaListEl.innerHTML = "";
        if (!payload.schemaFiles.length) {
          const row = document.createElement("div");
          row.className = "row empty";
          row.textContent = "No schema files found";
          schemaListEl.appendChild(row);
          return;
        }

        for (const schemaName of payload.schemaFiles) {
          const row = document.createElement("div");
          row.className = "row";

          const title = document.createElement("div");
          title.className = "schema-title";
          title.textContent = schemaName;
          row.appendChild(title);

          const controls = document.createElement("div");
          controls.className = "schema-controls";

          if (payload.activeSchema === schemaName) {
            const badge = document.createElement("span");
            badge.className = "badge teal";
            badge.textContent = "Active";
            controls.appendChild(badge);
          } else {
            const button = document.createElement("button");
            button.className = "secondary";
            button.textContent = "Set Active";
            button.addEventListener("click", async () => {
              statusEl.textContent = `Setting active schema to ${schemaName}...`;
              const nextPayload = await requestJson("/api/set-active-schema", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ schemaName })
              });
              renderState(nextPayload);
            });
            controls.appendChild(button);
          }

          row.appendChild(controls);
          schemaListEl.appendChild(row);
        }
      }

      function renderDocumentReviewRows(payload) {
        documentReviewListEl.innerHTML = "";
        const batchRunning = Boolean(payload.batchExtraction && payload.batchExtraction.running);
        if (!payload.documentQueue.length) {
          const row = document.createElement("div");
          row.className = "row empty";
          row.textContent = "No supported documents found";
          documentReviewListEl.appendChild(row);
          return;
        }

        for (const queueItem of payload.documentQueue) {
          const documentPath = queueItem.path;
          const row = document.createElement("div");
          row.className = "row";

          const header = document.createElement("div");
          header.className = "doc-row-header";

          if (!queueItem.reviewed) {
            const checkbox = document.createElement("input");
            checkbox.type = "checkbox";
            checkbox.className = "doc-checkbox";
            checkbox.checked = Boolean(queueItem.selected);
            checkbox.disabled = batchRunning;
            checkbox.addEventListener("change", async () => {
              const nextPayload = await requestJson("/api/toggle-document-selection", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ documentPath })
              });
              renderState(nextPayload);
            });
            header.appendChild(checkbox);
          }

          const titleGroup = document.createElement("div");
          titleGroup.className = "doc-title-group";

          const title = document.createElement("div");
          title.className = "schema-title";
          title.textContent = documentPath;
          titleGroup.appendChild(title);
          header.appendChild(titleGroup);
          row.appendChild(header);

          const controls = document.createElement("div");
          controls.className = "doc-controls";

          if (queueItem.active) {
            const badge = document.createElement("span");
            badge.className = "badge blue";
            badge.textContent = "Viewing";
            controls.appendChild(badge);
          }

          if (queueItem.reviewed) {
            const reviewedBadge = document.createElement("span");
            reviewedBadge.className = "badge reviewed";
            reviewedBadge.textContent = "Saved";
            controls.appendChild(reviewedBadge);
          } else if (queueItem.extracted) {
            const extractedBadge = document.createElement("span");
            extractedBadge.className = "badge teal";
            extractedBadge.textContent = "Extracted";
            controls.appendChild(extractedBadge);
          } else if (queueItem.failed) {
            const failedBadge = document.createElement("span");
            failedBadge.className = "badge amber";
            failedBadge.textContent = "Manual Review";
            controls.appendChild(failedBadge);
          }

          if (!queueItem.active) {
            const button = document.createElement("button");
            button.className = "secondary";
            button.textContent = "Open";
            button.disabled = batchRunning;
            button.addEventListener("click", async () => {
              statusEl.textContent = `Selecting ${documentPath}...`;
              const nextPayload = await requestJson("/api/select-document", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ documentPath })
              });
              renderState(nextPayload);
            });
            controls.appendChild(button);
          }

          row.appendChild(controls);
          documentReviewListEl.appendChild(row);
        }
      }

      function renderPreview(payload) {
        state.lastPreviewPayload = payload;
        reviewShellEl.classList.toggle("preview-hidden", !state.previewVisible);
        togglePreviewButton.textContent = state.previewVisible ? "Hide Preview" : "Show Preview";

        if (!state.previewVisible) {
          previewTitleEl.textContent = payload.previewTitle || "Document Preview";
          previewBodyEl.innerHTML = "";
          return;
        }

        previewTitleEl.textContent = payload.previewTitle || "Document Preview";
        previewBodyEl.innerHTML = "";

        if (!payload.previewUrl || !payload.previewKind) {
          const empty = document.createElement("div");
          empty.className = "document-preview-empty";
          empty.textContent = "Select a document to preview it side by side with the extracted content.";
          previewBodyEl.appendChild(empty);
          return;
        }

        if (payload.previewKind === "image") {
          const image = document.createElement("img");
          image.src = payload.previewUrl;
          image.alt = payload.previewTitle || "Selected document";
          previewBodyEl.appendChild(image);
          return;
        }

        if (payload.previewKind === "pdf") {
          const frame = document.createElement("iframe");
          frame.src = payload.previewUrl;
          frame.title = payload.previewTitle || "Selected document";
          previewBodyEl.appendChild(frame);
          return;
        }

        const unsupported = document.createElement("div");
        unsupported.className = "document-preview-empty";
        unsupported.textContent = "This file type cannot be previewed inline.";
        previewBodyEl.appendChild(unsupported);
      }

      function renderBatchProgress(payload) {
        const batch = payload.batchExtraction || {};
        const total = batch.total || 0;
        const completed = batch.completed || 0;
        const showProgress = Boolean(batch.running || total > 0 || batch.summaryText);
        batchProgressEl.classList.toggle("hidden", !showProgress);

        if (!showProgress) {
          return;
        }

        const percent = total > 0 ? Math.round((completed / total) * 100) : 0;
        batchProgressTitleEl.textContent = batch.running
          ? "Extracting selected documents"
          : "Last batch extraction";
        batchProgressCountEl.textContent = `${completed}/${total}`;
        batchProgressFillEl.style.width = `${percent}%`;
        batchProgressDetailEl.textContent = batch.summaryText || "No batch extraction has run yet.";
      }

      function renderCategories(payload) {
        const batchRunning = Boolean(payload.batchExtraction && payload.batchExtraction.running);
        categoriesErrorEl.textContent = payload.reviewCategoriesError
          ? `Categories unavailable: ${payload.reviewCategoriesError}`
          : "";
        categoryGridEl.innerHTML = "";

        if (!payload.reviewCategories.length) {
          const empty = document.createElement("p");
          empty.className = "subtitle";
          empty.textContent = "No categories configured for this folder.";
          categoryGridEl.appendChild(empty);
          return;
        }

        for (const category of payload.reviewCategories) {
          const card = document.createElement("div");
          card.className = "field-card";

          const label = document.createElement("label");
          label.textContent = category.name;
          card.appendChild(label);

          if (category.description) {
            const description = document.createElement("div");
            description.className = "field-description";
            description.textContent = category.description;
            card.appendChild(description);
          }

          if (category.kind === "select") {
            const select = document.createElement("select");

            const emptyOption = document.createElement("option");
            emptyOption.value = "";
            emptyOption.textContent = "Choose...";
            select.appendChild(emptyOption);

            for (const optionValue of category.options) {
              const option = document.createElement("option");
              option.value = optionValue;
              option.textContent = optionValue;
              if (category.value === optionValue) {
                option.selected = true;
              }
              select.appendChild(option);
            }

            select.disabled = batchRunning;
            select.addEventListener("change", async (event) => {
              const nextPayload = await requestJson("/api/update-category", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                  categoryName: category.name,
                  value: event.target.value
                })
              });
              renderState(nextPayload);
            });
            card.appendChild(select);
          } else {
            const checkbox = document.createElement("input");
            checkbox.type = "checkbox";
            checkbox.checked = Boolean(category.value);
            checkbox.disabled = batchRunning;
            checkbox.addEventListener("change", async (event) => {
              const nextPayload = await requestJson("/api/update-category", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                  categoryName: category.name,
                  value: event.target.checked
                })
              });
              renderState(nextPayload);
            });
            card.appendChild(checkbox);
          }

          categoryGridEl.appendChild(card);
        }
      }

      function renderFields(payload) {
        const batchRunning = Boolean(payload.batchExtraction && payload.batchExtraction.running);
        fieldsErrorEl.textContent = payload.reviewSchemaError
          ? `Fields unavailable: ${payload.reviewSchemaError}`
          : "";
        fieldGridEl.innerHTML = "";

        if (!payload.reviewFields.length) {
          const empty = document.createElement("p");
          empty.className = "subtitle";
          empty.textContent = "No schema fields available.";
          fieldGridEl.appendChild(empty);
          return;
        }

        for (const field of payload.reviewFields) {
          const card = document.createElement("div");
          card.className = "field-card";

          const label = document.createElement("label");
          label.textContent = field.label;
          card.appendChild(label);

          if (field.description) {
            const description = document.createElement("div");
            description.className = "field-description";
            description.textContent = field.description;
            card.appendChild(description);
          }

          const input = field.name === "other"
            ? document.createElement("textarea")
            : document.createElement("input");

          if (input.tagName === "INPUT") {
            input.type = field.type === "url" ? "url" : "text";
          }
          input.value = field.value || "";
          input.disabled = batchRunning;
          input.addEventListener("change", async (event) => {
            const nextPayload = await requestJson("/api/update-field", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                fieldName: field.name,
                value: event.target.value
              })
            });
            renderState(nextPayload);
          });

          card.appendChild(input);
          fieldGridEl.appendChild(card);
        }
      }

      function renderState(payload) {
        statusEl.textContent = payload.statusMessage;
        selectedFolderEl.textContent = payload.selectedFolder || "No folder selected.";
        supportFolderEl.textContent = payload.supportFolder || "No support folder selected.";
        documentCountEl.textContent = String(payload.documentCount);
        schemaCountEl.textContent = String(payload.schemaCount);
        categoriesFileEl.textContent = payload.categoriesFile || "Not found";
        labelsFileEl.textContent = payload.labelsFile || "Not found";
        const batchRunning = Boolean(payload.batchExtraction && payload.batchExtraction.running);
        refreshFolderButton.disabled = !payload.selectedFolder;
        refreshSchemasButton.disabled = !payload.selectedFolder;
        openActiveSchemaButton.disabled = !payload.activeSchema;
        openActiveDocumentButton.disabled = !payload.activeDocument;
        extractDocumentButton.disabled = batchRunning || !payload.activeDocument || !payload.activeSchema;
        extractSelectedDocumentsButton.disabled = batchRunning || !payload.activeSchema || payload.extractableDocumentCount === 0;
        cancelExtractSelectedDocumentsButton.disabled = !batchRunning || Boolean(payload.batchExtraction && payload.batchExtraction.cancelRequested);
        selectAllDocumentsButton.disabled = batchRunning || !payload.documentQueue.length;
        clearDocumentSelectionButton.disabled = batchRunning || payload.selectedDocumentCount === 0;
        saveReviewButton.disabled = batchRunning || !payload.activeDocument;
        exportCsvButton.disabled = payload.reviewedDocuments.length === 0;
        navReviewButton.disabled = !payload.selectedFolder;
        queueSummaryEl.textContent = `${payload.selectedDocumentCount} selected, ${payload.extractableDocumentCount} ready to extract, ${payload.reviewedDocuments.length} already saved.`;
        activeSchemaNameEl.textContent = payload.activeSchema || "No active schema selected";
        schemaPreviewEl.textContent = payload.schemaPreviewError
          ? `Preview unavailable: ${payload.schemaPreviewError}`
          : payload.schemaPreview;
        activeDocumentLabelEl.textContent = payload.activeDocument
          ? `Selected document: ${payload.activeDocument}`
          : "No document selected.";

        renderSchemaRows(payload);
        renderDocumentReviewRows(payload);
        renderPreview(payload);
        renderBatchProgress(payload);
        renderPreview(payload);
        renderRows(warningListEl, payload.warnings, "No warnings");
        renderCategories(payload);
        renderFields(payload);
        ensureBatchPolling(payload.batchExtraction);
      }

      async function loadState() {
        const payload = await requestJson("/api/state");
        renderState(payload);
      }

      navSetupButton.addEventListener("click", () => setActiveView("setup"));
      navReviewButton.addEventListener("click", () => setActiveView("review"));

      chooseFolderButton.addEventListener("click", async () => {
        statusEl.textContent = "Opening folder picker...";
        const payload = await requestJson("/api/choose-folder", { method: "POST" });
        renderState(payload);
      });

      refreshFolderButton.addEventListener("click", async () => {
        statusEl.textContent = "Refreshing folder...";
        const payload = await requestJson("/api/refresh", { method: "POST" });
        renderState(payload);
      });

      refreshSchemasButton.addEventListener("click", async () => {
        statusEl.textContent = "Refreshing schema list...";
        const payload = await requestJson("/api/refresh", { method: "POST" });
        renderState(payload);
      });

      selectAllDocumentsButton.addEventListener("click", async () => {
        statusEl.textContent = "Selecting all unsaved documents...";
        const payload = await requestJson("/api/select-all-documents", { method: "POST" });
        renderState(payload);
      });

      clearDocumentSelectionButton.addEventListener("click", async () => {
        statusEl.textContent = "Clearing document selection...";
        const payload = await requestJson("/api/clear-document-selection", { method: "POST" });
        renderState(payload);
      });

      extractSelectedDocumentsButton.addEventListener("click", async () => {
        statusEl.textContent = "Extracting selected documents...";
        const payload = await requestJson("/api/extract-selected-documents", { method: "POST" });
        renderState(payload);
        ensureBatchPolling(payload.batchExtraction);
      });

      cancelExtractSelectedDocumentsButton.addEventListener("click", async () => {
        statusEl.textContent = "Canceling extraction...";
        const payload = await requestJson("/api/cancel-extract-selected-documents", { method: "POST" });
        renderState(payload);
        ensureBatchPolling(payload.batchExtraction);
      });

      openActiveSchemaButton.addEventListener("click", async () => {
        statusEl.textContent = "Opening active schema...";
        const payload = await requestJson("/api/open-active-schema", { method: "POST" });
        renderState(payload);
      });

      saveReviewButton.addEventListener("click", async () => {
        statusEl.textContent = "Saving review...";
        const payload = await requestJson("/api/save-review", { method: "POST" });
        renderState(payload);
      });

      extractDocumentButton.addEventListener("click", async () => {
        statusEl.textContent = "Extracting fields...";
        const payload = await requestJson("/api/extract-document", { method: "POST" });
        renderState(payload);
      });

      exportCsvButton.addEventListener("click", () => {
        statusEl.textContent = "Preparing CSV download...";
        window.location.href = "/api/export-csv";
      });

      openActiveDocumentButton.addEventListener("click", async () => {
        statusEl.textContent = "Opening active document...";
        const payload = await requestJson("/api/open-active-document", { method: "POST" });
        renderState(payload);
      });

      togglePreviewButton.addEventListener("click", () => {
        state.previewVisible = !state.previewVisible;
        renderPreview(state.lastPreviewPayload || {});
      });

      loadState();
    </script>
  </body>
</html>
"""


def launch_app() -> None:
    """Launches the local browser-based application."""

    state = AppState()

    class BoundHandler(DataLabelerHandler):
        """Request handler bound to the shared application state."""

        app_state = state

    server = ThreadingHTTPServer((HOST, PORT), BoundHandler)
    url = f"http://{HOST}:{PORT}"

    print(f"Data Labeler running at {url}")
    print("Press Ctrl+C to stop the server.")

    threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\\nShutting down Data Labeler...")
    finally:
        server.server_close()
