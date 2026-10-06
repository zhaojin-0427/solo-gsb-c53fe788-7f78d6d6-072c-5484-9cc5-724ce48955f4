"""复现题目所述两类批量诊断请求的耗时（临时基准脚本）。"""
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.diagnose import diagnose
from app.model import Group, string_curve, total_current


def make_groups(degraded_idx=None, degraded_isc=20.0):
    gs = []
    for k in range(9):
        isc = degraded_isc if k == degraded_idx else 30.0
        gs.append(Group(f"G{k + 1}", 10.0, isc))
    return gs


def make_points(n, measured_groups, tol_jitter):
    """按“实测模型”在 n 个互异电压上生成实测电流。"""
    by_id = {g.id: g for g in measured_groups}
    curve = string_curve([by_id[f"G{k + 1}"] for k in range(9)])
    rng = random.Random(42)
    points = []
    for k in range(n):
        v = (k + 1) * (90.0 / n)  # 不重复、不超过基线 Voc=90
        i_th = total_current([curve], v)
        jitter = rng.uniform(-tol_jitter, tol_jitter) if tol_jitter else 0.0
        points.append((v, max(0.0, i_th + jitter)))
    return points


def run(label, measured_groups, points, tol):
    # 诊断始终基于“健康历史记录”（9×Voc10/Isc30 单串）
    hist_groups = make_groups()
    baseline = [[g.id for g in hist_groups]]
    t0 = time.perf_counter()
    result = diagnose(hist_groups, baseline, points, tol)
    dt = time.perf_counter() - t0
    print(f"[{label}] {len(points)} points, tol={tol}: {dt:.2f}s "
          f"verdict={result['verdict']} explainable="
          f"{[e['group'] for e in result['groups'] if e['feasible']]}")
    return dt


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1000

    # 场景一：健康模型实测点（宽容差下全可行）
    pts_healthy = make_points(n, make_groups(), 0.0)
    run("healthy-wide-tol", make_groups(), pts_healthy, 5.0)

    # 场景二：一组退化到 20 A 的实测点，容差 0.2
    pts_deg = make_points(n, make_groups(degraded_idx=0), 0.0)
    run("degraded-20A", make_groups(), pts_deg, 0.2)
