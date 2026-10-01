"""删除影响预演（只读分析，不修改草稿）。

把用户勾选的存活条款（父条款按现有级联规则连同后代）加入删除集，比较删除
前后两类绑定：

1. 引用绑定：删除前可解析且目标存活、删除后目标落入删除集的引用（区间按
   展开后的单条目标逐个比较，仅报告「新增失效」部分）；
2. 定义术语绑定：删除前**唯一**绑定某定义的使用，删除后失去该唯一定义
   （未定义 / 无存活同名定义）、命中多个同名定义（歧义）或改绑其他定义。

删除后的存活引用图先按**强连通分量（SCC）收缩**：每个分量（互指条款组，
含自环）只作为一个节点，直接影响在分量内去重合并，因此一个循环组至多生成
一个影响项；随后在收缩后的 DAG 上以直接受影响分量为根，沿「反向引用」边
多源 BFS，得到每个受影响分量到根因的最短原因链；等长链按文档顺序取字典序
最小者；链尾追加根因条款（被删目标 / 被移除定义所在条款）。循环组的全部
成员与各根因均可在结果中点击定位。
"""
from __future__ import annotations

from collections import deque

from .parser import Clause
from .references import (
    _scan,
    _canonical_order,
    cascade_deleted,
    expand_range,
)
from .terms import review_terms

# 明细类型 -> 次级排序（同一偏移处引用排在术语前）
_DETAIL_ORDER = {
    "ref_broken": 0,
    "term_lost_unique": 1,
    "term_ambiguous": 1,
    "term_rebound": 1,
}


def _clause_brief(c: Clause | None) -> dict:
    if c is None:
        return {"id": "", "label": "", "heading": ""}
    return {"id": c.id, "label": c.label, "heading": c.heading or ""}


# ---------------------------------------------------------------- 术语绑定对照

def _use_index(review: dict) -> dict[str, dict]:
    """occ_id -> 使用标记（附 block_id，便于定位所在条款）。"""
    out: dict[str, dict] = {}
    for b in review["blocks"]:
        for m in b["marks"]:
            if m["kind"] == "use":
                out[m["occ_id"]] = dict(m, block_id=b["id"])
    return out


def _def_index(review: dict) -> dict[str, dict]:
    return {d["id"]: d for d in review["definitions"]}


def _term_details(preamble: str, clauses: list[Clause],
                  cur_del: set[str], new_del: set[str]
                  ) -> tuple[dict[str, list[dict]], list[dict]]:
    """对比删除前后的术语绑定。

    返回 (存活条款 id -> 直接影响明细, 前置说明的直接影响明细)。
    只统计删除前「唯一绑定」的使用；歧义/未定义不因删除而计入。
    """
    cur_rev = review_terms(preamble, clauses, cur_del)
    new_rev = review_terms(preamble, clauses, new_del)
    cur_uses, new_uses = _use_index(cur_rev), _use_index(new_rev)
    cur_defs, new_defs = _def_index(cur_rev), _def_index(new_rev)

    by_clause: dict[str, list[dict]] = {}
    preamble_details: list[dict] = []

    def add(owner: str, d: dict) -> None:
        (preamble_details if owner == "preamble"
         else by_clause.setdefault(owner, [])).append(d)

    for uid, um in cur_uses.items():
        if um["status"] != "ok" or len(um.get("target_ids", [])) != 1:
            continue
        owner = um["block_id"]
        if owner != "preamble" and owner in new_del:
            continue  # 使用处自身将被删除，无从失效
        old_did = um["target_ids"][0]
        old_def = cur_defs.get(old_did, {})
        post = new_uses.get(uid)

        kind: str | None = None
        if post is None or post["status"] == "undefined":
            kind = "term_lost_unique"
        elif post["status"] == "ambiguous":
            kind = "term_ambiguous"
        elif (post["status"] == "ok" and post.get("target_ids")
              and post["target_ids"][0] != old_did):
            kind = "term_rebound"
        if kind is None:
            continue

        name = um["name"]
        old_where = old_def.get("scope_label", "")
        old_label = f"{old_where}局部定义" if old_def.get("scope") == "local" \
            else "全文定义"
        if kind == "term_lost_unique":
            message = (
                f"术语“{name}”失去唯一定义：其绑定的{old_where}定义"
                f"（{old_def.get('label', '')}）将随删除移除，本处变为未定义"
            )
        elif kind == "term_ambiguous":
            cand = [new_defs[i]["label"] for i in post["target_ids"]
                    if i in new_defs]
            message = (
                f"术语“{name}”原绑定的{old_label}（{old_def.get('label', '')}）"
                f"删除后，本处命中 {len(post['target_ids'])} 个同名定义"
                f"（{'、'.join(cand)}），变为歧义绑定"
            )
        else:
            new_def = new_defs.get(post["target_ids"][0], {})
            message = (
                f"术语“{name}”将改绑其他定义："
                f"{old_def.get('label', '')} → {new_def.get('label', '')}"
            )

        # 根因落到「被移除的旧定义」所在条款（前置说明中的全文定义不可删）
        terminal = old_def.get("clause_id") or owner
        add(owner, {
            "kind": kind,
            "message": message,
            "start": um["start"],
            "target_id": terminal if terminal != "preamble" else None,
        })

    return by_clause, preamble_details


# ---------------------------------------------------------------- 强连通分量（Tarjan，迭代版）

def _scc(nodes: list[str], edges: dict[str, set[str]],
         doc: dict[str, int]) -> dict[str, int]:
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    comp: dict[str, int] = {}
    counter = 0

    for start in nodes:
        if start in index:
            continue
        index[start] = low[start] = counter
        counter += 1
        stack.append(start)
        on_stack.add(start)
        work: list[tuple[str, object]] = [
            (start, iter(sorted(edges[start], key=lambda x: doc[x])))]
        while work:
            v, it = work[-1]
            descended = False
            for w in it:  # type: ignore[union-attr]
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, iter(sorted(edges[w],
                                                key=lambda x: doc[x]))))
                    descended = True
                    break
                if w in on_stack:
                    low[v] = min(low[v], index[w])
            if descended:
                continue
            if low[v] == index[v]:
                cid = len(comp)
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp[w] = cid
                    if w == v:
                        break
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[v])
    return comp


# ---------------------------------------------------------------- 主入口

def preview_deletion(preamble: str,
                     clauses: list[Clause],
                     order: list[str],
                     deleted: list[str],
                     candidates: list[str]) -> dict:
    by_id = {c.id: c for c in clauses}
    doc = {c.id: i for i, c in enumerate(clauses)}
    path_to_id: dict[tuple[int, ...], str] = {}
    for c in clauses:
        path_to_id.setdefault(c.path, c.id)

    cur_del = cascade_deleted(clauses, deleted)
    valid_pick = [cid for cid in candidates
                  if cid in by_id and cid not in cur_del]
    if not valid_pick:
        raise ValueError("没有可预演删除的存活条款")
    pick_set = set(valid_pick)
    new_del = cascade_deleted(clauses, deleted + valid_pick)
    added = new_del - cur_del

    _, survivors_cur, _ = _canonical_order(clauses, order, cur_del)
    _, survivors_new, _ = _canonical_order(clauses, order, new_del)
    survivors_new_set = set(survivors_new)

    raw_by = {c.id: _scan(c.text) for c in clauses}

    # ---- 1) 引用绑定：仅报告删除前存活、删除后落入删除集的目标 ----
    direct: dict[str, list[dict]] = {}
    seen: dict[str, set] = {}

    def add_direct(owner: str, key: tuple, d: dict) -> None:
        bucket = seen.setdefault(owner, set())
        if key in bucket:
            return
        bucket.add(key)
        direct.setdefault(owner, []).append(d)

    for cid in survivors_cur:
        if cid in new_del:
            continue
        for r in raw_by[cid]:
            if r.kind == "single":
                tid = path_to_id.get(r.a)  # type: ignore[arg-type]
                if tid and tid not in cur_del and tid in added:
                    t = by_id[tid]
                    add_direct(cid, ("ref", r.text, tid), {
                        "kind": "ref_broken",
                        "message": (
                            f"引用失效：「{r.text}」指向的{t.label}"
                            f"（{t.heading or '该条款'}）将被删除"
                        ),
                        "start": r.start,
                        "target_id": tid,
                        "lost_ids": (tid,),
                    })
            elif r.kind == "range":
                ids, _ = expand_range(r.a, r.b, path_to_id)  # type: ignore
                lost = [t for t in ids
                        if t not in cur_del and t in added]
                if lost:
                    labels = "、".join(
                        f"{by_id[t].label}（{by_id[t].heading or '该条款'}）"
                        for t in lost)
                    add_direct(cid, ("ref", tuple(lost)), {
                        "kind": "ref_broken",
                        "message": f"区间引用「{r.text}」中 {labels} 将失效",
                        "start": r.start,
                        "target_id": lost[0],
                        "lost_ids": tuple(lost),
                    })

    # ---- 2) 术语绑定对照 ----
    term_by_clause, preamble_details = _term_details(
        preamble, clauses, cur_del, new_del)
    for owner, dets in term_by_clause.items():
        for d in dets:
            add_direct(owner, ("term", d["kind"], d["message"], d["start"]), d)

    # ---- 3) 删除后存活条款上的引用图（含显式自引用，区间自指不计边） ----
    edges: dict[str, set[str]] = {cid: set() for cid in survivors_new}
    for cid in survivors_new:
        for r in raw_by[cid]:
            if r.kind == "single":
                tid = path_to_id.get(r.a)  # type: ignore[arg-type]
                if tid and tid in survivors_new_set:
                    edges[cid].add(tid)
            elif r.kind == "range":
                ids, _ = expand_range(r.a, r.b, path_to_id)  # type: ignore
                for tid in ids:
                    if tid != cid and tid in survivors_new_set:
                        edges[cid].add(tid)

    # ---- 4) 存活引用图按强连通分量（SCC）收缩：循环组只作一个节点 ----
    comp = _scc(survivors_new, edges, doc)
    comp_members: dict[int, list[str]] = {}
    for cid in survivors_new:
        comp_members.setdefault(comp[cid], []).append(cid)
    for mem in comp_members.values():
        mem.sort(key=lambda x: doc[x])
    comps = sorted(comp_members, key=lambda k: doc[comp_members[k][0]])
    # 分量文档序取其首成员，作为等长链字典序决胜依据
    cdoc = {k: doc[comp_members[k][0]] for k in comps}

    # 循环分量：多成员 SCC（互相引用），或单成员上存在显式自引用（自环）
    cyclic_comp: dict[int, bool] = {
        k: len(mem) > 1 or mem[0] in edges[mem[0]]
        for k, mem in comp_members.items()
    }

    cedges: dict[int, set[int]] = {k: set() for k in comps}
    for u, vs in edges.items():
        for v in vs:
            if comp[u] != comp[v]:
                cedges[comp[u]].add(comp[v])

    # ---- 5) 直接影响按分量归并：组内同根因引用去重，术语按出现处保留 ----
    direct_comp: dict[int, list[dict]] = {}
    merged_keys: dict[int, set] = {}
    for owner, dets in direct.items():
        k = comp[owner]
        for d in dets:
            if d["kind"] == "ref_broken":
                # 引用失效只与「被删目标」有关：组内指向同一批被删目标的
                # 引用（单条 / 区间）合并为一项
                key = ("ref", d.get("lost_ids", (d.get("target_id"),)))
                if key in merged_keys.setdefault(k, set()):
                    continue
                merged_keys[k].add(key)
            d = dict(d, owner_id=owner)
            direct_comp.setdefault(k, []).append(d)

    # 根分量 -> 根因条款（旧定义所在 / 被引目标；分量成员自身除外）
    root_terminals: dict[int, list[str]] = {}
    for k, dets in direct_comp.items():
        member_set = set(comp_members[k])
        roots = sorted({
            d.get("target_id") for d in dets
            if d.get("target_id") and d["target_id"] not in member_set
        }, key=lambda x: doc.get(x, 10 ** 9))
        if roots:
            root_terminals[k] = roots

    roots_comp = set(direct_comp)

    # ---- 6) 收缩后的 DAG 上多源 BFS（反向边）求最短距离 ----
    crev: dict[int, list[int]] = {k: [] for k in comps}
    for u, vs in cedges.items():
        for v in vs:
            crev[v].append(u)
    for vs in crev.values():
        vs.sort(key=lambda x: cdoc[x])

    dist: dict[int, int] = {k: 0 for k in roots_comp}
    dq: deque[int] = deque(sorted(roots_comp, key=lambda x: cdoc[x]))
    while dq:
        cur = dq.popleft()
        for nxt in crev[cur]:
            if nxt not in dist:
                dist[nxt] = dist[cur] + 1
                dq.append(nxt)

    # 等长链取文档顺序字典序最小者：按距离分层，沿出边选 best 链最小的前驱
    best: dict[int, list[int]] = {k: [k] for k in roots_comp}
    max_dist = max(dist.values(), default=0)
    for level in range(1, max_dist + 1):
        layer = sorted((n for n in dist if dist[n] == level),
                       key=lambda x: cdoc[x])
        for n in layer:
            preds = [p for p in cedges[n] if dist.get(p) == level - 1]
            chosen = min(preds,
                         key=lambda p: [cdoc[q] for q in best[p]])
            best[n] = [n] + best[chosen]

    # ---- 7) 组装影响行（每个受影响分量至多一行；组内明细合并后按处排序）----
    def step_for(k: int, terminal: bool = False) -> dict:
        mem = comp_members[k]
        return {
            "kind": "group" if len(mem) > 1 else "clause",
            "cycle": cyclic_comp[k],
            "terminal": terminal,
            "members": [_clause_brief(by_id.get(x)) for x in mem],
        }

    impacts: list[dict] = []
    for k in sorted(dist, key=lambda x: (dist[x], cdoc[x])):
        mem = comp_members[k]
        rep = by_id[mem[0]]  # 组代表：文档顺序最靠前的成员

        dets = sorted(
            direct_comp.get(k, []),
            key=lambda d: (doc.get(d["owner_id"], 10 ** 9),
                           d["start"], _DETAIL_ORDER.get(d["kind"], 9)))

        chain_steps = [step_for(x) for x in best[k]]
        # 根因条款（可能有多个，如组内成员分别指向不同被删目标）统一挂在
        # 链尾的根分量之后：连锁行为「本分量 ▸ … ▸ 根分量 ▸ 根因条款」。
        root_k = best[k][-1]
        for tid in root_terminals.get(root_k, []):
            if tid in comp:  # 根因理论上均在删除集；存活时退化为对应分量
                chain_steps.append(step_for(comp[tid], terminal=True))
            else:
                chain_steps.append({
                    "kind": "clause", "cycle": False, "terminal": True,
                    "members": [_clause_brief(by_id.get(tid))],
                })

        # 行标签：循环组列出全部成员；普通行沿用单条款标签
        if len(mem) > 1:
            label = "循环组（" + "、".join(by_id[x].label for x in mem) + "）"
            headings = [by_id[x].heading for x in mem if by_id[x].heading]
            heading = " ↔ ".join(dict.fromkeys(headings))
        else:
            label = rep.label
            heading = rep.heading or ""

        impacts.append({
            "clause_id": mem[0],
            "member_ids": list(mem),
            "cycle_group": len(mem) > 1,
            "label": label,
            "heading": heading,
            "distance": dist[k],
            "cycle": cyclic_comp[k],
            "chain": chain_steps,
            "details": [{
                "kind": d["kind"],
                "message": d["message"],
                "target_id": d.get("target_id"),
                "owner_id": d.get("owner_id"),
            } for d in dets],
        })

    # 前置说明中的直接影响（术语使用；不能成为引用边节点）
    if preamble_details:
        dets = sorted(preamble_details,
                      key=lambda d: (d["start"],
                                     _DETAIL_ORDER.get(d["kind"], 9)))
        chain = [{
            "kind": "preamble", "cycle": False, "terminal": False,
            "members": [{"id": "preamble", "label": "前置说明", "heading": ""}],
        }]
        for terminal in sorted({d.get("target_id") for d in dets if d.get("target_id")},
                               key=lambda x: doc.get(x, 10 ** 9)):
            chain.append({
                "kind": "clause", "cycle": False, "terminal": True,
                "members": [_clause_brief(by_id.get(terminal))],
            })
        impacts.insert(0, {
            "clause_id": "preamble",
            "member_ids": ["preamble"],
            "cycle_group": False,
            "label": "前置说明",
            "heading": "",
            "distance": 0,
            "cycle": False,
            "chain": chain,
            "details": [{
                "kind": d["kind"],
                "message": d["message"],
                "target_id": d.get("target_id"),
                "owner_id": None,
            } for d in dets],
        })

    to_delete = []
    for c in clauses:
        if c.id in added:
            item = _clause_brief(c)
            item["selected"] = c.id in pick_set
            to_delete.append(item)
    already = [
        _clause_brief(by_id[c.id]) for c in clauses if c.id in cur_del
    ]

    n_ref = sum(1 for row in impacts for d in row["details"]
                if d["kind"] == "ref_broken")
    n_term = sum(len(row["details"]) for row in impacts) - n_ref
    return {
        "candidates": [c.id for c in clauses if c.id in pick_set],
        "to_delete": to_delete,
        "already_deleted": already,
        "impacts": impacts,
        "counts": {
            "to_delete": len(to_delete),
            "selected": len(pick_set),
            "cascade": len(to_delete) - len(pick_set),
            "impacts": len(impacts),
            "direct": sum(1 for r in impacts if r["distance"] == 0),
            "transitive": sum(1 for r in impacts if r["distance"] > 0),
            "ref": n_ref,
            "term": n_term,
            "cycles": sum(1 for r in impacts if r["cycle"]),
        },
    }
