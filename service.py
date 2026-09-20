"""收银渠道合规编排运行入口。

用法：
  python3 service.py --check            校验 fixture、政策窗口与合规不变量
  python3 service.py --demo             输出横跨 2026-09-30 的审计复原报告
  python3 service.py [--port 8000]      启动 HTTP 服务（/health 等）
"""

import argparse
import json

from checkout import SERVICE_ID, SERVICE_NAME
from checkout.app import make_server
from checkout.health import health_payload
from checkout.demo import build_world, run_scenario


def run_check() -> None:
    """装配世界并逐项验证关键不变量。"""
    report = run_scenario(build_world(corrected=False))

    assert health_payload()["service"] == SERVICE_ID

    # 1. 生效日前后政策版本唯一且正确切换
    versions = report["effective_version_at"]
    assert versions["2026-09-29T22:15:00+08:00"] == "financial-marketing-2025"
    assert versions["2026-09-30T00:00:00+08:00"] == "financial-marketing-2026"

    # 2. 两个结账现场都没有服务端预选，且扣款跟随用户明确选择
    for key in ("c1_before_boundary", "c2_after_boundary"):
        integrity = report[key]["integrity"]
        assert integrity["no_server_preselection"]
        assert integrity["selection_was_explicit"]
        assert integrity["charges_match_selection"]

    # 3. C1：首次超时后重试沿用同一渠道并成功
    assert report["first_charge_failure"] == "channel-timeout"
    assert report["retry_result"]["status"] == "succeeded"
    assert report["retry_result"]["source_id"] == "cf-credit"
    assert report["retry_result"]["attempt"] == 2

    # 4. 回执中识别出迟到确认与错误分类
    receipts = report["c1_before_boundary"]["receipts"]
    assert any(r["late_confirmation"] for r in receipts)
    assert any(r["category_mismatch"] and r["claimed_category"] == "payment-tool"
               for r in receipts)

    # 5. 阻断营销全部命中，合规否定文案放行
    reviews = {m["material_id"]: m for m in report["marketing_reviews"]}
    assert reviews["AD-OLD-OK"]["decision"] == "approved"
    for material_id in (
        "AD-NEW-LOW-RISK", "AD-NEW-GUARANTEE", "AD-NEW-FIRST-ONLY",
        "AD-NEW-DIVERSION", "AD-NEW-DEFAULT", "AD-NEW-REPAYMENT",
        "AD-NEW-INCENTIVE",
    ):
        assert reviews[material_id]["decision"] == "blocked", material_id
        assert reviews[material_id]["hits"], material_id

    print("基础检查通过：政策版本唯一、无服务端预选、重试沿用原渠道、")
    print("迟到/错误分类回执可识别，营销阻断与放行均符合预期。")


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true", help="校验配置与不变量后退出")
    parser.add_argument("--demo", action="store_true", help="输出审计复原报告后退出")
    args = parser.parse_args()
    if args.check:
        run_check()
        return
    if args.demo:
        print(json.dumps(run_scenario(), ensure_ascii=False, indent=2))
        return
    httpd = make_server(args.port)
    print(f"{SERVICE_NAME} 监听 :{args.port}（GET /health）")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
