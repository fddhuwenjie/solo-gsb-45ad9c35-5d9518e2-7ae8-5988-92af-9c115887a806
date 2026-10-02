"""FastAPI 入口：解析/重排分析 + 草稿持久化（SQLite）+ 生效版本签发。"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .parser import parse_contract
from .references import analyze, cascade_deleted
from .terms import review_terms
from .preview import preview_deletion

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "..", "data"))
DB_PATH = os.path.join(DATA_DIR, "drafts.db")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                order_json TEXT NOT NULL DEFAULT '[]',
                deleted_json TEXT NOT NULL DEFAULT '[]',
                term_ignores_json TEXT NOT NULL DEFAULT '[]',
                revision INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        # 兼容旧库：补列（SQLite 不支持 IF NOT EXISTS 加列，先查再补）
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(drafts)")}
        if "term_ignores_json" not in cols:
            conn.execute(
                "ALTER TABLE drafts ADD COLUMN term_ignores_json "
                "TEXT NOT NULL DEFAULT '[]'"
            )
        if "revision" not in cols:
            conn.execute(
                "ALTER TABLE drafts ADD COLUMN revision INTEGER NOT NULL DEFAULT 1"
            )
        # 生效版本：一经写入不可变；同一草稿的同一内容指纹只对应一个版本，
        # 重复提交直接返回原版本（fingerprint 唯一约束保证幂等）。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                draft_id INTEGER NOT NULL,
                version_no INTEGER NOT NULL,
                fingerprint TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                order_json TEXT NOT NULL DEFAULT '[]',
                deleted_json TEXT NOT NULL DEFAULT '[]',
                term_ignores_json TEXT NOT NULL DEFAULT '[]',
                analyze_json TEXT NOT NULL DEFAULT '{}',
                terms_json TEXT NOT NULL DEFAULT '{}',
                summary_json TEXT NOT NULL DEFAULT '{}',
                created_at INTEGER NOT NULL,
                UNIQUE(draft_id, fingerprint)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_versions_draft "
            "ON versions(draft_id, version_no)"
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="条款引用重排台", lifespan=lifespan)


# ---------------------------------------------------------------- 请求模型

class AnalyzeRequest(BaseModel):
    text: str = ""
    order: list[str] = Field(default_factory=list)
    deleted: list[str] = Field(default_factory=list)


class DraftIn(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    content: str = ""
    order: list[str] = Field(default_factory=list)
    deleted: list[str] = Field(default_factory=list)
    term_ignores: list[str] = Field(default_factory=list)


class IssueRequest(BaseModel):
    """从当前草稿发起「生效版本签发」。

    expected_revision 必须与草稿当前修订号一致；校验期间草稿被改过（修订号
    变化）则拒绝签发，且不产生任何版本。
    """
    expected_revision: int


# ---------------------------------------------------------------- API

@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.post("/api/analyze")
def analyze_text(req: AnalyzeRequest) -> dict:
    preamble, clauses = parse_contract(req.text)
    known = {c.id for c in clauses}
    # order 仅作「同父兄弟换序」的意图输入：未列出的条款、跨父级拖动等
    # 不合法排列都交由 analyze 按父子树规整（未列出者回到解析顺序）。
    order = [cid for cid in req.order if cid in known]
    # 删除父条款会在 analyze 中级联删除整棵子树
    deleted = [cid for cid in req.deleted if cid in known]
    result = analyze(clauses, order, deleted)
    result["preamble"] = preamble
    return result


class TermsRequest(BaseModel):
    text: str = ""
    deleted: list[str] = Field(default_factory=list)


class DeletePreviewRequest(BaseModel):
    text: str = ""
    order: list[str] = Field(default_factory=list)
    deleted: list[str] = Field(default_factory=list)
    candidates: list[str] = Field(default_factory=list)


@app.post("/api/delete-preview")
def delete_preview(req: DeletePreviewRequest) -> dict:
    """删除影响预演（只读）：勾选存活条款 → 级联集 + 删除前后绑定差异 + 原因链。"""
    preamble, clauses = parse_contract(req.text)
    known = {c.id for c in clauses}
    order = [cid for cid in req.order if cid in known]
    deleted = [cid for cid in req.deleted if cid in known]
    candidates = [cid for cid in req.candidates if cid in known]
    try:
        return preview_deletion(preamble, clauses, order, deleted, candidates)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/terms")
def review(req: TermsRequest) -> dict:
    """定义术语审校：以当前删除状态（含级联删除）为准。"""
    preamble, clauses = parse_contract(req.text)
    known = {c.id for c in clauses}
    deleted = [cid for cid in req.deleted if cid in known]
    deleted_set = cascade_deleted(clauses, deleted)
    result = review_terms(preamble, clauses, deleted_set)
    result["deleted_clauses"] = [
        {"id": c.id, "label": c.label}
        for c in clauses if c.id in deleted_set
    ]
    return result


@app.get("/api/drafts")
def list_drafts() -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT d.id, d.title, d.revision, d.created_at, d.updated_at, "
            "(SELECT COUNT(*) FROM versions v WHERE v.draft_id = d.id) AS version_count "
            "FROM drafts d ORDER BY d.updated_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/drafts")
def create_draft(req: DraftIn) -> dict:
    now = int(time.time())
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO drafts (title, content, order_json, deleted_json, "
            "term_ignores_json, revision, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
            (req.title, req.content, json.dumps(req.order, ensure_ascii=False),
             json.dumps(req.deleted, ensure_ascii=False),
             json.dumps(req.term_ignores, ensure_ascii=False), now, now),
        )
        return {"id": cur.lastrowid, "revision": 1}


@app.get("/api/drafts/{draft_id}")
def get_draft(draft_id: int) -> dict:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM drafts WHERE id = ?", (draft_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="草稿不存在")
    return {
        "id": row["id"],
        "title": row["title"],
        "content": row["content"],
        "order": json.loads(row["order_json"]),
        "deleted": json.loads(row["deleted_json"]),
        "term_ignores": json.loads(row["term_ignores_json"] or "[]"),
        "revision": row["revision"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@app.put("/api/drafts/{draft_id}")
def update_draft(draft_id: int, req: DraftIn) -> dict:
    now = int(time.time())
    with db() as conn:
        cur = conn.execute(
            "UPDATE drafts SET title=?, content=?, order_json=?, "
            "deleted_json=?, term_ignores_json=?, "
            "revision=revision+1, updated_at=? WHERE id=?",
            (req.title, req.content, json.dumps(req.order, ensure_ascii=False),
             json.dumps(req.deleted, ensure_ascii=False),
             json.dumps(req.term_ignores, ensure_ascii=False), now, draft_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="草稿不存在")
        revision = conn.execute(
            "SELECT revision FROM drafts WHERE id=?", (draft_id,)
        ).fetchone()["revision"]
    return {"ok": True, "revision": revision}


@app.delete("/api/drafts/{draft_id}")
def delete_draft(draft_id: int) -> dict:
    with db() as conn:
        cur = conn.execute("DELETE FROM drafts WHERE id=?", (draft_id,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="草稿不存在")
    return {"ok": True}


# ---------------------------------------------------------------- 生效版本签发

class _IssueConflict(Exception):
    """签发前校验失败：带结构化 blockers（HTTP 422）或纯冲突原因（409）。"""

    def __init__(self, status_code: int, message: str,
                 blockers: list[dict] | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.blockers = blockers or []


def _normalize_snapshot(text: str, order: list[str], deleted: list[str],
                        term_ignores: list[str]) -> dict:
    """固定一次签发的输入：正文、顺序、删除状态（含级联）与术语忽略项。

    返回规范化后的 (order, deleted, ignores) 及解析出的条款，供指纹与
    引用 / 术语审校共用，保证三者基于同一份固定内容。
    """
    preamble, clauses = parse_contract(text)
    known = {c.id for c in clauses}
    order = [cid for cid in order if cid in known]
    deleted_req = [cid for cid in deleted if cid in known]
    deleted_set = cascade_deleted(clauses, deleted_req)
    # 忽略项以「重新跑术语审校后仍存在的问题」为准，失效忽略项不固化进版本
    review = review_terms(preamble, clauses, deleted_set)
    live_issues = {it["occ_id"] for it in review["issues"]}
    ignores = sorted({i for i in term_ignores if i in live_issues})
    return {
        "preamble": preamble,
        "clauses": clauses,
        "order": order,
        "deleted": sorted(deleted_set),
        "ignores": ignores,
        "review": review,
    }


def _fingerprint(text: str, order: list[str], deleted: list[str],
                 ignores: list[str]) -> str:
    """同一草稿内容的身份指纹（正文 / 顺序 / 删除 / 忽略项全部参与）。"""
    payload = json.dumps(
        {"c": text, "o": order, "d": sorted(deleted), "i": sorted(ignores)},
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _collect_blockers(analyze_res: dict, term_res: dict,
                      ignores: set[str]) -> list[dict]:
    """签发门槛：引用侧（无法解析 / 目标已删除 / 循环引用）与未忽略的
    术语问题任一存在即拒绝。"""
    blockers: list[dict] = []
    ref_meta = {
        "unparsable": ("ref_unparsable", "无法解析"),
        "deleted": ("ref_deleted", "目标已删除"),
        "circular": ("ref_circular", "循环引用"),
    }
    for it in analyze_res["issues"]:
        kind, tag = ref_meta.get(it["type"], ("ref_" + it["type"], it["type"]))
        blockers.append({
            "kind": kind,
            "tag": tag,
            "message": it["message"],
            "clause_id": it.get("clause_id"),
            "target_id": it.get("target_id"),
        })
    for it in term_res["issues"]:
        if it["occ_id"] in ignores:
            continue  # 已忽略的术语问题不阻止签发
        blockers.append({
            "kind": "term_" + it["type"],
            "tag": {"undefined": "未定义术语", "duplicate": "重复定义",
                    "ambiguous": "歧义绑定", "unused": "未使用定义"}
            .get(it["type"], it["type"]),
            "message": it["message"],
            "clause_id": it.get("clause_id"),
            "occ_id": it.get("occ_id"),
        })
    return blockers


def _build_summary(analyze_res: dict, term_res: dict,
                   ignores: set[str]) -> dict:
    rc, tc = analyze_res["counts"], term_res["counts"]
    rewritten = 0
    for c in analyze_res["clauses"]:
        if c["id"] in {d["id"] for d in analyze_res["deleted_clauses"]}:
            continue
        if any(m["status"] == "ok" for m in c["refs"]):
            rewritten += 1
    active_term = sum(1 for it in term_res["issues"]
                      if it["occ_id"] not in ignores)
    return {
        "clause_total": len(analyze_res["clauses"]),
        "clause_alive": len(analyze_res["result"]),
        "clause_deleted": len(analyze_res["deleted_clauses"]),
        "ref_total": analyze_res["total_refs"],
        "ref_unparsable": rc["unparsable"],
        "ref_deleted": rc["deleted"],
        "ref_circular": rc["circular"],
        "clauses_rewritten": rewritten,
        "term_definitions": term_res["total_definitions"],
        "term_usages": term_res["total_usages"],
        "term_undefined": tc["undefined"],
        "term_duplicate": tc["duplicate"],
        "term_ambiguous": tc["ambiguous"],
        "term_unused": tc["unused"],
        "term_ignored": len(ignores),
        "term_active_issues": active_term,
    }


def _serialize_version(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "draft_id": row["draft_id"],
        "version_no": row["version_no"],
        "version_label": f"V{row['version_no']}",
        "fingerprint": row["fingerprint"],
        "title": row["title"],
        "created_at": row["created_at"],
        "summary": json.loads(row["summary_json"] or "{}"),
    }


@app.post("/api/drafts/{draft_id}/versions")
def issue_version(draft_id: int, req: IssueRequest) -> dict:
    """从当前草稿签发一个不可变的生效版本。

    - 同一操作中固定正文 / 顺序 / 删除状态 / 术语忽略项，重新运行引用与
      术语审校；存在无法解析、目标已删除、循环引用或未忽略的术语问题时
      拒绝（HTTP 422），且不改草稿、不产生版本；
    - expected_revision 与草稿当前修订号不一致时拒绝（HTTP 409）；
    - 同一草稿同一内容重复提交：返回原版本（reused=true）。
    签发过程不写 drafts 表，草稿正文 / 顺序 / 删除 / 修订号均保持不变。
    """
    conn = db()
    try:
        # BEGIN IMMEDIATE：在校验期间锁住写事务，修订号变化由二次读取兜底
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM drafts WHERE id=?", (draft_id,)
        ).fetchone()
        if row is None:
            conn.rollback()
            raise HTTPException(status_code=404, detail="草稿不存在")
        if row["revision"] != req.expected_revision:
            conn.rollback()
            raise _IssueConflict(
                409,
                f"草稿修订号已变化（当前 {row['revision']}，"
                f"提交基于 {req.expected_revision}），请刷新后重新签发",
            )

        content = row["content"]
        order = json.loads(row["order_json"])
        deleted = json.loads(row["deleted_json"])
        ignores_raw = json.loads(row["term_ignores_json"] or "[]")

        snap = _normalize_snapshot(content, order, deleted, ignores_raw)
        fp = _fingerprint(content, snap["order"], snap["deleted"],
                          snap["ignores"])

        # 幂等：同一草稿同一内容（含忽略项）已签发过 → 原样返回
        existing = conn.execute(
            "SELECT * FROM versions WHERE draft_id=? AND fingerprint=?",
            (draft_id, fp),
        ).fetchone()
        if existing is not None:
            conn.commit()
            out = _serialize_version(existing)
            out["reused"] = True
            return out

        analyze_res = analyze(snap["clauses"], snap["order"], snap["deleted"])
        analyze_res["preamble"] = snap["preamble"]
        term_res = snap["review"]
        ignores = set(snap["ignores"])

        blockers = _collect_blockers(analyze_res, term_res, ignores)
        if blockers:
            conn.rollback()
            raise _IssueConflict(
                422,
                f"校验未通过：{len(blockers)} 项问题未解决，已拒绝签发"
                "（草稿未改动）",
                blockers,
            )

        # 写版本前最后核对一次修订号（极端并发下仍保证不基于旧内容签发）
        recheck = conn.execute(
            "SELECT revision FROM drafts WHERE id=?", (draft_id,)
        ).fetchone()
        if recheck is None or recheck["revision"] != req.expected_revision:
            conn.rollback()
            raise _IssueConflict(
                409, "草稿在校验期间被修改，修订号已变化，已拒绝签发",
            )

        next_no = conn.execute(
            "SELECT COALESCE(MAX(version_no), 0) + 1 AS n "
            "FROM versions WHERE draft_id=?",
            (draft_id,),
        ).fetchone()["n"]
        now = int(time.time())
        summary = _build_summary(analyze_res, term_res, ignores)
        cur = conn.execute(
            "INSERT INTO versions (draft_id, version_no, fingerprint, title, "
            "content, order_json, deleted_json, term_ignores_json, "
            "analyze_json, terms_json, summary_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (draft_id, next_no, fp, row["title"], content,
             json.dumps(snap["order"], ensure_ascii=False),
             json.dumps(snap["deleted"], ensure_ascii=False),
             json.dumps(snap["ignores"], ensure_ascii=False),
             json.dumps(analyze_res, ensure_ascii=False),
             json.dumps(term_res, ensure_ascii=False),
             json.dumps(summary, ensure_ascii=False), now),
        )
        conn.commit()
        vrow = conn.execute(
            "SELECT * FROM versions WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        out = _serialize_version(vrow)
        out["reused"] = False
        return out
    except _IssueConflict as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"message": exc.message, "blockers": exc.blockers},
        )
    except HTTPException:
        raise
    except Exception:
        conn.rollback()  # 任何意外都不得留下写锁或半成品事务
        raise
    finally:
        conn.close()


@app.get("/api/versions")
def list_versions(draft_id: Optional[int] = None) -> list[dict]:
    """生效版本列表；?draft_id= 只看某草稿，否则按签发时间倒序返回全部。"""
    conn = db()
    try:
        if draft_id is not None:
            rows = conn.execute(
                "SELECT * FROM versions WHERE draft_id=? "
                "ORDER BY version_no DESC", (draft_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM versions ORDER BY created_at DESC, id DESC"
            ).fetchall()
        return [_serialize_version(r) for r in rows]
    finally:
        conn.close()


@app.get("/api/versions/{version_id}")
def get_version(version_id: int) -> dict:
    """版本详情：签发时固定的正文 / 条款编号 / 引用改写结果 / 术语绑定。"""
    conn = db()
    try:
        row = conn.execute(
            "SELECT * FROM versions WHERE id=?", (version_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="生效版本不存在")
    return {
        **_serialize_version(row),
        "content": row["content"],
        "order": json.loads(row["order_json"]),
        "deleted": json.loads(row["deleted_json"]),
        "term_ignores": json.loads(row["term_ignores_json"] or "[]"),
        "analyze": json.loads(row["analyze_json"] or "{}"),
        "terms": json.loads(row["terms_json"] or "{}"),
    }


# ---------------------------------------------------------------- 静态页面

STATIC_DIR = os.path.join(BASE_DIR, "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))
