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

RUN pip install --no-cache-dir --timeout 300 \
    -i https://mirrors.aliyun.com/pypi/simple/ \
    -r requirements.txt

COPY . .

EXPOSE 5001

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD curl -sf http://localhost:5001/health || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "5001", "--workers", "1"]
