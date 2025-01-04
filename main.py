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
import shutil

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
    version="2.0",
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
            
            #加载多模态LLM配置
            self.multimodal_llm_api_base = self.config.get("multimodal_llm_api_base", "")
            self.multimodal_llm_model = self.config.get("multimodal_llm_model", "")
            self.multimodal_llm_api_key = self.config.get("multimodal_llm_api_key", "")
            
            # 验证多模态LLM配置
            if self.multimodal_llm_api_base and not self.multimodal_llm_api_key:
                logger.error("[Summary] 多模态LLM API 密钥未在配置中找到")
                raise Exception("多模态LLM API 密钥未配置")

            # 加载提示词，优先读取配置，否则用默认的
            config_summary_prompt = self.config.get("default_summary_prompt")
            self.default_summary_prompt = config_summary_prompt if config_summary_prompt else self.default_summary_prompt
            
            config_image_prompt = self.config.get("default_image_prompt")
            self.default_image_prompt = config_image_prompt if config_image_prompt else self.default_image_prompt

            # 加载其他配置
            self.summary_max_tokens = self.config.get("summary_max_tokens", 8000)
            self.input_max_tokens_limit = self.config.get("input_max_tokens_limit", 160000)
            self.chunk_max_tokens = self.config.get("chunk_max_tokens", 16000)
            
            # 初始化数据库
            curdir = os.path.dirname(__file__)
            db_path = os.path.join(curdir, "chat.db")
            self.conn = sqlite3.connect(db_path, check_same_thread=False)
            self._init_database()

            # 初始化线程池
            self.executor = ThreadPoolExecutor(max_workers=5)
            self.pending_tasks = 0
            self.max_pending_tasks = 20

            # 注册事件处理器
            self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context
            self.handlers[Event.ON_RECEIVE_MESSAGE] = self.on_receive_message
            logger.info("[Summary] 初始化完成，配置: %s", self.config)

            # 添加用户ID到昵称的缓存字典
            self.user_nickname_cache = {}
            self.group_name_cache = {}
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
            # 首先加载插件自己的配置
            config_path = os.path.join(os.path.dirname(__file__), "config.json")
            config = {}
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
            
            # 加载主配置文件
            main_config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "config.json")
            if os.path.exists(main_config_path):
                with open(main_config_path, "r", encoding="utf-8") as f:
                    main_config = json.load(f)
                    # 将主配置中的gewechat相关配置映射到插件配置中
                    config['api_base_url'] = main_config.get('gewechat_base_url')
                    config['api_token'] = main_config.get('gewechat_token')
                    config['app_id'] = main_config.get('gewechat_app_id')
            
            return config
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

    def _chat_completion(self, content, e_context, custom_prompt=None, prompt_type="summary"):
        """
        准备总结提示词并传递给下一个插件处理
        
        :param content: 需要总结的聊天内容
        :param e_context: 事件上下文
        :param custom_prompt: 可选的自定义 prompt
        :param prompt_type: 定义使用哪一个类型的prompt，可选值 summary，image
        :return: None，由下一个插件处理
        """
        try:
            # 使用默认 prompt
            if prompt_type == "summary":
                prompt_to_use = self.default_summary_prompt
            elif prompt_type == "image":
                prompt_to_use = self.default_image_prompt
            else:
                prompt_to_use = self.default_summary_prompt  # 默认选择 summary 类型

            # 使用 custom_prompt，如果 custom_prompt 为空，则替换为 "无"
            replacement_prompt = custom_prompt if custom_prompt else "无"
            prompt_to_use = prompt_to_use.replace("{custom_prompt}", replacement_prompt)
            
            # 构造完整的提示词
            full_prompt = f"{prompt_to_use}\n\n'''{content}'''"
            
            # 修改 context 内容，传递给下一个插件处理
            e_context['context'].type = ContextType.TEXT
            e_context['context'].content = full_prompt
            
            # 继续传递给下一个插件处理
            e_context.action = EventAction.CONTINUE
            logger.debug(f"[Summary] 传递内容给下一个插件处理: length={len(full_prompt)}")
            return
            
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

            # 添加详细的错误日志
            if response.status_code != 200:
                logger.error(f"[Summary] API 请求失败: 状态码 {response.status_code}")
                logger.error(f"[Summary] 响应内容: {response.text}")
                return None

            json_response = response.json()
            logger.debug(f"[Summary] API 响应: {json_response}")  # 添加调试日志

            # 4. 提取文本回复
            if 'choices' in json_response and json_response['choices']:
                return json_response['choices'][0]['message']['content']
            else:
                logger.error(f"[Summary] API 响应中没有找到文本回复: {json_response}")
                return None

        except requests.exceptions.RequestException as e:
            logger.error(f"[Summary] 请求 API 发生错误: {e}")
            logger.error(f"[Summary] 请求 URL: {api_url}")
            logger.error(f"[Summary] 请求头: {headers}")
            logger.error(f"[Summary] 请求体: {payload}")
            return None
        except json.JSONDecodeError as e:
            logger.error(f"[Summary] JSON 解析错误: {e}")
            logger.error(f"[Summary] 响应内容: {response.text}")
            return None
        except FileNotFoundError as e:
            logger.error(f"[Summary] 图片文件找不到: {e}")
            return None
        except Exception as e:
            logger.error(f"[Summary] 发生未知错误: {e}")
            logger.error(f"[Summary] 错误类型: {type(e)}")
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

    def _get_user_nickname(self, user_id):
        """获取用户昵称"""
        if user_id in self.user_nickname_cache:
            return self.user_nickname_cache[user_id]
        
        try:
            # 调用API获取用户信息
            response = requests.post(
                f"{self.config.get('api_base_url')}/contacts/getBriefInfo",
                headers={
                    "X-GEWE-TOKEN": self.config.get('api_token')
                },
                json={
                    "appId": self.config.get('app_id'),
                    "wxids": [user_id]
                }
            )
            
            if response.status_code == 200:
                data = response.json()
                if data.get('ret') == 200 and data.get('data'):
                    nickname = data['data'][0].get('nickName', user_id)
                    self.user_nickname_cache[user_id] = nickname
                    return nickname
        except Exception as e:
            logger.error(f"[Summary] 获取用户昵称失败: {e}")
        
        return user_id

    def _get_group_name(self, group_id):
        """获取群名称"""
        # 检查缓存
        if group_id in self.group_name_cache:
            logger.debug(f"[Summary] 从缓存获取群名称: {group_id} -> {self.group_name_cache[group_id]}")
            return self.group_name_cache[group_id]
        
        try:
            # 调用群信息API
            api_url = f"{self.config.get('api_base_url')}/group/getChatroomInfo"
            payload = {
                "appId": self.config.get('app_id'),
                "chatroomId": group_id
            }
            headers = {
                "X-GEWE-TOKEN": self.config.get('api_token')
            }
            
            response = requests.post(api_url, headers=headers, json=payload)
            
            if response.status_code == 200:
                data = response.json()
                if data.get('ret') == 200 and data.get('data'):
                    group_info = data['data']
                    group_name = group_info.get('nickName')  # 使用 nickName 字段
                    
                    if group_name:
                        self.group_name_cache[group_id] = group_name
                        return group_name
                    else:
                        logger.warning(f"[Summary] API返回的群名为空 - Group ID: {group_id}")
                        return group_id
                else:
                    logger.warning(f"[Summary] API返回数据异常: {data}")
                    return group_id
        except Exception as e:
            logger.error(f"[Summary] 获取群名称失败: {e}")
            return group_id

    def on_receive_message(self, e_context: EventContext):
        """处理接收到的消息"""
        context = e_context['context']
        cmsg : ChatMessage = e_context['context']['msg']
        
        # 检查消息内容是否需要过滤
        content = context.content
        if (('#' in content or '$' in content) and len(content) < 50):
            logger.debug(f"[Summary] 消息被过滤: {content}")
            return
        
        # 获取会话ID和用户名
        if context.get("isgroup", False):
            # 群聊：使用群名作为session_id，用户昵称作为username
            session_id = self._get_group_name(cmsg.from_user_id)
            username = cmsg.actual_user_nickname or self._get_user_nickname(cmsg.actual_user_id)
            
            # 只有当content以用户ID开头且后面紧跟冒号时才清理
            if content.startswith(f"{cmsg.actual_user_id}:"):
                content = content[len(cmsg.actual_user_id) + 1:].strip()
        else:
            # 单聊：使用用户昵称作为session_id和username
            nickname = self._get_user_nickname(cmsg.from_user_id)
            session_id = nickname
            username = nickname

        is_triggered = False
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
        logger.debug("[Summary] {}:{} ({})" .format(username, content, session_id))
        
        # 处理图片消息
        if context.type == ContextType.IMAGE and self.multimodal_llm_api_base and self.multimodal_llm_model and self.multimodal_llm_api_key:
            context.get("msg").prepare()
            image_path = context.content  # 假设 context.content 是图片本地路径
            self._process_image_async(session_id, cmsg.msg_id, username, image_path, cmsg.create_time)


    def _process_image_async(self, session_id, msg_id, username, image_path, create_time):
        """使用线程池异步处理图片消息"""
        if self.pending_tasks >= self.max_pending_tasks:
            logger.warning("[Summary] 图片处理队列已满，丢弃请求")
            return
        
        self.pending_tasks += 1
        future = self.executor.submit(self._process_image, session_id, msg_id, username, image_path, create_time)
        future.add_done_callback(self._handle_image_result)
        future.add_done_callback(lambda x: setattr(self, 'pending_tasks', self.pending_tasks - 1))

    def _process_image(self, session_id, msg_id, username, image_path, create_time):
        """处理图片消息，调用多模态LLM API"""
        try:
            # 确保图片文件存在
            if not os.path.exists(image_path):
                error_msg = "图片处理失败：文件不存在"
                logger.error(f"[Summary] {error_msg}")
                return error_msg

            # 复制图片到临时文件以避免并发访问问题
            temp_image_path = f"{image_path}.{time.time()}.tmp"
            try:
                shutil.copy2(image_path, temp_image_path)
                base64_image = self._resize_and_encode_image(temp_image_path)
            finally:
                # 清理临时文件
                if os.path.exists(temp_image_path):
                    try:
                        os.remove(temp_image_path)
                    except Exception as e:
                        logger.warning(f"[Summary] 清理临时文件失败: {e}")

            if not base64_image:
                error_msg = "图片处理失败：无法处理或图片太大"
                logger.error(f"[Summary] {error_msg}")
                return error_msg

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
                    # 将识别出的文本内容保存到数据库，并记录日志
                    content = f"[图片描述]{text_content}"
                    self._insert_record(session_id, msg_id, username, content, str(ContextType.TEXT), create_time, 0)
                    logger.info(f"[Summary] 图片识别成功并保存到数据库 - 会话ID: {session_id}, 用户: {username}, 内容: {content}")
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
            
            if record[4] in [str(ContextType.IMAGE),str(ContextType.VOICE)]:
                content = f"[{record[4]}]"
            
            sentence = f'[{time_str}] {username}: "{content}"'
            if is_triggered:
                sentence += " <T>"
                
            # 检查添加此记录后是否会超出限制
            if total_length + len(sentence) + 2 > max_input_chars:  # 2 是换行符的长度
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
        count = 0

        while len(records) > 0 and len(summarys) < max_summarys:
            # 修改变量名
            query = self._check_tokens(records) # 移除 max_tokens
            if not query:
                break

            try:
                result = self._chat_completion(query, custom_prompt, prompt_type="summary")
                summarys.append(result)
                count += 1
            except Exception as e:
                logger.error(f"[Summary] 总结失败: {e}")
                break

            # 修改变量名，使用字符长度判断
            query_chars_len = len(self._check_tokens(records))
            if query_chars_len > (self.chunk_max_tokens*4):
               records_temp = self._check_tokens(records)[:(self.chunk_max_tokens*4)] # 截取字符
               
               #找到截取字符对应的记录条数
               record_count = 0
               temp_records = []
               for record in records:
                  record_content = f'[{time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record[5]))}] {record[2] or ""}: "{record[3] or ""}"'
                  if record[6]:
                        record_content += " <T>"
                  
                  if len("\n\n".join(temp_records+[record_content])) <= (self.chunk_max_tokens*4):
                    temp_records.append(record_content)
                    record_count = record_count + 1
                  else:
                      break
               
               records = records[record_count:]
            else:
                break
        return summarys

    def _parse_summary_command(self, command_parts):
        """
        解析总结命令，支持以下格式：
        $总结 100                      # 最近100条消息
        $总结 -2h 100                 # 过去2小时内的消息，最多100条
        $总结 -24h                    # 过去24小时内的消息
        $总结 100 自定义指令            # 最近100条消息，使用自定义指令
        $总结 -2h 100 自定义指令       # 过去2小时内的消息，最多100条，使用自定义指令
        $总结 @群名称 密码 100          # 指定群的最近100条消息（需要密码验证）
        $总结 @用户名 密码 -2h          # 指定用户过去2小时的消息（需要密码验证）
        """
        current_time = int(time.time())
        custom_prompt = ""
        start_timestamp = 0
        limit = 9999
        target_session = None
        password = None  # 新增：密码字段

        # 处理命令参数
        i = 0
        while i < len(command_parts):
            part = command_parts[i]
            if part.startswith('@'):
                target_session = part[1:]  # 去掉@符号
                # 检查下一个参数是否为密码
                if i + 1 < len(command_parts):
                    password = command_parts[i + 1]
                    i += 1  # 跳过密码参数
            elif part.startswith('-') and part.endswith('h'):
                try:
                    hours = int(part[1:-1])
                    start_timestamp = current_time - (hours * 3600)
                except ValueError:
                    pass
            elif part.startswith('-') and part[1:].isdigit():
                start_timestamp = current_time + int(part)
            elif part.isdigit():
                if int(part) > 1000:
                    start_timestamp = int(part)
                else:
                    limit = int(part)
            else:
                # 如果不是密码参数，则添加到自定义提示中
                if not (target_session and password == part):
                    custom_prompt += part + " "
            i += 1

        custom_prompt = custom_prompt.strip()
        return start_timestamp, limit, custom_prompt, target_session, password

    def on_handle_context(self, e_context: EventContext):
        """处理上下文，进行总结"""
        context = e_context['context']
        content = context.content
        msg = context['msg']
        logger.debug("[Summary] on_handle_context. content: %s" % content)
        
        # 检查是否是文本消息
        if context.type != ContextType.TEXT:
            return
        
        # 清理消息内容中的用户ID前缀
        if context.get("isgroup", False) and content.startswith(f"{msg.actual_user_id}:"):
            content = content[len(msg.actual_user_id) + 1:].strip()
        
        # 获取触发前缀
        trigger_prefix = self.config.get('plugin_trigger_prefix', "$")
        clist = content.split()
        
        # 检查是否包含触发命令
        is_trigger = False
        if clist[0] == f"{trigger_prefix}总结":  # 严格匹配 "$总结"
            # 使用$前缀触发
            is_trigger = True
        else:
            # 检查是否是"总结"命令
            # 1. 消息以"总结"开头
            # 2. 消息为"@xxx 总结"格式
            # 3. 消息为"总结 xxx"格式
            content_stripped = content.strip()
            if content_stripped.startswith("总结") or \
               (content_stripped.startswith("@") and "总结" in content_stripped.split(" ", 1)[1].strip().split(" ", 1)[0]) or \
               any(part.strip() == "总结" for part in content_stripped.split(" ", 1)):
                is_trigger = True
                # 如果消息以"@"开头，移除@部分
                if content_stripped.startswith("@"):
                    parts = content_stripped.split(" ", 1)
                    if len(parts) > 1:
                        content = parts[1].strip()
                    else:
                        content = ""
                # 将"总结"关键词转换为命令格式
                content = content.replace("总结", f"{trigger_prefix}总结", 1)  # 只替换第一个"总结"
                clist = content.split()
        
        if not is_trigger:
            return
        
        # 解析命令
        start_time, limit, custom_prompt, target_session, password = self._parse_summary_command(clist[1:])

        # 如果指定了目标会话，先检查是否在群聊中
        if target_session:
            if e_context['context'].get("isgroup", False):
                reply = Reply(ReplyType.ERROR, "指定会话总结功能仅支持私聊使用")
                e_context["reply"] = reply
                e_context.action = EventAction.BREAK_PASS
                return
            
            # 验证密码
            config_password = self.config.get('summary_password', '')
            if not config_password:
                reply = Reply(ReplyType.ERROR, "管理员未设置访问密码，无法使用指定会话功能")
                e_context["reply"] = reply
                e_context.action = EventAction.BREAK_PASS
                return
            if not password or password != config_password:
                reply = Reply(ReplyType.ERROR, "访问密码错误")
                e_context["reply"] = reply
                e_context.action = EventAction.BREAK_PASS
                return

        msg:ChatMessage = e_context['context']['msg']
        session_id = msg.from_user_id
        if self.config.get('channel_type', 'wx') == 'wx' and msg.from_user_nickname is not None:
            session_id = msg.from_user_nickname

        # 使用目标会话ID
        if target_session:
            session_id = target_session

        records = self._get_records(session_id, start_time, limit)
        
        if not records:
            reply = Reply(ReplyType.ERROR, f"没有找到{'指定会话的' if target_session else ''}聊天记录")
            e_context["reply"] = reply
            e_context.action = EventAction.BREAK_PASS
            return
        
        # 准备聊天记录内容
        query = self._check_tokens(records)
        if not query:
            reply = Reply(ReplyType.ERROR, "聊天记录为空")
            e_context["reply"] = reply
            e_context.action = EventAction.BREAK_PASS
            return

        # 发送处理中的提示
        processing_reply = Reply(ReplyType.TEXT, "🎉正在为您生成总结，请稍候...")
        e_context["channel"].send(processing_reply, e_context["context"])
        
        # 调用总结功能并传递给下一个插件
        return self._chat_completion(query, e_context, custom_prompt, "summary")

    def get_help_text(self, verbose = False, **kwargs):
        help_text = "聊天记录总结插件。\n"
        if not verbose:
            return help_text
        trigger_prefix = self.config.get('plugin_trigger_prefix', "$")
        help_text += f"""使用方法:
1. 总结当前会话:
   - {trigger_prefix}总结 100 (总结最近100条消息)
   - {trigger_prefix}总结 -2h (总结最近2小时消息)
   - {trigger_prefix}总结 -24h 100 (总结24小时内最近100条消息)

2. 总结指定会话(需要密码):
   - {trigger_prefix}总结 @群名称 密码 100 (总结指定群最近100条消息)
   - {trigger_prefix}总结 @用户名 密码 -2h (总结指定用户最近2小时消息)

你也可以添加自定义指令，如：{trigger_prefix}总结 @群名称 密码 100 帮我找出重要的会议内容"""
        return help_text
