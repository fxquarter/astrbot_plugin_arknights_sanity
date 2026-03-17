import asyncio
import json
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

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
        self.config_path = Path(__file__).with_name("config.json")
        self.config = load_config(self.config_path)
        self.skland = SklandClient(self.config.get("token", ""))
        self.check_task = None
        self._config_lock = asyncio.Lock()
        self.reminded = False

    async def initialize(self):
        self.check_task = asyncio.create_task(self.check_sanity_loop())

    def reload_config(self):
        self.config = load_config(self.config_path)

    async def persist_config(self):
        async with self._config_lock:
            save_config(self.config_path, self.config)

    def _known_platform_ids(self) -> set[str]:
        ids: set[str] = set()
        for platform in self.context.platform_manager.platform_insts:
            ids.add(str(platform.meta().id))
        return ids

    async def _prune_invalid_notify_users(self) -> list[str]:
        """清理平台适配器已不存在的会话。"""
        notify_users = [
            str(umo).strip()
            for umo in self.config.get("notify_users", [])
            if str(umo).strip()
        ]
        if not notify_users:
            return []

        platform_ids = self._known_platform_ids()
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
            self.config["notify_users"] = valid
            await self.persist_config()

        return valid

    async def _send_full_sanity_reminder(self, ap: int, max_ap: int) -> tuple[int, int]:
        """主动推送满理智提醒，并返回 (成功数, 失败数)。"""
        from astrbot.api.event import MessageChain

        notify_users = await self._prune_invalid_notify_users()
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
        self.reload_config()
        umo = event.unified_msg_origin
        msg = event.message_str.strip()
        notify_users = self.config.setdefault("notify_users", [])

        if "notify on" in msg:
            await self._prune_invalid_notify_users()
            if umo not in notify_users:
                notify_users.append(umo)
                await self.persist_config()
            yield event.plain_result("满理智提醒已开启！理智达到最大值时将提醒您。")
        elif "notify off" in msg:
            if umo in notify_users:
                notify_users.remove(umo)
                await self.persist_config()
            yield event.plain_result("满理智提醒已关闭！")
        else:
            yield event.plain_result(
                "未知指令。请使用 /ark notify on 或 /ark notify off"
            )

    @filter.command("理智")
    async def check_sanity(self, event: AstrMessageEvent):
        self.reload_config()
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
                self.reload_config()
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
                            await asyncio.sleep(self.config.get("check_interval", 600))
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
                            self.reminded = sent > 0
                            if sent == 0 and failed > 0:
                                logger.warning(
                                    "full sanity reminder not delivered to any target, will retry next cycle"
                                )
                        elif reminder_state["should_reset"]:
                            self.reminded = False
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

            await asyncio.sleep(self.config.get("check_interval", 600))

    async def terminate(self):
        if self.check_task:
            self.check_task.cancel()
            try:
                await self.check_task
            except asyncio.CancelledError:
                pass
        await self.skland.close()
