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
import psycopg2  # 添加 PostgreSQL 连接库
import re  # 用于解析连接字符串
import urllib.parse

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

            # 检查是否有 PostgreSQL 连接配置
            self.postgres_url = self.config.get("POSTGRES_URL", "")
            self.use_postgres = bool(self.postgres_url)
            
            # 初始化数据库连接
            if self.use_postgres:
                # 使用 PostgreSQL
                logger.info("[Summary] 使用 PostgreSQL 数据库")
                self.conn = self._connect_postgres()
            else:
                # 使用 SQLite
                logger.info("[Summary] 使用 SQLite 数据库")
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

    def _connect_postgres(self):
        """连接到 PostgreSQL 数据库"""
        try:
            import urllib.parse
            
            # 解析连接字符串
            parsed_url = urllib.parse.urlparse(self.postgres_url)
            
            # 检查是否需要处理密码
            if '@' in parsed_url.password:
                # 提取组件
                username = parsed_url.username
                password = urllib.parse.quote_plus(parsed_url.password)  # URL编码密码
                hostname = parsed_url.hostname
                port = parsed_url.port
                dbname = parsed_url.path.strip('/')
                
                # 重建连接字符串
                postgres_url = f"postgresql://{username}:{password}@{hostname}:{port}/{dbname}"
                logger.info(f"[Summary] 修正后的PostgreSQL连接URL (密码已隐藏)")
                self.postgres_url = postgres_url
            
            logger.info(f"[Summary] 正在连接到 PostgreSQL (密码已隐藏)")
            conn = psycopg2.connect(self.postgres_url)
            return conn
        except Exception as e:
            logger.error(f"[Summary] PostgreSQL 连接失败: {e}")
            raise e

    def _init_database(self):
        """初始化数据库架构"""
        cursor = self.conn.cursor()
        
        if self.use_postgres:
            try:
                # 检查表是否存在
                cursor.execute("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'chat_records')")
                table_exists = cursor.fetchone()[0]
                
                if not table_exists:
                    # 创建新表，使用新的数据库结构
                    cursor.execute('''
                        CREATE TABLE chat_records (
                            msgid BIGINT NOT NULL,
                            sessionid TEXT NOT NULL, 
                            sessionname TEXT,
                            userid TEXT,
                            username TEXT,
                            content TEXT, 
                            type TEXT, 
                            timestamp INTEGER, 
                            is_triggered INTEGER,
                            PRIMARY KEY (sessionid, msgid)
                        )
                    ''')
                else:
                    # 检查 msgid 列的数据类型
                    cursor.execute('''
                        SELECT data_type 
                        FROM information_schema.columns 
                        WHERE table_name = 'chat_records' AND column_name = 'msgid'
                    ''')
                    column_type = cursor.fetchone()[0]
                    
                    # 如果不是 BIGINT，则修改列类型
                    if column_type.lower() != 'bigint':
                        cursor.execute("ALTER TABLE chat_records ALTER COLUMN msgid TYPE BIGINT")
                        logger.info("[Summary] 已将 msgid 列类型修改为 BIGINT")
                
                # 检查新增列是否存在，若不存在则添加
                required_columns = ['sessionname', 'userid', 'username']
                for column in required_columns:
                    cursor.execute(f'''
                        SELECT column_name 
                        FROM information_schema.columns 
                        WHERE table_name = 'chat_records' AND column_name = '{column}'
                    ''')
                    
                    if not cursor.fetchone():
                        cursor.execute(f"ALTER TABLE chat_records ADD COLUMN {column} TEXT")
                        logger.info(f"[Summary] 已添加 {column} 列")
                
                # 检查 is_triggered 列是否存在
                cursor.execute('''
                    SELECT column_name 
                    FROM information_schema.columns 
                    WHERE table_name = 'chat_records' AND column_name = 'is_triggered'
                ''')
                
                if not cursor.fetchone():
                    cursor.execute("ALTER TABLE chat_records ADD COLUMN is_triggered INTEGER DEFAULT 0")
                    cursor.execute("UPDATE chat_records SET is_triggered = 0")
                    
            except Exception as e:
                logger.error(f"[Summary] 初始化或修改数据库表结构失败: {e}")
                # 如果修改表结构失败，可以考虑创建一个新表并迁移数据
                # 或者在这里提供更详细的错误处理
        else:
            # SQLite 创建表
            cursor.execute('''CREATE TABLE IF NOT EXISTS chat_records
                            (msgid INTEGER, 
                            sessionid TEXT, 
                            sessionname TEXT,
                            userid TEXT,
                            username TEXT,
                            content TEXT, 
                            type TEXT, 
                            timestamp INTEGER, 
                            is_triggered INTEGER,
                            PRIMARY KEY (sessionid, msgid))''')
            
            # 检查新增列是否存在
            cursor.execute("PRAGMA table_info(chat_records);")
            columns = [column[1] for column in cursor.fetchall()]
            
            for column in ['sessionname', 'userid']:
                if column not in columns:
                    cursor.execute(f"ALTER TABLE chat_records ADD COLUMN {column} TEXT;")
            
            # 检查 is_triggered 列是否存在
            if 'is_triggered' not in columns:
                cursor.execute("ALTER TABLE chat_records ADD COLUMN is_triggered INTEGER DEFAULT 0;")
                cursor.execute("UPDATE chat_records SET is_triggered = 0;")
        
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

    def _insert_record(self, session_id, msg_id, username, content, msg_type, timestamp, is_triggered=0, session_name=None, user_id=None):
        """将记录插入到数据库"""
        cursor = self.conn.cursor()
        logger.debug("[Summary] 插入记录: {} {} {} {} {} {} {} {} {}" .format(session_id, msg_id, session_name, user_id, username, content, msg_type, timestamp, is_triggered))
        
        if self.use_postgres:
            cursor.execute(
                "INSERT INTO chat_records VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (sessionid, msgid) DO UPDATE SET sessionname = %s, userid = %s, username = %s, content = %s, type = %s, timestamp = %s, is_triggered = %s", 
                (msg_id, session_id, session_name, user_id, username, content, msg_type, timestamp, is_triggered, 
                 session_name, user_id, username, content, msg_type, timestamp, is_triggered)
            )
        else:
            cursor.execute(
                "INSERT OR REPLACE INTO chat_records VALUES (?,?,?,?,?,?,?,?,?)", 
                (msg_id, session_id, session_name, user_id, username, content, msg_type, timestamp, is_triggered)
            )
        
        self.conn.commit()
    
    def _get_records(self, session_id, start_timestamp=0, limit=9999, is_group=None):
        """
        从数据库获取记录
        
        针对群聊和私聊的处理逻辑不同：
        - 群聊：只返回is_triggered=0的记录（排除触发机器人回复的消息）
        - 私聊：返回所有记录（不过滤is_triggered，因为私聊中所有消息都是is_triggered=1）
        
        :param session_id: 会话ID
        :param start_timestamp: 开始时间戳，只返回该时间之后的记录
        :param limit: 限制返回的记录数量
        :param is_group: 是否为群聊，如果为None会自动检测
        :return: 记录列表，按时间戳降序排列
        """
        cursor = self.conn.cursor()
        
        # 检查会话是否为群聊（如果未指定）
        if is_group is None:
            if self.use_postgres:
                cursor.execute(
                    "SELECT DISTINCT sessionname FROM chat_records WHERE sessionid=%s LIMIT 1", 
                    (session_id,)
                )
            else:
                cursor.execute(
                    "SELECT DISTINCT sessionname FROM chat_records WHERE sessionid=? LIMIT 1", 
                    (session_id,)
                )
            
            result = cursor.fetchone()
            is_group = bool(result and result[0])  # 如果sessionname存在且非空，则视为群聊
        
        # 构建查询语句 - 对群聊过滤掉is_triggered=1的记录，私聊不过滤
        if is_group:
            if self.use_postgres:
                cursor.execute(
                    "SELECT * FROM chat_records WHERE sessionid=%s AND timestamp>%s AND is_triggered=0 ORDER BY timestamp DESC LIMIT %s", 
                    (session_id, start_timestamp, limit)
                )
            else:
                cursor.execute(
                    "SELECT * FROM chat_records WHERE sessionid=? AND timestamp>? AND is_triggered=0 ORDER BY timestamp DESC LIMIT ?", 
                    (session_id, start_timestamp, limit)
                )
        else:
            # 私聊不过滤is_triggered
            if self.use_postgres:
                cursor.execute(
                    "SELECT * FROM chat_records WHERE sessionid=%s AND timestamp>%s ORDER BY timestamp DESC LIMIT %s", 
                    (session_id, start_timestamp, limit)
                )
            else:
                cursor.execute(
                    "SELECT * FROM chat_records WHERE sessionid=? AND timestamp>? ORDER BY timestamp DESC LIMIT ?", 
                    (session_id, start_timestamp, limit)
                )
        
        return cursor.fetchall()

    def on_receive_message(self, e_context: EventContext):
        """处理接收到的消息"""
        context = e_context['context']
        cmsg : ChatMessage = e_context['context']['msg']
        
        # 获取会话ID和用户信息 - 使用 ChatMessage 对象的属性
        session_id = cmsg.from_user_id  # 始终使用实际ID作为session_id
        session_name = None
        user_id = None
        username = None
        
        if context.get("isgroup", False):
            # 群聊情况
            session_name = cmsg.other_user_nickname  # 群名称
            user_id = cmsg.actual_user_id  # 发送者ID
            username = cmsg.actual_user_nickname  # 发送者昵称
        else:
            # 私聊情况
            user_id = cmsg.from_user_id
            username = cmsg.from_user_nickname
        
        is_triggered = False
        content = context.content
        if context.get("isgroup", False):
            match_prefix = check_prefix(content, self.config.get('group_chat_prefix'))
            match_contain = check_contain(content, self.config.get('group_chat_keyword'))
            if match_prefix is not None or match_contain is not None:
                is_triggered = True
            if context['msg'].is_at and not self.config.get("group_at_off", False):
                is_triggered = True
                
            # 清理消息内容中的用户ID前缀
            if content.startswith(f"{cmsg.actual_user_id}:"):
                content = content[len(cmsg.actual_user_id) + 1:].strip()
        else:
            match_prefix = check_prefix(content, self.config.get('single_chat_prefix',['']))
            if match_prefix is not None:
                is_triggered = True

        # 记录消息处理的开始日志
        logger.debug(f"[Summary] 处理消息，类型：{context.type}，内容前50个字符：{content[:50] if len(content) > 0 else '空内容'}")
        
        # 首先处理特殊消息类型：SHARING 或 XML内容
        msg_type = str(context.type)
        processed_content = content
        
        # 如果是SHARING类型，或者内容包含XML特征，尝试处理
        if context.type == ContextType.SHARING or (
            "<?xml" in content and "<msg>" in content and "<appmsg" in content):
            
            # 检查是否是音乐分享
            is_music_share = False
            if "<?xml" in content and "<type>3</type>" in content and "<title>" in content:
                title_match = re.search(r'<title>\[(.*?)\](.*?)</title>', content)
                if title_match:
                    is_music_share = True
                    processed_content = self._process_message_content(content, context.type)
                    msg_type = "EXPLAIN"  # 修改为EXPLAIN类型
                    logger.debug(f"[Summary] 检测到音乐分享: {processed_content}")
            
            # 如果不是音乐分享，检查是否是不支持展示的内容（兼容中英文版本）
            if not is_music_share and "<title>" in content and (
                "不支持展示该内容" in content or 
                "Your current Weixin version does not support this content" in content):
                processed_content = self._process_wechat_video_content(content)
                if processed_content.startswith("[多媒体描述]"):
                    msg_type = "EXPLAIN"
                    logger.debug(f"[Summary] 检测到不支持展示的内容，处理为多媒体描述：{processed_content}")
        else:
            # 处理其他常规消息类型
            processed_content = self._process_message_content(content, context.type)
            if processed_content.startswith("[多媒体描述]") or processed_content.startswith("[音乐分享]"):
                msg_type = "EXPLAIN"
            
        self._insert_record(
            session_id, 
            cmsg.msg_id, 
            username, 
            processed_content, 
            msg_type, 
            cmsg.create_time, 
            int(is_triggered),
            session_name,
            user_id
        )
        
        logger.debug("[Summary] {}:{} ({})" .format(username, processed_content, session_id))
        
        # 处理图片消息
        if context.type == ContextType.IMAGE and self.multimodal_llm_api_base and self.multimodal_llm_model and self.multimodal_llm_api_key:
            context.get("msg").prepare()
            image_path = context.content  # 假设 context.content 是图片本地路径
            self._process_image_async(session_id, cmsg.msg_id, username, image_path, cmsg.create_time, session_name, user_id)

    def _process_wechat_video_content(self, content):
        """
        处理微信视频内容，提取描述信息
        
        :param content: 包含微信视频信息的XML内容
        :return: 处理后的描述文本，格式为 [多媒体描述]描述内容
        """
        try:
            logger.debug(f"[Summary] 尝试解析微信视频内容，内容特征：XML长度={len(content)}，包含appmsg={('<appmsg' in content)}")
            
            # 检查是否包含特定微信视频的XML标记
            if "<?xml" in content and "<msg>" in content and "<appmsg" in content:
                # 检查是否有"不支持展示该内容"的标记（兼容中英文版本的提示文本）
                title_match = re.search(r'<title>(.*?)</title>', content)
                if title_match and ("不支持展示该内容" in title_match.group(1) or 
                                 "Your current Weixin version does not support this content" in title_match.group(1)):
                    logger.debug(f"[Summary] 找到不支持展示内容标记，标题：{title_match.group(1)}")
                    
                    # 尝试多种方式提取描述信息
                    desc = None
                    
                    # 1. 尝试从finderFeed/desc提取
                    finder_desc_match = re.search(r'<finderFeed>.*?<desc>(.*?)</desc>', content, re.DOTALL)
                    if finder_desc_match and finder_desc_match.group(1).strip():
                        desc = finder_desc_match.group(1).strip()
                        logger.debug(f"[Summary] 从finderFeed/desc提取到描述：{desc}")
                    
                    # 2. 尝试从根级别的desc提取
                    if not desc:
                        root_desc_match = re.search(r'<desc>(.*?)</desc>', content)
                        if root_desc_match and root_desc_match.group(1).strip():
                            desc = root_desc_match.group(1).strip()
                            logger.debug(f"[Summary] 从根级别desc提取到描述：{desc}")
                    
                    # 3. 尝试从nickname提取
                    if not desc:
                        nickname_match = re.search(r'<nickname>(.*?)</nickname>', content)
                        if nickname_match and nickname_match.group(1).strip():
                            desc = f"来自{nickname_match.group(1).strip()}的视频"
                            logger.debug(f"[Summary] 从nickname提取到描述：{desc}")
                    
                    # 4. 尝试从bizNickname提取
                    if not desc:
                        biz_nickname_match = re.search(r'<bizNickname>(.*?)</bizNickname>', content)
                        if biz_nickname_match and biz_nickname_match.group(1).strip():
                            desc = f"来自{biz_nickname_match.group(1).strip()}的视频"
                            logger.debug(f"[Summary] 从bizNickname提取到描述：{desc}")
                    
                    # 如果找到描述，返回格式化内容
                    if desc:
                        return f"[多媒体描述]{desc}"
                    else:
                        logger.debug("[Summary] 未能提取到任何有效描述")
                        return "[多媒体描述]未知内容的视频"
            
            # 如果不满足条件或者提取失败，返回原内容
            logger.debug("[Summary] 内容不符合微信视频格式，返回原内容")
            return content
        except Exception as e:
            logger.error(f"[Summary] 处理微信视频内容失败: {e}")
            return content
            
    def _process_message_content(self, content, content_type):
        """
        处理不同类型的消息内容，特别是引用类型的消息
        
        格式：
        [引用]{JSON数据体不换行}
        JSON结构：{"reply":"回复内容","quote_type":"text|image|share|video|music","quoted_person":"引用人名字","content":内容对象}
        """
        # 检查是否是微信视频或特殊XML消息（处理需要在引用检查前进行）
        # 对任何类型的消息，只要内容包含XML结构且含有特定标记，就进行处理
        if "<?xml" in content and "<msg>" in content and "<appmsg" in content:
            # 检查是否是音乐分享
            type_match = re.search(r'<type>(\d+)</type>', content)
            title_match = re.search(r'<title>\[(.*?)\](.*?)</title>', content)
            
            if type_match and title_match and type_match.group(1) == "3":
                # 音乐分享，提取信息
                music_app = title_match.group(1).strip()
                song_title = title_match.group(2).strip()
                
                artist = ""
                des_match = re.search(r'<des>(.*?)</des>', content)
                if des_match:
                    artist = des_match.group(1).strip()
                
                music_info = f"[音乐分享] {song_title}" + (f" - {artist}" if artist else "") + f" ({music_app})"
                
                return music_info
            
            # 检查是否是不支持展示的内容
            title_match = re.search(r'<title>(.*?)</title>', content)
            if title_match and "不支持展示该内容" in title_match.group(1):
                return self._process_wechat_video_content(content)
        
        # 检查是否是引用消息
        quote_match = re.search(r'「(.*?):[\s\S]*?」[\s\S]*?----------([\s\S]*)', content)
        
        if not quote_match:
            # 如果不是引用消息，处理普通消息
            if content_type == ContextType.IMAGE:
                return "[图片]"
            elif content_type == ContextType.VOICE:
                return "[语音]"
            else:
                return content
        
        # 提取被引用人的名字
        quoted_person = quote_match.group(1).strip()
        # 提取回复内容（分隔符后的部分）
        reply_content = quote_match.group(2).strip()
        
        # 提取引用的内容（在「」内的部分）
        quoted_content_match = re.search(r'「.*?:([\s\S]*?)」', content)
        quoted_content = quoted_content_match.group(1).strip() if quoted_content_match else ""
        
        # 处理引用内容，确定引用类型和内容
        quote_info = self._process_quoted_content(quoted_content)
        
        # 构建JSON对象
        quote_json = {
            "reply": reply_content,
            "quote_type": quote_info["type"],
            "quoted_person": quoted_person,
            "content": quote_info["content"]
        }
        
        # 转换为JSON字符串（确保不换行）
        json_str = json.dumps(quote_json, ensure_ascii=False).replace("\n", "")
        
        # 构建最终格式
        return f"[引用]{json_str}"

    def _process_quoted_content(self, content):
        """
        处理引用的内容，确定引用类型和内容
        
        返回格式：{"type": "text|image|share|video|music", "content": 内容对象}
        """
        # 首先检查是否是微信视频消息
        if "<?xml" in content and "<msg>" in content and "<appmsg" in content:
            # 检查是否是音乐分享
            type_match = re.search(r'<type>(\d+)</type>', content)
            title_match = re.search(r'<title>\[(.*?)\](.*?)</title>', content)
            
            # 音乐类型通常是type=3，同时标题会有[音乐应用名称]格式
            if type_match and title_match and type_match.group(1) == "3":
                logger.debug("[Summary] 检测到音乐分享")
                
                # 提取音乐信息
                music_app = title_match.group(1).strip()
                song_title = title_match.group(2).strip()
                
                # 提取艺术家信息
                artist = ""
                des_match = re.search(r'<des>(.*?)</des>', content)
                if des_match:
                    artist = des_match.group(1).strip()
                
                # 创建音乐内容对象
                music_content = {
                    "app": music_app,
                    "title": song_title,
                    "artist": artist,
                    "description": f"{song_title}" + (f" - {artist}" if artist else "") + f" ({music_app})"
                }
                
                return {
                    "type": "music", 
                    "content": music_content
                }
            
            # 检查是否有"不支持展示该内容"的标记（兼容中英文版本）
            title_match = re.search(r'<title>(.*?)</title>', content)
            if title_match and ("不支持展示该内容" in title_match.group(1) or 
                             "not support this content" in title_match.group(1)):
                # 尝试多种方式提取描述信息
                desc = None
                
                # 尝试从finderFeed/desc提取
                finder_desc_match = re.search(r'<finderFeed>.*?<desc>(.*?)</desc>', content, re.DOTALL)
                if finder_desc_match and finder_desc_match.group(1).strip():
                    desc = finder_desc_match.group(1).strip()
                
                # 尝试从根级别的desc提取
                if not desc:
                    root_desc_match = re.search(r'<desc>(.*?)</desc>', content)
                    if root_desc_match and root_desc_match.group(1).strip():
                        desc = root_desc_match.group(1).strip()
                
                # 尝试从nickname提取
                if not desc:
                    nickname_match = re.search(r'<nickname>(.*?)</nickname>', content)
                    if nickname_match and nickname_match.group(1).strip():
                        desc = f"来自{nickname_match.group(1).strip()}的视频"
                
                # 尝试从bizNickname提取
                if not desc:
                    biz_nickname_match = re.search(r'<bizNickname>(.*?)</bizNickname>', content)
                    if biz_nickname_match and biz_nickname_match.group(1).strip():
                        desc = f"来自{biz_nickname_match.group(1).strip()}的视频"
                
                return {"type": "video", "content": {"desc": desc or "未知内容的视频"}}
        
        # 检查是否是分享卡片（包含appmsg标签）
        if "<msg>" in content and "<appmsg" in content:
            # 尝试提取标题
            title_match = re.search(r'<title>(.*?)</title>', content)
            title = title_match.group(1).strip() if title_match else "未知标题"
            
            # 尝试提取URL
            url_match = re.search(r'<url>(.*?)</url>', content)
            url = url_match.group(1).strip() if url_match else ""
            
            # 构建分享卡片内容
            share_content = {"title": title}
            if url:
                share_content["url"] = url
            
            return {"type": "share", "content": share_content}
        
        # 然后检查是否是图片（包含XML标记和img标签）
        elif "<msg>" in content and ("<img" in content or "cdnthumburl" in content):
            return {"type": "image", "content": None}
        
        # 其他内容（普通文字）
        return {"type": "text", "content": content}

    def _process_image_async(self, session_id, msg_id, username, image_path, create_time, session_name=None, user_id=None):
        """使用线程池异步处理图片消息"""
        future = self.executor.submit(self._process_image, session_id, msg_id, username, image_path, create_time, session_name, user_id)
        future.add_done_callback(self._handle_image_result)

    def _process_image(self, session_id, msg_id, username, image_path, create_time, session_name=None, user_id=None):
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
                    self._insert_record(session_id, msg_id, username, f"[图片描述]{text_content}", "EXPLAIN", create_time, 0, session_name, user_id) # 这里默认识别内容没有触发
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
            # 不需要特别处理 EXPLAIN 类型，因为内容已经包含了描述信息
            
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

            # 获取会话ID
            msg = e_context['context']['msg']
            context = e_context['context']
            
            # 始终使用ID作为session_id
            session_id = msg.from_user_id
            
            # 判断是否为群聊
            is_group = context.get("isgroup", False)
            
            # 清理消息内容中的用户ID前缀（如果存在）
            if is_group and content.startswith(f"{msg.actual_user_id}:"):
                content = content[len(msg.actual_user_id) + 1:].strip()
            
            # 传递is_group参数给_get_records方法
            records = self._get_records(session_id, start_time, limit, is_group=is_group)
            
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
