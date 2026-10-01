"""定义术语审校。

定义识别（仅取中文引号内的名称）：
- ``“术语”是指 / 系指 / 指``             → 全文定义（作用域为整篇合同）；
- ``本合同/本协议所称“术语”为/是指……``    → 全文定义；
- ``本条/本款所称“术语”为/是指……``        → 局部定义，作用域为所在条款及其后代。

使用识别：
- 中文引号（“” / 「」 / 『』 / 直引号）中的短语一律作为候选使用；
- 已定义名称在引号外的无引号出现同样扫描；
- 多个名称在同一位置重叠时按最长优先匹配；定义名称所在的引号片段不算使用。

绑定规则：
- 使用处优先绑定最近祖先条款上的局部定义，找不到再回退全文定义；
- 同一作用域内存在多个同名定义时不得任选，绑定记为「歧义」。

问题类型：未定义 / 重复定义 / 歧义绑定 / 未使用定义。
审校只看当前存活条款（删除级联以 references.cascade_deleted 为准）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .parser import Clause
from .references import path_label

# 中文引号（含直角引号与直引号），开引号/闭引号按类别配对但允许互换
_OPEN = "“「『\""
_CLOSE = "”」』\""
QUOTE_RE = re.compile(rf"[{_OPEN}](?P<name>[^{_CLOSE}\r\n]{{1,30}}?)[{_CLOSE}]")

# 作用域前缀（紧贴开引号之前）
GLOBAL_PREFIX_RE = re.compile(r"(?:本合同|本协议)(?:中|项下)?所称[ \t　]*$")
LOCAL_PREFIX_RE = re.compile(r"(?:本条|本款)(?:中)?所称[ \t　]*$")

# 带作用域前缀时，引号后允许的判定谓词；裸“短语”仅在 是指/系指/指 时成定义
PREFIX_VERBS = ("是指", "系指", "指", "为", "是")
BARE_VERBS = ("是指", "系指", "指")

_PREFIX_LOOKBACK = 12


@dataclass
class TDef:
    id: str
    name: str
    scope: str                 # global | local
    scope_path: tuple[int, ...]
    block_id: str
    clause_id: str | None
    start: int
    end: int
    label: str                 # 所在条款编号（展示用）
    used: bool = False         # 有无歧义之外的确定绑定
    referred: bool = False     # 歧义使用是否指向过它（用于抑制未使用告警）
    duplicate: bool = False
    usage_ids: list[str] = field(default_factory=list)
    ambiguous_usage_ids: list[str] = field(default_factory=list)


@dataclass
class TUse:
    id: str
    name: str
    block_id: str
    clause_id: str | None
    start: int
    end: int
    quoted: bool
    path: tuple[int, ...] | None     # 所在条款路径（preamble 为 None）
    status: str = "ok"               # ok | undefined | ambiguous
    def_ids: list[str] = field(default_factory=list)
    scope_path: tuple[int, ...] | None = None


def scope_label(path: tuple[int, ...]) -> str:
    return "全文" if not path else path_label(path)


# ---------------------------------------------------------------- 扫描

@dataclass
class _Block:
    id: str
    kind: str                # preamble | clause
    label: str
    heading: str
    depth: int
    text: str
    path: tuple[int, ...] | None
    clause_id: str | None


def _starts_with_any(s: str, verbs: tuple[str, ...]) -> bool:
    return any(s.startswith(v) for v in verbs)


def _scan_block(block: _Block) -> tuple[list[TDef], list[TUse], list[re.Match]]:
    """返回 (定义, 引号使用候选, 全部引号匹配)。"""
    text = block.text
    quotes = list(QUOTE_RE.finditer(text))
    defs: list[TDef] = []
    quoted_uses: list[TUse] = []

    for seq, m in enumerate(quotes):
        name = m.group("name").strip()
        if not name:
            continue
        pre = text[max(0, m.start() - _PREFIX_LOOKBACK):m.start()]
        post = text[m.end():m.end() + 2]

        scope_path: tuple[int, ...] | None = None
        is_def = False
        if block.path is not None and LOCAL_PREFIX_RE.search(pre):
            if _starts_with_any(post, PREFIX_VERBS):
                scope_path, is_def = block.path, True
        elif GLOBAL_PREFIX_RE.search(pre):
            if _starts_with_any(post, PREFIX_VERBS):
                scope_path, is_def = (), True
        elif _starts_with_any(post, BARE_VERBS):
            scope_path, is_def = (), True

        if is_def:
            defs.append(TDef(
                id=f"d-{block.id}-{m.start()}",
                name=name,
                scope="local" if scope_path else "global",
                scope_path=scope_path or (),
                block_id=block.id,
                clause_id=block.clause_id,
                start=m.start(), end=m.end(),
                label=block.label,
            ))
        else:
            quoted_uses.append(TUse(
                id=f"u-{block.id}-{m.start()}",
                name=name, block_id=block.id, clause_id=block.clause_id,
                start=m.start(), end=m.end(), quoted=True, path=block.path,
            ))

    return defs, quoted_uses, quotes


def _scan_unquoted(block: _Block, names: list[str],
                   quote_spans: list[tuple[int, int]]) -> list[TUse]:
    """引号之外按最长优先扫描已定义名称的无引号出现。"""
    if not names:
        return []
    text = block.text
    names = sorted(names, key=len, reverse=True)

    def in_quote(i: int) -> bool:
        return any(s <= i < e for s, e in quote_spans)

    uses: list[TUse] = []
    i = 0
    while i < len(text):
        if in_quote(i):
            i += 1
            continue
        hit = next((n for n in names if text.startswith(n, i)), None)
        if hit is None:
            i += 1
            continue
        uses.append(TUse(
            id=f"u-{block.id}-{i}",
            name=hit, block_id=block.id, clause_id=block.clause_id,
            start=i, end=i + len(hit), quoted=False, path=block.path,
        ))
        i += len(hit)
    return uses


# ---------------------------------------------------------------- 主入口

def review_terms(preamble: str, clauses: list[Clause],
                 deleted_set: set[str]) -> dict:
    blocks: list[_Block] = [
        _Block(id="preamble", kind="preamble", label="", heading="",
               depth=0, text=preamble or "", path=None, clause_id=None)
    ]
    for c in clauses:
        if c.id in deleted_set:
            continue
        blocks.append(_Block(
            id=c.id, kind="clause", label=c.label, heading=c.heading,
            depth=len(c.path), text=c.text, path=c.path, clause_id=c.id,
        ))

    all_defs: list[TDef] = []
    all_uses: list[TUse] = []
    quotes_by_block: dict[str, list[tuple[int, int]]] = {}

    for b in blocks:
        defs, quoted_uses, quotes = _scan_block(b)
        all_defs.extend(defs)
        all_uses.extend(quoted_uses)
        quotes_by_block[b.id] = [(m.start(), m.end()) for m in quotes]

    # 无引号出现：候选名取自全部存活定义（可见性在绑定阶段判定）
    name_pool = sorted({d.name for d in all_defs})
    for b in blocks:
        all_uses.extend(_scan_unquoted(b, name_pool, quotes_by_block[b.id]))

    def_by_id = {d.id: d for d in all_defs}

    # 作用域分组：(作用域路径, 名称) -> 定义列表（保持文档顺序）
    groups: dict[tuple[tuple[int, ...], str], list[str]] = {}
    for d in all_defs:
        groups.setdefault((d.scope_path, d.name), []).append(d.id)

    # 仅在别处子树存在的局部定义（未定义提示用）
    local_scopes_by_name: dict[str, list[tuple[int, ...]]] = {}
    for (p, name), ids in groups.items():
        if p:
            local_scopes_by_name.setdefault(name, [])
            if p not in local_scopes_by_name[name]:
                local_scopes_by_name[name].append(p)
    for v in local_scopes_by_name.values():
        v.sort()

    def resolve(u: TUse) -> None:
        # 由近到远的祖先局部作用域，最后回退全文作用域
        candidates: list[tuple[int, ...]] = []
        if u.path is not None:
            candidates = [u.path[:k] for k in range(len(u.path), 0, -1)]
        candidates.append(())

        for sc in candidates:
            ids = groups.get((sc, u.name))
            if not ids:
                continue
            u.scope_path = sc
            if len(ids) == 1:
                u.status = "ok"
                u.def_ids = [ids[0]]
                d = def_by_id[ids[0]]
                d.used = True
                d.usage_ids.append(u.id)
            else:
                u.status = "ambiguous"
                u.def_ids = list(ids)
                # 歧义使用不计入「已使用」（不抑制未使用、不计使用次数），
                # 但记录反向链接供双向定位（歧义定义 → 指向它的歧义使用）
                for did in ids:
                    def_by_id[did].referred = True
                    def_by_id[did].ambiguous_usage_ids.append(u.id)
            return
        u.status = "undefined"

    for u in all_uses:
        resolve(u)

    for ids in groups.values():
        if len(ids) > 1:
            for did in ids:
                def_by_id[did].duplicate = True

    # ---- 问题 ----
    issues: list[dict] = []

    # 按文档顺序输出重复定义（每个参与重复的定义各一条）
    for d in all_defs:
        if not d.duplicate:
            continue
        peers = groups[(d.scope_path, d.name)]
        issues.append({
            "type": "duplicate",
            "occ_id": d.id,
            "block_id": d.block_id,
            "clause_id": d.clause_id,
            "start": d.start,
            "end": d.end,
            "name": d.name,
            "message": (
                f"术语“{d.name}”在{scope_label(d.scope_path)}作用域内被重复定义"
                f"（共 {len(peers)} 处）"
            ),
        })

    for u in all_uses:
        if u.status == "ambiguous":
            issues.append({
                "type": "ambiguous",
                "occ_id": u.id,
                "block_id": u.block_id,
                "clause_id": u.clause_id,
                "start": u.start,
                "end": u.end,
                "name": u.name,
                "message": (
                    f"术语“{u.name}”在{scope_label(u.scope_path or ())}作用域有 "
                    f"{len(u.def_ids)} 个同名定义，绑定存在歧义"
                ),
            })
        elif u.status == "undefined":
            hint = ""
            elsewhere = local_scopes_by_name.get(u.name, [])
            if elsewhere:
                hint = f"（局部定义仅见于 {path_label(elsewhere[0])} 子树）"
            issues.append({
                "type": "undefined",
                "occ_id": u.id,
                "block_id": u.block_id,
                "clause_id": u.clause_id,
                "start": u.start,
                "end": u.end,
                "name": u.name,
                "message": f"未定义术语“{u.name}”{hint}",
            })

    for d in all_defs:
        if d.duplicate or d.used or d.referred:
            continue
        scope = scope_label(d.scope_path)
        where = "全文" if not d.scope_path else f"{scope}及其子条款"
        issues.append({
            "type": "unused",
            "occ_id": d.id,
            "block_id": d.block_id,
            "clause_id": d.clause_id,
            "start": d.start,
            "end": d.end,
            "name": d.name,
            "message": f"术语“{d.name}”已定义，但在{where}范围内未被使用",
        })

    # ---- 输出块（含标记） ----
    defs_by_block: dict[str, list[TDef]] = {}
    uses_by_block: dict[str, list[TUse]] = {}
    for d in all_defs:
        defs_by_block.setdefault(d.block_id, []).append(d)
    for u in all_uses:
        uses_by_block.setdefault(u.block_id, []).append(u)

    out_blocks: list[dict] = []
    for b in blocks:
        marks: list[dict] = []
        for d in defs_by_block.get(b.id, []):
            if d.duplicate:
                status = "duplicate"
            elif not d.used and not d.referred:
                status = "unused"
            else:
                status = "ok"
            marks.append({
                "occ_id": d.id, "kind": "def", "start": d.start, "end": d.end,
                "name": d.name, "status": status,
                "scope": d.scope, "scope_label": scope_label(d.scope_path),
                "target_ids": list(d.usage_ids) + list(d.ambiguous_usage_ids),
            })
        for u in uses_by_block.get(b.id, []):
            marks.append({
                "occ_id": u.id, "kind": "use", "start": u.start, "end": u.end,
                "name": u.name, "status": u.status, "quoted": u.quoted,
                "target_ids": list(u.def_ids),
                "scope_label": scope_label(u.scope_path or ())
                if u.status != "undefined" else "",
            })
        marks.sort(key=lambda m: m["start"])

        out_blocks.append({
            "id": b.id, "kind": b.kind, "label": b.label,
            "heading": b.heading, "depth": b.depth,
            "path": list(b.path) if b.path is not None else [],
            "text": b.text, "marks": marks,
        })

    definitions = [{
        "id": d.id, "name": d.name, "scope": d.scope,
        "scope_label": scope_label(d.scope_path),
        "block_id": d.block_id, "clause_id": d.clause_id,
        "start": d.start,
        "status": ("duplicate" if d.duplicate
                   else "unused" if not d.used and not d.referred
                   else "ok"),
        "usage_count": len(d.usage_ids),
        "label": d.label,
    } for d in all_defs]

    counts = {"undefined": 0, "duplicate": 0, "ambiguous": 0, "unused": 0}
    for it in issues:
        counts[it["type"]] += 1

    # 问题按文档位置排序（前置说明 → 各存活条款 → 块内偏移）
    block_order = {b.id: i for i, b in enumerate(blocks)}
    issues.sort(key=lambda it: (block_order.get(it["block_id"], 10 ** 9),
                                it["start"]))

    return {
        "blocks": out_blocks,
        "definitions": definitions,
        "issues": issues,
        "counts": counts,
        "total_definitions": len(all_defs),
        "total_usages": len(all_uses),
    }
