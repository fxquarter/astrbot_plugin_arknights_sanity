import asyncio
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import aiohttp
from astrbot.api import logger


class SklandClient:
    """Skland API 客户端，包含换票、签名与重试兜底逻辑。"""

    def __init__(self, token: str):
        self.token = self._normalize_token(token)
        self.cred = ""
        self.access_token = ""
        self.uid = ""
        self.preferred_uid = ""
        self.channel_master_id = "1"
        self.nickname = ""
        self.channel_name = ""
        self.dId = "de9759a5afaa634f"
        self.platform = "1"
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()

    @staticmethod
    def _normalize_token(token: str) -> str:
        """规范化 token 输入，支持纯文本或粘贴 JSON。"""
        t = str(token or "").strip()
        if t.lower().startswith("bearer "):
            t = t[7:].strip()

        try:
            payload = json.loads(t)
            if isinstance(payload, dict):
                if isinstance(payload.get("content"), str):
                    t = payload["content"].strip()
                elif isinstance(payload.get("data"), dict) and isinstance(
                    payload["data"].get("content"), str
                ):
                    t = payload["data"]["content"].strip()
                elif isinstance(payload.get("token"), str):
                    t = payload["token"].strip()
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

        if len(t) >= 2 and t[0] == '"' and t[-1] == '"':
            try:
                t = json.loads(t)
            except (json.JSONDecodeError, TypeError, ValueError):
                t = t[1:-1]

        return t

    def set_cred_token(self, token: str):
        normalized = self._normalize_token(token)
        if normalized == self.token:
            return
        self.token = normalized
        self.cred = ""
        self.access_token = ""
        self.uid = ""
        self.channel_master_id = "1"

    def set_preferred_uid(self, uid: str):
        self.preferred_uid = str(uid or "").strip()

    def _is_auth_error(self, data: dict) -> bool:
        code = data.get("code")
        if code in {10001, 10002, 10003, 10200, 10300}:
            return True
        message = str(data.get("message", "")).lower()
        return any(
            k in message
            for k in ["token", "auth", "sign", "expired", "无效", "鉴权", "签名"]
        )

    def _base_headers(self):
        return {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 12; SM-A5560 Build/V417IR; wv) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/101.0.4951.61 "
                "Safari/537.36; SKLand/1.52.1"
            ),
            "Accept-Encoding": "gzip",
            "Connection": "close",
            "X-Requested-With": "com.hypergryph.skland",
            "dId": self.dId,
        }

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session and not self._session.closed:
            return self._session

        async with self._session_lock:
            if self._session and not self._session.closed:
                return self._session
            self._session = aiohttp.ClientSession()
            return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _exchange_user_token_to_cred(self) -> bool:
        user_token = self._normalize_token(self.token)
        if not user_token:
            return False

        base_headers = self._base_headers()
        session = await self._get_session()
        grant_url = "https://as.hypergryph.com/user/oauth2/v2/grant"
        grant_body = {"appCode": "4ca99fa6b56cc2ba", "token": user_token, "type": 0}
        async with session.post(
            grant_url, headers=base_headers, json=grant_body
        ) as resp:
            grant_data = await resp.json(content_type=None)

        if grant_data.get("status") != 0:
            logger.warning(f"Skland grant failed: {grant_data}")
            return False

        auth_code = grant_data.get("data", {}).get("code")
        if not auth_code:
            logger.warning(f"Skland grant returned empty code: {grant_data}")
            return False

        cred_url = "https://zonai.skland.com/web/v1/user/auth/generate_cred_by_code"
        cred_body = {"code": auth_code, "kind": 1}
        async with session.post(cred_url, headers=base_headers, json=cred_body) as resp:
            cred_data = await resp.json(content_type=None)

        if cred_data.get("code") != 0:
            logger.warning(f"Skland generate cred failed: {cred_data}")
            return False

        payload = cred_data.get("data", {})
        self.cred = str(payload.get("cred", "")).strip()
        self.access_token = str(payload.get("token", "")).strip()
        return bool(self.cred and self.access_token)

    async def _refresh_access_token_by_cred(self, cred: str) -> bool:
        cred = self._normalize_token(cred)
        if not cred:
            return False
        url = "https://zonai.skland.com/api/v1/auth/refresh"
        headers = {"cred": cred}
        session = await self._get_session()
        async with session.get(url, headers=headers) as resp:
            data = await resp.json(content_type=None)
            if data.get("code") == 0:
                self.cred = cred
                self.access_token = data["data"]["token"]
                return True
            logger.error(f"Failed to refresh token: {data}")
            return False

    async def refresh_token(self):
        """从用户 token 刷新签名凭证（并兼容直接 cred token 兜底）。"""
        self.token = self._normalize_token(self.token)
        if not self.token:
            logger.error("Skland token is empty, cannot refresh access token")
            self.cred = ""
            self.access_token = ""
            return False

        if await self._exchange_user_token_to_cred():
            return True

        if await self._refresh_access_token_by_cred(self.token):
            return True

        self.cred = ""
        self.access_token = ""
        return False

    def _get_sign(
        self, path: str, query: str = "", platform: str = "1", use_md5: bool = True
    ):
        """基于当前 path/query/header 生成 Skland 请求签名。"""
        timestamp = str(int(time.time()))
        header_ca = json.dumps(
            {
                "platform": str(platform),
                "timestamp": timestamp,
                "dId": self.dId,
                "vName": "1.0.0",
            },
            separators=(",", ":"),
        )
        query_for_sign = query[1:] if query.startswith("?") else query
        s = f"{path}{query_for_sign}{timestamp}{header_ca}"
        mac = hmac.new(
            self.access_token.encode("utf-8"), s.encode("utf-8"), hashlib.sha256
        )
        sign = (
            hashlib.md5(mac.hexdigest().encode("utf-8")).hexdigest()
            if use_md5
            else mac.hexdigest()
        )
        return sign, timestamp

    async def _request_once(
        self, path: str, query_clean: str, platform: str, use_md5: bool
    ):
        sign, timestamp = self._get_sign(
            path, query_clean, platform=platform, use_md5=use_md5
        )
        headers = {
            "cred": self.cred,
            "sign": sign,
            "source": "1",
            "dId": self.dId,
            "platform": platform,
            "vName": "1.0.0",
            "timestamp": timestamp,
            "User-Agent": self._base_headers()["User-Agent"],
            "X-Requested-With": "com.hypergryph.skland",
        }
        full_url = f"https://zonai.skland.com{path}"
        if query_clean:
            full_url = f"{full_url}?{query_clean}"
        session = await self._get_session()
        async with session.get(full_url, headers=headers) as resp:
            return await resp.json(content_type=None)

    async def _request_json(
        self,
        path: str,
        query: str = "",
        retry_on_auth: bool = True,
        retry_on_sign_error: bool = True,
    ):
        """发送签名请求，并在鉴权失败或签名异常时执行兜底重试。"""
        if not self.access_token or not self.cred:
            ok = await self.refresh_token()
            if not ok:
                return {
                    "code": -1,
                    "message": "token 刷新失败，请检查 config.json 中的 token 是否有效",
                }

        query_clean = query[1:] if query.startswith("?") else query
        auth_retry_left = 1 if retry_on_auth else 0
        sign_retry_left = 1 if retry_on_sign_error else 0
        force_refresh_done = False

        while True:
            data = await self._request_once(
                path, query_clean, platform=self.platform, use_md5=True
            )

            if auth_retry_left > 0 and self._is_auth_error(data):
                auth_retry_left -= 1
                logger.warning(
                    f"Skland auth failed, trying to refresh token and retry once: {data}"
                )
                if await self.refresh_token():
                    continue

            if not (sign_retry_left > 0 and data.get("code") == 10000):
                return data

            sign_retry_left -= 1
            # 10000 常见于不同环境下签名或 platform 组合不匹配。
            # 在放弃前尝试多种已验证可用的组合。
            logger.warning(
                "Skland request code=10000 on platform=%s, retrying with fallback platform/sign",
                self.platform,
            )
            fallback_platform = "3" if self.platform == "1" else "1"

            # 放弃前尝试所有已知可用的签名/platform 组合。
            variants = [
                (fallback_platform, True),
                (self.platform, False),
                (fallback_platform, False),
            ]
            for platform, use_md5 in variants:
                data2 = await self._request_once(
                    path, query_clean, platform=platform, use_md5=use_md5
                )
                if data2.get("code") == 0:
                    self.platform = platform
                    return data2
                data = data2

            # 若签名仍失败，强制刷新一次凭证并在无签名递归场景下重试。
            if not force_refresh_done and await self.refresh_token():
                force_refresh_done = True
                continue

            return data

    async def get_binding(self):
        path = "/api/v1/game/player/binding"
        data = await self._request_json(path)
        if data.get("code") == 0:
            characters = data.get("data", {}).get("list", [])
            for char in characters:
                if char.get("appCode") != "arknights":
                    continue

                binding_list = char.get("bindingList", [])
                if not binding_list:
                    continue

                binding = None
                if self.preferred_uid:
                    for item in binding_list:
                        if str(item.get("uid", "")) == self.preferred_uid:
                            binding = item
                            break

                if not binding:
                    binding = binding_list[0]
                    if len(binding_list) > 1:
                        logger.warning(
                            "Skland has multiple Arknights bindings, using first uid=%s; set preferred_uid in config.json to pin target role",
                            str(binding.get("uid", "")),
                        )

                self.uid = str(binding.get("uid", ""))
                self.channel_master_id = str(binding.get("channelMasterId", "1"))
                self.nickname = str(binding.get("nickName", ""))
                self.channel_name = str(binding.get("channelName", ""))
                break

            if not self.uid:
                return {
                    "code": -2,
                    "message": "未找到明日方舟绑定角色，请先在森空岛绑定游戏账号",
                }

        return data

    async def get_player_info(self):
        if not self.uid:
            bind_data = await self.get_binding()
            if bind_data.get("code") != 0:
                return bind_data

        path = "/api/v1/game/player/info"
        query = urlencode({"uid": self.uid, "channelMasterId": self.channel_master_id})
        data = await self._request_json(path, query)

        if data.get("code") != 0 and "uid" in str(data.get("message", "")).lower():
            self.uid = ""
            self.channel_master_id = "1"
            bind_data = await self.get_binding()
            if bind_data.get("code") == 0 and self.uid:
                query = urlencode(
                    {"uid": self.uid, "channelMasterId": self.channel_master_id}
                )
                return await self._request_json(path, query)
        return data
