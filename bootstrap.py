"""Install the verified public starter bundle into a new local directory."""

from __future__ import annotations

import argparse
from hashlib import sha256
import io
import json
from pathlib import Path
import re
import shutil
import stat
import tempfile
import unicodedata
from urllib.parse import urljoin
from urllib.request import Request, urlopen
import zipfile


LATEST_URL = "https://raw.githubusercontent.com/zimo528/ai-customer-service-delivery/main/latest.json"
SHA256 = re.compile(r"[0-9a-f]{64}")
MAX_FILES = 2000
MAX_FILE_SIZE = 32 * 1024 * 1024
MAX_TOTAL_SIZE = 256 * 1024 * 1024


def fetch(url: str) -> bytes:
    with urlopen(Request(url, headers={"User-Agent": "customer-service-bootstrap"}), timeout=180) as response:
        return response.read()


def checked_latest(data: bytes) -> dict:
    latest = json.loads(data)
    if not isinstance(latest, dict) or latest.get("schema") != "customer-service-release.v1":
        raise ValueError("unsupported latest.json schema")
    hashed = latest.get("sha256")
    if not isinstance(hashed, str) or not SHA256.fullmatch(hashed):
        raise ValueError("invalid release SHA-256")
    if latest.get("version") != hashed[:16] or latest.get("path") != f"releases/{hashed}/startup.zip":
        raise ValueError("release identity or path mismatch")
    if not isinstance(latest.get("bytes"), int) or not 0 < latest["bytes"] <= MAX_TOTAL_SIZE:
        raise ValueError("invalid release size")
    return latest


def checked_name(name: str) -> str:
    if not name or name.startswith("/") or "\\" in name:
        raise ValueError(f"unsafe ZIP path: {name!r}")
    parts = name.rstrip("/").split("/")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10))}
    for part in parts:
        if (part in ("", ".", "..") or part.endswith((" ", ".")) or
                any(char in part for char in '<>:"|?*') or
                any(ord(char) < 32 or ord(char) == 127 for char in part) or
                part.upper().split(".", 1)[0] in reserved):
            raise ValueError(f"unsafe ZIP path: {name!r}")
    return "/".join(parts)


def checked_bundle(data: bytes) -> tuple[str, str, dict]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_FILES:
            raise ValueError("ZIP file count limit exceeded")
        files = {}
        portable = set()
        portable_files = set()
        total = 0
        for info in infos:
            name = checked_name(info.filename)
            if info.filename != name + ("/" if info.is_dir() else ""):
                raise ValueError(f"non-canonical ZIP path: {info.filename!r}")
            canonical = unicodedata.normalize("NFC", name.casefold())
            if canonical in portable:
                raise ValueError(f"ZIP path collision: {info.filename!r}")
            portable.add(canonical)
            mode = stat.S_IFMT((info.external_attr >> 16) & 0xFFFF)
            if mode not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ValueError(f"unsupported ZIP member: {info.filename!r}")
            if (info.is_dir() and mode == stat.S_IFREG) or \
                    (not info.is_dir() and mode == stat.S_IFDIR):
                raise ValueError(f"ZIP member type conflicts with path: {info.filename!r}")
            if info.is_dir():
                continue
            portable_files.add(canonical)
            if info.file_size > MAX_FILE_SIZE:
                raise ValueError("ZIP member exceeds size limit")
            total += info.file_size
            if total > MAX_TOTAL_SIZE:
                raise ValueError("ZIP expanded size limit exceeded")
            files[name] = archive.read(info)
        for name in portable:
            parts = name.split("/")
            if any("/".join(parts[:index]) in portable_files for index in range(1, len(parts))):
                raise ValueError(f"ZIP file conflicts with directory: {name!r}")
        tops = {name.split("/", 1)[0] for name in files}
        if len(tops) != 1:
            raise ValueError("ZIP must have one top-level directory")
        top = tops.pop()
        prefix = top + "/"
        manifest_name = prefix + "文件校验清单.json"
        if manifest_name not in files:
            raise ValueError("bundle manifest missing")
        manifest = json.loads(files[manifest_name])
        entries = manifest.get("files")
        skills = manifest.get("skills")
        if not isinstance(entries, list) or not isinstance(skills, list) or not skills:
            raise ValueError("bundle manifest is incomplete")
        expected = {prefix + "先看这里.md", manifest_name}
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                raise ValueError("invalid manifest file entry")
            path = checked_name(entry["path"])
            if path != entry["path"] or path.startswith("../"):
                raise ValueError("invalid manifest path")
            member = prefix + path
            content = files.get(member)
            hashed = entry.get("sha256")
            if (content is None or entry.get("bytes") != len(content) or not isinstance(hashed, str) or
                    hashed.lower() != sha256(content).hexdigest()):
                raise ValueError(f"bundle manifest mismatch: {path}")
            expected.add(member)
        if set(files) != expected:
            raise ValueError("bundle files differ from manifest")
        project_names = {entry["path"].rsplit("/", 1)[0] for entry in entries
                         if entry["path"].count("/") == 1 and entry["path"].endswith("/AGENTS.md")}
        if len(project_names) != 1:
            raise ValueError("project AGENTS.md missing")
        project = project_names.pop()
        for skill in skills:
            if not isinstance(skill, str) or not re.fullmatch(r"[a-z0-9-]+", skill):
                raise ValueError("invalid skill name")
            if prefix + project + f"/.agents/skills/{skill}/SKILL.md" not in files:
                raise ValueError(f"missing bundled skill: {skill}")
        return top, project, manifest


def install(manifest_url: str, output_root: Path, get=fetch) -> dict:
    latest = checked_latest(get(manifest_url))
    archive_url = urljoin(manifest_url, latest["path"])
    data = get(archive_url)
    if len(data) != latest["bytes"] or sha256(data).hexdigest() != latest["sha256"]:
        raise ValueError("downloaded ZIP differs from latest.json")
    top, project, manifest = checked_bundle(data)
    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    target = output_root / top
    if target.exists():
        raise FileExistsError(f"destination already exists: {target}")
    stage = Path(tempfile.mkdtemp(prefix=".starter-", dir=output_root))
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for info in archive.infolist():
                relative = checked_name(info.filename)
                destination = stage / relative
                if info.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with destination.open("xb") as handle:
                        handle.write(archive.read(info))
        (stage / top).replace(target)
    finally:
        if stage.resolve().is_relative_to(output_root) and stage.exists():
            shutil.rmtree(stage)
    return {"version": latest["version"], "sha256": latest["sha256"],
            "bundle_dir": str(target), "project_dir": str(target / project),
            "skills": manifest["skills"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--manifest-url", default=LATEST_URL)
    args = parser.parse_args()
    print(json.dumps(install(args.manifest_url, args.output_root), ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
