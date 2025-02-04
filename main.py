# encoding:utf-8

import asyncio
import json
import os
import time
import sqlite3
import requests
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor
import base64
from io import BytesIO
from PIL import Image

import plugins
from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from channel.chat_channel import check_contain, check_prefix
from channel.chat_message import ChatMessage
from common.log import logger
from plugins import *


@plugins.register(
    name="Summary",
    desire_priority=10,
    hidden=False,
    enabled=True,
    desc="聊天记录总结助手",
    version="1.2",
    author="lanvent",
)
class Summary(Plugin):
    # 默认配置值
    open_ai_api_base = "https://api.openai.com/v1"
    open_ai_model = "gpt-4o-mini"
    summary_max_tokens = 2000
    input_max_tokens_limit = 8000  # 默认限制输入 8000 个 token
    default_summary_prompt = '''
**核心规则：**
1. **指令优先级：**
    *   **最高优先级：** 用户特定指令:{custom_prompt} **，如果涉及总结可以参考总结的规则，否则只遵循用户特定指令执行。
    *   **次优先级：** 在指令为无时，执行默认的总结操作。

2.  **默认总结规则（仅在满足次优先级条件时执行）：**
    *   做群聊总结和摘要，主次层次分明；
    *   尽量突出重要内容以及关键信息（重要的关键字/数据/观点/结论等），请表达呈现出来，避免过于简略而丢失信息量；
    *   允许有多个主题/话题，分开描述；
    *   弱化非关键发言人的对话内容。
    *   如果把多个小话题合并成1个话题能更完整的体现对话内容，可以考虑合并，否则不合并；
    *   主题总数量不设限制，确实多就多列。
    *   格式：
        1️⃣[Topic][热度(用1-5个🔥表示)]
        • 时间：月-日 时:分 - -日 时:分(不显示年)
        • 参与者：
        • 内容：
        • 结论：
    ………

聊天记录格式：
[x]是emoji表情或者是对图片和声音文件的说明，消息最后出现<T>表示消息触发了群聊机器人的回复，内容通常是提问，若带有特殊符号如#和$则是触发你无法感知的某个插件功能，聊天记录中不包含你对这类消息的回复，可降低这些消息的权重。请不要在回复中包含聊天记录格式中出现的符号。

'''
    default_image_prompt = """
尽可能简单简要描述这张图片的客观内容，抓住整体和关键信息，但不做概述，不做评论，限制在100字以内.
如果是股票类截图，重点抓住主体股票名，关键的时间和当前价格，不关注其他细分价格和指数；
如果是文字截图，只关注文字内容，不用描述图的颜色颜色等；
如果图中有划线，画圈等，要注意这可能是表达的重点信息。
            """
    #新增的多模态LLM配置
    multimodal_llm_api_base = ""
    multimodal_llm_model = ""
    multimodal_llm_api_key = ""

    def __init__(self):
        super().__init__()
        try:
            self.config = self._load_config()
            # 加载配置，使用默认值
            self.open_ai_api_base = self.config.get("open_ai_api_base", self.open_ai_api_base)
            self.open_ai_api_key = self.config.get("open_ai_api_key", "")
            
            # 验证 API 密钥
            if not self.open_ai_api_key:
                logger.error("[Summary] OpenAI API 密钥未在配置中找到")
                raise Exception("OpenAI API 密钥未配置")
                
            self.open_ai_model = self.config.get("open_ai_model", self.open_ai_model)
            # 修改变量名
            self.summary_max_tokens = self.config.get("max_tokens", self.summary_max_tokens)
            self.input_max_tokens_limit = self.config.get("max_input_tokens", self.input_max_tokens_limit)

            #加载提示词，优先读取配置，否则用默认的
            self.default_summary_prompt = self.config.get("default_summary_prompt", self.default_summary_prompt)
            self.default_image_prompt = self.config.get("default_image_prompt", self.default_image_prompt)
            # 新增 chunk_max_tokens 从 config 加载，默认值是 3600
            #self.chunk_max_tokens = self.config.get("max_tokens_persession", 3600)

            #加载多模态LLM配置
            self.multimodal_llm_api_base = self.config.get("multimodal_llm_api_base", "")
            self.multimodal_llm_model = self.config.get("multimodal_llm_model", "")
            self.multimodal_llm_api_key = self.config.get("multimodal_llm_api_key", "")
            
             # 验证多模态LLM配置
            if self.multimodal_llm_api_base and not self.multimodal_llm_api_key :
                logger.error("[Summary] 多模态LLM API 密钥未在配置中找到")
                raise Exception("多模态LLM API 密钥未配置")


            # 初始化数据库
            curdir = os.path.dirname(__file__)
            db_path = os.path.join(curdir, "chat.db")
            self.conn = sqlite3.connect(db_path, check_same_thread=False)
            self._init_database()

             # 初始化线程池
            self.executor = ThreadPoolExecutor(max_workers=5) #你可以根据实际情况调整线程池大小

            # 注册事件处理器
            self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context
            self.handlers[Event.ON_RECEIVE_MESSAGE] = self.on_receive_message
            logger.info("[Summary] 初始化完成，配置: %s", self.config)
        except Exception as e:
            logger.error(f"[Summary] 初始化失败: {e}")
            raise e

    def _init_database(self):
        """初始化数据库架构"""
        c = self.conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS chat_records
                    (sessionid TEXT, msgid INTEGER, user TEXT, content TEXT, type TEXT, timestamp INTEGER, is_triggered INTEGER,
                    PRIMARY KEY (sessionid, msgid))''')
        
        # 检查 is_triggered 列是否存在
        c = c.execute("PRAGMA table_info(chat_records);")
        column_exists = False
        for column in c.fetchall():
            if column[1] == 'is_triggered':
                column_exists = True
                break
        if not column_exists:
            self.conn.execute("ALTER TABLE chat_records ADD COLUMN is_triggered INTEGER DEFAULT 0;")
            self.conn.execute("UPDATE chat_records SET is_triggered = 0;")
        self.conn.commit()

    def _load_config(self):
        """从 config.json 加载配置"""
        try:
            config_path = os.path.join(os.path.dirname(__file__), "config.json")
            if not os.path.exists(config_path):
                return {}
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"[Summary] 加载配置失败: {e}")
            return {}

    def _get_openai_chat_url(self):
        """获取 OpenAI 聊天补全 API URL"""
        return f"{self.open_ai_api_base}/chat/completions"

    def _get_openai_headers(self):
        """获取 OpenAI API 请求头"""
        return {
            'Authorization': f"Bearer {self.open_ai_api_key}",
            'Host': urlparse(self.open_ai_api_base).netloc,
            'Content-Type': 'application/json'
        }
    
    def _get_multimodal_llm_headers(self):
        """获取多模态LLM API 请求头"""
        return {
            'Authorization': f"Bearer {self.multimodal_llm_api_key}",
            'Host': urlparse(self.multimodal_llm_api_base).netloc,
            'Content-Type': 'application/json'
        }

    def _get_openai_payload(self, content):
        """准备 OpenAI API 请求载荷"""
        messages = [{"role": "user", "content": content}]
        return {
            'model': self.open_ai_model,
            'messages': messages,
            'max_tokens': self.summary_max_tokens #修改变量名
        }

    def _chat_completion(self, content, custom_prompt=None, prompt_type="summary"):
        """
        调用 OpenAI 聊天补全 API
        
        :param content: 需要总结的聊天内容
        :param custom_prompt: 可选的自定义 prompt，用于替换默认 prompt
        :param prompt_type:  定义使用哪一个类型的prompt，可选值 summary，image
        :return: 总结后的文本
        """
        try:
            # 使用默认 prompt
            if prompt_type == "summary":
              prompt_to_use = self.default_summary_prompt
            elif prompt_type == "image":
                prompt_to_use = self.default_image_prompt
            else:
                prompt_to_use = self.default_summary_prompt #默认选择 summary 类型
            # 使用 custom_prompt，如果 custom_prompt 为空，则替换为 "无"
            replacement_prompt = custom_prompt if custom_prompt else "无"
            prompt_to_use = prompt_to_use.replace("{custom_prompt}", replacement_prompt)

            
            # 增加日志：打印完整提示词
            logger.info(f"[Summary] 完整提示词: {prompt_to_use}")
            
            # 准备完整的载荷
            payload = {
                "model": self.open_ai_model,
                "messages": [
                    {"role": "system", "content": prompt_to_use},
                    {"role": "user", "content": content}
                ],
                "max_tokens": self.summary_max_tokens #修改变量名
            }
            
            # 获取 OpenAI API URL 和请求头
            url = self._get_openai_chat_url()
            headers = self._get_openai_headers()
            
            # 发送 API 请求
            response = requests.post(url, headers=headers, json=payload)
            
            # 检查并处理响应
            if response.status_code == 200:
                result = response.json()
                summary = result['choices'][0]['message']['content'].strip()
                return summary
            else:
                logger.error(f"[Summary] OpenAI API 错误: {response.text}")
                return f"总结失败：{response.text}"
        
        except Exception as e:
            logger.error(f"[Summary] 总结生成失败: {e}")
            return f"总结失败：{str(e)}"
    
    def _multimodal_completion(self, api_key, image_path, text_prompt, model="GLM-4V-Flash", detail="low"):
        """
        调用多模态 API 进行图片理解和文本生成。
        """

        api_url = f"{self.multimodal_llm_api_base}/chat/completions" # 从配置项读取并拼接 URL
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Host": urlparse(self.multimodal_llm_api_base).netloc # 从配置项读取，并解析host
        }

        try:
            # 1. 读取图片并进行 base64 编码
            with open(image_path, "rb") as image_file:
                encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
            image_url_data = f"data:image/jpeg;base64,{encoded_string}"


            # 2. 构建 JSON Payload
            payload = {
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": image_url_data,
                                    "detail": detail
                                }
                            },
                            {
                                "type": "text",
                                "text": text_prompt
                            }
                        ]
                    }
                ]
            }

            # 3. 发送请求并处理响应
            response = requests.post(api_url, headers=headers, json=payload)
            response.raise_for_status()  # 检查 HTTP 错误

            json_response = response.json()

            # 4. 提取文本回复
            if 'choices' in json_response and json_response['choices']:
                return json_response['choices'][0]['message']['content']
            else:
                print(f"API 响应中没有找到文本回复: {json_response}")
                return None


        except requests.exceptions.RequestException as e:
            print(f"请求 API 发生错误: {e}")
            return None
        except json.JSONDecodeError as e:
            print(f"JSON 解析错误: {e}")
            return None
        except FileNotFoundError as e:
            print(f"图片文件找不到: {e}")
            return None
        except Exception as e:
            print(f"发生未知错误: {e}")
            return None


    def _resize_and_encode_image(self, image_path):
        """将图片调整大小并编码为 base64"""
        try:
            img = Image.open(image_path)
            
            # 将图片转换为 RGB 模式，去除 alpha 通道
            if img.mode == 'RGBA':
                img = img.convert('RGB')

            max_size = (2048, 2048)
            img.thumbnail(max_size)

            # 检查图片大小，如果超过 1M 就尝试降低质量
            if os.path.getsize(image_path) > 1 * 1024 * 1024:
                buffer = BytesIO()
                img.save(buffer, format="JPEG", quality=80)  # 降低质量
                base64_str = base64.b64encode(buffer.getvalue()).decode('utf-8')
                if len(base64_str) * 3 / 4 / 1024 / 1024 > 1: #评估base64后的图片大小是否超过1M，是的话直接放弃
                   return None
                return base64_str
            else:
                buffer = BytesIO()
                img.save(buffer, format="JPEG")
                return base64.b64encode(buffer.getvalue()).decode('utf-8')
        except Exception as e:
            logger.error(f"[Summary] 图片处理失败: {e}")
            return None

    def _insert_record(self, session_id, msg_id, user, content, msg_type, timestamp, is_triggered = 0):
        """将记录插入到数据库"""
        c = self.conn.cursor()
        logger.debug("[Summary] 插入记录: {} {} {} {} {} {} {}" .format(session_id, msg_id, user, content, msg_type, timestamp, is_triggered))
        c.execute("INSERT OR REPLACE INTO chat_records VALUES (?,?,?,?,?,?,?)", (session_id, msg_id, user, content, msg_type, timestamp, is_triggered))
        self.conn.commit()
    
    def _get_records(self, session_id, start_timestamp=0, limit=9999):
        """从数据库获取记录"""
        c = self.conn.cursor()
        c.execute("SELECT * FROM chat_records WHERE sessionid=? and timestamp>? ORDER BY timestamp DESC LIMIT ?", (session_id, start_timestamp, limit))
        return c.fetchall()

    def on_receive_message(self, e_context: EventContext):
        """处理接收到的消息"""
        context = e_context['context']
        cmsg : ChatMessage = e_context['context']['msg']
        username = None
        session_id = cmsg.from_user_id
        if self.config.get('channel_type', 'wx') == 'wx' and cmsg.from_user_nickname is not None:
            session_id = cmsg.from_user_nickname

        if context.get("isgroup", False):
            username = cmsg.actual_user_nickname
            if username is None:
                username = cmsg.actual_user_id
        else:
            username = cmsg.from_user_nickname
            if username is None:
                username = cmsg.from_user_id

        is_triggered = False
        content = context.content
        if context.get("isgroup", False):
            match_prefix = check_prefix(content, self.config.get('group_chat_prefix'))
            match_contain = check_contain(content, self.config.get('group_chat_keyword'))
            if match_prefix is not None or match_contain is not None:
                is_triggered = True
            if context['msg'].is_at and not self.config.get("group_at_off", False):
                is_triggered = True
        else:
            match_prefix = check_prefix(content, self.config.get('single_chat_prefix',['']))
            if match_prefix is not None:
                is_triggered = True

        self._insert_record(session_id, cmsg.msg_id, username, content, str(context.type), cmsg.create_time, int(is_triggered))
        logger.debug("[Summary] {}:{} ({})" .format(username, context.content, session_id))
        
        # 处理图片消息
        if context.type == ContextType.IMAGE and self.multimodal_llm_api_base and self.multimodal_llm_model and self.multimodal_llm_api_key:
            context.get("msg").prepare()
            image_path = context.content  # 假设 context.content 是图片本地路径
            self._process_image_async(session_id, cmsg.msg_id, username, image_path, cmsg.create_time)


    def _process_image_async(self, session_id, msg_id, username, image_path, create_time):
        """使用线程池异步处理图片消息"""
        future = self.executor.submit(self._process_image, session_id, msg_id, username, image_path, create_time)
        future.add_done_callback(self._handle_image_result)

    def _process_image(self, session_id, msg_id, username, image_path, create_time):
        """处理图片消息，调用多模态LLM API"""
        try:
            base64_image = self._resize_and_encode_image(image_path)
            if not base64_image:
                    error_msg = "图片处理失败：无法处理或图片太大"
                    logger.error(f"[Summary] {error_msg}")
                    return error_msg #返回错误信息

            text_content = self._multimodal_completion(self.multimodal_llm_api_key, image_path, self.default_image_prompt, model=self.multimodal_llm_model)

            if text_content is None:
                    error_msg = "识图失败：多模态LLM API返回为空"
                    logger.error(f"[Summary] {error_msg}")
                    return error_msg #返回错误信息
            elif text_content.startswith("图片转文字失败"):
                    error_msg = f"识图失败：{text_content}"
                    logger.error(f"[Summary] {error_msg}")
                    return error_msg #返回错误信息
            else:
                    # 将识别出的文本内容保存到数据库
                    self._insert_record(session_id, msg_id, username, f"[图片描述]{text_content}", str(ContextType.TEXT), create_time, 0) # 这里默认识别内容没有触发
                    return True # 返回 True 表示成功
        except Exception as e:
            error_msg = f"识图失败：未知错误 {str(e)}"
            logger.error(f"[Summary] {error_msg}")
            return error_msg #返回错误信息

    def _handle_image_result(self, future):
        try:
            result = future.result()
            if result is None:  # 检查 result 是否为 None
                logger.error("[Summary] 异步图片处理结果为空")
                print("[Summary] 异步图片处理结果为空")  # 添加打印到控制台的逻辑
                return # 处理返回None的情况
            elif isinstance(result, str) and (result.startswith("识图失败") or result.startswith("图片处理失败")):  # 确保返回的是字符串
                logger.error(f"[Summary] 异步图片处理失败：{result}")
                print(f"[Summary] 异步图片处理失败：{result}")  # 添加打印到控制台的逻辑
            elif result is True:
                logger.info("[Summary] 异步图片处理成功")
                print("[Summary] 异步图片处理成功")
        except Exception as e:
            logger.error(f"[Summary] 异步处理结果错误：{e}")
            print(f"[Summary] 异步处理结果错误：{e}")  # 添加打印到控制台的逻辑

    def _check_tokens(self, records, max_tokens=None):  # 添加默认值
        """准备用于总结的聊天内容"""
        messages = []
        total_length = 0
        # 修改变量名
        max_input_chars = self.input_max_tokens_limit * 4  # 粗略估计：1个 token 约等于 4 个字符

        # 记录已经是倒序的（最新的在前），直接处理
        for record in records:
            username = record[2] or ""  # 处理空用户名
            content = record[3] or ""   # 处理空内容
            timestamp = record[5]
            is_triggered = record[6]

            # 将时间戳转换为可读格式
            time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))

            if record[4] in [str(ContextType.IMAGE), str(ContextType.VOICE)]:
                content = f"[{record[4]}]"

            sentence = f'[{time_str}] {username}: "{content}"'
            if is_triggered:
                sentence += " <T>"

            # 检查添加此记录后是否会超出限制
            if total_length + len(sentence) + 2 > self.input_max_tokens_limit * 4:  # 2 是换行符的长度
                logger.info(f"[Summary] 输入长度限制已达到 {total_length} 个字符")
                break
            messages.append(sentence)
            total_length += len(sentence) + 2

        # 将消息按时间顺序拼接（从早到晚）
        query = "\n\n".join(messages[::-1])
        return query

    def _split_messages_to_summarys(self, records, custom_prompt="", max_summarys=10):
        """将消息分割成块并总结每个块"""
        summarys = []
        query = self._check_tokens(records)
        if query:
            try:
                result = self._chat_completion(query, custom_prompt, prompt_type="summary")
                summarys.append(result)
            except Exception as e:
                logger.error(f"[Summary] 总结失败: {e}")
        return summarys

    def _parse_summary_command(self, command_parts):
        """
        解析总结命令，支持以下格式：
        $总结 100                   # 最近100条消息
        $总结 -7200 100             # 过去2小时内的消息，最多100条
        $总结 -86400                # 过去24小时内的消息
        $总结 100 自定义指令         # 最近100条消息，使用自定义指令
        $总结 -7200 100 自定义指令   # 过去2小时内的消息，最多100条，使用自定义指令
        """
        current_time = int(time.time())
        custom_prompt = ""  # 初始化为空字符串
        start_timestamp = 0
        limit = 9999  # 默认最大消息数

        # 处理时间戳和消息数量
        for part in command_parts:
            if part.startswith('-') and part[1:].isdigit():
                # 负数时间戳：表示从过去多少秒开始
                start_timestamp = current_time + int(part)
            elif part.isdigit():
                # 如果是正整数，判断是消息数量还是时间戳
                if int(part) > 1000:  # 假设大于1000的数字被视为时间戳
                    start_timestamp = int(part)
                else:
                    limit = int(part)
            else:
                # 非数字部分被视为自定义指令
                custom_prompt += part + " "

        custom_prompt = custom_prompt.strip()
        return start_timestamp, limit, custom_prompt

    def on_handle_context(self, e_context: EventContext):
        """处理上下文，进行总结"""
        content = e_context['context'].content
        logger.debug("[Summary] on_handle_context. content: %s" % content)
        trigger_prefix = self.config.get('plugin_trigger_prefix', "$")
        clist = content.split()
        if clist[0].startswith(trigger_prefix):
            
            # 解析命令
            start_time, limit, custom_prompt = self._parse_summary_command(clist[1:])


            msg:ChatMessage = e_context['context']['msg']
            session_id = msg.from_user_id
            if self.config.get('channel_type', 'wx') == 'wx' and msg.from_user_nickname is not None:
                session_id = msg.from_user_nickname
            records = self._get_records(session_id, start_time, limit)
            
            if not records:
                reply = Reply(ReplyType.ERROR, "没有找到聊天记录")
                e_context["reply"] = reply
                e_context.action = EventAction.BREAK_PASS
                return
            
            summarys = self._split_messages_to_summarys(records, custom_prompt)
            if not summarys:
                reply = Reply(ReplyType.ERROR, "总结失败")
                e_context["reply"] = reply
                e_context.action = EventAction.BREAK_PASS
                return
            
            result = "\n\n".join(summarys)
            reply = Reply(ReplyType.TEXT, result)
            e_context["reply"] = reply
            e_context.action = EventAction.BREAK_PASS

    def get_help_text(self, verbose = False, **kwargs):
        help_text = "聊天记录总结插件。\n"
        if not verbose:
            return help_text
        trigger_prefix = self.config.get('plugin_trigger_prefix', "$")
        help_text += f"使用方法:输入\"{trigger_prefix}总结 最近消息数量\"，我会帮助你总结聊天记录。\n例如：\"{trigger_prefix}总结 100\"，我会总结最近100条消息。\n\n你也可以直接输入\"{trigger_prefix}总结前99条信息\"或\"{trigger_prefix}总结3小时内的最近10条消息\"\n我会尽可能理解你的指令。"
        return help_text
