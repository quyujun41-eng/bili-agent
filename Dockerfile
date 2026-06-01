FROM python:3.11-slim

WORKDIR /app

# 切换腾讯云内网 apt 镜像
RUN set -e; \
    if ls /etc/apt/sources.list.d/*.sources 2>/dev/null | head -1 | grep -q .; then \
        sed -i 's|deb.debian.org|mirrors.tencentyun.com|g; s|security.debian.org|mirrors.tencentyun.com|g' \
            /etc/apt/sources.list.d/*.sources; \
    else \
        sed -i 's|deb.debian.org|mirrors.tencentyun.com|g; s|security.debian.org|mirrors.tencentyun.com|g' \
            /etc/apt/sources.list; \
    fi

RUN apt-get update -qq && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# Step1: 先锁定 numpy<2，避免后续包升级覆盖
RUN pip install --no-cache-dir --timeout 300 \
    -i https://mirrors.aliyun.com/pypi/simple/ \
    "numpy<2"

# Step2: 安装 CPU-only torch（必须以 PyTorch WHL 为主源）
RUN pip install --no-cache-dir --timeout 300 \
    --index-url https://download.pytorch.org/whl/cpu \
    "torch==2.4.1+cpu"

# Step3: 安装其余依赖（requirements.txt 中 numpy<2 防止被升级）
RUN pip install --no-cache-dir --timeout 300 \
    -i https://mirrors.aliyun.com/pypi/simple/ \
    -r requirements.txt

# Step4: 预下载 embedding 模型（bake-in 进镜像，避免运行时从 huggingface 下载）
ENV HF_ENDPOINT=https://hf-mirror.com
RUN python3 -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-zh-v1.5')"

COPY . .

EXPOSE 5001

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
  CMD curl -sf http://localhost:5001/health || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "5001", "--workers", "2"]
