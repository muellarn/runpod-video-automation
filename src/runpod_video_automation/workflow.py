from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_workflow(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("The API workflow must be a JSON object")
    if not all(isinstance(node, dict) and "class_type" in node for node in value.values()):
        raise ValueError("Export the workflow with ComfyUI's 'Export (API)' command")
    return value


def apply_overrides(workflow: dict[str, Any], overrides: list[str]) -> None:
    for override in overrides:
        target, separator, raw_value = override.partition("=")
        if not separator or "." not in target:
            raise ValueError(f"Invalid override {override!r}; expected NODE.INPUT=JSON")
        node_id, input_name = target.split(".", 1)
        node = workflow.get(node_id)
        if not isinstance(node, dict):
            raise ValueError(f"Workflow has no node {node_id!r}")
        inputs = node.get("inputs")
        if not isinstance(inputs, dict) or input_name not in inputs:
            raise ValueError(f"Node {node_id!r} has no input {input_name!r}")
        try:
            inputs[input_name] = json.loads(raw_value)
        except json.JSONDecodeError:
            inputs[input_name] = raw_value


def collect_output_files(history: dict[str, Any]) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []

    def visit(value: object) -> None:
        if isinstance(value, dict):
            filename = value.get("filename")
            if isinstance(filename, str):
                found.append(
                    {
                        "filename": filename,
                        "subfolder": str(value.get("subfolder", "")),
                        "type": str(value.get("type", "output")),
                    }
                )
                return
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(history.get("outputs", {}))
    unique: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in found:
        key = (item["filename"], item["subfolder"], item["type"])
        if key not in seen and item["type"] != "temp":
            seen.add(key)
            unique.append(item)
    return unique
