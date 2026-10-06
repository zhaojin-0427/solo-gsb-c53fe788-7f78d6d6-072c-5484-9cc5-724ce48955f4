"""随机差分测试：新实现（两端搜索）与逐档枚举的参考实现逐字段比对。

覆盖：3~9 组、1~3 串、重复/相异 Voc/Isc、测点含零电压与串 Voc、
零容差、拐点电压、宽容差等。
"""
import math
import random
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.diagnose import (
    GRID, _grid_count, _within, diagnose as diagnose_fast,
)
from app.model import Group, MERGE_EPS, string_curve, total_current


def diagnose_reference(groups, baseline, points, tol):
    """题目所述原始逐档重建 + 逐点枚举参考实现。"""
    by_id = {g.id: g for g in groups}
    base_strings = [
        [by_id[gid] for gid in sorted(s, key=lambda g: by_id[g].id)]
        for s in baseline
    ]
    base_curves = [string_curve(s) for s in base_strings]
    baseline_voc = max(c.voc for c in base_curves)
    string_of_group = {}
    for s_idx, string in enumerate(base_strings):
        for pos, g in enumerate(string):
            string_of_group[g.id] = (s_idx, pos)

    healthy_points, healthy_ok = [], True
    for v, i_meas in points:
        i_th = total_current(base_curves, v)
        fit = _within(i_th, i_meas, tol)
        healthy_ok = healthy_ok and fit
        healthy_points.append(round6(i_th, i_meas, fit, v))

    group_results = []
    for g in groups:
        s_idx, pos = string_of_group[g.id]
        others = [c for j, c in enumerate(base_curves) if j != s_idx]
        sensitive_points = sum(
            1 for v, _ in points if v < base_curves[s_idx].voc - MERGE_EPS)
        feasible = []
        for k in range(1, _grid_count(g.isc) + 1):
            isc_new = k * GRID
            degraded = Group(g.id, g.voc, isc_new)
            members = [
                degraded if j == pos else mg
                for j, mg in enumerate(base_strings[s_idx])
            ]
            curve_new = string_curve(members)
            ok = True
            for v, i_meas in points:
                i_th = total_current(others + [curve_new], v)
                if not _within(i_th, i_meas, tol):
                    ok = False
                    break
            if ok:
                feasible.append(isc_new)
        entry = {
            "group": g.id,
            "isc_original": _r(g.isc),
            "sensitive_points": sensitive_points,
            "feasible": bool(feasible),
        }
        if feasible:
            entry["isc_min"] = _r2(feasible[0])
            entry["isc_max"] = _r2(feasible[-1])
            entry["isc_candidates"] = len(feasible)
        group_results.append(entry)

    explainable = [e for e in group_results if e["feasible"]]
    if healthy_ok:
        verdict = "unconfirmed"
    elif not explainable:
        verdict = "out_of_model"
    elif len(explainable) == 1:
        verdict = "single_group"
    else:
        verdict = "ambiguous"

    return {
        "baseline_voc": _r(baseline_voc),
        "tolerance": tol,
        "points": healthy_points,
        "healthy_explainable": healthy_ok,
        "verdict": verdict,
        "groups": group_results,
        "explainable_groups": [e["group"] for e in explainable],
    }


def _r(x):
    v = round(float(x), 6)
    return 0.0 if v == 0 else v


def _r2(x):
    return round(float(x) + 0.0, 2)


def round6(i_th, i_meas, fit, v):
    dev = i_meas - i_th
    return {
        "v": v, "i_measured": i_meas,
        "i_theory_healthy": _r(i_th), "deviation": _r(dev),
        "within_tolerance": fit,
    }


def random_case(rng):
    n = rng.randint(3, 9)
    groups = []
    for k in range(n):
        # 刻意制造重复 Voc/Isc 与不同档位
        voc = rng.choice([5.0, 10.0, 12.5, 20.0, 33.3, 36.5])
        isc = rng.choice([1.0, 2.5, 5.0, 8.0, 8.2, 15.0, 20.0, 30.0])
        groups.append(Group(f"g{k}", voc, isc))
    # 1~3 串随机划分
    nstr = rng.randint(1, 3)
    labels = [rng.randrange(nstr) for _ in range(n)]
    # 保证每串非空
    missing = set(range(nstr)) - set(labels)
    for m in missing:
        labels[rng.randrange(n)] = m
    baseline = [[] for _ in range(nstr)]
    ids = [g.id for g in groups]
    rng.shuffle(ids)
    buckets = {i: [] for i in range(nstr)}
    for idx, lab in enumerate(labels):
        buckets[lab].append(ids[idx])
    baseline = [buckets[i] for i in range(nstr) if buckets[i]]

    curves = [string_curve([by_id(groups, gid) for gid in s])
              for s in [[x for x in b] for b in baseline]]
    voc_max = max(c.voc for c in curves)

    # 测点：3~12 个互异电压，含 0、可能含某些串 Voc/拐点，以及随机值
    m = rng.randint(3, 12)
    vs = {0.0}
    if rng.random() < 0.5:
        vs.add(round(voc_max, 6))
    for c in curves:
        if rng.random() < 0.5:
            vs.add(round(c.voc, 6))
        for knee in c.knees[1:-1]:
            if rng.random() < 0.3:
                vs.add(round(knee, 6))
    while len(vs) < m:
        vs.add(round(rng.uniform(0.0, voc_max), 3))
    vs = sorted(vs)[:m]

    # 实测电流：从某个“真相”生成（健康/单组退化/多组退化/随机）
    truth_groups = [Group(g.id, g.voc, g.isc) for g in groups]
    mode = rng.randrange(4)
    if mode in (1, 2):  # 退化 1~2 组
        degs = rng.sample(range(n), 1 if mode == 1 else 2)
        truth_groups = [
            Group(g.id, g.voc, rng.randint(1, max(1, int(g.isc / GRID) - 1)) * GRID)
            if j in degs else g for j, g in enumerate(truth_groups)
        ]
    tby = {g.id: g for g in truth_groups}
    tcurves = [string_curve([tby[gid] for gid in s]) for s in baseline]
    tol = rng.choice([0.0, 0.0, 0.01, 0.05, 0.2, 1.0, 5.0])
    points = []
    for v in vs:
        i_th = total_current(tcurves, v)
        if mode == 3:  # 完全随机噪声，制造模型外
            i_meas = abs(i_th + rng.gauss(0.0, 3.0))
        else:
            i_meas = max(0.0, i_th + rng.uniform(-tol, tol)
                         if rng.random() < 0.9 else
                         i_th + rng.gauss(0.0, tol + 0.01))
        points.append((v, i_meas))
    return groups, baseline, points, tol


def by_id(groups, gid):
    return next(g for g in groups if g.id == gid)


def main():
    rng = random.Random(20261006)
    trials = 400
    mismatches = 0
    for t in range(trials):
        groups, baseline, points, tol = random_case(rng)
        ref = diagnose_reference(groups, baseline, points, tol)
        fast = diagnose_fast(groups, baseline, points, tol)
        # message 文本由同样字段生成，这里比较除 message 外全部内容
        ref.pop("message", None)
        msg_fast = fast.pop("message", None)
        if ref != fast:
            mismatches += 1
            print(f"--- MISMATCH trial {t} tol={tol}")
            for key in ref:
                if ref[key] != fast[key]:
                    print(f"  key={key}\n   ref ={ref[key]}\n   fast={fast[key]}")
            print("  groups:", [(g.id, g.voc, g.isc) for g in groups])
            print("  baseline:", baseline)
            print("  points:", [(round(v, 3), round(i, 3)) for v, i in points])
            if mismatches >= 5:
                break
    print(f"{trials - mismatches}/{trials} cases match"
          + (" — ALL OK" if mismatches == 0 else f" — {mismatches} MISMATCHES"))
    sys.exit(1 if mismatches else 0)


if __name__ == "__main__":
    main()
