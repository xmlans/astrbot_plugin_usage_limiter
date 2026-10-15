"""Validate public repository files and build deterministic, allowlisted ZIPs."""

import argparse
import ast
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
INSTALL_FILES = (
    "main.py",
    "_conf_schema.json",
    "metadata.yaml",
    "requirements.txt",
    "README.md",
    "logo.png",
    "LICENSE",
    "CHANGELOG.md",
    "examples/config.example.json",
)
SOURCE_FILES = INSTALL_FILES + (
    ".gitignore",
    ".gitattributes",
    "requirements-dev.txt",
    "pyproject.toml",
    "CONTRIBUTING.md",
    "PUBLISH.md",
    "RELEASE_NOTES.md",
    ".github/workflows/ci.yml",
    ".github/workflows/release.yml",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
    ".github/ISSUE_TEMPLATE/config.yml",
    ".github/pull_request_template.md",
    "tools/release.py",
    "tests/test_usage_limiter.py",
    "tests/test_release.py",
)
# Configuration types accepted by the minimum supported AstrBot release (4.0.0).
CONFIG_TYPES = {"int", "float", "bool", "string", "text", "list", "object"}
MAX_INSTALL_SIZE = 16 * 1024 * 1024
BINARY_FILES = frozenset({"logo.png"})


def read_public_file(root: Path, relative: str) -> bytes:
    root = root.resolve()
    target = root / relative
    if not target.resolve().is_relative_to(root) or target.is_symlink():
        raise ValueError(f"Unsafe public file path: {relative}")
    if not target.is_file():
        raise ValueError(f"Missing public file: {relative}")
    return target.read_bytes()


def validate_project(root: Path = ROOT) -> dict:
    files = {name: read_public_file(root, name) for name in SOURCE_FILES}
    for name, data in files.items():
        if name in BINARY_FILES:
            continue
        files[name] = data.replace(b"\r\n", b"\n")
        data = files[name]
        text = data.decode("utf-8")
        if re.search(r"[A-Za-z]:[\\/](?:Users|project)[\\/]", text):
            raise ValueError(f"Local absolute path in public file: {name}")
        if re.search(
            r"https://github\.com/(?:your[-_]?name|your[-_]?username|example)/", text, re.I
        ):
            raise ValueError(f"Placeholder repository URL in {name}")

    metadata = yaml.safe_load(files["metadata.yaml"])
    expected_identity = {
        "name": "astrbot_plugin_usage_limiter",
        "author": "xmlans",
        "repo": "https://github.com/xmlans/astrbot_plugin_usage_limiter",
        "astrbot_version": ">=4.0.0",
    }
    for field, value in expected_identity.items():
        if metadata.get(field) != value:
            raise ValueError(f"Unexpected metadata {field}")
    version = metadata.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Metadata version must be a semantic version string")

    tree = ast.parse(files["main.py"].decode("utf-8"), filename="main.py", feature_version=(3, 10))
    constants = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name) and target.id.startswith("PLUGIN_"):
                    constants[target.id] = ast.literal_eval(statement.value)
    for constant, field in (
        ("PLUGIN_NAME", "name"),
        ("PLUGIN_VERSION", "version"),
        ("PLUGIN_AUTHOR", "author"),
        ("PLUGIN_REPO", "repo"),
    ):
        if constants.get(constant) != metadata[field]:
            raise ValueError(f"{constant} disagrees with metadata")

    schema = json.loads(files["_conf_schema.json"])
    example = json.loads(files["examples/config.example.json"])
    if set(example) != set(schema):
        raise ValueError("Example configuration fields disagree with schema")
    for field, item in schema.items():
        if item.get("type") not in CONFIG_TYPES or "default" not in item:
            raise ValueError(f"Unsupported AstrBot 4.0 configuration field: {field}")
    for field in ("custom_limits", "custom_5h_limits"):
        if schema[field]["type"] != "text":
            raise ValueError("Custom limits must use a compatible JSON text field")
        for value in (schema[field]["default"], example[field]):
            if not isinstance(value, str) or not isinstance(json.loads(value), dict):
                raise ValueError("Custom limits must contain a JSON object string")
    if schema["reset_all_usage"]["default"] is not False or example["reset_all_usage"] is not False:
        raise ValueError("Public configuration must not request a global usage reset")

    license_text = files["LICENSE"].decode("utf-8")
    if (
        not license_text.startswith("MIT License\n")
        or "Copyright (c) 2026 xmlans" not in license_text
    ):
        raise ValueError("MIT license or author statement is missing")
    for document in ("README.md", "RELEASE_NOTES.md", "CHANGELOG.md"):
        if version not in files[document].decode("utf-8"):
            raise ValueError(f"Release version missing from {document}")

    for name, data in files.items():
        if name.endswith((".yml", ".yaml")):
            yaml.safe_load(data)
    for filename in (".github/workflows/ci.yml", ".github/workflows/release.yml"):
        workflow = yaml.safe_load(files[filename])
        if not isinstance(workflow.get("on"), dict) or workflow.get("permissions") != {
            "contents": "read"
        }:
            raise ValueError(f"Workflow events or default permissions invalid: {filename}")
        for job in workflow["jobs"].values():
            for step in job["steps"]:
                if "uses" in step and not re.fullmatch(
                    r"actions/[a-z-]+@[0-9a-f]{40}", step["uses"]
                ):
                    raise ValueError(f"Unpinned action in {filename}")
    return metadata


def write_archive(root: Path, destination: Path, name: str, files: tuple) -> None:
    with zipfile.ZipFile(
        destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for relative in sorted(files):
            info = zipfile.ZipInfo(f"{name}/{relative}", date_time=(2026, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            payload = read_public_file(root, relative)
            # Normalize text line endings while preserving image bytes exactly.
            if relative not in BINARY_FILES:
                payload = payload.replace(b"\r\n", b"\n")
            archive.writestr(info, payload, compresslevel=9)


def build_archives(
    root: Path = ROOT, output: Path | None = None, tag: str | None = None
) -> list[Path]:
    metadata = validate_project(root)
    expected_tag = f"v{metadata['version']}"
    if tag is not None and tag != expected_tag:
        raise ValueError(f"Tag {tag!r} disagrees with metadata; expected {expected_tag}")
    output = output or root / "dist"
    output.mkdir(parents=True, exist_ok=True)
    stem = f"{metadata['name']}-{expected_tag}"
    install = output / f"{stem}.zip"
    source = output / f"{stem}-source.zip"
    write_archive(root, install, metadata["name"], INSTALL_FILES)
    if install.stat().st_size > MAX_INSTALL_SIZE:
        raise ValueError("Installation archive exceeds 16 MiB")
    write_archive(root, source, metadata["name"], SOURCE_FILES)
    checksums = output / "SHA256SUMS.txt"
    lines = [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        for path in (install, source)
    ]
    checksums.write_bytes("".join(lines).encode("utf-8"))
    return [install, source, checksums]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "build"))
    parser.add_argument("--output", type=Path, help="Archive output directory (default: dist)")
    parser.add_argument("--tag", help="Require an exact v<version> release tag")
    args = parser.parse_args()
    try:
        if args.command == "check":
            metadata = validate_project()
            print(f"Public files validated: {metadata['name']} v{metadata['version']}")
        else:
            for path in build_archives(output=args.output, tag=args.tag):
                print(path)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"Release check failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
