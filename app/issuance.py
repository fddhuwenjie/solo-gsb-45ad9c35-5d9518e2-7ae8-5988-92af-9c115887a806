"""生效版本签发：在同一操作里固定草稿快照，并重跑引用 / 术语终审。

签发只接受「零阻塞」快照：

- 引用审校不得存在 无法解析 / 目标已删除 / 循环引用；
- 术语审校除草稿固定的忽略项（``term_ignores``）外不得遗留任何问题。

校验通过后，正文、顺序、删除状态、忽略项连同当时的条款编号、引用改写结果
与术语绑定一并固化为不可变版本；相同快照重复签发返回同一版本（幂等）。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .parser import parse_contract
from .references import analyze, cascade_deleted
from .terms import review_terms


def snapshot_fingerprint(content: str, order: list[str], deleted: list[str],
                         term_ignores: list[str]) -> str:
    """草稿快照指纹：正文 / 顺序 / 删除状态 / 术语忽略项四要素的规范哈希。"""
    body = json.dumps(
        {"content": content, "order": order,
         "deleted": deleted, "term_ignores": term_ignores},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def validate_snapshot(content: str, order_in: list[str],
                      deleted_in: list[str], ignores: list[str]):
    """以签发快照重跑引用与术语审校，返回 (前置说明, 引用结果, 术语结果, 阻塞项)。

    与 /api/analyze、/api/terms 相同的入参规整：剔除解析不到的条款 id，
    删除父条款按级联展开。阻塞项中的术语问题已扣除固定忽略项。
    """
    preamble, clauses = parse_contract(content)
    known = {c.id for c in clauses}
    order = [cid for cid in order_in if cid in known]
    deleted = [cid for cid in deleted_in if cid in known]

    ref_result = analyze(clauses, order, deleted)
    deleted_set = cascade_deleted(clauses, deleted)
    term_result = review_terms(preamble, clauses, deleted_set)

    ignore_set = set(ignores)
    active_term_issues = [
        it for it in term_result["issues"] if it["occ_id"] not in ignore_set
    ]
    blockers = {
        "reference_issues": list(ref_result["issues"]),
        "term_issues": active_term_issues,
    }
    return preamble, ref_result, term_result, blockers


def has_blockers(blockers: dict) -> bool:
    return bool(blockers["reference_issues"] or blockers["term_issues"])


def _block_label(b: dict) -> str:
    return "前置说明" if b.get("kind") == "preamble" else b.get("label", "")


def build_summary(ref_result: dict, term_result: dict,
                  ignores: list[str]) -> dict[str, Any]:
    """签发摘要：当时的条款编号、引用改写结果与术语绑定（精简、可列表展示）。"""
    # 条款编号（按签发时的新顺序）
    clauses_order = [
        {
            "id": c["id"], "label": c["label"], "heading": c["heading"],
            "new_label": c["new_label"], "new_position": c["new_position"],
        }
        for c in ref_result["result"]
    ]
    by_id = {c["id"]: c for c in ref_result["clauses"]}

    # 引用改写结果（仅成功重写的引用）
    rewrites: list[dict] = []
    for c in ref_result["clauses"]:
        for m in c["refs"]:
            if m["status"] != "ok":
                continue
            tgt = by_id.get(m["target_id"]) if m.get("target_id") else None
            rewrites.append({
                "clause_id": c["id"],
                "clause_label": c["new_label"] or c["label"],
                "text": m["text"],
                "new_text": m["new_text"],
                "target_id": m.get("target_id"),
                "target_label": tgt["new_label"] if tgt else None,
            })

    # 术语绑定：每处已确定绑定的使用 → 唯一定义
    defs = {d["id"]: d for d in term_result["definitions"]}
    bindings: list[dict] = []
    for b in term_result["blocks"]:
        for m in b.get("marks", []):
            if m["kind"] != "use" or m["status"] != "ok" \
                    or not m.get("target_ids"):
                continue
            d = defs.get(m["target_ids"][0])
            bindings.append({
                "use_id": m["occ_id"],
                "name": m["name"],
                "block_id": b["id"],
                "where": _block_label(b),
                "def_id": d["id"] if d else None,
                "def_name": d["name"] if d else m["name"],
                "scope_label": m.get("scope_label")
                or (d["scope_label"] if d else ""),
                "def_where": d["label"] if d else "",
            })

    ignore_set = set(ignores)
    ignored_issues = [
        {"occ_id": it["occ_id"], "type": it["type"],
         "name": it["name"], "message": it["message"]}
        for it in term_result["issues"] if it["occ_id"] in ignore_set
    ]

    return {
        "clause_count": len(ref_result["result"]),
        "deleted_count": len(ref_result["deleted_clauses"]),
        "reference_total": ref_result["total_refs"],
        "reference_rewritten": len(rewrites),
        "term_definition_count": term_result["total_definitions"],
        "term_usage_count": term_result["total_usages"],
        "term_binding_count": len(bindings),
        "ignored_count": len(ignored_issues),
        "clauses": clauses_order,
        "rewrites": rewrites,
        "term_bindings": bindings,
        "ignored_issues": ignored_issues,
    }
