"""
logger.py —— 全局结构化日志（loguru）
用法：
    from logger import app_log
    app_log.info("服务启动")

    # 绑定 request_id 后使用
    log = app_log.bind(rid="abc123")
    log.info("处理请求")
"""
import sys, os
from loguru import logger as _logger

_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(_DATA_DIR, exist_ok=True)

_FMT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level:<8}</level> | "
    "<cyan>{extra[rid]:<14}</cyan> | "
    "{message}"
)
_FMT_FILE = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {extra[rid]:<14} | {message}"

# 移除默认 handler，重新添加
_logger.remove()
_logger.add(
    sys.stdout,
    format=_FMT,
    level="INFO",
    colorize=True,
)
_logger.add(
    os.path.join(_DATA_DIR, "app.log"),
    rotation="50 MB",
    retention="7 days",
    compression="gz",
    level="INFO",
    encoding="utf-8",
    format=_FMT_FILE,
)

# 默认绑定 rid="-"，所有代码可以直接用；需要 rid 时再 .bind(rid=...)
app_log = _logger.bind(rid="-")
