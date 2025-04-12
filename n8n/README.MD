# N8N中文版部署指南

本文档详细说明如何使用Docker部署N8N中文版，并配置Nginx反向代理和PostgreSQL数据库。

## 环境要求

- Docker 和 Docker Compose
- Nginx
- 域名，假设为n8n.yourdomain.com（已配置解析到服务器）

## 快速部署
### 0. 创建工作目录
```bash
mkdir ~/n8n && cd ~/n8n
```
### 1. 从Github下载文件
```bash
git init
git remote add origin https://github.com/rv192/plugin_summary.git
git config core.sparseCheckout true
echo "n8n/" > .git/info/sparse-checkout
git pull origin master
```
1.1 复制环境变量模板：
```bash
cp docker/env.template .env
```

1.2 修改环境变量：
```env
POSTGRES_USER=root                    # PostgreSQL用户名
POSTGRES_PASSWORD=n8n!yyds            # PostgreSQL密码
POSTGRES_DB=n8n                       # PostgreSQL数据库名
N8N_ENCRYPTION_KEY=<生成随机字符串>     # 加密密钥
N8N_USER_MANAGEMENT_JWT_SECRET=<生成随机字符串>  # JWT密钥
N8N_DEFAULT_LOCALE=zh-CN              # 设置默认语言为中文
N8N_HOST=n8n.yourdomain.com          # 替换为你的域名
WEBHOOK_URL=https://n8n.yourdomain.com # 替换为你的域名
```

### 2. 配置Nginx

2.1 将nginx/n8n.conf复制到Nginx配置目录：
```bash
sudo cp nginx/n8n.conf /etc/nginx/conf.d/
```

2.2 修改Nginx配置文件中的域名：
```nginx
server {
    listen 80;
    server_name n8n.yourdomain.com;    # 替换为你的域名

    location / {
        proxy_pass http://localhost:5678;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        ...
    }
}
```

2.3 重启Nginx：
```bash
sudo nginx -t && sudo systemctl restart nginx
```

### 3. 启动N8N

3.1 使用Docker Compose启动服务：
```bash
cd docker
docker-compose up -d
```

3.2 检查服务状态：
```bash
docker-compose ps
```

## 特性说明

1. **中文界面**：已默认配置中文界面，无需额外设置
2. **数据持久化**：
   - PostgreSQL数据存储在Docker卷中
   - 工作流和凭证会自动备份到backup目录
3. **自动导入**：系统会自动导入backup目录下的凭证和工作流

## 常见问题

1. **无法访问界面**
   - 检查Docker容器状态
   - 确认Nginx配置是否正确
   - 验证域名解析是否生效

2. **数据库连接失败**
   - 检查环境变量中的数据库配置
   - 确认PostgreSQL容器是否正常运行

3. **Webhook无法使用**
   - 确保WEBHOOK_URL配置正确
   - 检查域名SSL证书是否有效

## 安全建议

1. 建议配置SSL证书，启用HTTPS
2. 定期备份数据库和工作流
3. 使用强密码，定期更换加密密钥

## 升级说明

1. 备份当前数据：
```bash
docker-compose exec n8n n8n export:workflow --all --output=/backup/workflows
docker-compose exec n8n n8n export:credentials --all --output=/backup/credentials
```

2. 更新镜像并重启：
```bash
docker-compose pull
docker-compose up -d
```