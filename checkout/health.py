"""稳定的服务身份信息。"""

from . import SERVICE_ID, SERVICE_NAME


def health_payload():
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}
