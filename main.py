import asyncio
import json
import secrets
from collections.abc import Mapping
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

from .api_process import SklandClient
from .sanity import evaluate_reminder_state, extract_status


def load_config(config_path: Path) -> dict:
    """读取 config.json 原始数据。

    这里不注入默认字段，确保配置来源始终只有 config.json。
    """
    if not config_path.exists():
        return {}

    try:
        with config_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("load config failed, using empty config: %r", e)
        return {}


def save_config(config_path: Path, config: dict) -> None:
    """持久化运行期配置（例如提醒会话订阅列表）。"""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=4)


@register(
    "astrbot_plugin_arknights_sanity",
    "fxquarter",
    "明日方舟理智查询插件",
    "1.0.0",
    "https://github.com/fxquarter/astrbot_plugin_arknights_sanity",
)
class ArknightsHelper(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.config_path = (
            StarTools.get_data_dir("astrbot_plugin_arknights_sanity") / "config.json"
        )
        self.config = load_config(self.config_path)
        self.device_id = self._ensure_device_id()
        self.skland = SklandClient(self.config.get("token", ""), self.device_id)
        self.check_task = None
        self._config_lock = asyncio.Lock()
        self._init_lock = asyncio.Lock()
        self.reminded = bool(self.config.get("reminded_full", False))

    def _ensure_device_id(self) -> str:
        d_id = str(self.config.get("device_id", "")).strip()
        if d_id:
            return d_id

        # 首次生成并持久化，避免多实例共享固定设备指纹。
        d_id = secrets.token_hex(8)
        self.config["device_id"] = d_id
        save_config(self.config_path, self.config)
        return d_id

    async def _set_reminded_state(self, value: bool):
        async with self._config_lock:
            self.reminded = bool(value)
            self.config["reminded_full"] = self.reminded
            self._persist_config_unlocked()

    async def initialize(self):
        async with self._init_lock:
            if self.check_task and not self.check_task.done():
                return
            self.check_task = asyncio.create_task(self.check_sanity_loop())

    async def reload_config(self):
        async with self._config_lock:
            self.config = load_config(self.config_path)
            self.reminded = bool(self.config.get("reminded_full", self.reminded))

    def _persist_config_unlocked(self):
        save_config(self.config_path, self.config)

    @staticmethod
    def _normalize_check_interval(raw_value) -> int:
        try:
            interval = int(raw_value)
        except (TypeError, ValueError):
            return 600
        # 防止异常配置导致 0/负数空转轮询。
        return max(30, interval)

    @staticmethod
    def _parse_notify_action(msg: str) -> str | None:
        normalized = " ".join(str(msg or "").strip().lower().split())
        if normalized in {"notify on", "ark notify on", "/ark notify on"}:
            return "on"
        if normalized in {"notify off", "ark notify off", "/ark notify off"}:
            return "off"
        return None

    @staticmethod
    def _extract_platform_id(platform) -> str | None:
        if platform is None:
            return None

        if isinstance(platform, str):
            candidate = platform.strip()
            return candidate or None

        if isinstance(platform, Mapping):
            for key in ("id", "platform_id"):
                candidate = platform.get(key)
                if candidate is not None:
                    candidate = str(candidate).strip()
                    if candidate:
                        return candidate

        direct_id = getattr(platform, "id", None)
        if direct_id is not None:
            candidate = str(direct_id).strip()
            if candidate:
                return candidate

        meta = getattr(platform, "meta", None)
        if not callable(meta):
            return None
        try:
            meta_val = meta()
        except Exception:
            return None

        if isinstance(meta_val, Mapping):
            for key in ("id", "platform_id"):
                candidate = meta_val.get(key)
                if candidate is not None:
                    candidate = str(candidate).strip()
                    if candidate:
                        return candidate
            return None

        candidate = getattr(meta_val, "id", None)
        if candidate is None:
            return None
        candidate = str(candidate).strip()
        return candidate or None

    def _known_platform_ids(self) -> set[str]:
        ids: set[str] = set()
        platform_manager = getattr(self.context, "platform_manager", None)
        platform_insts = getattr(platform_manager, "platform_insts", None)
        if platform_insts is None:
            return ids

        candidates = []
        if isinstance(platform_insts, Mapping):
            candidates.extend(platform_insts.keys())
            candidates.extend(platform_insts.values())
        elif isinstance(platform_insts, (str, bytes)):
            candidates.append(platform_insts)
        else:
            try:
                candidates.extend(platform_insts)
            except TypeError:
                candidates.append(platform_insts)

        for platform in candidates:
            platform_id = self._extract_platform_id(platform)
            if platform_id:
                ids.add(platform_id)
        return ids

    def _prune_invalid_notify_users(self, notify_users: list[str]) -> list[str]:
        """清理平台适配器已不存在的会话。"""
        notify_users[:] = [str(umo).strip() for umo in notify_users if str(umo).strip()]
        if not notify_users:
            return []

        platform_ids = self._known_platform_ids()
        if not platform_ids:
            return notify_users
        valid: list[str] = []
        removed: list[str] = []
        for umo in notify_users:
            platform_id = umo.split(":", 1)[0]
            if platform_id in platform_ids:
                valid.append(umo)
            else:
                removed.append(umo)

        if removed:
            logger.warning("removed stale notify sessions: %s", ",".join(removed))
            # 原地修改列表，避免外部持有旧引用导致写入丢失。
            notify_users[:] = valid

        return notify_users

    async def _send_full_sanity_reminder(self, ap: int, max_ap: int) -> tuple[int, int]:
        """主动推送满理智提醒，并返回 (成功数, 失败数)。"""
        from astrbot.api.event import MessageChain

        async with self._config_lock:
            self.config = load_config(self.config_path)
            notify_users = self.config.setdefault("notify_users", [])
            notify_users = self._prune_invalid_notify_users(notify_users)
            self._persist_config_unlocked()
        if not notify_users:
            return 0, 0

        message_chain = MessageChain().message(
            f"理智提醒：当前理智已满 ({ap}/{max_ap})，请及时清理！"
        )
        sent = 0
        failed = 0
        for umo in notify_users:
            try:
                # 主动推送：使用保存的 unified_msg_origin 定位原会话。
                ok = await self.context.send_message(umo, message_chain)
                if ok:
                    sent += 1
                else:
                    failed += 1
                    logger.warning(
                        "full sanity reminder send failed, umo=%s reason=platform-not-found",
                        umo,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                failed += 1
                logger.warning(
                    "full sanity reminder send failed, umo=%s err=%r", umo, e
                )
                if "ApiNotAvailable" in repr(e):
                    logger.warning(
                        "OneBot proactive API unavailable for umo=%s. "
                        "If this is private chat, try subscribing in a group session instead.",
                        umo,
                    )

        logger.info("full sanity reminder delivered, sent=%s failed=%s", sent, failed)
        return sent, failed

    @filter.command("ark")
    async def ark_command(self, event: AstrMessageEvent):
        """明日方舟助手指令：/ark notify on/off"""
        umo = event.unified_msg_origin
        action = self._parse_notify_action(event.message_str)
        if not action:
            yield event.plain_result(
                "未知指令。请使用 /ark notify on 或 /ark notify off"
            )
            return

        async with self._config_lock:
            self.config = load_config(self.config_path)
            notify_users = self.config.setdefault("notify_users", [])
            notify_users = self._prune_invalid_notify_users(notify_users)

            if action == "on":
                if umo not in notify_users:
                    notify_users.append(umo)
                self._persist_config_unlocked()
            else:
                if umo in notify_users:
                    notify_users.remove(umo)
                self._persist_config_unlocked()

        if action == "on":
            yield event.plain_result("满理智提醒已开启！理智达到最大值时将提醒您。")
        else:
            yield event.plain_result("满理智提醒已关闭！")

    @filter.command("理智")
    async def check_sanity(self, event: AstrMessageEvent):
        await self.reload_config()
        if not self.config.get("token"):
            yield event.plain_result("未配置森空岛 token，请在 config.json 中配置。")
            return

        self.skland.set_cred_token(self.config.get("token", ""))
        self.skland.set_preferred_uid(self.config.get("preferred_uid", ""))

        try:
            data = await self.skland.get_player_info()
            if data.get("code") != 0:
                if data.get("code") == 10002:
                    yield event.plain_result(
                        "森空岛 token 已失效或格式不正确（10002: 用户未登录）。"
                        "请从 https://web-api.skland.com/account/info/hg 的响应中取 content 字段作为 token，"
                        "并确保 config.json 中不要带 Bearer 前缀。"
                    )
                    return
                yield event.plain_result(
                    f"获取数据失败: {data.get('message', '未知错误')} (code={data.get('code', 'N/A')})"
                )
                return

            status = extract_status(data)
            if not status:
                logger.error(
                    "Skland player info missing expected fields: %s",
                    json.dumps(data, ensure_ascii=False)[:600],
                )
                yield event.plain_result("获取数据失败: 返回结构异常，请查看日志。")
                return

            role_info = f"查询账号: uid={self.skland.uid}"
            if self.skland.nickname:
                role_info += f" 昵称={self.skland.nickname}"
            if self.skland.channel_name:
                role_info += f" 渠道={self.skland.channel_name}"
            msg = f"{role_info}\n当前理智: {status['ap_realtime']}/{status['max_ap']}"

            eta = status.get("ap_full_eta")
            if eta and status["ap_realtime"] < status["max_ap"]:
                hours = eta // 3600
                mins = (eta % 3600) // 60
                msg += f"\n预计满理智: {hours}小时{mins}分"

            if status["ap_realtime"] != status["ap"]:
                msg += f"\n(接口快照={status['ap']}，已按恢复时间推算实时值)"
            yield event.plain_result(msg)
        except Exception as e:
            logger.error(f"Error checking sanity: {e}")
            yield event.plain_result("查询理智时发生错误，请查看日志。")

    async def check_sanity_loop(self):
        """轮询状态；当状态进入“满理智”时触发提醒。"""
        while True:
            try:
                await self.reload_config()
                if self.config.get("token") and self.config.get("notify_users"):
                    self.skland.set_cred_token(self.config.get("token", ""))
                    self.skland.set_preferred_uid(self.config.get("preferred_uid", ""))

                    data = await self.skland.get_player_info()
                    if data.get("code") == 0:
                        status = extract_status(data)
                        if not status:
                            logger.error(
                                "Skland loop parse failed, invalid payload: %s",
                                json.dumps(data, ensure_ascii=False)[:600],
                            )
                            await asyncio.sleep(
                                self._normalize_check_interval(
                                    self.config.get("check_interval", 600)
                                )
                            )
                            continue

                        reminder_state = evaluate_reminder_state(status, self.reminded)
                        ap = status["ap_realtime"]
                        max_ap = status["max_ap"]

                        if reminder_state["should_notify"]:
                            sent, failed = await self._send_full_sanity_reminder(
                                ap=ap, max_ap=max_ap
                            )
                            # 仅当至少一个目标发送成功时才置 reminded。
                            # 若本轮全部失败，后续轮询会继续重试。
                            await self._set_reminded_state(sent > 0)
                            if sent == 0 and failed > 0:
                                logger.warning(
                                    "full sanity reminder not delivered to any target, will retry next cycle"
                                )
                        elif reminder_state["should_reset"]:
                            await self._set_reminded_state(False)
                    else:
                        logger.warning(
                            "check_sanity_loop skipped reminder due to API error: code=%s message=%s",
                            data.get("code"),
                            data.get("message"),
                        )
            except asyncio.CancelledError:
                logger.info("check_sanity_loop cancelled")
                raise
            except Exception as e:
                logger.error(f"Error in check_sanity_loop: {e}")

            await asyncio.sleep(
                self._normalize_check_interval(self.config.get("check_interval", 600))
            )

    async def terminate(self):
        if self.check_task:
            self.check_task.cancel()
            try:
                await self.check_task
            except asyncio.CancelledError:
                pass
        await self.skland.close()
