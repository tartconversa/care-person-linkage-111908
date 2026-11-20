"""身份图解析：只把确认级 same 证据连成人员。

含互斥证明的组合（same 链与 different 证明相矛盾）不按记录先后取胜：
矛盾路径上的 same 边标记为待裁定（contested），发布后的人员只由无争议边
连接。解析是证据集合的纯函数，与证据到达顺序无关，重放结果一致。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .periods import intersect

# 简单路径枚举上限；超过则保守地把整个连通分量的边都标记为待裁定。
_PATH_CAP = 256


@dataclass(frozen=True)
class Edge:
    edge_id: str
    relation: str  # same | different
    left: str
    right: str
    period: tuple  # (from_date|None, until_date|None)
    origin: str    # proof | confirmation | ruling


def person_id_for(members):
    """人员引用由成员实例集合决定：成员不变则引用稳定，可核对、可重放。"""
    return 'P-' + hashlib.sha256('|'.join(sorted(members)).encode('utf-8')).hexdigest()[:12]


class _UF:
    def __init__(self, items):
        self.p = {i: i for i in items}

    def find(self, x):
        root = x
        while self.p[root] != root:
            root = self.p[root]
        while self.p[x] != root:
            self.p[x], x = root, self.p[x]
        return root

    def union(self, a, b):
        self.p[self.find(a)] = self.find(b)


def _components(nodes, edges):
    uf = _UF(nodes)
    for e in edges:
        uf.union(e.left, e.right)
    groups = {}
    for n in nodes:
        groups.setdefault(uf.find(n), set()).add(n)
    return [frozenset(g) for g in groups.values()]


def _path_edges(adj, source, target):
    """source→target 所有简单路径经过的边 id 集合；路径过多返回 None。"""
    found = set()
    count = [0]

    def dfs(node, seen, acc):
        if count[0] >= _PATH_CAP:
            return
        if node == target:
            count[0] += 1
            found.update(acc)
            return
        for nxt, eid in adj.get(node, ()):
            if nxt not in seen:
                dfs(nxt, seen | {nxt}, acc + [eid])

    dfs(source, {source}, [])
    return None if count[0] >= _PATH_CAP else found


def resolve(identities, same_edges, diff_edges):
    """解析人员连通分量。

    identities: {identity_id: (from_date, until_date)}。
    返回 dict：
      published    发布用连通分量（无争议边连接）
      full         全部生效 same 边的连通分量（仅用于候选解释）
      contested    待裁定边 id 列表
      conflicts    互斥组合列表（left/right/different_edge/same_path_edges）
      coverage     每个实例被无争议 same 边覆盖的时段分片
      inapplicable 适用时段与实例有效期不相交、未生效的证据边
    """
    nodes = sorted(identities)

    def segment(e):
        seg = e.period
        for inst in (e.left, e.right):
            if inst not in identities:
                return None
            seg = intersect(seg, identities[inst])
            if seg is None:
                return None
        return seg

    same_app, diff_app, inapplicable = [], [], []
    for e in same_edges:
        s = segment(e)
        (same_app.append((e, s)) if s else inapplicable.append(e))
    for e in diff_edges:
        s = segment(e)
        (diff_app.append((e, s)) if s else inapplicable.append(e))

    full = _components(nodes, [e for e, _ in same_app])
    comp_of = {}
    for comp in full:
        for n in comp:
            comp_of[n] = comp

    adj = {n: [] for n in nodes}
    for e, _ in same_app:
        adj[e.left].append((e.right, e.edge_id))
        adj[e.right].append((e.left, e.edge_id))

    conflicts, contested = [], set()
    for e, _ in diff_app:
        if comp_of[e.left] != comp_of[e.right]:
            continue
        pe = _path_edges(adj, e.left, e.right)
        if pe is None:  # 图过大：保守处理，整个分量的边都待裁定
            comp = comp_of[e.left]
            pe = {ed.edge_id for ed, _ in same_app if ed.left in comp and ed.right in comp}
        contested |= pe
        conflicts.append({
            'left': e.left,
            'right': e.right,
            'different_edge': e.edge_id,
            'same_path_edges': sorted(pe),
        })

    published_edges = [(e, s) for e, s in same_app if e.edge_id not in contested]
    published = _components(nodes, [e for e, _ in published_edges])
    coverage = {n: [] for n in nodes}
    for e, s in published_edges:
        coverage[e.left].append(s)
        coverage[e.right].append(s)

    return {
        'published': published,
        'full': full,
        'contested': sorted(contested),
        'conflicts': conflicts,
        'coverage': coverage,
        'inapplicable': inapplicable,
    }
