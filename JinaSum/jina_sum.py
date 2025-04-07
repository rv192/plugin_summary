# encoding:utf-8
import json
import os
import html
from urllib.parse import urlparse

import requests
import io
import plugins
from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from common.log import logger
from plugins import *
import re
import time

@plugins.register(
    name="JinaSum",
    desire_priority=10,
    hidden=False,
    enabled=False,
    desc="Sum url link content with firecrawl and llm",
    version="0.1.0",
    author="hanfangyuan",
)
class JinaSum(Plugin):
    firecrawl_api_base = "https://api.firecrawl.dev/v1/scrape"
    firecrawl_api_key = "fc-6f4b572b4a514fa9b2076ff895c6893a"
    open_ai_api_base = "https://api.openai.com/v1"
    open_ai_model = "gpt-4o-mini"
    max_words = 8000
    prompt = "请总结下面引号内的文档内容。\n\n"
    white_url_list = []
    black_url_list = [
        "https://support.weixin.qq.com",  # 视频号视频
        "https://channels-aladin.wxqcloud.qq.com",  # 视频号音乐
    ]
    generate_image = True

    def __init__(self):
        super().__init__()
        try:
            self.config = super().load_config()
            if not self.config:
                self.config = self._load_config_template()
            self.firecrawl_api_base = self.config.get("firecrawl_api_base", self.firecrawl_api_base)
            self.firecrawl_api_key = self.config.get("firecrawl_api_key", self.firecrawl_api_key)
            self.open_ai_api_base = self.config.get("open_ai_api_base", self.open_ai_api_base)
            self.open_ai_api_key = self.config.get("open_ai_api_key", "")
            self.open_ai_model = self.config.get("open_ai_model", self.open_ai_model)
            self.max_words = self.config.get("max_words", self.max_words)
            self.prompt = self.config.get("prompt", self.prompt)
            self.white_url_list = self.config.get("white_url_list", self.white_url_list)
            self.black_url_list = self.config.get("black_url_list", self.black_url_list)
            self.generate_image = self.config.get("generate_image", True)
            self.black_group_list = self.config.get("black_group_list", [])
            logger.info(f"[JinaSum] inited, config={self.config}")
            self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context
        except Exception as e:
            logger.error(f"[JinaSum] 初始化异常：{e}")
            raise "[JinaSum] init failed, ignore "

    def on_handle_context(self, e_context: EventContext, retry_count: int = 0):
        try:
            context = e_context["context"]
            content = context.content
            if context.get("isgroup", True):
                msg:ChatMessage = e_context['context']['msg']
                group_name = msg.other_user_nickname
                
                # 检查群名称是否在黑名单中
                for black_group in self.black_group_list:
                    if group_name == black_group or black_group in group_name:
                        logger.debug(f"[JinaSum] 群组 '{group_name}' 在黑名单中，跳过处理")
                        return

            if context.type != ContextType.SHARING and context.type != ContextType.TEXT:
                return
            if not self._check_url(content):
                logger.debug(f"[JinaSum] {content} is not a valid url, skip")
                return
            target_url = html.unescape(content)  # 解决公众号卡片链接校验问题

            # 在获取内容之前，先检查 FireCrawl 服务是否可用
            try:
                test_url = self.firecrawl_api_base.replace('/v1/scrape', '')  # 获取基础URL
                test_response = requests.get(test_url, timeout=5)
                logger.info(f"[JinaSum] FireCrawl服务状态检查: {test_response.status_code}")
            except Exception as e:
                logger.error(f"[JinaSum] FireCrawl服务不可用: {str(e)}")
                reply = Reply(ReplyType.ERROR, "内容抓取服务暂时不可用，请稍后再试")
                e_context["reply"] = reply
                e_context.action = EventAction.BREAK_PASS
                return

            # 使用FireCrawl抓取网页内容
            target_url_content = self._get_firecrawl_content(target_url)
            if not target_url_content:
                if "mp.weixin.qq.com" in target_url:
                    reply = Reply(ReplyType.ERROR, "微信公众号文章需要验证，无法自动抓取内容，请考虑手动复制文章内容")
                else:
                    reply = Reply(ReplyType.ERROR, "我无法抓取这个网页内容，请稍后再试")
                e_context["reply"] = reply
                e_context.action = EventAction.BREAK_PASS
                return

            openai_chat_url = self._get_openai_chat_url()
            openai_headers = self._get_openai_headers()
            openai_payload = self._get_openai_payload(target_url_content)
            logger.debug(f"[JinaSum] openai_chat_url: {openai_chat_url}, openai_headers: {openai_headers}, openai_payload: {openai_payload}")
            
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"}
            response = requests.post(openai_chat_url, headers={**openai_headers, **headers}, json=openai_payload, timeout=60)
            response.raise_for_status()
            result = response.json()['choices'][0]['message']['content']
            logger.info(f"[JinaSum] LLM原始返回内容：\n{result}")
            
            try:
                 # 尝试解析JSON
                summary_data = self._parse_json_with_fallback(result)
                if summary_data:
                    # 合并Summary和Tags
                    summary = summary_data.get('Content', {}).get('Summary', '暂无总结')
                    keypoints = summary_data.get('Content', {}).get('Keypoints', [])
                    tags = summary_data.get('Content', {}).get('Tags', '无标签')
                    title = summary_data.get('Title', "无标题")
                    author = summary_data.get('Author', "未知作者")
                    date = summary_data.get('Date', str(time.strftime("%Y-%m-%d", time.localtime())))
                    
                    # 将关键要点转换为字符串
                    keypoints_str = "\n".join([f"{i+1}. {point}" for i, point in enumerate(keypoints)])
                    
                    summary_content = f"{summary}\n\n{keypoints_str}\n\n🏷 {tags}"
                    
                    if self.generate_image:
                        image_content = self._save_summary_as_image(
                            summary_content=summary_content,
                            date=f"{date}日",
                            title=title,
                            author=author
                        )
                        if image_content:
                            image_storage = io.BytesIO(image_content)
                            reply = Reply(ReplyType.IMAGE, image_storage)
                        else:
                            reply = Reply(ReplyType.ERROR, "生成图片总结失败")
                    else:
                         reply = Reply(ReplyType.TEXT, summary_content)
                else:
                   reply = Reply(ReplyType.ERROR, "解析总结内容失败，请检查LLM输出")
            except Exception as e:
                logger.error(f"[JinaSum] 处理总结内容失败：{str(e)}")
                reply = Reply(ReplyType.ERROR, "处理总结内容失败，请重试")

            e_context["reply"] = reply
            e_context.action = EventAction.BREAK_PASS

        except Exception as e:
            if retry_count < 3:
                logger.warning(f"[JinaSum] {str(e)}, retry {retry_count + 1}")
                self.on_handle_context(e_context, retry_count + 1)
                return

            logger.exception(f"[JinaSum] {str(e)}")
            reply = Reply(ReplyType.ERROR, "我暂时无法总结链接，请稍后再试")
            e_context["reply"] = reply
            e_context.action = EventAction.BREAK_PASS

    def get_help_text(self, verbose, **kwargs):
        return f'使用FireCrawl抓取页面内容，并使用LLM总结网页链接内容，并可以生成图片总结。'

    def _load_config_template(self):
        logger.debug("No Suno plugin config.json, use plugins/jina_sum/config.json.template")
        try:
            plugin_config_path = os.path.join(self.path, "config.json.template")
            if os.path.exists(plugin_config_path):
                with open(plugin_config_path, "r", encoding="utf-8") as f:
                    plugin_conf = json.load(f)
                    return plugin_conf
        except Exception as e:
            logger.exception(e)

    def _get_firecrawl_content(self, target_url):
        """使用FireCrawl API获取网页内容"""
        try:
            # 基础请求头
            headers = {
                'Content-Type': 'application/json'
            }
            
            # 如果有API key，则添加到请求头
            if self.firecrawl_api_key:
                headers['Authorization'] = f'Bearer {self.firecrawl_api_key}'
            
            # 检测是否是微信公众号链接
            is_wechat_mp = "mp.weixin.qq.com" in target_url
            
            # 针对自部署实例，简化请求参数
            payload = {
                'url': target_url
            }
            
            logger.info(f"[JinaSum] 开始抓取URL: {target_url}, 是否是微信公众号: {is_wechat_mp}")
            
            response = requests.post(
                self.firecrawl_api_base, 
                headers=headers, 
                json=payload,
                timeout=90  # 增加超时时间
            )
            response.raise_for_status()
            result = response.json()
            
            # 打印完整响应以便调试
            logger.debug(f"[JinaSum] FireCrawl 原始响应: {result}")
            
            # 根据自部署FireCrawl的响应格式灵活提取正文内容
            # 尝试多种可能的结构
            content = None
            
            # 1. 尝试 success/data/markdown 结构
            if result.get('success') and 'data' in result:
                if 'markdown' in result['data']:
                    content = result['data']['markdown']
            
            # 2. 尝试直接的 markdown 字段
            elif 'markdown' in result:
                content = result['markdown']
            
            # 3. 尝试 content 或 text 字段（一些爬虫API会使用这些字段名）
            elif 'content' in result:
                content = result['content']
            elif 'text' in result:
                content = result['text']
            
            # 4. 如果是嵌套的结构
            elif 'data' in result and isinstance(result['data'], dict):
                data = result['data']
                if 'content' in data:
                    content = data['content']
                elif 'text' in data:
                    content = data['text']
                elif 'html' in data:
                    content = data['html']  # 可能需要额外处理HTML
            
            # 如果找到内容
            if content:
                logger.info(f"[JinaSum] FireCrawl抓取成功，内容长度: {len(content)}")
                
                # 检测内容中是否包含验证码或者环境异常的关键词
                if any(keyword in content for keyword in ["环境异常", "完成验证", "拖动滑块", "验证码"]):
                    logger.warning(f"[JinaSum] 检测到目标网站需要验证码，无法抓取内容")
                    return None
                
                return content
            
            logger.error(f"[JinaSum] 无法从FireCrawl响应中提取内容: {result}")
            return None
            
        except Exception as e:
            logger.error(f"[JinaSum] FireCrawl抓取失败: {str(e)}")
            # 如果是连接错误，提供更详细的错误信息
            if "Connection" in str(e):
                logger.error(f"[JinaSum] 连接到FireCrawl服务器失败，请检查网络或服务器状态")
            return None

    def _get_openai_chat_url(self):
        return self.open_ai_api_base + "/chat/completions"

    def _get_openai_headers(self):
         return {
             'Authorization': f"Bearer {self.open_ai_api_key}",
             'Host': urlparse(self.open_ai_api_base).netloc
        }

    def _get_openai_payload(self, target_url_content):
        target_url_content = target_url_content[:self.max_words]
        sum_prompt = f"{self.prompt}\n\n'''{target_url_content}'''"
        messages = [{"role": "user", "content": sum_prompt}]
       
        payload = {
             'model': self.open_ai_model,
             'messages': messages
        }
        
        # 使用正则表达式检查模型是否以 "gpt" 开头且不是 "gpt-4o-mini"
        if re.match(r'^gpt', self.open_ai_model) and self.open_ai_model != 'gpt-4o-mini':
           payload['response_format'] = {"type": "json_object"}
        return payload

    def _parse_json_with_fallback(self, text):
        """
        尝试解析JSON，如果失败则使用正则表达式提取关键信息
        """
        def clean_text(text):
            if not text:
                return text
            # 清理多余的符号和空白
            text = re.sub(r'\*\*|\\n|\\r|\\t','',text)
            text = re.sub(r'\s+',' ',text)
            return text.strip()
            
        try:
            # 首先尝试提取JSON部分
            json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
            if json_match:
                text = json_match.group(1)
        
            # 清理文本并尝试JSON解析
            text = clean_text(text)
            return json.loads(text)
    
        except json.JSONDecodeError:
            logger.warning("[JinaSum] JSON解析失败，尝试使用正则表达式提取")
            try:
            # 使用更简单的正则表达式模式
                patterns = {
                    'summary': r'["\']?Summary["\']?\s*[:：]\s*["\']?([^"\'}\n]+)["\']?',
                    'tags': r'["\']?Tags["\']?\s*[:：]\s*["\']?([^"\'}\n]+)["\']?',
                    'title': r'["\']?Title["\']?\s*[:：]\s*["\']?([^"\'}\n]+)["\']?', 
                    'author': r'["\']?Author["\']?\s*[:：]\s*["\']?([^"\'}\n]+)["\']?',
                    'keypoints': r'(?:\d+\.\s*([^\n]+)|["\']?Keypoints["\']?\s*[:：]\s*\[(.*?)\])'
                }
            
                results = {}
                for key, pattern in patterns.items():
                    match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
                    if match:
                        if key == 'keypoints':
                            # 处理关键点列表
                            if match.group(2):  # JSON数组格式
                                points = re.findall(r'["\']([^"\']+)["\']', match.group(2))
                            else:  # 普通列表格式
                                points = re.findall(r'\d+\.\s*([^\n]+)', text)
                            results[key] = [clean_text(p) for p in points if p.strip()]
                        else:
                            results[key] = clean_text(match.group(1))
                    else:
                        results[key] = "未提供" if key == 'author' else ("无标题" if key == 'title' else []) if key == 'keypoints' else "暂无内容"

                # 构建返回的字典结构
                extracted_data = {
                    "Content": {
                        "Summary": results['summary'],
                        "Keypoints": results['keypoints'],
                        "Tags": results['tags']
                    },
                    "Title": results['title'],
                    "Author": results['author'],
                    "Date": str(time.strftime("%Y-%m-%d", time.localtime()))
                }

                return extracted_data

            except Exception as e:
                logger.error(f"[JinaSum] 正则表达式提取失败: {str(e)}")
                # 返回基本结构而不是None，确保后续处理不会出错
                return {
                    "Content": {
                        "Summary": "内容解析失败",
                        "Keypoints": [],
                        "Tags": "无标签"
                    },
                    "Title": "解析失败",
                    "Author": "未知",
                    "Date": str(time.strftime("%Y-%m-%d", time.localtime()))
                }

    def _check_url(self, target_url: str):
        stripped_url = target_url.strip()
        if not stripped_url.startswith("http://") and not stripped_url.startswith("https://"):
            return False

        if len(self.white_url_list):
            if not any(stripped_url.startswith(white_url) for white_url in self.white_url_list):
                return False

        for black_url in self.black_url_list:
            if stripped_url.startswith(black_url):
                return False
        return True

    def _save_summary_as_image(self, summary_content, date=None, title=None, author=None):
        """将总结内容转换为图片"""
        try:
            api_url = "https://fireflycard-api.302ai.cn/api/saveImg"
            data = {
                "icon": "https://mrxc-1300093961.cos.ap-shanghai.myqcloud.com/2024/12/8/1865676194712899585.png",
                "date": date or str(time.strftime("%Y-%m-%d", time.localtime())),
                "title": title or "📝 内容总结",
                "author": author or "AI助手",
                "content": summary_content,
                "font": "Noto Sans SC",
                "fontStyle": "Regular",
                "titleFontSize": 36,
                "contentFontSize": 28,
                "contentLineHeight": 44,
                "contentColor": "#333333",
                "backgroundColor": "#FFFFFF",
                "width": 440,
                "height": 0,
                "useFont": "MiSans-Thin",
                "fontScale": 0.7,
                "ratio": "Auto",
                "padding": 15,
                "watermark": "蓝胖子速递",
                "qrCodeTitle": "<p>蓝胖子速递</p>",
                "qrCode": "https://u.wechat.com/MPJjlS-S7P8v5Cm0zxXx2kw",
                "watermarkText": "",
                "watermarkColor": "#999999",
                "watermarkSize": 24,
                "watermarkGap": 20,
                "exportType": "png",
                "exportQuality": 100,
                "switchConfig": {
                    "showIcon": True,
                    "showTitle": True,
                    "showContent": True,
                    "showAuthor": True,
                    "showQRCode": False,
                    "showSignature": False,
                    "showQuotes": False
                }
            }
            response = requests.post(api_url, json=data, timeout=30)
            response.raise_for_status()
            if response.headers.get('content-type', '').startswith('image/'):
                logger.info("[JinaSum] 成功生成图片")
                return response.content
            logger.error("[JinaSum] 生成图片失败：响应格式错误")
            return None
        except Exception as e:
            logger.error(f"[JinaSum] 生成图片失败：{str(e)}")
            return None
