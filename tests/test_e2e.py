"""端到端模拟 main.diagnose_trial 的调用链（无需 FastAPI）：

DB 写入 → 读记录 → validate_payload → 校验请求 → diagnose，
并核对响应字段、只读性（诊断后记录未变）与 400 中文错误。
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["PVBENCH_DB_DIR"] = tempfile.mkdtemp()

from app import db  # noqa: E402
from app.diagnose import (  # noqa: E402
    DiagnoseError, diagnose, validate_diagnose_request,
)
from app.model import Group, string_curve  # noqa: E402
from app.validation import validate_payload  # noqa: E402


def run_endpoint(payload, body):
    """复刻 main.diagnose_trial 的核心逻辑。"""
    groups = validate_payload(payload)
    by_id = {g.id: g for g in groups}
    curves = [
        string_curve([by_id[gid]
                      for gid in sorted(s, key=lambda g: by_id[g].id)])
        for s in payload["baseline"]
    ]
    baseline_voc = max(c.voc for c in curves)
    points = validate_diagnose_request(body, baseline_voc)
    tol = float(body["tolerance"])
    return diagnose(groups, payload["baseline"], points, tol)


def main():
    payload = {
        "name": "批量诊断验收",
        "groups": [{"id": f"G{k}", "voc": "10", "isc": "30"}
                   for k in range(1, 10)],
        "baseline": [[f"G{k}" for k in range(1, 10)]],
    }
    groups = validate_payload(payload)
    result = {"baseline": {"voc": 90}, "optimal": {}, "comparison": {},
              "candidates": []}
    saved = db.create_trial(payload["name"], payload, result)
    tid = saved["id"]

    # 一千个合法测点 + 宽容差（按健康模型生成理论电流，叠加宽容差内抖动）
    from app.model import total_current
    healthy_curve = string_curve(groups)
    body = {
        "tolerance": 5.0,
        "points": [
            {"v": round((k + 1) * 90.0 / 1000, 6),
             "i": max(0.0, total_current(
                 [healthy_curve], (k + 1) * 90.0 / 1000) - 0.03)}
            for k in range(1000)
        ],
    }
    t0 = time.perf_counter()
    diag = run_endpoint(payload, body)
    dt = time.perf_counter() - t0
    assert diag["verdict"] == "unconfirmed", diag["verdict"]
    assert len(diag["points"]) == 1000
    assert len(diag["groups"]) == 9
    for gr in diag["groups"]:
        assert set(gr) >= {"group", "isc_original", "sensitive_points",
                           "feasible"}
        if gr["feasible"]:
            assert set(gr) >= {"isc_min", "isc_max", "isc_candidates"}
    assert diag["explainable_groups"] == [f"G{k}" for k in range(1, 10)]
    print(f"1000-point wide-tol: {dt:.3f}s (<5s)")

    # 只读性：诊断后 DB 记录（payload/result）原样
    row = db.get_trial(tid)
    assert row["payload"] == payload
    assert row["result"] == result

    # 一组退化 20 A 实测 + 0.2 容差
    deg_groups = [Group("G1", 10.0, 20.0)] + groups[1:]
    c = string_curve(deg_groups)
    from app.model import total_current
    body2 = {
        "tolerance": 0.2,
        "points": [
            {"v": round((k + 1) * 90.0 / 1000, 6),
             "i": total_current([c], (k + 1) * 90.0 / 1000)}
            for k in range(1000)
        ],
    }
    t0 = time.perf_counter()
    diag2 = run_endpoint(payload, body2)
    dt2 = time.perf_counter() - t0
    print(f"1000-point degraded tol=0.2: {dt2:.3f}s (<5s), "
          f"verdict={diag2['verdict']}")
    assert dt2 < 5.0
    assert dt < 5.0

    # 非法输入 → 中文 400
    def expect_error(b, *frags):
        try:
            run_endpoint(payload, b)
        except DiagnoseError as e:
            assert all(f in str(e) for f in frags), str(e)
            return
        raise AssertionError("should have raised")

    expect_error({"tolerance": 0, "points": [{"v": 1, "i": 1},
                                             {"v": 2, "i": 1}]}, "至少")
    expect_error({"tolerance": -1, "points": [{"v": 1, "i": 1}] * 3}, "容差")
    expect_error({"tolerance": 0, "points": [
        {"v": 90.001, "i": 0}, {"v": 1, "i": 0}, {"v": 2, "i": 0}]},
        "超过")
    expect_error({"tolerance": 0, "points": [
        {"v": 1, "i": 0}, {"v": 1, "i": 0}, {"v": 2, "i": 0}]}, "重复")
    expect_error({"tolerance": 0, "points": [
        {"v": -1, "i": 0}, {"v": 1, "i": 0}, {"v": 2, "i": 0}]}, "非负")
    expect_error({"tolerance": "nan", "points": [
        {"v": 1, "i": 0}, {"v": 2, "i": 0}, {"v": 3, "i": 0}]}, "有限")
    print("validation 400 errors OK")

    # 记录不存在语义（main 返回 404 的前置）
    assert db.get_trial(99999) is None

    print("END-TO-END OK")


if __name__ == "__main__":
    main()
