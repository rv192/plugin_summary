#!/bin/bash
# -----  自动下载并解压 editor-ui.tar.gz  -----

# 设置 dist 目录
DIST_DIR="./dist"

# 确保 dist 目录存在
mkdir -p "$DIST_DIR"

# 如果 data 目录不存在则创建
mkdir -p data
# 确保Docker容器中具有正确的权限
chown -R 1000:1000 ./data 
chmod -R 775 ./data

# 获取下载 URL
DOWNLOAD_URL=$(curl -s https://api.github.com/repos/other-blowsnow/n8n-i18n-chinese/releases/latest | jq -r '.assets[] | select(.name == "editor-ui.tar.gz") | .browser_download_url')

# 检查是否成功获取 URL
if [ -z "$DOWNLOAD_URL" ]; then
  echo "错误：无法获取 editor-ui.tar.gz 的下载 URL。请检查网络连接和 GitHub API 是否可用。"
  exit 1
fi

echo "下载 URL: $DOWNLOAD_URL"

# 下载文件
echo "正在下载 editor-ui.tar.gz..."
wget -O editor-ui.tar.gz "$DOWNLOAD_URL"

# 检查下载是否成功
if [ ! -f "editor-ui.tar.gz" ]; then
  echo "错误：下载 editor-ui.tar.gz 失败。请检查 URL 和网络连接。"
  exit 1
fi

echo "editor-ui.tar.gz 下载完成。"

# 解压文件到 dist 目录
echo "正在解压 editor-ui.tar.gz 到 $DIST_DIR..."
tar -xzf editor-ui.tar.gz -C "$DIST_DIR" --strip-components 1

# 检查解压是否成功 (简单检查，可以根据需要完善)
# 检查 dist 目录下是否存在关键文件 (例如 index.html)
if [ ! -f "$DIST_DIR/index.html" ]; then
  echo "错误：解压 editor-ui.tar.gz 失败。请检查文件是否损坏或解压路径是否正确。"
  exit 1
fi

echo "editor-ui.tar.gz 解压完成。"

# 清理下载的压缩包
rm -f editor-ui.tar.gz

echo "已清理下载的压缩包 editor-ui.tar.gz"

# -----  自动下载并解压完成  -----

# 启动 Docker Compose
docker compose -f docker-compose.basic.yaml up -d
