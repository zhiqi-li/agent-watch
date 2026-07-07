from __future__ import annotations

import importlib.util
import json
import pathlib
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile


ROOT = pathlib.Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "package_cursor_extension.py"
SPEC = importlib.util.spec_from_file_location("package_cursor_extension", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load {SCRIPT}")
packager = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(packager)


class CursorExtensionPackageTests(unittest.TestCase):
    def test_vsix_manifest_matches_extension_and_build_is_reproducible(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = pathlib.Path(tmp) / "first.vsix"
            second = pathlib.Path(tmp) / "second.vsix"
            packager.build(first)
            packager.build(second)
            self.assertEqual(first.read_bytes(), second.read_bytes())

            with zipfile.ZipFile(first) as archive:
                names = set(archive.namelist())
                self.assertEqual(
                    names,
                    {
                        "[Content_Types].xml",
                        "extension.vsixmanifest",
                        "extension/LICENSE",
                        "extension/README.md",
                        "extension/extension.js",
                        "extension/package.json",
                        "extension/protocol.js",
                    },
                )
                package = json.loads(archive.read("extension/package.json"))
                self.assertIn(f"extension/{package['main'].removeprefix('./')}", names)

                namespace = {
                    "vsix": "http://schemas.microsoft.com/developer/vsx-schema/2011"
                }
                root = ET.fromstring(archive.read("extension.vsixmanifest"))
                identity = root.find("vsix:Metadata/vsix:Identity", namespace)
                self.assertIsNotNone(identity)
                self.assertEqual(identity.get("Id"), package["name"])
                self.assertEqual(identity.get("Publisher"), package["publisher"])
                self.assertEqual(identity.get("Version"), package["version"])

                properties = {
                    item.get("Id"): item.get("Value")
                    for item in root.findall(
                        "vsix:Metadata/vsix:Properties/vsix:Property",
                        namespace,
                    )
                }
                self.assertEqual(
                    properties["Microsoft.VisualStudio.Code.Engine"],
                    package["engines"]["vscode"],
                )
                self.assertEqual(
                    properties["Microsoft.VisualStudio.Code.ExtensionKind"],
                    "workspace",
                )

                assets = {
                    asset.get("Path")
                    for asset in root.findall("vsix:Assets/vsix:Asset", namespace)
                }
                self.assertLessEqual(assets, names)


if __name__ == "__main__":
    unittest.main()
