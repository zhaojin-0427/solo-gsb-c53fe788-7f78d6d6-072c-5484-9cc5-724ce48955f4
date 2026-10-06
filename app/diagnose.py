"""现场电流偏差定位。

对一条**已保存的历史试算记录**，从其保存的组参数与基线接线重新构建
分段串曲线（绝不使用结果快照中那套稀疏采样做插值），在用户提交的实测
端电压测点上求理论电流，并比较两类假设：

1. 健康状态：全部组参数与记录一致；
2. 单组 Isc 退化：恰有一组短路电流降为某个**小于原 Isc 的正数、至多
   两位小数**的值（Voc 不变，接线不变）。

每个测点都满足 |实测电流 − 理论电流| ≤ 容差，该假设才算“可解释”。

拐点处由 ``_segment_at`` 精确选段（分段连续，不会因选段产生跳变）；
端电压达到某串 Voc 的支路电流为 0、对该串内各组的退化不敏感，
这些事实由 ``string_current`` / ``total_current`` 精确处理。

性能：对某组而言，固定测点电压下总理论电流随退化候选 Isc 单调不下降
（组被旁路或其串已达 Voc 时呈平台），故各测点的可行候选构成一条连续
0.01 网格带。实现逐测点对**单调谓词**做“两端搜索”（首个达下界、末个
不超上界），再取各测点可行带的交集；不做逐档曲线重建与逐点枚举，
也不能对“全部点合格”这一整体谓词直接布尔二分——单调性只保证可行集是
一条（可能为空的）离散带，整体谓词在带两侧均为假，不满足二分条件。
"""

import math
from typing import List, Optional, Sequence

from .model import (
    Group, MERGE_EPS, StringCurve, string_current, string_curve, total_current,
)

# 理论电流与实测电流比较时的浮点容差（输入量化到 0.01，远小于物理量纲）
_EPS = 1e-9
# 单组 Isc 退化假设的量化步长：至多两位小数
GRID = 0.01


class DiagnoseError(ValueError):
    """诊断入参非法（调用方映射为 HTTP 400）。"""


def _finite_nonneg(raw, field: str, *, allow_zero: bool = True) -> float:
    """解析有限非负数。

    接受 int/float（拒绝 bool）或可被 float() 解析为有限数的字符串
    （允许 "1.5"、"1e3" 等）；NaN / Infinity / None / 非数值文本一律拒绝。
    """
    if isinstance(raw, bool):
        raise DiagnoseError(f"{field} 必须是有限数值")
    if isinstance(raw, int):
        value = float(raw)
    elif isinstance(raw, float):
        value = raw
    elif isinstance(raw, str):
        text = raw.strip()
        try:
            value = float(text)
        except ValueError:
            raise DiagnoseError(f"{field} 必须是有限数值（收到 {raw!r}）")
    else:
        raise DiagnoseError(f"{field} 必须是有限数值")
    if not math.isfinite(value):
        raise DiagnoseError(f"{field} 必须是有限数值")
    if value < 0.0 or (not allow_zero and value <= 0.0):
        raise DiagnoseError(f"{field} 必须为非负数")
    return value


def validate_diagnose_request(payload: dict, baseline_voc: float) -> List[tuple]:
    """校验诊断请求，返回 [(电压 V, 电流 I), ...]（电压两两互异）。

    规则：
    - points 至少 3 个；每个点含 v、i，均为有限非负数；
    - 电压可为零，但不得超过该记录基线开路电压 baseline_voc；
    - 电压必须两两互异；
    - tolerance 为有限非负数（可为零）。
    """
    if not isinstance(payload, dict):
        raise DiagnoseError("请求体必须是 JSON 对象")

    if "tolerance" not in payload:
        raise DiagnoseError("缺少电流容差 tolerance")
    tol = _finite_nonneg(payload.get("tolerance"), "电流容差")

    raw_points = payload.get("points")
    if not isinstance(raw_points, list):
        raise DiagnoseError("points 必须为数组")
    if len(raw_points) < 3:
        raise DiagnoseError("至少提交 3 个实测 (V,I) 测点")

    points: List[tuple] = []
    seen_v: set = set()
    for idx, p in enumerate(raw_points):
        if not isinstance(p, dict):
            raise DiagnoseError(f"第 {idx + 1} 个测点格式错误")
        v = _finite_nonneg(p.get("v"), f"第 {idx + 1} 个测点的电压")
        i = _finite_nonneg(p.get("i"), f"第 {idx + 1} 个测点的电流")
        if v > baseline_voc + 1e-7:
            raise DiagnoseError(
                f"第 {idx + 1} 个测点电压 {_num(v)} V 超过该记录基线开路电压 "
                f"{_num(baseline_voc)} V"
            )
        # 电压按原始数值去重（浮点输入，精确比较；字符串经 float 归一化）
        v_key = round(v, 12)
        if v_key in seen_v:
            raise DiagnoseError(f"第 {idx + 1} 个测点电压与前点重复，电压必须互异")
        seen_v.add(v_key)
        points.append((v, i))

    payload["tolerance"] = tol  # 回写归一化值，供主流程使用
    return points


def _num(x: float) -> str:
    """去掉无意义尾零的紧凑数值文本。"""
    return f"{x:.10f}".rstrip("0").rstrip(".")


def _grid_count(isc0: float) -> int:
    """小于 isc0 的正两位小数候选个数：候选为 k·0.01, k=1..n（n 即返回值）。"""
    n = int(math.floor(isc0 / GRID + 1e-9))
    if n * GRID >= isc0 - 1e-9:
        n -= 1  # 等档（原 Isc 本身）不属于退化假设
    return max(0, n)


def _within(theory: float, measured: float, tol: float) -> bool:
    return abs(theory - measured) <= tol + _EPS


class _DegradedStringProbe:
    """某组退化候选 Isc 下的逐测点理论总电流求值器。

    退化只发生在该组所在的一条串上：其余串曲线与各测点的贡献在整组
    扫描中恒定，预算一次；退化串的分段曲线按 0.01 网格下标懒构建并缓存
    （同一网格串曲线只重建一次，供该组全部测点的两端搜索复用）。

    ``total(k)`` 在固定测点电压下对 k（退化 Isc 网格下标）单调不下降，
    平台对应：退化组在该电压被旁路、或其所在串已达到串 Voc（支路恒为
    零电流，此时退化不可察觉，实测仍须与该零电流+其余串贡献核对）。
    """

    __slots__ = ("members", "pos", "other_points", "string_voc", "_cache")

    def __init__(self, base_members: Sequence[Group], pos: int,
                 other_curves: Sequence[StringCurve],
                 points: Sequence[tuple]):
        self.members = list(base_members)
        self.pos = pos
        # 其余串在每个测点电压上的电流之和（与退化候选无关）
        self.other_points = [total_current(other_curves, v)
                             for v, _ in points]
        self.string_voc = sum(g.voc for g in self.members)
        self._cache: dict[int, StringCurve] = {}

    def _curve(self, k: int) -> StringCurve:
        curve = self._cache.get(k)
        if curve is None:
            members = self.members
            degraded = Group(members[self.pos].id,
                             members[self.pos].voc, k * GRID)
            new_members = [
                degraded if j == self.pos else g
                for j, g in enumerate(members)
            ]
            curve = string_curve(new_members)
            self._cache[k] = curve
        return curve

    def total(self, k: int, point_idx: int, v: float) -> float:
        # 端电压达到（或超过）该串开路电压：支路电流恒为 0，
        # 与退化候选无关——仍按其余串贡献与实测逐点核对。
        # 判定与 string_current 的开路选段（_segment_at 返回 None 的
        # v > voc + MERGE_EPS 及 v 恰等于 voc 时线性段给出 0）保持一致。
        if v >= self.string_voc - MERGE_EPS:
            return self.other_points[point_idx]
        return self.other_points[point_idx] + string_current(self._curve(k), v)


def _first_ge(probe: _DegradedStringProbe, pidx: int, v: float,
              threshold: float, n: int) -> Optional[int]:
    """最小网格下标 k∈[1,n] 使 total(k) ≥ threshold；不存在返回 None。

    total(k) 对 k 单调不下降（含平台），故用“找下界”二分而非线性扫描。
    """
    lo, hi = 1, n + 1  # hi 为越界哨兵：n+1 表示不存在
    while lo < hi:
        mid = (lo + hi) >> 1
        if probe.total(mid, pidx, v) >= threshold - _EPS:
            hi = mid
        else:
            lo = mid + 1
    return lo if lo <= n else None


def _last_le(probe: _DegradedStringProbe, pidx: int, v: float,
             threshold: float, n: int) -> Optional[int]:
    """最大网格下标 k∈[1,n] 使 total(k) ≤ threshold；不存在返回 None。

    total(k) 对 k 单调不下降（含平台），故用“找上界”二分而非线性扫描。
    """
    lo, hi = 0, n  # lo=0 为越界哨兵：0 表示不存在
    while lo < hi:
        mid = (lo + hi + 1) >> 1
        if probe.total(mid, pidx, v) <= threshold + _EPS:
            lo = mid
        else:
            hi = mid - 1
    return lo if lo >= 1 else None


def _feasible_band(probe: _DegradedStringProbe,
                   points: Sequence[tuple], tol: float,
                   n: int) -> Optional[tuple]:
    """求该组在全部测点上均合格的连续网格下标带 [lo, hi]；不可行返回 None。

    单测点可行条件 ``|total(k) − I| ≤ tol`` 在单调（含平台）的 total(k)
    上等价于 ``I−tol ≤ total(k) ≤ I+tol``，可行下标本身也是一条连续带
    （两端由单调谓词二分定位）；全部测点取交集即整体可行带。
    不对“全部点合格”做布尔二分：整体谓词仅在该带内为真、带外两侧皆假，
    不是单调布尔谓词。
    """
    band_lo, band_hi = 1, n
    for pidx, (v, i_meas) in enumerate(points):
        lo = _first_ge(probe, pidx, v, i_meas - tol, n)
        if lo is None:
            return None
        hi = _last_le(probe, pidx, v, i_meas + tol, n)
        if hi is None or hi < lo:
            return None
        if lo > band_lo:
            band_lo = lo
        if hi < band_hi:
            band_hi = hi
        if band_hi < band_lo:
            return None
    return band_lo, band_hi


def diagnose(groups: Sequence[Group], baseline: Sequence[Sequence[str]],
             points: Sequence[tuple], tol: float) -> dict:
    """执行现场偏差定位。

    返回结构见模块开头说明：含健康假设逐点偏差、单组退化可行候选的
    Isc 网格区间，以及最终中文结论。
    """
    by_id = {g.id: g for g in groups}
    # 基线串曲线：组内顺序无关，按编号排序以与试算主流程保持一致
    base_strings = [
        [by_id[gid] for gid in sorted(s, key=lambda g: by_id[g].id)]
        for s in baseline
    ]
    base_curves = [string_curve(s) for s in base_strings]
    baseline_voc = max(c.voc for c in base_curves)

    # 组 -> 所在 (串索引, 串内位置)，退化时只重建这一条串
    string_of_group = {}
    for s_idx, string in enumerate(base_strings):
        for pos, g in enumerate(string):
            string_of_group[g.id] = (s_idx, pos)

    # ---- 健康假设：全部测点理论电流与逐点偏差 ----
    healthy_points = []
    healthy_ok = True
    for v, i_meas in points:
        i_th = total_current(base_curves, v)
        dev = i_meas - i_th
        fit = _within(i_th, i_meas, tol)
        healthy_ok = healthy_ok and fit
        healthy_points.append({
            "v": v, "i_measured": i_meas,
            "i_theory_healthy": _r(i_th), "deviation": _r(dev),
            "within_tolerance": fit,
        })

    # ---- 单组 Isc 退化假设：逐测点两端搜索，求连续可行 0.01 网格带 ----
    group_results = []
    for g in groups:
        s_idx, pos = string_of_group[g.id]
        others = [c for j, c in enumerate(base_curves) if j != s_idx]
        # 该组所在串“端电压严格小于其 Voc、仍贡献非零电流”的测点数量
        # （仅用于结果展示；V 恰等于 Voc 时该串恒为零，对组退化不敏感）。
        # 注意：不在此处跳过这些测点——开路支路对总电流贡献 0，
        # 该组退化不会改变总电流，若实测偏离健康值则可行带自然为空，
        # 不能因“无约束”而真空判为可行。
        sensitive_points = sum(
            1 for v, _ in points if v < base_curves[s_idx].voc - MERGE_EPS)
        entry = {
            "group": g.id,
            "isc_original": _r(g.isc),
            "sensitive_points": sensitive_points,
            "feasible": False,
        }
        n = _grid_count(g.isc)
        if n > 0:
            probe = _DegradedStringProbe(base_strings[s_idx], pos,
                                         others, points)
            band = _feasible_band(probe, points, tol, n)
            if band is not None:
                lo, hi = band
                entry["feasible"] = True
                entry["isc_min"] = _r2(lo * GRID)
                entry["isc_max"] = _r2(hi * GRID)
                entry["isc_candidates"] = hi - lo + 1
        group_results.append(entry)

    explainable = [e for e in group_results if e["feasible"]]

    # ---- 结论判定 ----
    if healthy_ok:
        verdict = "unconfirmed"
        message = ("健康状态即可解释全部测点偏差，无法确认异常"
                   + ("；同时存在可解释的单组退化假设，但不足以判定为故障。"
                      if explainable else "。"))
    elif len(explainable) == 0:
        verdict = "out_of_model"
        message = ("全部健康与单组 Isc 退化假设均无法解释测点偏差："
                   "模型外偏差（可能存在多组退化、Voc 漂移或接线改变）。")
    elif len(explainable) == 1:
        e = explainable[0]
        verdict = "single_group"
        if e["isc_min"] == e["isc_max"]:
            rng = f"Isc = {e['isc_min']}"
        else:
            rng = f"{e['isc_min']} ≤ Isc ≤ {e['isc_max']}"
        message = (f"定位到单组退化：组 {e['group']} 的短路电流可解释全部"
                   f"测点偏差，可行 Isc 范围 {rng} A（共 "
                   f"{e['isc_candidates']} 个 0.01 网格取值，"
                   f"原 Isc {e['isc_original']} A）。")
    else:
        verdict = "ambiguous"
        names = "、".join(
            f"组 {e['group']}（{e['isc_min']}～{e['isc_max']} A）"
            if e["isc_min"] != e["isc_max"]
            else f"组 {e['group']}（{e['isc_min']} A）"
            for e in explainable
        )
        message = f"存在多个可解释的单组退化假设，组间歧义：{names}。"

    return {
        "baseline_voc": _r(baseline_voc),
        "tolerance": tol,
        "points": healthy_points,
        "healthy_explainable": healthy_ok,
        "verdict": verdict,
        "message": message,
        "groups": group_results,
        "explainable_groups": [e["group"] for e in explainable],
    }


def _r(x: float) -> float:
    """理论电流/偏差保留 6 位小数。"""
    v = round(float(x), 6)
    return 0.0 if v == 0 else v


def _r2(x: float) -> float:
    """退化 Isc 网格值保留 2 位小数。"""
    return round(float(x) + 0.0, 2)
