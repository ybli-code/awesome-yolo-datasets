#!/usr/bin/env python3
"""
百度网盘空文件夹清理工具
========================
扫描百度网盘 /apps 目录下所有带时间戳的空文件夹（如 同享AI数据集_YYYYMMDD_HHMMSS），
批量删除。这些空文件夹通常是旧版本脚本或上传失败遗留的。

使用方法：
    python clean_empty_folders.py --dry-run    # 预览，不删除
    python clean_empty_folders.py               # 实际删除
"""
import sys
import os
import json
import time
import urllib.request
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "core"))
from utils import BAIDU_ACCESS_TOKEN, setup_logger

logger = setup_logger("clean_empty_folders")

APPS_DIR = "/apps"
FOLDER_PREFIX = "同享AI数据集_"  # 带时间戳的空文件夹前缀


def list_dir(path):
    """列出百度网盘目录下的文件和文件夹"""
    query = urllib.parse.urlencode({
        "method": "list",
        "access_token": BAIDU_ACCESS_TOKEN,
        "dir": path,
        "limit": 1000,
    })
    url = f"https://pan.baidu.com/rest/2.0/xpan/file?{query}"
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode()).get("list", [])
    except Exception as e:
        logger.error(f"列出目录失败 {path}: {e}")
        return []


def delete_files(paths):
    """批量删除文件/文件夹（使用 filemanager API）"""
    query = urllib.parse.urlencode({
        "method": "filemanager",
        "access_token": BAIDU_ACCESS_TOKEN,
        "opera": "delete",
    })
    body = urllib.parse.urlencode({
        "filelist": json.dumps(paths),
    }).encode()
    url = f"https://pan.baidu.com/rest/2.0/xpan/file?{query}"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()[:300]
        return {"error": f"HTTP {e.code}", "detail": err_body}


def find_empty_folders():
    """查找 /apps 目录下所有带时间戳的空文件夹"""
    logger.info(f"扫描 {APPS_DIR} 目录...")
    items = list_dir(APPS_DIR)
    empty_dirs = []

    for item in items:
        if not item.get("isdir", 0):
            continue
        name = item.get("server_filename", "")
        path = item.get("path", "")

        # 只处理带时间戳前缀的文件夹
        if not name.startswith(FOLDER_PREFIX):
            continue

        # 检查是否为空
        sub = list_dir(path)
        if not sub:
            empty_dirs.append((name, path))
            logger.info(f"  发现空文件夹: {name}")

    logger.info(f"共找到 {len(empty_dirs)} 个空文件夹")
    return empty_dirs


def main():
    import argparse
    parser = argparse.ArgumentParser(description="百度网盘空文件夹清理工具")
    parser.add_argument("--dry-run", action="store_true", help="预览模式，不实际删除")
    args = parser.parse_args()

    # 查找空文件夹
    empty_dirs = find_empty_folders()

    if not empty_dirs:
        logger.info("没有需要清理的空文件夹")
        return

    if args.dry_run:
        logger.info("[DRY RUN] 预览模式，不执行删除")
        for name, path in empty_dirs:
            logger.info(f"  将删除: {name}")
        return

    # 批量删除（每次最多50个）
    logger.info(f"开始删除 {len(empty_dirs)} 个空文件夹...")
    paths = [p for _, p in empty_dirs]
    batch_size = 50
    deleted = 0

    for i in range(0, len(paths), batch_size):
        batch = paths[i:i+batch_size]
        result = delete_files(batch)
        if result.get("errno") == 0:
            deleted += len(batch)
            logger.info(f"  批次 {i//batch_size + 1}: 删除 {len(batch)} 个")
        else:
            logger.error(f"  批次 {i//batch_size + 1}: 失败 - {result}")
        time.sleep(1)  # 避免请求过快

    logger.info(f"完成！共删除 {deleted} 个空文件夹")

    # 验证
    remaining = find_empty_folders()
    if remaining:
        logger.warning(f"仍有 {len(remaining)} 个空文件夹未删除")
    else:
        logger.info("所有空文件夹已清理完毕")


if __name__ == "__main__":
    main()
