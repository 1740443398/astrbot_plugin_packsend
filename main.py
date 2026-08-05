from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Node, Plain
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_packsend"


@register(
    PLUGIN_NAME,
    "YourName",
    "QQ群消息合并转发插件 - 按群独立设置Bot单次发送消息长度上限，超限自动转为合并转发",
    "1.1.0",
    "https://github.com/1740443398/astrbot_plugin_packsend",
)
class PackSendPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

    async def initialize(self):
        """插件初始化完成后调用"""
        default_limit = self._safe_get_int(self.config.get("default_limit"), -1)
        logger.info(f"[{PLUGIN_NAME}] 插件已初始化，默认发送上限: {default_limit}")

    def _safe_get_int(self, value, default: int) -> int:
        """安全地将值转换为整数，转换失败时返回默认值"""
        try:
            return int(value)
        except (ValueError, TypeError):
            return default

    def _get_group_limit(self, group_id: str) -> int:
        """
        获取指定群聊的 Bot 单次发送消息长度上限

        优先级：群独立配置 > 默认配置

        Args:
            group_id: 群号

        Returns:
            int: Bot 单次发送消息长度上限
                -1: 不启用此功能
                 0: 始终使用合并转发
                >0: Bot 单次发送消息的 Plain 文本长度达到该值时转为合并转发
        """
        group_limits = self.config.get("group_limits", {})
        if isinstance(group_limits, dict) and group_id in group_limits:
            limit = self._safe_get_int(group_limits[group_id], -1)
            if limit != -1:
                return limit
        return self._safe_get_int(self.config.get("default_limit"), -1)

    @staticmethod
    def _count_plain_text_length(chain: list) -> int:
        """计算消息链中 Plain 文本的总长度"""
        total = 0
        for comp in chain:
            if isinstance(comp, Plain):
                total += len(comp.text)
        return total

    # 指令组: /pack
    @filter.command_group("pack")
    def pack(self):
        pass

    @pack.command("rate")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def pack_rate(self, event: AstrMessageEvent, limit: int):
        """
        管理员指令: /pack rate <数字>

        设置当前群聊的 Bot 单次发送消息长度上限。
        -1: 不启用此功能
        0: 该群所有消息均以合并转发形式发送
        >0: Bot 单次发送消息的文本长度达到该值时转为合并转发
        """
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

            yield event.plain_result(
                f"群 {group_id} 的消息合并转发上限已设置为 {limit}（{desc}）"
            )
            logger.info(f"[{PLUGIN_NAME}] 管理员设置群 {group_id} 上限为 {limit}")

        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] /pack rate 指令执行失败: {e}", exc_info=True)
            yield event.plain_result(f"设置失败: {e}")

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """
        发送消息前拦截，检查 Bot 单次发送的消息是否需要转为QQ合并转发

        处理逻辑：
        1. 仅在 aiocqhttp 平台的群聊消息中生效
        2. 根据群聊配置的 Bot 单次发送消息长度上限决定是否转换
        3. 上限为 -1：不启用，消息正常发送
        4. 上限为 0：始终使用合并转发
        5. 上限 > 0：Bot 单次发送消息的 Plain 文本总长度达到上限时转为合并转发
        """
        try:
            # 仅在 aiocqhttp 平台生效
            if event.get_platform_name() != "aiocqhttp":
                return

            # 获取群号，非群聊消息不处理
            group_id = (
                str(event.message_obj.group_id) if event.message_obj.group_id else ""
            )
            if not group_id:
                return

            # 获取该群的发送上限
            limit = self._get_group_limit(group_id)

            # -1 表示不启用此功能，直接放行
            if limit == -1:
                return

            # 获取原始消息链
            result = event.get_result()
            if result is None or not result.chain:
                return

            chain = result.chain

            # 计算 Bot 单次发送消息中 Plain 文本的总长度
            text_length = self._count_plain_text_length(chain)

            # 如果 limit > 0 且文本长度未达到上限，则不处理
            if limit > 0 and text_length < limit:
                return

            # 创建合并转发节点
            node = Node(
                uin=event.get_self_id(),
                name="AstrBot",
                content=[*chain],
            )

            # 替换消息链为合并转发节点
            result.chain = [node]

            logger.info(
                f"[{PLUGIN_NAME}] 群 {group_id} Bot 单次发送消息文本长度 {text_length} "
                f"达到上限 {limit}，已转为合并转发消息"
            )

        except Exception as e:
            # 出错时不拦截，让消息正常发送，避免影响正常聊天功能
            logger.error(f"[{PLUGIN_NAME}] 处理消息时发生错误: {e}", exc_info=True)

    async def terminate(self):
        """插件卸载/停用时调用"""
        logger.info(f"[{PLUGIN_NAME}] 插件已卸载")
