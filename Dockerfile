# 使用官方提供的 Playwright Python 镜像作为基础
FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

# 设置工作目录
WORKDIR /app

# 拷贝项目文件
COPY requirements.txt .
COPY app.py .

# 安装 Python 依赖
RUN pip install --no-cache-dir -r requirements.txt

# 暴露 Flask 默认端口
EXPOSE 5000

# 运行整合了 Flask 和定时任务的脚本
CMD ["python", "-u", "app.py"]
