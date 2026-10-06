"""定向边界测试：零容差、拐点、Voc 支路、平台可行带、多串接线。

新实现与逐档枚举参考实现必须逐字段一致。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.model import Group, string_curve, total_current  # noqa: E402
from test_differential import diagnose_reference  # noqa: E402
from app.diagnose import diagnose as diagnose_fast  # noqa: E402


def compare(name, groups, baseline, points, tol):
    ref = diagnose_reference(groups, baseline, points, tol)
    fast = diagnose_fast(groups, baseline, points, tol)
    ref.pop("message", None)
    fast.pop("message", None)
    status = "OK " if ref == fast else "FAIL"
    print(f"[{status}] {name}: verdict={fast['verdict']} "
          f"feasible={fast['explainable_groups']}")
    if ref != fast:
        for key in ref:
            if ref[key] != fast[key]:
                print(f"  key={key}\n   ref ={ref[key]}\n   fast={fast[key]}")
        return False
    return True


def synth_points(groups, baseline, voltages, noise=0.0):
    by = {g.id: g for g in groups}
    curves = [string_curve([by[gid] for gid in s]) for s in baseline]
    return [(v, total_current(curves, v) + noise) for v in voltages]


def main():
    ok = True

    # 1) 健康单串、零容差、电压恰落在多个旁路拐点与 Voc 上
    groups = [Group(f"G{k}", 10.0, 30.0) for k in range(1, 10)]
    baseline = [[g.id for g in groups]]
    cur = string_curve(groups)
    knees = list(cur.knees)
    pts = synth_points(groups, baseline, knees[:-1] + [cur.voc])
    ok &= compare("zero-tol healthy knees+Voc", groups, baseline, pts, 0.0)

    # 2) 零容差、电压在拐点之间
    vs = [2.3, 7.7, 15.01, 33.33, 60.0, 89.99]
    pts = synth_points(groups, baseline, vs)
    ok &= compare("zero-tol healthy interior", groups, baseline, pts, 0.0)

    # 3) 一组退化 30→20，零容差：应定位单组（各组对称→歧义）
    deg = [Group("G1", 10.0, 20.0)] + groups[1:]
    pts = synth_points(deg, baseline, vs + [0.0, 90.0])
    ok &= compare("zero-tol degraded 20A", groups, baseline, pts, 0.0)

    # 4) 同上，容差 0.2
    ok &= compare("tol 0.2 degraded 20A", groups, baseline, pts, 0.2)

    # 5) 多串接线（3 串，各串 Voc 不同），退化点落在已达 Voc 的串上
    groups3 = [
        Group("A", 30.0, 8.0), Group("B", 30.0, 8.0), Group("C", 30.0, 6.0),
        Group("D", 40.0, 9.0), Group("E", 20.0, 5.0),
    ]
    base3 = [["A", "B", "C"], ["D", "E"]]
    curves3 = [string_curve([groups3[0], groups3[1], groups3[2]]),
               string_curve([groups3[3], groups3[4]])]
    voc3 = max(c.voc for c in curves3)
    # V=90：两串都达 Voc，总电流必须为 0；再混入低压点
    vs3 = [0.0, 10.0, 60.0, 60.0 + 1e-12, 90.0, 90.0 - 1e-12]
    pts3 = synth_points(groups3, base3, vs3)
    ok &= compare("multi-string Voc branches zero-tol",
                  groups3, base3, pts3, 0.0)

    # 6) Voc 支路实测非零电流（模型外：零电流支路与实测核对失败）
    bad = [(v, i + (0.5 if abs(v - voc3) < 1e-9 else 0.0))
           for v, i in pts3]
    ok &= compare("Voc branch nonzero measured", groups3, base3, bad, 0.01)

    # 7) 宽容差全可行（含边界 i=0 的夹断）
    pts_wide = [(v, max(0.0, i - 0.1)) for v, i in
                synth_points(groups, baseline, [0.0, 1.0, 45.0, 90.0])]
    ok &= compare("wide tol", groups, baseline, pts_wide, 50.0)

    # 8) 退化到 0.01 网格极小值的单点可行带（tol 极窄）
    deg_min = [Group("G1", 10.0, 0.01)] + groups[1:]
    pts_min = synth_points(deg_min, baseline, [0.0, 3.3, 50.0, 89.0])
    ok &= compare("min-grid degraded", groups, baseline, pts_min, 1e-6)

    # 9) Isc 非整百分（如 8.23）→ 候选数 822，等档排除
    groups_x = [Group("X1", 10.0, 8.23), Group("X2", 10.0, 8.23),
                Group("X3", 12.5, 7.5)]
    bx = [["X1", "X2", "X3"]]
    deg_x = [Group("X1", 10.0, 5.55), groups_x[1], groups_x[2]]
    pts_x = synth_points(deg_x, bx, [0.0, 4.0, 17.0, 32.0], noise=0.0)
    ok &= compare("non-round isc 8.23", groups_x, bx, pts_x, 0.05)

    # 10) 测量电流夹零：理论 0.3、实测 0、tol 0.3 恰边界
    groups_z = [Group("Z1", 10.0, 1.0), Group("Z2", 10.0, 1.0),
                Group("Z3", 10.0, 1.0)]
    bz = [["Z1", "Z2", "Z3"]]
    cz = string_curve(groups_z)
    pts_z = [(cz.voc, 0.0), (cz.voc - 1e-10, 0.0), (20.0, 0.0)]
    ok &= compare("clipped zero measured", groups_z, bz, pts_z, 0.3)

    print("ALL EDGE CASES PASS" if ok else "SOME CASES FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
