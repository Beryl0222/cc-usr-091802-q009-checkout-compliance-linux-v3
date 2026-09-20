"""基于标准库的 HTTP 路由层。

端点：

- GET  /health
- GET  /checkout?order_id=&region=&mcc=&at=&user_id=   生成并登记方案呈现
- POST /checkout/select                                 登记用户显式选择
- POST /payments/attempt                                首次扣款
- POST /payments/retry                                  失败重试（同渠道+重核验）
- POST /marketing/review                                营销素材送审
- GET  /audit/orders[/{order_id}]                       审计复原
- GET  /policies                                        政策版本列表/解析
- POST /admin/policies/publish                          并发发布（CAS）
- POST /admin/policies/{version}/promote                灰度提升全量
- GET  /admin/check                                     配置自检
"""

from __future__ import annotations

import json
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .app import CheckoutApp
from .marketing import material_from_dict
from .models import CST, ComplianceError, parse_dt
from .policy import PolicyConflict, policy_from_dict


class ApiHandler(BaseHTTPRequestHandler):
    app: CheckoutApp = None  # 由 make_server 注入（类属性）

    server_version = "CheckoutCompliance/1.0"

    # ---- 基础设施 ---------------------------------------------------

    def _send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=_json_default).encode(
            "utf-8"
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ApiError(400, "bad-json", f"请求体不是合法 JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ApiError(400, "bad-body", "请求体必须是 JSON 对象")
        return data

    def _moment(self, params, body):
        value = (params.get("at") or [None])[0] or body.get("at")
        return parse_dt(value) if value else datetime.now(tz=CST)

    def log_message(self, *_args):
        return

    # ---- 路由 -------------------------------------------------------

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        try:
            parts = urlsplit(self.path)
            # 查询串中的 "+" 是 ISO 时区偏移（如 +08:00），parse_qs 默认会
            # 按表单编码把它转成空格，这里先转义保护
            params = parse_qs(parts.query.replace("+", "%2B"))
            path = parts.path.rstrip("/") or "/"
            body = self._read_json() if method == "POST" else {}

            if method == "GET" and path == "/health":
                return self._health()
            if method == "GET" and path == "/checkout":
                return self._get_checkout(params)
            if method == "POST" and path == "/checkout/select":
                return self._select(body)
            if method == "POST" and path == "/payments/attempt":
                return self._attempt(body, retry=False)
            if method == "POST" and path == "/payments/retry":
                return self._attempt(body, retry=True)
            if method == "POST" and path == "/marketing/review":
                return self._review(body, params)
            if method == "GET" and path == "/audit/orders":
                return self._audit_all()
            if method == "GET" and path.startswith("/audit/orders/"):
                return self._audit_order(path.rsplit("/", 1)[-1])
            if method == "GET" and path == "/policies":
                return self._list_policies(params)
            if method == "POST" and path == "/admin/policies/publish":
                return self._publish(body)
            if method == "POST" and path.endswith("/promote") and path.startswith(
                "/admin/policies/"
            ):
                version = path.split("/")[3]
                return self._promote(version, body)
            if method == "GET" and path == "/admin/check":
                return self._check()
            raise ApiError(404, "not-found", f"未知端点: {method} {path}")
        except ApiError as exc:
            self._send_json(
                {"error": {"code": exc.code, "message": str(exc)}}, exc.status
            )
        except PolicyConflict as exc:
            self._send_json(
                {"error": {"code": exc.code, "message": str(exc)}}, 409
            )
        except ComplianceError as exc:
            self._send_json(
                {"error": {"code": exc.code, "message": str(exc)}}, 422
            )

    # ---- 处理函数 ---------------------------------------------------

    def _health(self):
        self._send_json(
            {
                "status": "ok",
                "service": "checkout-compliance",
                "name": "收银渠道合规编排",
            }
        )

    def _require(self, body: dict, *keys: str):
        missing = [k for k in keys if not body.get(k)]
        if missing:
            raise ApiError(400, "missing-fields", f"缺少必填字段: {', '.join(missing)}")

    def _get_checkout(self, params):
        order_id = (params.get("order_id") or [None])[0]
        region = (params.get("region") or [""])[0]
        mcc = (params.get("mcc") or ["0000"])[0]
        user_id = (params.get("user_id") or [None])[0]
        at = (params.get("at") or [None])[0]
        moment = parse_dt(at) if at else datetime.now(tz=CST)
        if not region:
            raise ApiError(400, "missing-fields", "缺少必填参数: region")
        plan = self.app.orchestrator.build_plan(
            region=region, mcc=mcc, moment=moment, canary_unit=user_id
        )
        if order_id:
            # 方案实际呈现给用户：以请求时刻登记呈现事件
            self.app.payments.present(order_id, plan, moment=moment)
        self._send_json(plan.to_json())

    def _select(self, body):
        self._require(body, "order_id", "source_id", "region", "mcc")
        moment = self._moment({}, body)
        self.app.payments.select(
            body["order_id"],
            body["source_id"],
            region=body["region"],
            mcc=body["mcc"],
            moment=moment,
        )
        self._send_json(
            {
                "order_id": body["order_id"],
                "source_id": body["source_id"],
                "selected": True,
                "preselection": "forbidden",
            }
        )

    def _attempt(self, body, *, retry: bool):
        self._require(body, "order_id", "amount_cents", "region", "mcc")
        moment = self._moment({}, body)
        kwargs = dict(
            amount_cents=int(body["amount_cents"]),
            region=body["region"],
            mcc=body["mcc"],
            moment=moment,
            client_attempt_id=body.get("client_attempt_id"),
        )
        if retry:
            record = self.app.payments.retry(body["order_id"], **kwargs)
        else:
            record = self.app.payments.attempt(body["order_id"], **kwargs)
        payload = record.to_json()
        payload["recheck_required_next_time"] = True
        self._send_json(payload, 200 if record.status == "succeeded" else 202)

    def _review(self, body, params):
        material = material_from_dict(body)
        region = body.get("region") or (params.get("region") or [""])[0]
        if not region:
            raise ApiError(400, "missing-fields", "缺少必填字段: region")
        moment = self._moment({}, body)
        result = self.app.reviewer.review(
            material,
            region=region,
            moment=moment,
            policy_version=body.get("policy_version"),
        )
        self._send_json(result.to_json(), 200 if result.approved else 422)

    def _audit_all(self):
        self._send_json(self.app.auditor.audit_all())

    def _audit_order(self, order_id):
        report = self.app.auditor.audit_order(order_id)
        self._send_json(report.to_json())

    def _list_policies(self, params):
        region = (params.get("region") or [None])[0]
        at = (params.get("at") or [None])[0]
        user_id = (params.get("user_id") or [None])[0]
        policy_id = self.app.policy_id
        if region and at:
            resolution = self.app.policies.resolve(
                policy_id, region, parse_dt(at), user_id
            )
            if resolution is None:
                return self._send_json(
                    {"resolved": None, "versions": self._version_summaries()}
                )
            return self._send_json(
                {
                    "resolved": {
                        "version": resolution.policy.version,
                        "mode": resolution.mode,
                    },
                    "versions": self._version_summaries(),
                }
            )
        self._send_json({"versions": self._version_summaries()})

    def _version_summaries(self):
        return [
            {
                "version": p.version,
                "regions": sorted(p.regions),
                "window": p.window.to_json(),
                "canary_percent": p.canary_percent,
                "head": p.version == self.app.head_version(),
            }
            for p in self.app.policies.list_versions(self.app.policy_id)
        ]

    def _publish(self, body):
        # expected_version 允许显式为 null（表示首次发布），只检查键存在
        if not body.get("policy") or "expected_version" not in body:
            raise ApiError(
                400,
                "missing-fields",
                "缺少必填字段: policy / expected_version",
            )
        policy = policy_from_dict(body["policy"])
        expected = body["expected_version"]
        published = self.app.publish_policy(
            policy, None if expected is None else int(expected)
        )
        self._send_json(
            {
                "published": True,
                "policy_id": published.policy_id,
                "version": published.version,
                "head": self.app.head_version(),
            },
            201,
        )

    def _promote(self, version: str, body):
        expected = body.get("expected_canary_percent")
        promoted = self.app.promote_policy(
            int(version), None if expected is None else int(expected)
        )
        self._send_json(
            {
                "promoted": True,
                "version": promoted.version,
                "canary_percent": promoted.canary_percent,
            }
        )

    def _check(self):
        result = self.app.self_check()
        self._send_json(result, 200 if result["ok"] else 500)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code


def _json_default(obj):
    if isinstance(obj, datetime):
        return obj.astimezone(CST).isoformat(timespec="seconds")
    if isinstance(obj, frozenset):
        return sorted(obj)
    raise TypeError(f"不可序列化: {type(obj)}")


def make_server(app: CheckoutApp, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("BoundApiHandler", (ApiHandler,), {"app": app})
    return ThreadingHTTPServer((host, port), handler)
