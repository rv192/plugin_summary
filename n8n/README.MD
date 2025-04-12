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
克隆完成后，你会看到以下目录结构：
```
n8n/
├── docker/         # Docker相关配置文件
│   ├── build.sh
│   ├── docker-compose.yaml
│   └── env.template
└── nginx/          # Nginx配置文件
    └── n8n.conf
```
### 2. 配置环境变量
2.1 复制环境变量模板：
```bash
cd docker
cp env.template .env
nano .env
```

2.2 修改环境变量（注意：必须先配置域名相关设置）：
```env
# 首先修改以下两项，确保使用HTTPS
N8N_HOST=n8n.yourdomain.com                    # 替换为你的域名
WEBHOOK_URL=https://n8n.yourdomain.com         # 替换为你的域名，必须使用HTTPS

# 其他配置项（一般不需要修改）
POSTGRES_USER=root                             # PostgreSQL用户名
POSTGRES_PASSWORD=n8n!yyds                     # PostgreSQL密码
POSTGRES_DB=n8n                                # PostgreSQL数据库名
N8N_DEFAULT_LOCALE=zh-CN                       # 中文界面的关键配置项

# 以下两项会由build.sh自动生成，无需手动设置
N8N_ENCRYPTION_KEY=                             # 首次运行build.sh时自动生成
N8N_USER_MANAGEMENT_JWT_SECRET=                 # 首次运行build.sh时自动生成
```

### 3. 构建和启动服务
3.1 设置build.sh脚本权限并执行：
```bash
cd docker
chmod +x build.sh
./build.sh  # 首次运行会自动生成加密密钥
./build.sh  # 需要运行第二次以完成构建
```

3.2 检查服务状态，此时应该能看到n8n暴露了5678端口：
```bash
docker ps
```

### 4. 配置Nginx
4.1 修改Nginx配置文件：
首先修改 nginx/n8n.conf 中的域名，找到erver_name，将 n8n.yourdomain.com 替换为你的实际域名。
```bash
nano nginx/n8n.conf
```

4.2 部署Nginx配置：
```bash
# 复制配置文件到Nginx目录
sudo cp nginx/n8n.conf /etc/nginx/sites-available/n8n.conf

# 创建软链接启用配置
sudo ln -s /etc/nginx/sites-available/n8n.conf /etc/nginx/sites-enabled/

# 验证Nginx配置
sudo nginx -t

# 如果配置正确，重启Nginx
sudo systemctl restart nginx
```

### 5. 配置HTTPS
5.1 安装certbot（如果未安装）：
```bash
sudo apt-get update
sudo apt-get install certbot python3-certbot-nginx
```

5.2 获取SSL证书：
```bash
sudo certbot --nginx -d n8n.yourdomain.com
```

5.3 确认Nginx配置：
Certbot会自动修改Nginx配置，添加SSL相关配置。完成后的配置文件应该包含以下内容：
```nginx
server {
    listen 443 ssl;
    server_name n8n.yourdomain.com;

    ssl_certificate /etc/letsencrypt/live/n8n.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/n8n.yourdomain.com/privkey.pem;

    location / {
        proxy_pass http://localhost:5678;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}

server {
    listen 80;
    server_name n8n.yourdomain.com;
    return 301 https://$server_name$request_uri;
}
```

5.4 验证配置并重启Nginx：
```bash
sudo nginx -t && sudo systemctl restart nginx
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