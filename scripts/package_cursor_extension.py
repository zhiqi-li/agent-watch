#!/usr/bin/env python3
"""Build the dependency-free Cursor companion extension as a VSIX archive."""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import zipfile
from xml.sax.saxutils import escape, quoteattr


ROOT = pathlib.Path(__file__).resolve().parents[1]
EXTENSION_ROOT = ROOT / "cursor-extension"
PACKAGE_FILES = (
    "package.json",
    "extension.js",
    "protocol.js",
    "README.md",
)
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")


def read_manifest() -> dict[str, object]:
    value = json.loads((EXTENSION_ROOT / "package.json").read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("cursor-extension/package.json must contain an object")
    return value


def required_string(manifest: dict[str, object], key: str) -> str:
    value = manifest.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"extension manifest field {key!r} must be a non-empty string")
    return value.strip()


def vsix_manifest(manifest: dict[str, object]) -> str:
    name = required_string(manifest, "name")
    publisher = required_string(manifest, "publisher")
    version = required_string(manifest, "version")
    display_name = required_string(manifest, "displayName")
    description = required_string(manifest, "description")
    engines = manifest.get("engines")
    if not isinstance(engines, dict) or not isinstance(engines.get("vscode"), str):
        raise ValueError("extension manifest must declare engines.vscode")
    engine = str(engines["vscode"])
    if not IDENTIFIER_RE.fullmatch(name) or not IDENTIFIER_RE.fullmatch(publisher):
        raise ValueError("extension name and publisher contain unsupported characters")
    if not VERSION_RE.fullmatch(version):
        raise ValueError("extension version must be semantic versioning")

    categories = manifest.get("categories", ["Other"])
    if not isinstance(categories, list) or not all(
        isinstance(item, str) and item for item in categories
    ):
        raise ValueError("extension categories must be non-empty strings")
    category_text = ",".join(categories)

    return f"""<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
  <Metadata>
    <Identity Language="en-US" Id={quoteattr(name)}
              Version={quoteattr(version)} Publisher={quoteattr(publisher)} />
    <DisplayName>{escape(display_name)}</DisplayName>
    <Description xml:space="preserve">{escape(description)}</Description>
    <Tags>agent-watch,cursor,notifications,remote-ssh</Tags>
    <Categories>{escape(category_text)}</Categories>
    <GalleryFlags>Public</GalleryFlags>
    <Properties>
      <Property Id="Microsoft.VisualStudio.Code.Engine" Value={quoteattr(engine)} />
      <Property Id="Microsoft.VisualStudio.Code.ExtensionKind" Value="workspace" />
    </Properties>
  </Metadata>
  <Installation>
    <InstallationTarget Id="Microsoft.VisualStudio.Code" />
  </Installation>
  <Dependencies />
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true" />
    <Asset Type="Microsoft.VisualStudio.Services.Content.Details"
           Path="extension/README.md" Addressable="true" />
    <Asset Type="Microsoft.VisualStudio.Services.Content.License"
           Path="extension/LICENSE" Addressable="true" />
  </Assets>
</PackageManifest>
"""


CONTENT_TYPES = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="json" ContentType="application/json" />
  <Default Extension="js" ContentType="application/javascript" />
  <Default Extension="md" ContentType="text/markdown" />
  <Override PartName="/extension.vsixmanifest" ContentType="text/xml" />
  <Override PartName="/extension/LICENSE" ContentType="text/plain" />
</Types>
"""


def archive_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    return info


def build(output: pathlib.Path) -> pathlib.Path:
    manifest = read_manifest()
    output.parent.mkdir(parents=True, exist_ok=True)
    sources = {
        f"extension/{relative}": EXTENSION_ROOT / relative
        for relative in (*PACKAGE_FILES, "LICENSE")
    }
    missing = [str(path) for path in sources.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"missing extension package files: {', '.join(missing)}")

    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(archive_info("[Content_Types].xml"), CONTENT_TYPES.encode())
        archive.writestr(
            archive_info("extension.vsixmanifest"),
            vsix_manifest(manifest).encode(),
        )
        for archive_name, source in sorted(sources.items()):
            archive.writestr(archive_info(archive_name), source.read_bytes())
    return output


def main() -> int:
    manifest = read_manifest()
    default_name = (
        f"{required_string(manifest, 'name')}-"
        f"{required_string(manifest, 'version')}.vsix"
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=pathlib.Path,
        default=ROOT / "dist" / default_name,
        help="output VSIX path",
    )
    args = parser.parse_args()
    result = build(args.output.expanduser().resolve())
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
