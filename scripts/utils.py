#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
简化版工具模块 - 批量处理脚本专用
只包含必要的函数，密钥从环境变量读取
"""
import json
import os
import re
import time
import urllib.request
import urllib.error
import logging

# ============================================================
# 配置（从环境变量读取，不在代码中硬编码密钥）
# ============================================================

TENCENT_TOKEN = os.environ.get("TENCENT_TOKEN", "")
MCP_URL = "https://docs.qq.com/openapi/mcp"
FILE_ID = os.environ.get("FILE_ID", "")
SHEET_ID = os.environ.get("SHEET_ID", "")

# 百度网盘 OAuth 认证
BAIDU_APP_KEY = os.environ.get("BAIDU_APP_KEY", "")
BAIDU_SECRET_KEY = os.environ.get("BAIDU_SECRET_KEY", "")
BAIDU_ACCESS_TOKEN = os.environ.get("BAIDU_ACCESS_TOKEN", "")
BAIDU_REFRESH_TOKEN = os.environ.get("BAIDU_REFRESH_TOKEN", "")

# 百度网盘 BDUSS 认证（备选）
BAIDU_BDUSS = os.environ.get("BAIDU_BDUSS", "")
BAIDU_APP_ID = os.environ.get("BAIDU_APP_ID", "266719")

# ============================================================
# 日志系统
# ============================================================

def setup_logger(name, log_file=None):
    """创建统一格式的 logger"""
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", "%H:%M:%S")
    
    if not logger.handlers:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    
    if log_file:
        os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    
    return logger

# ============================================================
# 重试装饰器
# ============================================================

def retry(max_retries=3, delay=2, backoff=2, exceptions=(Exception,)):
    """通用重试装饰器（指数退避）"""
    def decorator(func):
        def wrapper(*args, **kwargs):
            last_err = None
            wait = delay
            for attempt in range(1, max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_err = e
                    if attempt < max_retries:
                        time.sleep(wait)
                        wait *= backoff
            raise last_err
        return wrapper
    return decorator

# ============================================================
# 腾讯文档 MCP 调用
# ============================================================

@retry(max_retries=3, delay=2, backoff=2, exceptions=(urllib.error.URLError, TimeoutError))
def call_mcp(tool_name, arguments, token=None, req_id=10):
    """调用腾讯文档 MCP API（带重试）"""
    token = token or TENCENT_TOKEN
    payload = json.dumps({
        "jsonrpc": "2.0", "id": req_id,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments}
    }).encode()
    req = urllib.request.Request(MCP_URL, data=payload, method="POST")
    req.add_header("Authorization", token)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())

def write_cell(row, col, value, token=None, file_id=None, sheet_id=None):
    """写入单个单元格（1-based 行号）"""
    token = token or TENCENT_TOKEN
    file_id = file_id or FILE_ID
    sheet_id = sheet_id or SHEET_ID
    return call_mcp("sheet.set_cell_value", {
        "file_id": file_id, "sheet_id": sheet_id,
        "row": row, "col": col,
        "value_type": "STRING", "string_value": value
    }, token=token)

def read_sheet_all(token=None, file_id=None, sheet_id=None,
                   start_row=2, end_row=200, start_col=1, end_col=12,
                   batch_size=30):
    """分块读取整个表格，返回 {row: {col: value}} 字典"""
    token = token or TENCENT_TOKEN
    file_id = file_id or FILE_ID
    sheet_id = sheet_id or SHEET_ID
    all_rows = {}
    for start in range(start_row, end_row + 1, batch_size):
        end = min(start + batch_size - 1, end_row)
        result = call_mcp("sheet.get_cell_data", {
            "file_id": file_id, "sheet_id": sheet_id,
            "start_row": start, "end_row": end,
            "start_col": start_col, "end_col": end_col,
            "return_csv": False
        }, token=token, req_id=start)
        cells = result.get("result", {}).get("structuredContent", {}).get("cells", [])
        if not cells:
            break
        for cell in cells:
            row = cell.get("row", 0)
            col = cell.get("col", 0)
            val = cell.get("string_value", "") or ""
            if row not in all_rows:
                all_rows[row] = {}
            all_rows[row][col] = val.strip()
    return all_rows

# ============================================================
# 百度网盘 Token 自动刷新
# ============================================================

def refresh_baidu_token(refresh_token=None, app_key=None, secret_key=None):
    """使用 Refresh Token 刷新 Access Token"""
    refresh_token = refresh_token or BAIDU_REFRESH_TOKEN
    app_key = app_key or BAIDU_APP_KEY
    secret_key = secret_key or BAIDU_SECRET_KEY
    
    if not refresh_token or not app_key or not secret_key:
        return None, None, None
    
    url = (
        f"https://openapi.baidu.com/oauth/2.0/token"
        f"?grant_type=refresh_token"
        f"&refresh_token={refresh_token}"
        f"&client_id={app_key}"
        f"&client_secret={secret_key}"
    )
    
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        
        if "access_token" in data:
            return data["access_token"], data.get("refresh_token"), data.get("expires_in")
        else:
            return None, None, None
    except Exception as e:
        print(f"刷新百度网盘 Token 失败: {e}")
        return None, None, None

def get_valid_baidu_token():
    """获取有效的百度网盘 Access Token（自动刷新）"""
    token = os.environ.get("BAIDU_ACCESS_TOKEN", BAIDU_ACCESS_TOKEN)
    if token and len(token) > 10:
        return token
    
    new_token, new_refresh, expires = refresh_baidu_token()
    if new_token:
        return new_token
    
    return None

# ============================================================
# Roboflow URL 解析
# ============================================================

def parse_roboflow_url(url):
    """从 Roboflow Universe URL 解析 workspace, project, version"""
    match = re.match(r'https?://universe\.roboflow\.com/([^/]+)/([^/?#]+)', url)
    if not match:
        return None, None, None
    workspace = match.group(1)
    project = match.group(2)
    ver_match = re.search(r'/dataset/(\d+)', url)
    version = int(ver_match.group(1)) if ver_match else None
    return workspace, project, version
