#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量处理60个无标题数据集：下载→上传百度网盘→创建分享→更新腾讯文档
"""
import json
import os
import sys
import re
import time
import hashlib
import urllib.request
import urllib.parse
import urllib.error
import subprocess
import shutil
import zipfile
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

# 添加当前目录到路径
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from utils import (
    read_sheet_all, write_cell, call_mcp,
    TENCENT_TOKEN, FILE_ID, SHEET_ID,
    BAIDU_ACCESS_TOKEN, BAIDU_REFRESH_TOKEN,
    BAIDU_APP_KEY, BAIDU_SECRET_KEY,
    BAIDU_BDUSS, BAIDU_APP_ID,
    setup_logger, parse_roboflow_url,
    refresh_baidu_token, get_valid_baidu_token,
    ROBOFLOW_API_KEY,
)

logger = setup_logger("batch_no_title")

WORK_DIR = os.environ.get("WORK_DIR", "/mnt")
BASE_DIR = os.path.join(WORK_DIR, "no_title_datasets")
PACKAGE_DIR = os.path.join(BASE_DIR, "packages")
BAIDU_REMOTE_DIR = "/apps/同享AI数据集"
UPLOAD_WORKERS = 8


def generate_title_from_roboflow(roboflow_url, img_info=""):
    """从Roboflow链接生成标题"""
    workspace, project, version = parse_roboflow_url(roboflow_url)
    if not project:
        return "未知数据集"
    
    # 将项目名转换为更友好的标题
    title = project.replace("-", " ").replace("_", " ").title()
    # 添加数据集后缀
    if not title.endswith("数据集"):
        title += "数据集"
    
    return title


def download_roboflow_dataset(workspace, project, version_num=None, output_dir=None):
    """从Roboflow下载数据集"""
    if not output_dir:
        output_dir = os.path.join(BASE_DIR, f"{workspace}_{project}")
    
    os.makedirs(output_dir, exist_ok=True)
    
    api_key = ROBOFLOW_API_KEY or os.environ.get("ROBOFLOW_API_KEY", "")
    if not api_key:
        logger.error("未设置 ROBOFLOW_API_KEY")
        return None
    
    # 构建下载 URL
    base_url = f"https://universe.roboflow.com/ds/{workspace}/{project}"
    if version_num:
        base_url += f"/{version_num}"
    
    download_url = f"{base_url}?api_key={api_key}"
    zip_path = os.path.join(BASE_DIR, f"{workspace}_{project}.zip")
    
    logger.info(f"  下载: {download_url[:80]}...")
    
    try:
        result = subprocess.run([
            "curl", "-s", "-L", "-o", zip_path,
            "--connect-timeout", "30",
            "--max-time", "600",
            download_url
        ], capture_output=True, text=True, timeout=650)
        
        if result.returncode != 0 or not os.path.exists(zip_path):
            logger.error(f"  下载失败: {result.stderr[:200]}")
            return None
        
        file_size = os.path.getsize(zip_path)
        if file_size < 1000:
            logger.error(f"  下载文件过小: {file_size} bytes")
            return None
        
        logger.info(f"  下载完成: {file_size / 1024 / 1024:.1f} MB")
        
        # 解压
        try:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                zf.extractall(output_dir)
        except Exception as e:
            logger.error(f"  解压失败: {e}")
            return None
        
        # 删除 zip
        if os.path.exists(zip_path):
            os.remove(zip_path)
        
        return output_dir
    except Exception as e:
        logger.error(f"  下载异常: {e}")
        return None


def compress_dataset(source_dir, zip_path):
    """压缩数据集为 ZIP"""
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(source_dir):
            for file in files:
                filepath = os.path.join(root, file)
                arcname = os.path.relpath(filepath, source_dir)
                zf.write(filepath, arcname)
    return zip_path


def baidu_upload_xpan_concurrent(file_path, remote_path, access_token, max_workers=8):
    """百度网盘 xpan 分片上传（并发版）"""
    logger.info(f"  使用 xpan 并发分片上传（{max_workers} 线程）...")
    
    file_size = os.path.getsize(file_path)
    slice_size = 4 * 1024 * 1024
    
    # 计算分片 MD5
    block_list = []
    with open(file_path, 'rb') as f:
        while True:
            chunk = f.read(slice_size)
            if not chunk:
                break
            block_list.append(hashlib.md5(chunk).hexdigest())
    
    # Step 1: precreate
    query_params = urllib.parse.urlencode({"method": "precreate", "access_token": access_token})
    body_data = urllib.parse.urlencode({
        "path": remote_path, "size": str(file_size),
        "isdir": "0", "autoinit": "1",
        "block_list": json.dumps(block_list), "ondup": "overwrite",
    }).encode()
    
    req = urllib.request.Request(
        f"https://pan.baidu.com/rest/2.0/xpan/file?{query_params}",
        data=body_data, method="POST"
    )
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
    except Exception as e:
        logger.warning(f"  precreate 失败: {e}")
        return None
    
    if result.get("errno") != 0:
        logger.warning(f"  precreate 错误: errno={result.get('errno')}")
        return None
    
    uploadid = result.get("uploadid", "")
    if not uploadid:
        return None
    logger.info(f"  precreate 成功, 共 {len(block_list)} 个分片")
    
    # Step 2: 并发分片上传
    t0 = time.time()
    total_parts = len(block_list)
    uploaded_parts = 0
    progress_lock = threading.Lock()
    failed = False
    
    def upload_single_part(part_seq):
        nonlocal uploaded_parts, failed
        
        if failed:
            return False
        
        with open(file_path, 'rb') as f:
            f.seek(part_seq * slice_size)
            chunk = f.read(slice_size)
        
        for attempt in range(3):
            query = urllib.parse.urlencode({
                "method": "upload", "access_token": access_token,
                "type": "tmpfile", "uploadid": uploadid,
                "partseq": str(part_seq), "path": remote_path,
            })
            superfile_url = f"https://d.pcs.baidu.com/rest/2.0/pcs/superfile2?{query}"
            
            boundary = uuid.uuid4().hex
            multipart_body = (
                f"--{boundary}\r\n".encode() +
                b'Content-Disposition: form-data; name="file"; filename="file"\r\n' +
                b"Content-Type: application/octet-stream\r\n\r\n" +
                chunk +
                f"\r\n--{boundary}--\r\n".encode()
            )
            
            try:
                req = urllib.request.Request(superfile_url, data=multipart_body, method="POST")
                req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
                with urllib.request.urlopen(req, timeout=120) as resp:
                    json.loads(resp.read().decode())
                
                with progress_lock:
                    uploaded_parts += 1
                    if uploaded_parts % 20 == 0 or uploaded_parts == total_parts:
                        elapsed = time.time() - t0
                        speed = uploaded_parts * slice_size / 1024 / 1024 / elapsed if elapsed > 0 else 0
                        logger.info(f"    进度: {uploaded_parts}/{total_parts} ({speed:.1f} MB/s)")
                return True
            except Exception as e:
                if attempt < 2:
                    time.sleep(2)
                else:
                    logger.error(f"    分片 {part_seq} 上传失败: {e}")
                    failed = True
                    return False
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(upload_single_part, i) for i in range(total_parts)]
        for future in as_completed(futures):
            if not future.result():
                failed = True
                break
    
    if failed:
        logger.error("  分片上传失败")
        return None
    
    # Step 3: create 合并
    create_query = urllib.parse.urlencode({"method": "create", "access_token": access_token})
    create_body = urllib.parse.urlencode({
        "path": remote_path, "size": str(file_size),
        "isdir": "0", "block_list": json.dumps(block_list),
        "uploadid": uploadid, "ondup": "overwrite",
    }).encode()
    
    req = urllib.request.Request(
        f"https://pan.baidu.com/rest/2.0/xpan/file?{create_query}",
        data=create_body, method="POST"
    )
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
    except Exception as e:
        logger.error(f"  create 合并失败: {e}")
        return None
    
    if result.get("errno") == 0 or "path" in result:
        path = result.get("path", remote_path)
        logger.info(f"  上传成功: {path}")
        return path
    else:
        logger.error(f"  create 错误: errno={result.get('errno')}")
        return None


def baidu_create_share(file_path, access_token, pwd="yolo"):
    """创建百度网盘分享链接"""
    # 获取 fs_id
    list_query = urllib.parse.urlencode({
        "method": "list", "access_token": access_token,
        "dir": os.path.dirname(file_path),
    })
    list_url = f"https://pan.baidu.com/rest/2.0/xpan/file?{list_query}"
    fs_id = None
    try:
        req = urllib.request.Request(list_url)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        for f in data.get("list", []):
            if f.get("path") == file_path:
                fs_id = f.get("fs_id")
                break
    except Exception as e:
        logger.warning(f"  获取 fs_id 失败: {e}")
    
    if not fs_id:
        logger.error("  无法获取文件 fs_id")
        return None
    
    # 创建分享
    share_query = urllib.parse.urlencode({"method": "set", "access_token": access_token})
    share_url = f"https://pan.baidu.com/rest/2.0/xpan/share?{share_query}"
    share_body = urllib.parse.urlencode({
        "fid_list": json.dumps([int(fs_id)]),
        "period": "0", "schannel": "4",
        "channel_list": "[]", "pwd": pwd,
    }).encode()
    
    req = urllib.request.Request(share_url, data=share_body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            if result.get("errno") == 0 and result.get("link"):
                link = result["link"]
                if "pwd=" not in link:
                    separator = "&" if "?" in link else "?"
                    link = f"{link}{separator}pwd={pwd}"
                logger.info(f"  分享链接: {link}")
                return link
            else:
                logger.error(f"  分享失败: errno={result.get('errno')}")
                return None
    except Exception as e:
        logger.error(f"  创建分享失败: {e}")
        return None


def process_one_dataset(row_data, access_token):
    """处理单个数据集"""
    row = row_data["row"]
    roboflow_url = row_data.get("roboflow", "")
    img_info = row_data.get("img_info", "")
    
    logger.info(f"\n{'='*60}")
    logger.info(f"处理: 行{row}")
    logger.info(f"Roboflow: {roboflow_url[:80]}")
    
    if not roboflow_url or "roboflow.com" not in roboflow_url:
        logger.error("  无有效 Roboflow URL，跳过")
        return False
    
    # 生成标题
    title = generate_title_from_roboflow(roboflow_url, img_info)
    logger.info(f"  生成标题: {title}")
    
    # 解析 Roboflow URL
    workspace, project, version_num = parse_roboflow_url(roboflow_url)
    if not workspace or not project:
        logger.error("  无法解析 Roboflow URL")
        return False
    
    # 1. 下载
    dataset_dir = download_roboflow_dataset(workspace, project, version_num)
    if not dataset_dir:
        return False
    
    # 2. 压缩
    safe_title = title.replace(" ", "_").replace("/", "_").replace(":", "_")[:80]
    zip_path = os.path.join(PACKAGE_DIR, f"{safe_title}.zip")
    compress_dataset(dataset_dir, zip_path)
    logger.info(f"  ZIP 大小: {os.path.getsize(zip_path) / 1024 / 1024:.1f} MB")
    
    # 3. 上传百度网盘
    remote_path = f"{BAIDU_REMOTE_DIR}/{safe_title}.zip"
    uploaded_path = baidu_upload_xpan_concurrent(zip_path, remote_path, access_token, UPLOAD_WORKERS)
    
    if not uploaded_path:
        logger.error("  上传失败")
        return False
    
    # 4. 创建分享链接
    share_link = baidu_create_share(uploaded_path, access_token)
    
    if share_link:
        share_text = f"通过网盘分享的文件：{safe_title}.zip\n链接: {share_link} 提取码: yolo"
    else:
        share_text = f"百度网盘路径: {uploaded_path}（分享功能暂不可用）"
    
    # 5. 更新腾讯文档（标题列 = 第1列）
    logger.info(f"  更新腾讯文档行{row}...")
    result = write_cell(row, 1, share_text)
    logger.info(f"  表格已更新")
    
    # 6. 清理
    shutil.rmtree(dataset_dir, ignore_errors=True)
    if os.path.exists(zip_path):
        os.remove(zip_path)
    
    return True


def main():
    import argparse
    parser = argparse.ArgumentParser(description="批量处理无标题数据集")
    parser.add_argument("--limit", type=int, default=0, help="限制处理数量")
    parser.add_argument("--row", type=int, default=0, help="只处理指定行")
    parser.add_argument("--dry-run", action="store_true", help="只预览不执行")
    args = parser.parse_args()
    
    os.makedirs(BASE_DIR, exist_ok=True)
    os.makedirs(PACKAGE_DIR, exist_ok=True)
    
    # 获取有效的 Access Token
    access_token = get_valid_baidu_token()
    if not access_token:
        logger.error("无法获取有效的百度网盘 Access Token")
        return
    
    # 读取待处理数据集列表
    with open(os.path.join(SCRIPT_DIR, "..", "no_title_datasets.json"), "r", encoding="utf-8") as f:
        datasets = json.load(f)
    
    logger.info(f"共 {len(datasets)} 个待处理数据集")
    
    if args.row:
        datasets = [d for d in datasets if d["row"] == args.row]
    if args.limit:
        datasets = datasets[:args.limit]
    
    if args.dry_run:
        logger.info("\n[DRY RUN] 待处理列表:")
        for d in datasets:
            logger.info(f"  行{d['row']}: {d.get('name', '未知')}")
        return
    
    success = 0
    failed = 0
    for i, item in enumerate(datasets, 1):
        logger.info(f"\n[{i}/{len(datasets)}]")
        if process_one_dataset(item, access_token):
            success += 1
        else:
            failed += 1
        time.sleep(2)
    
    logger.info(f"\n{'='*60}")
    logger.info(f"完成: 成功 {success}, 失败 {failed}")
    logger.info(f"{'='*60}")


if __name__ == "__main__":
    main()
