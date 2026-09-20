"""服务装配与 stdlib HTTP 接线。

路由：
  GET  /health
  POST /checkout/plan        请求收银台方案（按请求时刻选政策版本）
  POST /checkout/select      提交用户明确选择，领取选择令牌
  POST /payments/charge      扣款 / 重试（重试沿用同一渠道并重新核验）
  POST /payments/receipt     接入渠道回执（迟到、错误分类原样登记）
  POST /marketing/review     营销素材送审（按送审时刻规则集）
  GET  /audit/checkouts      结账会话列表
  GET  /audit/checkouts/<id> 复原布局/选择/政策来源/扣款/回执
  GET  /audit/policies       发布时间线
"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import timeutil
from .marketing import MarketingMaterial
from .models import Category
from .orchestrator import CheckoutRequest
from .payments import PaymentError
from .demo import build_world


class Application:
    def __init__(self, world=None):
        self.world = world or build_world(corrected=True)

    def health(self):
        from . import SERVICE_ID, SERVICE_NAME
        return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}

    def create_plan(self, body: dict) -> dict:
        req = CheckoutRequest(
            merchant_id=body["merchant_id"],
            region=body["region"],
            subject_key=body.get("subject_key", "anonymous"),
            amount=str(body["amount"]),
            at=body.get("at"),
            checkout_id=body.get("checkout_id"),
        )
        plan = self.world.orchestrator.build_plan(req)
        if req.checkout_id:
            self.world.audit.append("plan.rendered",
                                    {"checkout_id": req.checkout_id, "plan": plan},
                                    plan["generated_at"])
        return plan

    def select(self, body: dict, at) -> dict:
        checkout_id = body["checkout_id"]
        plan = self.world.orchestrator.rendered_plan(checkout_id)
        if not body.get("explicit", False):
            raise PaymentError("selection-not-explicit",
                               "缺少用户明确选择，服务端不得预选或默认勾选")
        selection = self.world.payments.record_selection(
            checkout_id=checkout_id,
            source_id=body["source_id"],
            merchant_id=plan["merchant_id"],
            region=plan["region"],
            plan=plan,
            explicit=True,
            at=body.get("at", at),
        )
        return {
            "selection_token": selection.token,
            "checkout_id": selection.checkout_id,
            "source_id": selection.source_id,
            "selected_at": selection.selected_at,
        }

    def charge(self, body: dict, at) -> dict:
        is_retry = bool(body.get("is_retry", False))
        method = self.world.payments.retry if is_retry else self.world.payments.charge
        return method(
            body["selection_token"], str(body["amount"]),
            body.get("at", at),
            channel_ref=body.get("channel_ref"),
        )

    def ingest_receipt(self, body: dict, at) -> dict:
        return self.world.payments.ingest_receipt(
            checkout_id=body["checkout_id"],
            source_id=body["source_id"],
            status=body.get("status", "unknown"),
            claimed_category=body.get("claimed_category"),
            channel_ref=body["channel_ref"],
            attempt_at=body["attempt_at"],
            received_at=body.get("received_at", at),
        )

    def review(self, body: dict, at) -> dict:
        material = MarketingMaterial(
            material_id=body["material_id"],
            text=body.get("text", ""),
            target_category=Category(body["target_category"]),
            publisher_role=body["publisher_role"],
            surface=body["surface"],
            default_checked=bool(body.get("default_checked", False)),
            incentive=body.get("incentive", ""),
            risk_confirmed=bool(body.get("risk_confirmed", False)),
        )
        result = self.world.reviews.review(material, body.get("at", at))
        return result.to_dict()

    def audit_checkout(self, checkout_id: str) -> dict:
        return self.world.audit.reconstruct_checkout(checkout_id, self.world.registry)


class Handler(BaseHTTPRequestHandler):
    app: Application = None  # 由 make_server 注入到类属性

    def do_GET(self):
        try:
            if self.path == "/health":
                self._write(200, self.app.health())
            elif self.path == "/audit/checkouts":
                self._write(200, {"checkouts": self.app.world.audit.checkouts()})
            elif self.path == "/audit/policies":
                self._write(200, {"policies": self.app.world.audit.effective_version_timeline()})
            elif self.path.startswith("/audit/checkouts/"):
                checkout_id = self.path.rsplit("/", 1)[-1]
                self._write(200, self.app.audit_checkout(checkout_id))
            else:
                self._error(404, "not-found", f"未知路径 {self.path}")
        except LookupError as exc:
            self._error(404, "not-found", str(exc))
        except Exception as exc:  # noqa: BLE001
            self._error(500, "internal-error", str(exc))

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw.decode("utf-8") or "{}")
            at = timeutil.now()
            if self.path == "/checkout/plan":
                self._write(200, self.app.create_plan(body))
            elif self.path == "/checkout/select":
                self._write(200, self.app.select(body, at))
            elif self.path == "/payments/charge":
                self._write(200, self.app.charge(body, at))
            elif self.path == "/payments/receipt":
                self._write(200, self.app.ingest_receipt(body, at))
            elif self.path == "/marketing/review":
                self._write(200, self.app.review(body, at))
            else:
                self._error(404, "not-found", f"未知路径 {self.path}")
        except PaymentError as exc:
            self._error(422, exc.code, str(exc))
        except (KeyError, ValueError) as exc:
            self._error(400, "bad-request", str(exc))
        except Exception as exc:  # noqa: BLE001
            self._error(500, "internal-error", str(exc))

    def _write(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, code: str, message: str) -> None:
        self._write(status, {"error": code, "message": message})

    def log_message(self, *_args):
        return


def make_server(port: int, *, world=None, app=None) -> ThreadingHTTPServer:
    application = app or Application(world)
    handler = type("BoundHandler", (Handler,), {"app": application})
    return ThreadingHTTPServer(("0.0.0.0", port), handler)
