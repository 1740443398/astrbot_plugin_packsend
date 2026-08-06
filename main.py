import asyncio

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Node, Nodes, Plain
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_packsend"
PLUGIN_PRIORITY = 100


@register(
    PLUGIN_NAME,
    "YourName",
    "QQ群消息合并转发插件",
    "1.3.0",
    "https://github.com/1740443398/astrbot_plugin_packsend",
)
class PackSendPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._intercept_buffers: dict[str, dict] = {}
        self._intercept_lock = asyncio.Lock()
        self._intercept_enabled: bool = self._safe_get_bool(
            self.config.get("intercept_enabled"), True
        )
        self._intercept_duration: int = self._safe_get_int(
            self.config.get("intercept_duration"), 5
        )
        self._use_llm_intercept: bool = self._safe_get_bool(
            self.config.get("use_llm_intercept"), False
        )

    async def initialize(self):
        default_limit = self._safe_get_int(self.config.get("default_limit"), -1)
        logger.info(f"[{PLUGIN_NAME}] 插件已初始化，默认发送上限: {default_limit}")

    def _safe_get_int(self, value, default: int) -> int:
        try:
            return int(value)
        except (ValueError, TypeError):
            return default

    def _safe_get_bool(self, value, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes", "on")
        if isinstance(value, (int, float)):
            return bool(value)
        return default

    def _get_group_limit(self, group_id: str) -> int:
        group_limits = self.config.get("group_limits", {})
        if isinstance(group_limits, dict) and group_id in group_limits:
            limit = self._safe_get_int(group_limits[group_id], -1)
            if limit != -1:
                return limit
        return self._safe_get_int(self.config.get("default_limit"), -1)

    @staticmethod
    def _count_plain_text_length(chain: list) -> int:
        total = 0
        for comp in chain:
            if isinstance(comp, Plain):
                total += len(comp.text)
        return total

    def _should_merge_forward(self, group_id: str, chain: list) -> bool:
        limit = self._get_group_limit(group_id)
        if limit == -1:
            return False
        if limit == 0:
            return True
        text_length = self._count_plain_text_length(chain)
        return text_length >= limit

    def _wrap_as_merge_forward(self, event: AstrMessageEvent, chain: list) -> Node:
        return Node(
            uin=event.get_self_id(),
            name="AstrBot",
            content=[*chain],
        )

    async def _start_interception(
        self, group_id: str, event: AstrMessageEvent, chain: list
    ):
        umo = event.unified_msg_origin
        self_id = event.get_self_id()

        async with self._intercept_lock:
            if group_id in self._intercept_buffers:
                old_buffer = self._intercept_buffers[group_id]
                if old_buffer["timer"] and not old_buffer["timer"].done():
                    old_buffer["timer"].cancel()
                old_buffer["messages"].append(chain)
                old_buffer["umo"] = umo
                old_buffer["timer"] = None
            else:
                self._intercept_buffers[group_id] = {
                    "messages": [chain],
                    "timer": None,
                    "umo": umo,
                    "self_id": self_id,
                }

        async def _flush_after_delay():
            await asyncio.sleep(self._intercept_duration)
            if self._use_llm_intercept:
                should_extend = await self._llm_should_extend(group_id)
                if should_extend:
                    logger.info(
                        f"[{PLUGIN_NAME}] LLM 判断群 {group_id} 可能还有后续输出，延长拦截"
                    )
                    await asyncio.sleep(3)
            await self._flush_interception(group_id)

        timer = asyncio.create_task(_flush_after_delay())
        async with self._intercept_lock:
            if group_id in self._intercept_buffers:
                self._intercept_buffers[group_id]["timer"] = timer

    async def _llm_should_extend(self, group_id: str) -> bool:
        try:
            buffer = self._intercept_buffers.get(group_id)
            if not buffer or not buffer["messages"]:
                return False

            all_texts = []
            for chain in buffer["messages"]:
                for comp in chain:
                    if isinstance(comp, Plain):
                        all_texts.append(comp.text)

            if not all_texts:
                return False

            last_text = all_texts[-1].strip()
            continuation_markers = [
                "正在",
                "处理中",
                "请稍候",
                "思考中",
                "查询中",
                "加载中",
                "生成中",
                "...",
                "……",
                "接下来",
                "继续",
                "另外",
                "还有",
                "此外",
            ]
            if any(last_text.endswith(marker) for marker in continuation_markers):
                return True

        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] LLM 拦截判断失败: {e}", exc_info=True)

        return False

    async def _flush_interception(self, group_id: str):
        async with self._intercept_lock:
            buffer = self._intercept_buffers.pop(group_id, None)

        if not buffer or not buffer["messages"]:
            return

        messages = buffer["messages"]
        umo = buffer["umo"]
        self_id = buffer.get("self_id", "0")

        nodes = [
            Node(uin=self_id, name="AstrBot", content=[*chain]) for chain in messages
        ]

        if len(nodes) == 1:
            chain_to_send = [nodes[0]]
        else:
            chain_to_send = [Nodes(nodes=nodes)]

        try:
            await self.context.send_message(umo, MessageChain(chain=chain_to_send))
            logger.info(
                f"[{PLUGIN_NAME}] 群 {group_id} 拦截 {len(messages)} 条消息，已合并转发"
            )
        except Exception as e:
            logger.error(
                f"[{PLUGIN_NAME}] 发送拦截合并消息失败 (群 {group_id}): {e}",
                exc_info=True,
            )

    @filter.command_group("pack", priority=PLUGIN_PRIORITY)
    def pack(self):
        pass

    @pack.command("rate", priority=PLUGIN_PRIORITY)
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def pack_rate(self, event: AstrMessageEvent, limit: int):
        try:
            group_id = (
                str(event.message_obj.group_id) if event.message_obj.group_id else ""
            )
            if not group_id:
                yield event.plain_result("请在群聊中使用此指令。")
                return

            group_limits = self.config.get("group_limits", {})
            if not isinstance(group_limits, dict):
                group_limits = {}

            group_limits[group_id] = limit
            self.config["group_limits"] = group_limits
            self.config.save_config()

            if limit == -1:
                desc = "已关闭"
            elif limit == 0:
                desc = "所有消息均以合并转发形式发送"
            else:
                desc = f"Bot 单次发送消息文本长度 ≥ {limit} 时转为合并转发"

            result = event.plain_result(
                f"群 {group_id} 的消息合并转发上限已设置为 {limit}（{desc}）"
            )

            if self._should_merge_forward(group_id, result.chain):
                node = self._wrap_as_merge_forward(event, result.chain)
                result.chain = [node]

            yield result
            logger.info(f"[{PLUGIN_NAME}] 管理员设置群 {group_id} 上限为 {limit}")

        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] /pack rate 指令执行失败: {e}", exc_info=True)
            yield event.plain_result(f"设置失败: {e}")

    @filter.on_decorating_result(priority=PLUGIN_PRIORITY)
    async def on_decorating_result(self, event: AstrMessageEvent):
        try:
            if event.get_platform_name() != "aiocqhttp":
                return

            group_id = (
                str(event.message_obj.group_id) if event.message_obj.group_id else ""
            )
            if not group_id:
                return

            result = event.get_result()
            if result is None or not result.chain:
                return

            chain = result.chain

            if len(chain) == 1 and isinstance(chain[0], (Node, Nodes)):
                return

            if self._intercept_enabled:
                original_msg = event.get_message_str()
                if original_msg.startswith("/"):
                    async with self._intercept_lock:
                        is_already_intercepting = group_id in self._intercept_buffers

                    await self._start_interception(group_id, event, chain)

                    if not is_already_intercepting:
                        logger.info(
                            f"[{PLUGIN_NAME}] 检测到群 {group_id} 的 / 指令，"
                            f"开始 {self._intercept_duration}s 消息拦截"
                        )

                    result.chain = []
                    return

                async with self._intercept_lock:
                    if group_id in self._intercept_buffers:
                        self._intercept_buffers[group_id]["messages"].append(chain)
                        result.chain = []
                        return

            if not self._should_merge_forward(group_id, chain):
                return

            node = self._wrap_as_merge_forward(event, chain)
            result.chain = [node]

            text_length = self._count_plain_text_length(chain)
            limit = self._get_group_limit(group_id)
            logger.info(
                f"[{PLUGIN_NAME}] 群 {group_id} 消息文本长度 {text_length} "
                f"达到上限 {limit}，已转为合并转发"
            )

        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 处理消息时发生错误: {e}", exc_info=True)

    async def terminate(self):
        async with self._intercept_lock:
            for group_id, buffer in list(self._intercept_buffers.items()):
                if buffer["timer"] and not buffer["timer"].done():
                    buffer["timer"].cancel()
                if buffer["messages"]:
                    logger.info(
                        f"[{PLUGIN_NAME}] 插件卸载，发送群 {group_id} 的剩余 "
                        f"{len(buffer['messages'])} 条拦截消息"
                    )
                    await self._flush_interception(group_id)
            self._intercept_buffers.clear()

        logger.info(f"[{PLUGIN_NAME}] 插件已卸载")
