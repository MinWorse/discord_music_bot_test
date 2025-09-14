# 使用 Python 3.12 作為基底
FROM python:3.12-slim

# 安裝 ffmpeg 和其他必要套件
RUN apt-get update && \
    apt-get install -y ffmpeg git curl && \
    apt-get clean

# 設定工作目錄
WORKDIR /app

# 複製所有檔案進容器
COPY . .

# 安裝 Python 套件
RUN pip install --no-cache-dir -r requirements.txt

# 啟動 bot
CMD ["python", "main.py"]