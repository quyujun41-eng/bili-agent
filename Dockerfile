FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir     -i https://mirrors.aliyun.com/pypi/simple/     --timeout 120 --retries 5     -r requirements.txt
COPY . .
EXPOSE 5001
CMD ["gunicorn", "-w", "4", "--timeout", "180", "-b", "0.0.0.0:5001", "app:app"]
