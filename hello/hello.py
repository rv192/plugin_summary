# encoding:utf-8

import plugins
from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from channel.chat_message import ChatMessage
from common.log import logger
from plugins import *
from config import conf
import json
import os
import datetime

@plugins.register(
    name="Hello",
    desire_priority=-1,
    hidden=True,
    desc="A simple plugin that says hello",
    version="0.3",
    author="lanvent",
)
class Hello(Plugin):
    hi_prompt = "你好！"
    group_welc_prompt = "请你随机使用一种风格说一句问候语来欢迎新用户\"{nickname}\"加入群聊。"
    group_exit_prompt = "请你随机使用一种风格介绍你自己，并告诉用户输入#help可以查看帮助信息。"
    patpat_prompt = "请你随机使用一种风格跟其他群用户说他违反规则\"{nickname}\"退出群聊。"

    def __init__(self):
        super().__init__()
        try:
            self.config = super().load_config()
            if not self.config:
                self.config = self._load_config_template()
            self.group_welc_fixed_msg = self.config.get("group_welc_fixed_msg", {})
            self.group_welc_prompt = self.config.get("group_welc_prompt", self.group_welc_prompt)
            self.group_exit_prompt = self.config.get("group_exit_prompt", self.group_exit_prompt)
            self.patpat_prompt = self.config.get("patpat_prompt", self.patpat_prompt)
            self.hi_prompt = self.config.get("hi_prompt", self.hi_prompt)
            self.hi_keywords = [keyword.lower() for keyword in self.config.get("hi_keywords", ["hello", "hi", "你好", "哈喽"])]
            logger.info("[Hello] inited")
            self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context
        except Exception as e:
            logger.error(f"[Hello]初始化异常：{e}")
            raise "[Hello] init failed, ignore "

    def _append_time_suffix(self, prompt):
        now = datetime.datetime.now()
        time_str = now.strftime("%Y-%m-%d %H:%M:%S")
        return f"{prompt} 当前时间是{time_str}。"

    def on_handle_context(self, e_context: EventContext):
        if e_context["context"].type not in [
            ContextType.TEXT,
            ContextType.JOIN_GROUP,
            ContextType.PATPAT,
            ContextType.EXIT_GROUP
        ]:
            return
        msg: ChatMessage = e_context["context"]["msg"]
        group_name = msg.from_user_nickname

        if e_context["context"].type == ContextType.JOIN_GROUP:
            if "group_welcome_msg" in conf() or group_name in self.group_welc_fixed_msg:
                reply = Reply()
                reply.type = ReplyType.TEXT
                if group_name in self.group_welc_fixed_msg:
                    reply.content = self.group_welc_fixed_msg.get(group_name, "")
                else:
                    reply.content = conf().get("group_welcome_msg", "")
                e_context["reply"] = reply
                e_context.action = EventAction.BREAK_PASS
                return
            e_context["context"].type = ContextType.TEXT
            e_context["context"].content = self._append_time_suffix(self.group_welc_prompt.format(nickname=msg.actual_user_nickname))
            e_context.action = EventAction.BREAK
            if not self.config or not self.config.get("use_character_desc"):
                e_context["context"]["generate_breaked_by"] = EventAction.BREAK
            return

        if e_context["context"].type == ContextType.EXIT_GROUP:
            if "group_exit_msg" in conf():
                reply = Reply()
                reply.type = ReplyType.TEXT
                reply.content = conf().get("group_exit_msg", "")
                e_context["reply"] = reply
                e_context.action = EventAction.BREAK_PASS
                return
            if conf().get("group_chat_exit_group"):
                e_context["context"].type = ContextType.TEXT
                e_context["context"].content = self._append_time_suffix(self.group_exit_prompt.format(nickname=msg.actual_user_nickname))
                e_context.action = EventAction.BREAK
                return
            e_context.action = EventAction.BREAK
            return

        if e_context["context"].type == ContextType.PATPAT:
            e_context["context"].type = ContextType.TEXT
            e_context["context"].content = self._append_time_suffix(self.patpat_prompt.format(nickname=msg.actual_user_nickname))
            e_context.action = EventAction.BREAK  # 交给LLM处理
            if not self.config or not self.config.get("use_character_desc"):
                e_context["context"]["generate_breaked_by"] = EventAction.BREAK
            return

        content = e_context["context"].content
        logger.debug("[Hello] on_handle_context. content: %s" % content)

        # 新增：处理打招呼关键词
        if content.lower() in self.hi_keywords:
            reply = Reply()
            reply.type = ReplyType.TEXT
            reply.content = self.hi_prompt
            e_context["reply"] = reply
            e_context.action = EventAction.BREAK_PASS  # 拦截，不再往下处理
            return

        if content == "End":
            e_context["context"].type = ContextType.IMAGE_CREATE
            content = "The World"
            e_context.action = EventAction.CONTINUE

    def get_help_text(self, **kwargs):
        help_text = f"输入[{'|'.join(self.hi_keywords)}]，我会回复：{self.hi_prompt}\n输入End，我会回复你世界的图片\n"
        return help_text

    def _load_config_template(self):
        logger.debug("No Hello plugin config.json, use plugins/hello/config.json.template")
        try:
            plugin_config_path = os.path.join(self.path, "config.json.template")
            if os.path.exists(plugin_config_path):
                with open(plugin_config_path, "r", encoding="utf-8") as f:
                    plugin_conf = json.load(f)
                    return plugin_conf
        except Exception as e:
            logger.exception(e)
