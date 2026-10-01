"""引用绑定、层级重编号与改写。

流程：
1. 在每条正文中扫描引用 token：区间（第3条至第5条 / 第3-5条）→ 单条（第3.2条）；
   扫描过的字符区间记录下来，对未覆盖的「第…条」可疑片段标记为「无法解析」。
2. 按原始路径把引用绑定到条款；区间按层级展开为单条集合。
3. 用引用图 DFS 检测循环引用（沿「存活条款」的引用边可回到自身即为循环）。
4. 按「父子树 + 同父兄弟换序」重排并逐层重编号：
   - 条款以原路径构成父子树（如 1.1.1 的父级是 1.1），order 为存活条款的
     先序扁平序列，拖动父条款等价于连同整棵子树一起移动；
   - 重编号只允许发生在同一父级的兄弟之间：各层兄弟按新顺序分别连续编号
     （第1条、第1.1条、第1.1.1条……），子条款不再被展平成顶层序号；
   - 删除父条款时级联删除其全部后代；
   - 正文逐 token 改写：单条引用改写为目标的完整新路径；区间目标只有在
     「同属一个父级且末段在新位置连续」时才合并，否则按新顺序拆成单条或
     同级连续区间枚举；目标缺失/已删除 → 保留原文并标注。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .parser import Clause, NUM_TOKEN, parse_path

# 相对/泛指引用，合法但不参与绑定（扫描到即跳过）
RELATIVE_TOKENS = ("本条", "本条约", "本条所称", "前条", "后条", "上条",
                   "下条", "该条", "本条之", "本条另有", "本条规定",
                   "各条", "每条", "任何条", "其他条", "前述条款", "本合同条款")

# 区间：第A条至第B条 / 第A条到第B条 / 第A-B条 / 第A～B条（结尾「条」必填，
# 避免把「第3条至第5款」这类不同类型引用误判为区间）
RANGE_RE = re.compile(
    rf"第(?P<a>{NUM_TOKEN})条?\s*(?:至|到|——|—|–|－|-|～|~)\s*"
    rf"(?:第)?(?P<b>{NUM_TOKEN})条"
)
# 单条
SINGLE_RE = re.compile(rf"第(?P<n>{NUM_TOKEN})条")
# 可疑片段：第 与 条 之间不是合法编号
BROAD_RE = re.compile(r"第[^第条\r\n]{0,12}?条")

_PATH_LABEL_CACHE: dict[tuple[int, ...], str] = {}


def path_label(path: tuple[int, ...]) -> str:
    """路径的规范引用写法，如 (3, 2) -> '第3.2条'，(3,) -> '第3条'。"""
    if path not in _PATH_LABEL_CACHE:
        _PATH_LABEL_CACHE[path] = "第" + ".".join(str(p) for p in path) + "条"
    return _PATH_LABEL_CACHE[path]


# ---------------------------------------------------------------- 扫描

@dataclass
class RawRef:
    kind: str            # range | single | unparsable
    start: int
    end: int
    text: str
    a: tuple[int, ...] | None = None
    b: tuple[int, ...] | None = None


def _scan(text: str) -> list[RawRef]:
    refs: list[RawRef] = []
    covered: list[tuple[int, int]] = []

    def is_covered(i: int) -> bool:
        return any(s <= i < e for s, e in covered)

    # 1) 区间优先（最长匹配）
    for m in RANGE_RE.finditer(text):
        if is_covered(m.start()):
            continue
        a, b = parse_path(m.group("a")), parse_path(m.group("b"))
        if a is None or b is None or len(a) != len(b) or a > b:
            # 形状像区间但编号层级不一致/倒置：整条无法解析
            refs.append(RawRef("unparsable", m.start(), m.end(), m.group(0)))
            covered.append((m.start(), m.end()))
            continue
        refs.append(RawRef("range", m.start(), m.end(), m.group(0), a, b))
        covered.append((m.start(), m.end()))

    # 2) 单条
    for m in SINGLE_RE.finditer(text):
        if is_covered(m.start()):
            continue
        path = parse_path(m.group("n"))
        if path is None:
            refs.append(RawRef("unparsable", m.start(), m.end(), m.group(0)))
        else:
            refs.append(RawRef("single", m.start(), m.end(), m.group(0), path))
        covered.append((m.start(), m.end()))

    # 3) 泛指词标记为已消费（合法引用，不报警）
    for word in sorted(RELATIVE_TOKENS, key=len, reverse=True):
        start = 0
        while True:
            i = text.find(word, start)
            if i < 0:
                break
            if not is_covered(i):
                covered.append((i, i + len(word)))
            start = i + len(word)

    # 4) 剩余「第…条」可疑片段：无法解析
    for m in BROAD_RE.finditer(text):
        if is_covered(m.start()):
            continue
        refs.append(RawRef("unparsable", m.start(), m.end(), m.group(0)))
        covered.append((m.start(), m.end()))

    refs.sort(key=lambda r: r.start)
    return refs


# ---------------------------------------------------------------- 区间展开

def expand_range(a: tuple[int, ...], b: tuple[int, ...],
                 path_to_id: dict[tuple[int, ...], str]
                 ) -> tuple[list[str], list[tuple[int, ...]]]:
    """把区间展开成 (命中的条款 id, 未命中的路径)。

    顶层（深度 1）：按整数区间枚举，精确路径优先（第4条优先于第4.1条）；
    若精确路径不存在，则回退到该编号下「最浅一层」子条款（支持漏写父条款、
    直接引用 4.1～4.3 的合同写法）。
    多层：共享除最后一段外的前缀，末段在 [a, b] 间枚举；前缀不同则整体无效。
    """
    if len(a) != len(b) or a > b:
        return [], []
    found: list[str] = []
    missing: list[tuple[int, ...]] = []

    if len(a) == 1:
        for n in range(a[0], b[0] + 1):
            exact = (n,)
            if exact in path_to_id:
                found.append(path_to_id[exact])
                continue
            children = sorted(
                (p for p in path_to_id if len(p) == 2 and p[0] == n),
                key=lambda p: p[1],
            )
            if children:
                found.extend(path_to_id[p] for p in children)
            else:
                missing.append(exact)
        return found, missing

    if a[:-1] != b[:-1]:
        return [], [a, b]
    for tail in range(a[-1], b[-1] + 1):
        p = a[:-1] + (tail,)
        cid = path_to_id.get(p)
        if cid is not None:
            found.append(cid)
        else:
            missing.append(p)
    return found, missing


# ---------------------------------------------------------------- 父子树

@dataclass
class _Node:
    """路径前缀节点；clause_ids 为空表示原文未单列该条款（虚父节点）。"""
    path: tuple[int, ...]
    children: dict[tuple[int, ...], "_Node"] = field(default_factory=dict)
    clause_ids: list[str] = field(default_factory=list)


def _build_tree(clauses: list[Clause]) -> _Node:
    root = _Node(())
    node_at: dict[tuple[int, ...], _Node] = {(): root}
    for c in clauses:
        node = root
        for depth in range(1, len(c.path) + 1):
            pref = c.path[:depth]
            nxt = node_at.get(pref)
            if nxt is None:
                nxt = _Node(pref)
                node_at[pref] = nxt
            node.children.setdefault(pref, nxt)
            node = nxt
        node.clause_ids.append(c.id)
    return root


def _is_descendant(path: tuple[int, ...], ancestor: tuple[int, ...]) -> bool:
    return path[:len(ancestor)] == ancestor


def cascade_deleted(clauses: list[Clause], deleted: list[str]) -> set[str]:
    """删除父条款时级联删除整棵子树（路径前缀匹配）。"""
    by_id = {c.id: c for c in clauses}
    deleted_paths = [by_id[cid].path for cid in deleted if cid in by_id]
    out: set[str] = set()
    for c in clauses:
        if any(_is_descendant(c.path, p) for p in deleted_paths):
            out.add(c.id)
    return out


def _canonical_order(
    clauses: list[Clause],
    order: list[str],
    deleted_set: set[str],
) -> tuple[_Node, list[str], dict[tuple[int, ...], list[_Node]]]:
    """把请求的扁平顺序规整为合法的树先序序列。

    只承认同一父级下兄弟之间的换序：每个存活节点按其子树在 order 中最早
    出现的位置排序（未出现在 order 中的条款回退到解析顺序），子树整体
    连续输出。跨父级拖动等不合法排列会在此被自动归正。
    返回 (树根, 存活条款 id 的先序列表, 每个节点的存活子节点新顺序)。
    """
    root = _build_tree(clauses)
    requested_rank = {cid: i for i, cid in enumerate(order)}
    doc_index = {c.id: i for i, c in enumerate(clauses)}
    fallback_base = len(order) + len(clauses) + 1

    def alive(node: _Node) -> bool:
        if any(i not in deleted_set for i in node.clause_ids):
            return True
        return any(alive(ch) for ch in node.children.values())

    def first_doc(node: _Node) -> int:
        best = min((doc_index[i] for i in node.clause_ids), default=10 ** 9)
        for ch in node.children.values():
            best = min(best, first_doc(ch))
        return best

    def subtree_rank(node: _Node) -> int:
        # 合法先序中，子树根（节点自身条款；虚节点则为最靠前的存活子树根）
        # 一定是整棵子树在 order 中出现的第一个位置，用它决定同级排序。
        own = [requested_rank.get(i, fallback_base + doc_index[i])
               for i in node.clause_ids if i not in deleted_set]
        if own:
            return min(own)
        return min((subtree_rank(ch) for ch in node.children.values()
                    if alive(ch)), default=10 ** 9)

    child_order: dict[tuple[int, ...], list[_Node]] = {}

    def compute(node: _Node) -> None:
        kids = [ch for ch in node.children.values() if alive(ch)]
        kids.sort(key=lambda n: (subtree_rank(n), first_doc(n)))
        child_order[node.path] = kids
        for ch in kids:
            compute(ch)

    compute(root)

    flat: list[str] = []

    def emit(node: _Node) -> None:
        for cid in node.clause_ids:
            if cid not in deleted_set:
                flat.append(cid)
        for ch in child_order[node.path]:
            emit(ch)

    emit(root)
    return root, flat, child_order


def _renumber(root: _Node,
              child_order: dict[tuple[int, ...], list[_Node]]
              ) -> dict[str, tuple[int, ...]]:
    """各层兄弟分别从 1 连续编号，返回 条款 id -> 完整新路径。"""
    new_path: dict[str, tuple[int, ...]] = {}

    def walk(node: _Node, prefix: tuple[int, ...]) -> None:
        for i, ch in enumerate(child_order.get(node.path, []), start=1):
            p = prefix + (i,)
            for cid in ch.clause_ids:
                new_path[cid] = p
            walk(ch, p)

    walk(root, ())
    return new_path


# ---------------------------------------------------------------- 最短表达

def shortest_expr(ids_in_order: list[str],
                  id_to_path: dict[str, tuple[int, ...]],
                  id_to_pos: dict[str, int]) -> str:
    """按新顺序把存活目标压成最短文本。

    仅当若干目标「同属一个父级且末段编号连续」时合并为区间
    「第x…至第y…」（两端都写完整路径）；否则按新顺序逐个枚举，
    单条即其完整新路径。
    """
    ordered = sorted(set(ids_in_order), key=lambda cid: id_to_pos[cid])
    if not ordered:
        return ""

    runs: list[list[str]] = []
    cur = [ordered[0]]
    for cid in ordered[1:]:
        p, q = id_to_path[cur[-1]], id_to_path[cid]
        same_parent = len(p) == len(q) and p[:-1] == q[:-1]
        consecutive = same_parent and q[-1] == p[-1] + 1
        if consecutive:
            cur.append(cid)
        else:
            runs.append(cur)
            cur = [cid]
    runs.append(cur)

    parts = []
    for run in runs:
        if len(run) == 1:
            parts.append(path_label(id_to_path[run[0]]))
        else:
            parts.append(
                f"{path_label(id_to_path[run[0]])}至{path_label(id_to_path[run[-1]])}"
            )
    if len(parts) == 1:
        return parts[0]
    return "、".join(parts)


# ---------------------------------------------------------------- 主入口

@dataclass
class Issue:
    type: str           # unparsable | deleted | circular
    clause_id: str
    ref_text: str
    message: str
    target_id: str | None = None


@dataclass
class RefMark:
    start: int
    end: int
    text: str
    new_text: str
    status: str         # ok | unparsable | deleted | circular
    target_id: str | None = None


@dataclass
class ClauseOut:
    id: str
    label: str
    heading: str
    before_text: str    # 原文（不含编号标记）
    after_text: str     # 改写后正文（不含新编号）
    new_label: str
    new_position: int
    depth: int
    path: tuple[int, ...]
    refs: list[RefMark] = field(default_factory=list)


def analyze(clauses: list[Clause], order: list[str],
            deleted: list[str]) -> dict:
    by_id = {c.id: c for c in clauses}

    # 路径 -> id（重复路径取第一条，与绑定语义一致）
    path_to_id: dict[tuple[int, ...], str] = {}
    for c in clauses:
        path_to_id.setdefault(c.path, c.id)

    # 删除父条款 → 级联删除整棵子树，再把请求顺序规整为合法树先序
    deleted_set = cascade_deleted(clauses, deleted)
    root, survivors, child_order = _canonical_order(clauses, order, deleted_set)
    new_path = _renumber(root, child_order)
    # 存活条款在新文档中的先序位置（1 起）
    new_pos = {cid: i + 1 for i, cid in enumerate(survivors)}

    # ---- 扫描每条正文 ----
    raw_by_clause: dict[str, list[RawRef]] = {
        c.id: _scan(c.text) for c in clauses
    }

    # ---- 引用图（边 = 可解析且目标存活的引用）----
    edges: dict[str, set[str]] = {c.id: set() for c in clauses}

    def resolve_single(path: tuple[int, ...]) -> str | None:
        return path_to_id.get(path)

    for cid, raws in raw_by_clause.items():
        if cid in deleted_set:
            continue
        for r in raws:
            if r.kind == "single":
                tid = resolve_single(r.a)  # type: ignore[arg-type]
                if tid and tid not in deleted_set:
                    edges[cid].add(tid)
            elif r.kind == "range":
                ids, _ = expand_range(r.a, r.b, path_to_id)
                for tid in ids:
                    # 区间包含自身条款（如「第4.1条至第4.2条」出现在 4.1）
                    # 不构成循环边；显式单条自引用才计入
                    if tid != cid and tid not in deleted_set:
                        edges[cid].add(tid)

    # ---- 循环判定 ----
    # on_cycle: 自身处在某个环上；边 (cid -> tid) 属于环 ⇔
    # cid 在环上且 tid 能沿引用边回到 cid
    def reaches(src: str, dst: str) -> bool:
        stack, seen = list(edges[src]), {src}
        while stack:
            cur = stack.pop()
            if cur == dst:
                return True
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(edges[cur])
        return False

    on_cycle = {cid for cid in survivors if reaches(cid, cid)}

    def ref_targets(r: RawRef) -> list[str]:
        if r.kind == "single":
            tid = resolve_single(r.a)  # type: ignore[arg-type]
            return [tid] if tid else []
        if r.kind == "range":
            ids, _ = expand_range(r.a, r.b, path_to_id)
            return ids
        return []

    circular_refs: set[tuple[str, int]] = set()
    for cid in on_cycle:
        for i, r in enumerate(raw_by_clause[cid]):
            if r.kind == "unparsable":
                continue
            if any(t in on_cycle and t not in deleted_set
                   and reaches(t, cid) for t in ref_targets(r)):
                circular_refs.add((cid, i))

    # ---- 逐 token 改写 ----
    issues: list[Issue] = []
    out_by_id: dict[str, ClauseOut] = {}

    for c in clauses:
        marks: list[RefMark] = []
        pieces: list[str] = []
        cursor = 0

        for i, r in enumerate(raw_by_clause[c.id]):
            pieces.append(c.text[cursor:r.start])

            if r.kind == "unparsable" or (c.id, i) in circular_refs:
                if r.kind == "unparsable":
                    status, new_text = "unparsable", r.text
                    msg = f"无法解析的引用「{r.text}」"
                    tid = None
                else:
                    status, new_text = "circular", r.text
                    msg = f"循环引用：{c.label} 通过「{r.text}」回到自身链条"
                    tid = None
                marks.append(RefMark(r.start, r.end, r.text, new_text,
                                     status, tid))
                pieces.append(new_text)
                if c.id not in deleted_set:
                    issues.append(Issue(status, c.id, r.text, msg))
                cursor = r.end
                continue

            if r.kind == "single":
                tid = resolve_single(r.a)  # type: ignore[arg-type]
                if tid is None:
                    status = "unparsable"  # 编号根本不存在，归入无法定位
                    msg = f"引用目标不存在：{path_label(r.a)}"
                    marks.append(RefMark(r.start, r.end, r.text, r.text,
                                         status, None))
                    if c.id not in deleted_set:
                        issues.append(Issue("unparsable", c.id, r.text, msg))
                    pieces.append(r.text)
                elif tid in deleted_set:
                    target_label = by_id[tid].label
                    marks.append(RefMark(r.start, r.end, r.text, r.text,
                                         "deleted", tid))
                    if c.id not in deleted_set:
                        issues.append(Issue(
                            "deleted", c.id, r.text,
                            f"引用目标已删除：{target_label}（{by_id[tid].heading or '该条款'}）",
                            tid))
                    pieces.append(r.text)
                else:
                    # 单条引用 → 完整新路径
                    new_text = path_label(new_path[tid])
                    marks.append(RefMark(r.start, r.end, r.text, new_text,
                                         "ok", tid))
                    pieces.append(new_text)
                cursor = r.end
                continue

            # range
            ids, missing = expand_range(r.a, r.b, path_to_id)  # type: ignore
            deleted_hits = [t for t in ids if t in deleted_set]
            alive_ids = [t for t in ids if t not in deleted_set]

            if not alive_ids:
                # 全部失效
                if ids and not missing:
                    labels = "、".join(by_id[t].label for t in deleted_hits)
                    marks.append(RefMark(r.start, r.end, r.text, r.text,
                                         "deleted", deleted_hits[0]))
                    if c.id not in deleted_set:
                        issues.append(Issue(
                            "deleted", c.id, r.text,
                            f"区间引用的目标已全部删除：{labels}",
                            deleted_hits[0]))
                else:
                    miss_labels = "、".join(path_label(p) for p in missing)
                    marks.append(RefMark(r.start, r.end, r.text, r.text,
                                         "unparsable", None))
                    if c.id not in deleted_set:
                        issues.append(Issue(
                            "unparsable", c.id, r.text,
                            f"区间引用目标不存在：{miss_labels or r.text}"))
                pieces.append(r.text)
            else:
                new_text = shortest_expr(alive_ids, new_path, new_pos)
                # 区间有缺口时给出提示，但仍生成可替换文本
                if deleted_hits or missing:
                    gone = ([by_id[t].label for t in deleted_hits]
                            + [path_label(p) for p in missing])
                    marks.append(RefMark(
                        r.start, r.end, r.text, new_text, "deleted",
                        next((t for t in ids if t in deleted_set), None)))
                    if c.id not in deleted_set:
                        issues.append(Issue(
                            "deleted", c.id, r.text,
                            f"区间「{r.text}」中以下目标已失效（"
                            f"{'、'.join(gone)}），已按剩余条款重写为：{new_text}",
                            marks[-1].target_id))
                else:
                    marks.append(RefMark(r.start, r.end, r.text, new_text,
                                         "ok", alive_ids[0]))
                pieces.append(new_text)
            cursor = r.end

        pieces.append(c.text[cursor:])
        after_text = "".join(pieces)

        np = new_path.get(c.id)
        out_by_id[c.id] = ClauseOut(
            id=c.id, label=c.label, heading=c.heading,
            before_text=c.text, after_text=after_text,
            new_label=path_label(np) if np is not None else "",
            new_position=new_pos.get(c.id, 0),
            depth=len(np) if np is not None else len(c.path),
            path=np if np is not None else c.path,
            refs=marks,
        )

    # 响应按原解析顺序 + 新顺序两个视角给出
    original_view = [_serialize(out_by_id[c.id]) for c in clauses]
    ordered_view = [_serialize(out_by_id[cid]) for cid in survivors]
    deleted_view = [
        {"id": c.id, "label": c.label, "heading": c.heading}
        for c in clauses if c.id in deleted_set
    ]

    counts = {"unparsable": 0, "deleted": 0, "circular": 0}
    for it in issues:
        counts[it.type] += 1

    return {
        "clauses": original_view,
        "order": survivors,
        "deleted_clauses": deleted_view,
        "issues": [it.__dict__ for it in issues],
        "counts": counts,
        "total_refs": sum(len(v) for v in raw_by_clause.values()),
        "result": ordered_view,
    }


def _serialize(o: ClauseOut) -> dict:
    return {
        "id": o.id,
        "label": o.label,
        "heading": o.heading,
        "before_text": o.before_text,
        "after_text": o.after_text,
        "new_label": o.new_label,
        "new_position": o.new_position,
        "depth": o.depth,
        "path": list(o.path),
        "refs": [
            {
                "start": m.start, "end": m.end, "text": m.text,
                "new_text": m.new_text, "status": m.status,
                "target_id": m.target_id,
            } for m in o.refs
        ],
    }
