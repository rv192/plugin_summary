# encoding:utf-8
import time
import queue
import threading
from common.log import logger
from bridge.context import ContextType
from common.tmp_dir import TmpDir
import plugins
from plugins import *
from config import conf
from lib.gewechat.client import GewechatClient

@plugins.register(
    name="GroupCast",
    desire_priority=100,
    hidden=False,
    enabled=False,
    desc="将群聊消息广播到其他群聊",
    version="0.1.0",
    author="hanfangyuan",
)
class GroupCast(Plugin):
    # 定义队列最大容量
    MAX_QUEUE_SIZE = 100
    
    def __init__(self):
        super().__init__()
        # 初始化成员变量
        self.running = False
        self.sender_thread = None
        self.msg_queue = None
        self.broadcast_groups = {}  # 改为字典，key为共享组名称，value为该组内的群列表
        self.client = None
        self.app_id = None
        self.callback_url = None
        self.sync_interval = 3  # 默认同步间隔
        self.ignore_at_bot_msg = True  # 默认忽略@机器人的消息
        
        try:
            # 初始化消息队列
            self.msg_queue = queue.Queue(maxsize=self.MAX_QUEUE_SIZE)
            
            # 加载配置文件
            self.config = super().load_config()
            if not self.config:
                raise Exception("GroupCast 插件配置文件不存在")
                
            # 获取配置参数
            self.sync_interval = self.config.get("sync_interval", 3)
            self.ignore_at_bot_msg = self.config.get("ignore_at_bot_msg", True)
            self.is_prefix_for_media = self.config.get("is_prefix_for_media", True)     # 是否转发图片时先发一条消息表明是谁发的
            
            # 检查是否是 gewechat 渠道
            if conf().get("channel_type") != "gewechat":
                raise Exception("GroupCast 插件仅支持 gewechat 渠道")
                
            # 检查必要的配置
            base_url = conf().get("gewechat_base_url")
            callback_url = conf().get("gewechat_callback_url")
            token = conf().get("gewechat_token")
            app_id = conf().get("gewechat_app_id")
            
            if not all([base_url, callback_url, token, app_id]):
                raise Exception("GroupCast 插件需要配置 gewechat_base_url, callback_url, gewechat_token 和 gewechat_app_id")
            
            # 初始化 gewechat client
            self.client = GewechatClient(base_url, token)
            self.app_id = app_id
            self.callback_url = callback_url
            
            # 获取通讯录列表
            contacts = self.client.fetch_contacts_list(self.app_id)
            logger.debug(f"[GroupCast] 获取通讯录列表: {contacts}")
            
            if contacts and contacts.get("data"):
                chatrooms = contacts["data"].get("chatrooms", [])
                if chatrooms:
                    # 获取群聊详细信息
                    group_details = self.client.get_detail_info(self.app_id, chatrooms)
                    logger.debug(f"[GroupCast] 获取群聊详细信息: {group_details}")
                    
                    # 处理每个共享群组的配置
                    for group_name, group_config in self.config.items():
                        if isinstance(group_config, dict) and group_config.get("enable", False):
                            keywords = group_config.get("group_name_keywords", [])
                            if keywords:
                                self.broadcast_groups[group_name] = []
                                # 查找匹配关键字的群
                                if group_details and group_details.get("data"):
                                    for group in group_details["data"]:
                                        group_nickname = group.get("nickName", "")
                                        if any(keyword in group_nickname for keyword in keywords):
                                            self.broadcast_groups[group_name].append({
                                                "name": group_nickname,
                                                "wxid": group.get("userName")
                                            })
            
            logger.info(f"[GroupCast] 找到的共享群组: {self.broadcast_groups}")
            
            # 启动发送线程
            self.running = True
            self.sender_thread = threading.Thread(target=self._message_sender, name="groupcast_sender")
            self.sender_thread.daemon = True
            self.sender_thread.start()
            
            self.handlers[Event.ON_RECEIVE_MESSAGE] = self.on_handle_receive

        except Exception as e:
            self.cleanup()
            logger.error(f"[GroupCast] 初始化异常：{e}")
            raise e

    def _message_sender(self):
        """消息发送线程"""
        while self.running:
            try:
                # 从队列获取消息，设置1秒超时防止阻塞
                try:
                    msg_data = self.msg_queue.get(timeout=1)
                except queue.Empty:
                    continue
                
                success = False
                context_type = msg_data['context_type']

                logger.debug(f"[GroupCast] 消息类型 {context_type}, {msg_data['content']}")

                # 发送消息
                try:
                    if context_type == ContextType.TEXT:
                        self.client.post_text(self.app_id, msg_data['group_id'], msg_data['content'])
                    
                    if context_type == ContextType.IMAGE:
                        # 如果需要转发人身份信息，则转发图片前，先发送一条身份信息
                        if self.is_prefix_for_media:
                            self.client.post_text(self.app_id, msg_data['group_id'], msg_data['content'])

                        final_url = f"{self.callback_url}?file={msg_data['url']}"
                        self.client.post_image(self.app_id, msg_data['group_id'], final_url)

                    logger.debug(f"[GroupCast] 消息已转发到群 {msg_data['group_name']}")
                    success = True
                except Exception as e:
                    logger.error(f"[GroupCast] 转发消息到群 {msg_data['group_name']} 失败: {e}")
                finally:
                    # 标记任务完成，无论成功失败
                    self.msg_queue.task_done()
                
                # 只在发送成功时等待
                if success:
                    time.sleep(self.sync_interval)
                
            except Exception as e:
                logger.error(f"[GroupCast] 消息发送线程异常: {e}")

    def on_handle_receive(self, e_context: EventContext):
        context = e_context['context']
        logger.debug(f"[GroupCast] 收到群聊消息: {context}")
        
        try:
            # 检查是否是群聊文本消息和图片类型
            if not context.kwargs.get('isgroup') or context.type not in (ContextType.TEXT, ContextType.IMAGE):
                return
            
            # 获取 GeWeChatMessage 对象
            msg = context.kwargs.get('msg')
            if not msg:
                logger.error("[GroupCast] 无法获取消息对象")
                return
                
            # 如果配置了忽略@机器人的消息，则检查是否@机器人
            if self.ignore_at_bot_msg and msg.is_at:
                return
            
            # 获取消息来源群ID
            group_id = msg.from_user_id
            
            # 查找该群所属的共享组
            target_share_group = None
            target_groups = []
            for share_group_name, groups in self.broadcast_groups.items():
                if any(group['wxid'] == group_id for group in groups):
                    target_share_group = share_group_name
                    target_groups = groups
                    break
            
            if not target_share_group:
                return
                
            # 获取群名称和发送者昵称
            group_name = msg.other_user_nickname
            sender_name = msg.actual_user_nickname
            
            url = None

            # 文字消息需要重新组织
            if context.type == ContextType.TEXT:
                # 构造转发消息
                content = f"[{sender_name}@{group_name}]:\n{context.content}"
                
            if context.type == ContextType.IMAGE:
                content = f"[{sender_name}@{group_name}]:发图"
                url = TmpDir().path() + str(msg.msg_id) + ".png"
            
            # 将消息加入队列，转发到同一共享组的其他群
            for group in target_groups:
                if group['wxid'] != group_id:  # 不转发到源群
                    try:
                        self.msg_queue.put_nowait({
                            'context_type': context.type,
                            'group_id': group['wxid'],
                            'group_name': group['name'],
                            'content': content,
                            'url': url
                        })
                    except queue.Full:
                        logger.warning(f"[GroupCast] 消息队列已满，丢弃转发到群 {group['name']} 的消息")
                    
        except Exception as e:
            logger.error(f"[GroupCast] 处理消息异常: {e}")

    def get_help_text(self, **kwargs):
        help_text = "群消息广播插件。将配置的群聊消息广播到其他群聊。\n"
        return help_text

    def cleanup(self):
        """清理资源的方法"""
        if hasattr(self, 'running'):
            self.running = False
        
        # 等待队列中的消息处理完成
        if hasattr(self, 'msg_queue') and self.msg_queue:
            try:
                self.msg_queue.join()
            except:
                pass
        
        # 等待线程结束
        if hasattr(self, 'sender_thread') and self.sender_thread and self.sender_thread.is_alive():
            self.sender_thread.join(timeout=5)
            
    def __del__(self):
        """析构函数调用清理方法"""
        self.cleanup()
