# 本地自用镜像：python run.py 即可启动
# 构建：docker build -t video-distiller .
# 运行：docker run -d -p 8848:8848 -v "%cd%/data:/app/data" --name distiller video-distiller
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=8848

# ffmpeg：无字幕视频转写时需要把音频转成 16k wav；
# 其余依赖（faster-whisper 等）按需自行追加，保持镜像精简。
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 笔记产物、数据库、缓存都在 data/ 下，建议挂载出来
VOLUME ["/app/data"]
EXPOSE 8848

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8848/api/health || exit 1

CMD ["python", "run.py", "--host", "0.0.0.0", "--no-browser"]
