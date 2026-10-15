"""Verify installation structure, excluded local data, and release integrity."""

import hashlib
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path

from tools import release


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="usage-limiter-release-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "project"
        self.root.mkdir()
        for relative in release.SOURCE_FILES:
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(release.read_public_file(release.ROOT, relative))
        self.output = Path(temporary.name) / "artifacts"

    def test_repository_metadata_and_schema_are_consistent(self):
        metadata = release.validate_project(self.root)
        self.assertEqual(metadata["author"], "xmlans")
        self.assertEqual(metadata["version"], "1.0.0")

    def test_tag_must_match_version_before_writing_archives(self):
        with self.assertRaisesRegex(ValueError, "disagrees"):
            release.build_archives(self.root, self.output, "v9.9.9")
        self.assertFalse(self.output.exists())

    def test_archive_structure_excludes_private_data_and_keeps_license(self):
        private_marker = uuid.uuid4().hex
        for relative in (".env", "data/usage_data.json", "__pycache__/main.pyc", "local-notes.txt"):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(private_marker, encoding="utf-8")
        install, source, checksums = release.build_archives(self.root, self.output, "v1.0.0")
        prefix = "astrbot_plugin_usage_limiter/"
        with zipfile.ZipFile(install) as archive:
            self.assertEqual(
                set(archive.namelist()), {prefix + name for name in release.INSTALL_FILES}
            )
            for required in (
                "main.py",
                "metadata.yaml",
                "_conf_schema.json",
                "LICENSE",
                "logo.png",
            ):
                self.assertIn(prefix + required, archive.namelist())
            self.assertEqual(
                archive.read(prefix + "logo.png"), (self.root / "logo.png").read_bytes()
            )
            self.assertNotIn(prefix + "tests/test_usage_limiter.py", archive.namelist())
            extracted = self.output / "installation"
            archive.extractall(extracted)
            self.assertTrue((extracted / prefix / "main.py").is_file())
        with zipfile.ZipFile(source) as archive:
            self.assertIn(prefix + ".github/workflows/ci.yml", archive.namelist())
            self.assertIn(prefix + "tests/test_usage_limiter.py", archive.namelist())
            self.assertEqual(
                archive.read(prefix + "logo.png"), (self.root / "logo.png").read_bytes()
            )
            for name in archive.namelist():
                self.assertNotIn(private_marker.encode("utf-8"), archive.read(name))
        for line in checksums.read_text(encoding="utf-8").splitlines():
            digest, filename = line.split("  ")
            self.assertEqual(
                digest, hashlib.sha256((self.output / filename).read_bytes()).hexdigest()
            )

    def test_repeated_builds_are_reproducible_across_line_endings(self):
        first = release.build_archives(self.root, self.output)
        original = [path.read_bytes() for path in first]
        for relative in release.SOURCE_FILES:
            if relative in release.BINARY_FILES:
                continue
            target = self.root / relative
            target.write_bytes(target.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        second = release.build_archives(self.root, self.output)
        self.assertEqual(original, [path.read_bytes() for path in second])

    def test_mismatched_plugin_version_is_rejected(self):
        target = self.root / "main.py"
        target.write_text(
            target.read_text(encoding="utf-8").replace(
                'PLUGIN_VERSION = "1.0.0"', 'PLUGIN_VERSION = "9.9.9"'
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "PLUGIN_VERSION"):
            release.validate_project(self.root)

    def test_unsupported_configuration_type_is_rejected(self):
        target = self.root / "_conf_schema.json"
        target.write_text(
            target.read_text(encoding="utf-8").replace('"type": "text"', '"type": "dict"'),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "Unsupported AstrBot 4.0"):
            release.validate_project(self.root)


if __name__ == "__main__":
    unittest.main()
