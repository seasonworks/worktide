"""将活动状态通过 HTTP POST 上报到 FastAPI 服务端。"""
import logging

import requests

from .config import Config

logger = logging.getLogger(__name__)


class Reporter:
    def __init__(
        self,
        config: Config,
        machine_id: str,
        hostname: str,
        *,
        hw_fingerprint: str = "",
        legacy_machine_id: str | None = None,
    ) -> None:
        self.config = config
        self.machine_id = machine_id
        self.hostname = hostname
        # Phase 6.0D · Device Identity Hardening 注入字段
        # 这两个字段从 MachineIdentity 透传；测试代码（不传）拿默认空值。
        # legacy_machine_id 仅在 machine.json 是本进程刚生成时为非 None；
        # 一旦进程重启读到缓存的 machine.json，本字段恒为 None ⇒ 服务端不再
        # 走迁移分支。
        self.hw_fingerprint = hw_fingerprint
        self.legacy_machine_id = legacy_machine_id
        self.session = requests.Session()

    def build_payload(self, idle_seconds: float, is_active: bool) -> dict:
        return {
            "machine_id": self.machine_id,
            # 为空则让服务端先用 machine_id 占位，后续可在后台改名
            "name": self.config.employee_name or None,
            "hostname": self.hostname,
            "idle_seconds": int(idle_seconds),
            "is_active": is_active,
            # Phase 6.0D · 6.0D-S server 接受这两个可选字段；老 server 会因
            # pydantic extra="ignore" 而无视，无 schema 破坏风险。
            "hw_fingerprint": self.hw_fingerprint or None,
            "legacy_machine_id": self.legacy_machine_id,
        }

    def send(self, idle_seconds: float, is_active: bool) -> dict:
        """上报一次，返回服务端响应（含 status / is_idle_alert）。

        网络异常由调用方处理，不在此吞掉。
        """
        payload = self.build_payload(idle_seconds, is_active)
        resp = self.session.post(
            self.config.report_url,
            json=payload,
            timeout=self.config.request_timeout_seconds,
        )
        resp.raise_for_status()
        return resp.json()
