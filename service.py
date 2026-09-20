"""收银渠道合规编排服务入口。

保留基线契约（SERVICE_ID / health_payload / /health / --check），
默认从 fixtures/ 加载政策版本、资金源登记与历史回执。
"""

from __future__ import annotations

import argparse
import json

from checkout.api import make_server
from checkout.app import load_app

SERVICE_ID = "checkout-compliance"
SERVICE_NAME = "收银渠道合规编排"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--fixtures", default="fixtures")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        app = load_app(args.fixtures)
        report = app.self_check()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        assert report["ok"], report["issues"]
        print("基础检查通过")
        return

    app = load_app(args.fixtures)
    server = make_server(app, args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
