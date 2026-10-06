#!/usr/bin/env python3
"""Minimal local legal-corpus installer and inspector.

The MVP supports network installation and single-document fetching from
source A, as well as installation from an explicitly selected local snapshot.
"""

from __future__ import annotations

import argparse
import hashlib
import html as html_module
import json
import os
import random
import re
import shutil
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path


HERE = Path(__file__).resolve().parent
CATALOG_PATH = HERE / "catalog.json"
SOURCE_A_API = "http://actual.pravo.gov.ru:8000/api/ebpi"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def corpus_root(args: argparse.Namespace) -> Path:
    if args.root:
        return Path(args.root).expanduser().resolve()
    env_root = os.environ.get("LEGAL_CORPUS_HOME")
    if env_root:
        return Path(env_root).expanduser().resolve()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return (Path(base) / "legal-corpus").resolve()
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "Application Support" / "legal-corpus").resolve()
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return (Path(base) / "legal-corpus").resolve()


def source_root(args: argparse.Namespace) -> Path:
    if args.source:
        return Path(args.source).expanduser().resolve()
    env_source = os.environ.get("LEGAL_CORPUS_SOURCE")
    if env_source:
        return Path(env_source).expanduser().resolve()
    raise ValueError(
        "No local source specified. Use --source PATH or LEGAL_CORPUS_SOURCE. "
        "For network installation, omit --source and use install or setup."
    )


def catalog() -> dict:
    return load_json(CATALOG_PATH)


def source_manifest(source: Path) -> dict | None:
    path = source / "MANIFEST.json"
    return load_json(path) if path.exists() else None


def installed_manifest(root: Path) -> dict | None:
    path = root / "MANIFEST.json"
    return load_json(path) if path.exists() else None


def selected_ids(profile: str, data: dict) -> list[str]:
    profiles = data["profiles"]
    if profile not in profiles:
        raise ValueError(f"Unknown profile: {profile}. Available: {', '.join(profiles)}")
    return profiles[profile]


def document_rows(data: dict, manifest: dict | None) -> list[dict]:
    records = (manifest or {}).get("parts", {})
    rows = []
    for doc_id, item in data["documents"].items():
        record = records.get(str(item["manifest_key"]), {})
        rows.append({
            "id": doc_id,
            "name": item["name"],
            "act": record.get("docpassing", item.get("act", "")),
            "status": "installed" if record else "catalogue-only",
        })
    return rows


def cmd_list(args: argparse.Namespace) -> int:
    data = catalog()
    manifest = installed_manifest(corpus_root(args))
    print("Available documents:")
    for row in document_rows(data, manifest):
        print(f"- {row['id']}: {row['name']} [{row['status']}]")
    print("\nProfiles:")
    for name, ids in data["profiles"].items():
        print(f"- {name}: {', '.join(ids)}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    root = corpus_root(args)
    manifest = installed_manifest(root)
    installed = list((manifest or {}).get("documents", {}).keys())
    result = {
        "root": str(root),
        "exists": root.exists(),
        "manifest": str(root / "MANIFEST.json") if manifest else None,
        "installed_documents": installed,
        "installed_count": len(installed),
        "corpus": (manifest or {}).get("corpus"),
        "built_at": (manifest or {}).get("built_at"),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def copy_tree(source: Path, target: Path) -> None:
    if not source.exists():
        return
    shutil.copytree(source, target, dirs_exist_ok=True)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def source_a_get(url: str, timeout: int = 180) -> bytes:
    last_error: Exception | None = None
    for attempt in range(1, 11):
        request = urllib.request.Request(url, headers={"User-Agent": "legal-corpus/0.1"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.URLError as error:
            last_error = error
        except ConnectionError as error:
            last_error = error
        if attempt < 10:
            delay = min(30, 2 ** attempt) + random.uniform(0, 2)
            time.sleep(delay)
    raise ValueError(f"Source A is unavailable after 10 attempts: {last_error}") from last_error


def source_a_json(url: str, timeout: int = 180) -> tuple[bytes, dict]:
    raw = source_a_get(url, timeout=timeout)
    try:
        return raw, json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Source A returned invalid JSON: {error}") from error


def write_bytes(path: Path, data: bytes, base: Path) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() == data:
        return {"path": path.relative_to(base).as_posix(), "bytes": len(data), "sha256": sha256_bytes(data)}
    path.write_bytes(data)
    return {"path": path.relative_to(base).as_posix(), "bytes": len(data), "sha256": sha256_bytes(data)}


P_BLOCK = re.compile(r"<p\b([^>]*)>(.*?)</p>", re.I | re.S)
CLASS = re.compile(r'class="([^"]*)"', re.I)
SUP = re.compile(r'<span class="W9">\s*([^<]*?)\s*</span>', re.I)
TAGS = re.compile(r"<[^>]+>")
ARTICLE_HEAD = re.compile(r"^Статья\s+(\d+(?:\.\d+)*(?:-\d+)?)\.\s*(.*)$")


def html_block_text(fragment: str) -> str:
    text = SUP.sub(r".\1", fragment)
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = TAGS.sub("", text)
    text = html_module.unescape(text).replace("\xa0", " ")
    return re.sub(r"[ \t]+", " ", text).strip()


def build_text(redtext: str) -> tuple[str, list[dict]]:
    blocks = []
    for match in P_BLOCK.finditer(redtext):
        attrs, inner = match.group(1), match.group(2)
        text = html_block_text(inner)
        if not text:
            continue
        classes = (CLASS.search(attrs).group(1) if CLASS.search(attrs) else "").split()
        blocks.append(("H" in classes, text))
    lines, articles, current = [], [], None
    for is_heading, text in blocks:
        match = ARTICLE_HEAD.match(text) if is_heading else None
        if match:
            if current:
                articles.append(current)
            current = {"num": match.group(1), "lines": [text]}
        elif current:
            current["lines"].append(text)
        lines.append(text)
    if current:
        articles.append(current)
    return "\n\n".join(lines).strip() + "\n", articles


def article_sort_key(number: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.split(r"[.\-]", number))


def cmd_fetch(args: argparse.Namespace) -> int:
    data = catalog()
    if args.document_id not in data["documents"]:
        available = ", ".join(data["documents"])
        print(f"Unknown document: {args.document_id}\nAvailable IDs: {available}", file=sys.stderr)
        return 2
    item = data["documents"][args.document_id]
    source_cfg = item.get("source_a")
    if not source_cfg or "hash" not in source_cfg:
        print(
            f"Source A parameters are not configured yet for {args.document_id}. "
            "The document is listed in the catalogue but cannot be fetched yet.",
            file=sys.stderr,
        )
        return 2

    root = corpus_root(args)
    root.mkdir(parents=True, exist_ok=True)
    manifest = installed_manifest(root) or {
        "corpus": "Russian legal corpus",
        "tool": {"name": "legal-corpus", "mvp_version": "0.1.0"},
        "documents": {},
        "parts": {},
    }
    source_key = str(item["manifest_key"])
    old = manifest.get("parts", {}).get(source_key, {})
    query = urllib.parse.quote(json.dumps({"hash": source_cfg["hash"], "ttl": 3}, separators=(",", ":")))
    card_url = f"{SOURCE_A_API}/card/?bpa=ebpi&t={query}"
    reds_url = f"{SOURCE_A_API}/redactions/?bpa=ebpi&t={query}"
    print(f"Fetching {args.document_id} from source A")
    raw_card, card = source_a_json(card_url, timeout=60)
    raw_reds, reds_payload = source_a_json(reds_url, timeout=60)
    redactions = reds_payload.get("redactions") or []
    actual = next((record for record in redactions if record.get("redstatus") == "актуальная"), None)
    if not actual and redactions:
        actual = redactions[0]
    if not actual or "redid" not in actual:
        raise ValueError("Source A returned no usable current revision")
    redid = str(actual["redid"])
    existing_text = (installed_manifest(root) or {}).get("parts", {}).get(str(item["manifest_key"]), {}).get("text_file")
    existing_articles = (installed_manifest(root) or {}).get("parts", {}).get(str(item["manifest_key"]), {}).get("articles_dir")
    if old.get("current_revision") == redid and existing_text and (root / existing_text).exists():
        print(f"Already current: {args.document_id} revision {redid}; full text download skipped")
        return 0
    raw_text_url = f"{SOURCE_A_API}/redtext?bpa=ebpi&t={urllib.parse.quote(redid)}&ttl=0"
    raw_text, text_payload = source_a_json(raw_text_url, timeout=300)
    redtext = text_payload.get("redtext")
    if not isinstance(redtext, str) or "<p" not in redtext:
        raise ValueError("Source A returned no usable redtext HTML")

    prefix = args.document_id
    raw_card_info = write_bytes(root / "raw" / f"{prefix}-actual-card.json", raw_card, root)
    raw_reds_info = write_bytes(root / "raw" / f"{prefix}-actual-redactions-{redid}.json", raw_reds, root)
    raw_text_info = write_bytes(root / "raw" / f"{prefix}-actual-redtext-{redid}.json", raw_text, root)
    text, articles = build_text(redtext)
    date = str(actual.get("reddate") or "undated")
    text_path = root / "text" / f"{prefix}-{date}.txt"
    text_path.parent.mkdir(parents=True, exist_ok=True)
    text_path.write_text(text, encoding="utf-8", newline="\n")
    article_dir = root / "articles" / f"{prefix}-{date}"
    article_dir.mkdir(parents=True, exist_ok=True)
    for article in articles:
        (article_dir / f"{article['num']}.txt").write_text(
            "\n".join(article["lines"]).strip() + "\n", encoding="utf-8", newline="\n"
        )
    nums = sorted((article["num"] for article in articles), key=article_sort_key)
    revision = {
        "revision": redid,
        "source_level": "A",
        "source_url": raw_text_url,
        "retrieved_at": utc_now(),
        "reddate": actual.get("reddate"),
        "redcaption": actual.get("redcaption"),
        "raw_card": raw_card_info,
        "raw_redactions": raw_reds_info,
        "raw_text": raw_text_info,
        "text_file": text_path.relative_to(root).as_posix(),
        "text_sha256": sha256_bytes(text.encode("utf-8")),
        "articles_dir": article_dir.relative_to(root).as_posix(),
        "articles_count": len(articles),
    }
    history = list(old.get("revisions", []))
    if old.get("current_revision") and old.get("current_revision") != redid:
        history.append({key: old[key] for key in ("current_revision", "source_level", "source_snapshot", "text_file", "text_sha256") if key in old})
    elif old and not old.get("current_revision"):
        history.append({
            "source_level": old.get("source_level", "C"),
            "source_snapshot": manifest.get("source_snapshot"),
            "text_file": old.get("text_file"),
            "text_sha256": old.get("text_sha256"),
        })
    record = dict(revision)
    record["current_revision"] = redid
    record["revisions"] = history
    record["key"] = source_key
    record["prefix"] = prefix
    record["docname"] = card.get("docname", item["name"])
    record["docpassing"] = card.get("docpassing")
    record["docstate"] = card.get("docstate")
    manifest["corpus"] = manifest.get("corpus", "Russian legal corpus")
    manifest["parts"][source_key] = record
    manifest["documents"][args.document_id] = {
        "current_revision": redid,
        "source_level": "A",
        "name": item["name"],
    }
    manifest["updated_at"] = utc_now()
    (root / "MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Fetched revision {redid}; text: {text_path}")
    return 0


def cmd_install_network(args: argparse.Namespace) -> int:
    data = catalog()
    ids = selected_ids(args.profile, data)
    print(f"Network installation plan for profile '{args.profile}' ({len(ids)} documents)")
    print(f"Target: {corpus_root(args)}")
    for document_id in ids:
        state = (installed_manifest(corpus_root(args)) or {}).get("documents", {}).get(document_id)
        state_text = f"current revision {state.get('current_revision')}" if state else "missing"
        print(f"- {document_id}: {data['documents'][document_id]['name']} [{state_text}]")
    if args.dry_run:
        print("Dry run: no network requests were made")
        return 0
    for document_id in ids:
        args.document_id = document_id
        result = cmd_fetch(args)
        if result:
            return result
    print(f"Profile '{args.profile}' is installed")
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    data = catalog()
    root = corpus_root(args)
    source = source_root(args)
    source_man = source_manifest(source)
    if not source_man:
        print(f"Source corpus manifest not found: {source / 'MANIFEST.json'}", file=sys.stderr)
        return 2

    ids = selected_ids(args.profile, data)
    missing = [doc_id for doc_id in ids if str(data["documents"][doc_id]["manifest_key"]) not in source_man.get("parts", {})]
    if missing:
        print(f"Documents missing from source manifest: {', '.join(missing)}", file=sys.stderr)
        return 2

    print(f"Installing profile '{args.profile}' ({len(ids)} documents)")
    print(f"Source: {source}")
    print(f"Target: {root}")
    for doc_id in ids:
        item = data["documents"][doc_id]
        prefix = item["prefix"]
        print(f"- {doc_id}: {item['name']}")
        for dirname in ("raw", "text", "articles"):
            copy_tree(source / dirname / prefix, root / dirname / prefix)
        for path in (source / "raw").glob(f"{prefix}-*"):
            target = root / "raw" / path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        for path in (source / "text").glob(f"{prefix}-*"):
            target = root / "text" / path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)

    installed_parts = {}
    for key in ids:
        source_record = source_man["parts"][str(data["documents"][key]["manifest_key"])]
        installed_record = dict(source_record)
        installed_record["source_level"] = "C"
        installed_record["original_source_level"] = source_record.get("source_level", "A")
        installed_record["installed_from"] = str(source)
        installed_parts[str(data["documents"][key]["manifest_key"])] = installed_record

    manifest = {
        "corpus": source_man.get("corpus", "Russian legal corpus"),
        "tool": {"name": "legal-corpus", "mvp_version": "0.1.0"},
        "installed_at": utc_now(),
        "source_snapshot": str(source),
        "profile": args.profile,
        "documents": {str(key): installed_parts[str(data["documents"][key]["manifest_key"])] for key in ids},
        "parts": installed_parts,
    }
    root.mkdir(parents=True, exist_ok=True)
    with (root / "MANIFEST.json").open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(f"Installed. Manifest: {root / 'MANIFEST.json'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="legal-corpus",
        description="Установка и проверка локального корпуса российского законодательства.",
        epilog=(
            "Примеры:\n"
            "  python legal_corpus.py list\n"
            "  python legal_corpus.py fetch sk\n"
            "  python legal_corpus.py install min\n"
            "  python legal_corpus.py install max --dry-run"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--root",
        help="Папка хранения корпуса; по умолчанию стандартное хранилище пользователя",
    )
    parser.add_argument("--source", help="Явный путь к локальному снимку для install")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("status", help="Показать состояние установленного корпуса")
    sub.add_parser("list", help="Показать документы и профили")
    install = sub.add_parser("install", help="Установить профиль документов")
    install.add_argument("profile", choices=("min", "max"))
    install.add_argument("--dry-run", action="store_true", help="Показать план без изменения файлов")
    fetch = sub.add_parser("fetch", help="Загрузить один документ из источника A")
    fetch.add_argument("document_id", help="ID из команды list, например gk1 или sk")
    setup = sub.add_parser("setup", help="Первичная установка корпуса для пользователя")
    setup.add_argument("--profile", choices=("min", "max"), default="min", help="Профиль; по умолчанию min")
    setup.add_argument("--dry-run", action="store_true", help="Показать план без изменения файлов")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.command:
        build_parser().print_help()
        return 0
    try:
        if args.command == "status":
            return cmd_status(args)
        if args.command == "list":
            return cmd_list(args)
        if args.command == "install":
            if args.source or os.environ.get("LEGAL_CORPUS_SOURCE") or args.dry_run:
                if args.dry_run:
                    data = catalog()
                    ids = selected_ids(args.profile, data)
                    print(f"Installation plan for profile '{args.profile}' ({len(ids)} documents)")
                    for document_id in ids:
                        print(f"- {document_id}: {data['documents'][document_id]['name']}")
                    print("Dry run: no files or network requests were made")
                    return 0
                return cmd_install(args)
            return cmd_install_network(args)
        if args.command == "fetch":
            return cmd_fetch(args)
        if args.command == "setup":
            return cmd_install_network(args)
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
