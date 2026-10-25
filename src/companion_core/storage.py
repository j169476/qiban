"""Editable YAML is canonical. SQLite is only transactions and rebuildable search."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import uuid
import zipfile
from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import yaml
from filelock import FileLock

from .errors import AssistantError
from .index_cache import indexed
from .profiles import default_profile


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def digest(raw: bytes):
    return hashlib.sha256(raw).hexdigest()


def recorded_time(doc):
    value = doc.get("source", {}).get("recorded_at") or doc.get("last_interaction_at")
    try:
        stamp = datetime.fromisoformat(value)
        return stamp.replace(tzinfo=stamp.tzinfo or timezone.utc).timestamp()
    except (ValueError, TypeError, OverflowError):
        return 0


def searchable_text(doc):
    parts = [doc.get(key, "") for key in ("title", "content", "display_name", "impression", "when_to_use")]
    parts += doc.get("tags", []) + doc.get("facts", [])
    parts += [item["topic"] for item in doc.get("topics", []) if isinstance(item, dict)]
    return " ".join(parts).casefold()


def search_terms(query):
    terms = set(re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]+", query.casefold()))
    for word in list(terms):
        if re.fullmatch(r"[\u3400-\u9fff]{3,}", word):
            terms.update(word[i : i + 2] for i in range(len(word) - 1))
    return sorted(terms)[:200]


class StrictLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node, deep=False):
    pairs = loader.construct_pairs(node, deep=deep)
    result = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in result:
            raise ValueError("duplicate or non-string key")
        result[key] = value
    return result


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def atomic_write(path: Path, raw: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class Store:
    def __init__(self, root: Path, profile=None):
        self.profile = profile or default_profile()
        self.root = root.resolve()
        if (self.root / ".service-retired.json").exists():
            raise AssistantError("service_entry_required", "此旧资料目录已停用；请从本机服务入口操作")
        self._read_cache = {}
        self._index_local = threading.local()
        self.index_stats = {"persistent_hits": 0, "parsed": 0, "cache_errors": 0}
        self.runtime = self.root / "runtime"
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.lock = FileLock(str(self.runtime / "write.lock"), timeout=20)
        self.db_path = self.runtime / "state.sqlite3"
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions(thread TEXT PRIMARY KEY, mode TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS actions(
                    id TEXT PRIMARY KEY, dedupe TEXT UNIQUE NOT NULL,
                    owner TEXT NOT NULL, generation TEXT NOT NULL, account TEXT NOT NULL,
                    kind TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL,
                    day TEXT, units INTEGER NOT NULL, target_uid TEXT,
                    created TEXT NOT NULL, dispatched TEXT, result TEXT);
                CREATE TABLE IF NOT EXISTS events(
                    id TEXT NOT NULL, account TEXT NOT NULL, uid TEXT NOT NULL,
                    kind TEXT NOT NULL, received TEXT NOT NULL, body TEXT NOT NULL,
                    PRIMARY KEY(account,id));
                CREATE TABLE IF NOT EXISTS relationship_pending(
                    receipt TEXT PRIMARY KEY, uid TEXT UNIQUE NOT NULL, event_id TEXT NOT NULL,
                    before_version TEXT, after_version TEXT NOT NULL, data TEXT NOT NULL,
                    after_yaml TEXT NOT NULL, actor TEXT NOT NULL, created TEXT NOT NULL,
                    status TEXT NOT NULL, error TEXT NOT NULL);
            """)
            # Platform IDs can repeat across accounts. Preserve old rows when upgrading.
            primary = [row["name"] for row in db.execute("PRAGMA table_info(events)") if row["pk"]]
            if primary == ["id"]:
                db.execute("BEGIN IMMEDIATE")
                db.execute("""CREATE TABLE events_scoped(
                    id TEXT NOT NULL, account TEXT NOT NULL, uid TEXT NOT NULL,
                    kind TEXT NOT NULL, received TEXT NOT NULL, body TEXT NOT NULL,
                    PRIMARY KEY(account,id))""")
                db.execute("INSERT INTO events_scoped SELECT * FROM events")
                db.execute("DROP TABLE events")
                db.execute("ALTER TABLE events_scoped RENAME TO events")
        self.profile.migrate(self)
        from .relationship_store import recover

        recover(self)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def state(self, key, default=None, db=None):
        if db is None:
            with self.db() as conn:
                return self.state(key, default, conn)
        row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return self.ledger_decode("state:" + key, row[0]) if row else default

    def ledger_encode(self, domain, value):
        """Serialize a trusted ledger value; database backends may encrypt it."""
        return json.dumps(value, ensure_ascii=False, allow_nan=False)

    def ledger_decode(self, domain, value):
        return json.loads(value)

    def states(self, pattern, db=None):
        if db is None:
            with self.db() as connection:
                return self.states(pattern, connection)
        return {row["key"]: self.ledger_decode("state:" + row["key"], row["value"])
                for row in db.execute("SELECT key,value FROM state WHERE key LIKE ?", (pattern,))}

    def set_state(self, key, value, db=None):
        if db is None:
            with self.db() as conn:
                return self.set_state(key, value, conn)
        db.execute("INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, self.ledger_encode("state:" + key, value)))

    def path(self, kind, item_id=None):
        if kind not in self.profile.schemas:
            raise AssistantError("invalid_path", "未知文件类型")
        if item_id is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", item_id):
            raise AssistantError("invalid_path", "条目 ID 只能使用字母、数字、下划线和连字符")
        if kind in self.profile.collections:
            if not item_id:
                raise AssistantError("missing_id", "集合文件需要 ID")
            path = self.root / kind / f"{item_id}.yaml"
        else:
            if item_id is not None:
                raise AssistantError("invalid_id", "配置文件不接受条目 ID")
            path = self.root / f"{kind}.yaml"
        if not path.resolve().is_relative_to(self.root):
            raise AssistantError("invalid_path", "路径超出数据目录")
        return path

    def initialize(self):
        with self.lock:
            for kind, data in self.profile.defaults.items():
                target = self.root / (kind + ".yaml")
                if not target.exists():
                    doc = deepcopy(data)
                    if kind == "manifest":
                        doc["project_id"] = uuid.uuid4().hex
                    atomic_write(target, yaml.safe_dump(doc, allow_unicode=True, sort_keys=False).encode("utf-8"))
            for folder in (*self.profile.collections, "history", "conflicts", "cache", "schemas"):
                (self.root / folder).mkdir(exist_ok=True)
            for kind, schema in self.profile.schemas.items():
                atomic_write(
                    self.root / "schemas" / f"{kind}.schema.json",
                    json.dumps(schema, ensure_ascii=False, indent=2).encode(),
                )
        return self.validate_all()

    def exists(self, kind, item_id=None):
        return self.path(kind, item_id).exists()

    def version(self, kind, item_id=None):
        path = self.path(kind, item_id)
        return digest(path.read_bytes()) if path.exists() else None

    def read(self, kind, item_id=None):
        path = self.path(kind, item_id)
        try:
            raw = path.read_bytes()
            if len(raw) > 1_000_000:
                raise ValueError("file too large")
            version = digest(raw)
            cached = self._read_cache.get((kind, item_id))
            if cached and cached["version"] == version:
                return deepcopy(cached)
            if kind in self.profile.collections:
                from .index_cache import load

                data = load(self, kind, item_id, version)
                if data is not None:
                    result = {"data": data, "version": version}
                    if len(self._read_cache) >= 12000:
                        self._read_cache.clear()
                    self._read_cache[(kind, item_id)] = deepcopy(result)
                    return result
            data = yaml.load(raw.decode("utf-8-sig"), Loader=StrictLoader)
            # Reject aliases/cycles and non-JSON YAML types.
            json.dumps(data, allow_nan=False)
        except FileNotFoundError:
            raise AssistantError("not_found", f"未找到 {path.name}") from None
        except (ValueError, TypeError, RecursionError, yaml.YAMLError, UnicodeError):
            raise AssistantError("invalid_yaml", f"{path.name}: 需要无重复键的 UTF-8 YAML 普通数据") from None
        self.profile.validate(kind, data)
        if item_id is not None and data["id"] != item_id:
            raise AssistantError("id_mismatch", f"{path.name}: 文件名和条目 ID 不一致")
        if kind in self.profile.collections:
            from .index_cache import save

            self.index_stats["parsed"] += 1
            save(self, kind, item_id, version, data)
        result = {"data": data, "version": version}
        # Hash bytes on every access: timestamps and file lengths can both stay unchanged after an edit.
        if len(self._read_cache) >= 12000:
            self._read_cache.clear()
        self._read_cache[(kind, item_id)] = deepcopy(result)
        return result

    def write(self, kind, data, expected, actor, item_id=None, allow_locked=False):
        self.profile.validate(kind, data)
        path = self.path(kind, item_id)
        if item_id is not None and data["id"] != item_id:
            raise AssistantError("id_mismatch", "文件名和条目 ID 不一致")
        raw = yaml.safe_dump(data, allow_unicode=True, sort_keys=False).encode("utf-8")
        with self.lock:
            from .baseline import guard_write

            guard_write(self, kind, item_id, data)
            old = path.read_bytes() if path.exists() else None
            actual = digest(old) if old is not None else None
            if actual != expected:
                conflict = self.root / "conflicts" / f"{kind}-{uuid.uuid4().hex}.yaml"
                atomic_write(conflict, raw)
                raise AssistantError("edit_conflict", f"本地版本已变化，候选保存于 {conflict.name}")
            if old and not allow_locked and self.read(kind, item_id)["data"].get("locked"):
                raise AssistantError("locked", "该条目已锁定，保留本地版本")
            # A final compare narrows the race with editors that do not participate in our lock.
            if path.exists() and path.read_bytes() != old:
                raise AssistantError("edit_conflict", "保存期间检测到人工修改，请重新读取")
            if old:
                snapshot = self.root / "history" / f"{kind}-{actual[:16]}.yaml"
                if not snapshot.exists():
                    atomic_write(snapshot, old)
            atomic_write(path, raw)
            self.audit(
                "config" if kind in self.profile.config else "content",
                {
                    "actor": actor,
                    "file": path.relative_to(self.root).as_posix(),
                    "before": actual,
                    "after": digest(raw),
                },
            )
        return {"data": data, "version": digest(raw)}

    def audit(self, category, event):
        # No cookies, credentials, full request headers, or raw API responses.
        with self.lock:
            path = self.root / "history" / f"{category}.jsonl"
            path.parent.mkdir(exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"at": utcnow(), **event}, ensure_ascii=False) + "\n")

    @indexed
    def records(self, kind):
        records, issues = [], []
        for path in sorted((self.root / kind).glob("*.yaml")):
            try:
                record = self.read(kind, path.stem)
                if kind == "skills":
                    # A deleted/invalid source must not survive as an apparently grounded skill.
                    for knowledge_id in record["data"]["knowledge_ids"]:
                        self.read("knowledge", knowledge_id)
                records.append(record)
            except AssistantError as exc:
                issues.append({"file": str(path), **exc.as_dict()})
        from .index_cache import prune

        prune(self, kind, {record["data"]["id"] for record in records})
        return records, issues

    def validate_critical(self):
        """Bounded liveness guard; content files validate when consumed or explicitly audited."""
        issues = []
        versions = {}
        for kind in (*self.profile.config, "growth"):
            try:
                versions[kind] = self.read(kind)["version"]
            except AssistantError as exc:
                issues.append({"file": f"{kind}.yaml", "critical": True, **exc.as_dict()})
        from .relationship_store import status

        issues += [{"file": f"relationships/{item['uid']}.yaml", "critical": True,
                    "error": "relationship_recovery_conflict", "message": "关系提交待裁决"}
                   for item in status(self)]
        return {"ok": not issues, "versions": versions, "issues": issues}

    @indexed
    def validate_all(self):
        result = self.validate_critical()
        counts = {}
        for kind in self.profile.collections:
            items, errors = self.records(kind)
            counts[kind] = len(items)
            result["issues"] += errors
        return {**result, "ok": not result["issues"], "counts": counts}

    @indexed
    def search(self, query, limit=10, *, queries=(), uids=()):
        # YAML remains authoritative. Hash-checked reads avoid repeatedly parsing an unchanged corpus.
        if limit <= 0:
            return []
        expressions = list(dict.fromkeys([query, *queries[:5]]))
        terms = search_terms(" ".join(expressions))
        from .memory_maintenance import semantic_scores
        from .passages import catalog_excerpt
        from .passages import index as passage_index
        from .passages import terms as passage_terms
        from .retrieval import scores

        wanted = {uid for uid in uids if uid}
        records = [{"kind": kind, **r} for kind in self.profile.memory_collections
                   if kind != "relationships" or not wanted for r in self.records(kind)[0]]
        for uid in sorted(wanted):
            try:
                records.append({"kind": "relationships", **self.read("relationships", uid)})
            except AssistantError:
                continue
        sources = {(r["kind"], r["data"]["id"]): r for r in records}
        semantics = semantic_scores(self, " ".join(expressions), sources) if terms else {}
        passages = passage_index(self, records)
        query_terms = passage_terms(query, queries)
        lexical = scores(records, query, queries, passages=passages)
        ranked = []
        for record in records:
            kind, doc = record["kind"], record["data"]
            item_id = doc["id"]
            score = lexical.get((kind, item_id), 0) + 10 * semantics.get((kind, item_id), 0)
            if score or not terms:
                ranked.append((score, recorded_time(doc), kind, item_id))
        ranked.sort(reverse=True)
        results = []
        for _, _, kind, item_id in ranked:
            try:
                current = self.read(kind, item_id)
            except AssistantError:
                continue
            if current["version"] == sources[(kind, item_id)]["version"]:
                result = {"kind": kind, **current}
                if (kind, item_id) in passages:
                    result["matched_excerpt"] = catalog_excerpt(result, passages[(kind, item_id)], query_terms)
                results.append(result)
            if len(results) >= min(limit, 200):
                break
        return self.profile.annotate(self, results)

    @indexed
    def context(self, query="", references=(), uids=(), queries=(), *, record_filter=None):
        """Bounded, editable context plus a catalog for Codex to select older material."""
        settings = self.read("companion")["data"]
        persona = self.read("persona")["data"]
        foundations = set(persona.get("foundation_memories", []))
        from .memory_maintenance import semantic_scores
        from .passages import catalog_excerpt, excerpt
        from .passages import index as passage_index
        from .passages import terms as passage_terms
        from .retrieval import scores

        selected = {(ref["kind"], ref["id"]) for ref in references}
        wanted = {uid for uid in uids if uid}
        if wanted:
            wanted |= {ref["id"] for ref in references if ref["kind"] == "relationships"}
            if persona.get("relationship_baseline"):
                wanted.add(persona["relationship_baseline"]["owner_uid"])
        records = [
            {"kind": kind, **record}
            for kind in self.profile.memory_collections
            if kind != "relationships" or not wanted
            for record in self.records(kind)[0]
        ]
        for uid in sorted(wanted):
            try:
                records.append({"kind": "relationships", **self.read("relationships", uid)})
            except AssistantError:
                pass
        # Visibility must constrain ranking, truncation and the fallback catalog alike.
        # Filtering only the selected tail lets unrelated conversations exhaust its budget.
        if record_filter is not None:
            records = [record for record in records if record_filter(record)]
        from .baseline import enabled as shared_baseline

        if shared_baseline(self):
            # Fixed settings have their own complete context, outside retrieval budgets.
            records = [record for record in records
                       if not (record["kind"] == "memories" and record["data"]["id"] in foundations)]
            foundations = set()
        sources = {(r["kind"], r["data"]["id"]): r for r in records}
        expressions = " ".join([query, *queries[:5]])
        semantics = semantic_scores(self, expressions, sources) if expressions.strip() else {}
        passages = passage_index(self, records)
        query_terms = passage_terms(query, queries)
        lexical = scores(records, query, queries, passages=passages)
        anchors = sorted([r for r in records if r["data"].get("locked")
                          or r["data"].get("kind") == "user_preference"],
                         key=lambda r: (lexical.get((r["kind"], r["data"]["id"]), 0), recorded_time(r["data"])),
                         reverse=True)[:min(4, max(1, settings["context_record_limit"] // 3))]
        anchored = {(r["kind"], r["data"]["id"]) for r in anchors}
        self.profile.annotate(self, records)

        def rank(record):
            kind, doc = record["kind"], record["data"]
            priority = (
                5 if kind == "memories" and doc["id"] in foundations
                else 4 if (kind, doc["id"]) in selected
                else 3 if kind == "relationships" and doc["id"] in uids
                else 2 if (kind, doc["id"]) in anchored
                else 0
            )
            score = lexical.get((kind, doc["id"]), 0) + 10 * semantics.get((kind, doc["id"]), 0)
            return priority, score, recorded_time(doc), kind, doc["id"]

        ordered = sorted(records, key=rank, reverse=True)
        chosen, size = [], 0
        for record in ordered:
            if len(chosen) >= settings["context_record_limit"]:
                break
            key = record["kind"], record["data"]["id"]
            remaining = settings["context_char_limit"] - size
            allowance = (remaining if key in selected or key[0] == "memories" and key[1] in foundations
                         else min(remaining, max(2000, settings["context_char_limit"] // 4)))
            length = len(json.dumps(record, ensure_ascii=False))
            if length > remaining or (key in passages and length > allowance):
                record = excerpt(record, passages.get(key, []), query_terms, allowance)
                if record is None:
                    continue
                length = len(json.dumps(record, ensure_ascii=False))
            if size + length > settings["context_char_limit"]:
                continue
            chosen.append(record)
            size += length
        # Old locked preferences stay in the full context; the catalog gives the model
        # a second chance to select semantically relevant older notes and skills.
        catalog = [
            {
                "kind": record["kind"],
                "id": record["data"]["id"],
                "version": record["version"],
                "summary": searchable_text(record["data"])[:240],
                "recorded_at": record["data"].get("source", {}).get("recorded_at", ""),
                **(catalog_excerpt(record, passages[(record["kind"], record["data"]["id"])], query_terms, compact=True)
                   if (record["kind"], record["data"]["id"]) in passages else {}),
            }
            for record in (ordered if expressions.strip() else sorted(
                records, key=lambda item: (recorded_time(item["data"]), item["data"]["id"]), reverse=True
            ))[: settings["catalog_limit"]]
        ]
        result = {
            "records": chosen,
            "catalog": catalog,
            "total_records": len(records),
            "catalog_truncated": len(records) > len(catalog),
            "context_chars": size,
            "catalog_chars": len(json.dumps(catalog, ensure_ascii=False)),
            "relationship_scope": {"mode": "uids" if wanted else "all", "uids": sorted(wanted)},
            "foundation_status": {
                "loaded": sorted(r["data"]["id"] for r in chosen
                                 if r["kind"] == "memories" and r["data"]["id"] in foundations
                                 and not r.get("content_truncated")),
                "missing": sorted(i for i in foundations if ("memories", i) not in sources),
                "partial": sorted(r["data"]["id"] for r in chosen if r.get("content_truncated")
                                  and r["kind"] == "memories" and r["data"]["id"] in foundations),
                "omitted_by_budget": sorted(i for i in foundations if ("memories", i) in sources
                                            and not any(r["kind"] == "memories" and r["data"]["id"] == i
                                                         and not r.get("content_truncated")
                                                        for r in chosen)),
            },
        }
        # Preserve the established separate record/catalog limits while exposing
        # the whole memory bundle size. Config, history and platform material are
        # added by callers and are deliberately not claimed as part of this count.
        result["total_context_chars"] = 0
        for _ in range(3):
            result["total_context_chars"] = len(json.dumps(result, ensure_ascii=False))
        return result

    def rebuild_index(self):
        with self.lock:
            from .index_cache import reset

            reset(self)
            cache = self.root / "cache"
            cache.mkdir(exist_ok=True)
            target = cache / "search.sqlite3"
            temp = cache / "search-new.sqlite3"
            if temp.exists():
                temp.unlink()
            count = 0
            with closing(sqlite3.connect(temp)) as db:
                db.execute("CREATE TABLE entries(kind TEXT,id TEXT,content TEXT)")
                for kind in self.profile.memory_collections:
                    for entry in self.records(kind)[0]:
                        doc = entry["data"]
                        db.execute(
                            "INSERT INTO entries VALUES (?,?,?)",
                            (kind, doc["id"], json.dumps(doc, ensure_ascii=False)),
                        )
                        count += 1
                db.commit()
            os.replace(temp, target)
            return {"indexed": count}

    def export(self, destination: Path):
        destination = destination.resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        missing_assets = []
        # Explicit allowlist deliberately excludes runtime, logs, conflicts and credentials.
        with self.lock, zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
            for kind in (*self.profile.config, "growth"):
                self.read(kind)
                path = self.path(kind)
                archive.write(path, path.name)
            for kind in self.profile.collections:
                for record in self.records(kind)[0]:
                    path = self.path(kind, record["data"]["id"])
                    archive.write(path, path.relative_to(self.root))
            # Only registered, matching image bytes are content. Models/cache/runtime remain excluded.
            included = set()
            for record in (self.records("publication_images")[0] if "publication_images" in self.profile.schemas else []):
                doc = record["data"]
                image = self.root / "assets" / "publication" / doc["filename"]
                if (not re.fullmatch(r"[a-f0-9]{64}\.(?:jpg|png|webp)", doc["filename"])
                        or not image.resolve().is_relative_to(self.root) or image.is_symlink()
                        or not image.is_file() or not 0 < image.stat().st_size <= 5_000_000
                        or digest(image.read_bytes()) != doc["sha256"]):
                    missing_assets.append(doc["id"])
                elif image not in included:
                    archive.write(image, image.relative_to(self.root))
                    included.add(image)
            if self.read("persona")["data"].get("visual_identity"):
                from .persona import resolve_visuals

                try:
                    _, paths = resolve_visuals(self)
                except AssistantError:
                    missing_assets.append("persona_visual_identity")
                else:
                    for value in paths:
                        image = Path(value)
                        archive.write(image, image.relative_to(self.root))
        return {"path": str(destination), "missing_assets": missing_assets}

    def backup(self, force=False):
        service = self.backup_service()
        create, sha, verify = service.create, service.sha, lambda path: service.verify(path, profile=self.profile)

        folder = self.root / "history" / "backups"
        folder.mkdir(exist_ok=True)
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        target = folder / (date + ("-" + uuid.uuid4().hex[:8] if force else "") + ".full.zip")
        with self.lock:
            cached = getattr(self, "_verified_backups", {})
            reused, repaired = target.exists(), False
            if reused:
                try:
                    signature = sha(target)
                    result = cached.get(str(target), {})
                    if result.get("sha256") != signature:
                        result = verify(target)
                except (AssistantError, OSError):
                    repaired = True
                    reused = False
            if not reused:
                create(self, target)
                result = verify(target)
            # Cleanup is reached only after a usable backup has been verified.
            self._verified_backups = {str(target): result}
            keep = self.read("policy")["data"]["backup_keep"]
            older = sorted((path for path in folder.glob("*.zip") if path != target),
                           key=lambda path: path.stat().st_mtime_ns, reverse=True)
            for old in older[keep - 1:]:
                old.unlink()
            return {**result, "reused": reused, "repaired": repaired}

    def checkpoint(self):
        """Remove unbounded change snapshots after they are covered by daily backups."""
        self.backup()
        snapshots = sorted((self.root / "history").glob("*.yaml"), key=lambda p: p.stat().st_mtime)
        for old in snapshots[:-200]:
            old.unlink()

    def backup_service(self):
        from . import backups
        return backups
