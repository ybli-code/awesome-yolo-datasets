#!/usr/bin/env python3
"""
GitHub Actions: 通用URL数据集下载 → 上传百度网盘 → 写回腾讯文档
支持直接下载URL（如Harvard Dataverse、HuggingFace、OpenDataLab等）
不解压，直接重命名上传。
"""
import os
import sys
import json
import time
import hashlib
import shutil
import urllib.request
import urllib.parse
import urllib.error
import concurrent.futures
import logging
import re

# ===== 配置（从 GitHub Secrets 读取）=====
BAIDU_ACCESS_TOKEN = os.environ.get("BAIDU_ACCESS_TOKEN", "")
TENCENT_TOKEN = os.environ.get("TENCENT_TOKEN", "")
FILE_ID = os.environ.get("FILE_ID", "DUWVWRHN4bVhWb3Rp")
SHEET_ID = os.environ.get("SHEET_ID", "BB08J2")
MCP_URL = "https://docs.qq.com/openapi/mcp"

TEMP_DIR = "/tmp/dataset_downloads"
os.makedirs(TEMP_DIR, exist_ok=True)

DOWNLOAD_THREADS = 16
CHUNK_SIZE = 4 * 1024 * 1024

# ===== 日志 =====
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("gh_url_download")


# ===== 腾讯文档 MCP =====
def call_mcp(tool_name, arguments, req_id=10):
    payload = json.dumps({
        "jsonrpc": "2.0", "id": req_id,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments}
    }).encode()
    req = urllib.request.Request(MCP_URL, data=payload, method="POST")
    req.add_header("Authorization", TENCENT_TOKEN)
    req.add_header("Content-Type", "application/json")
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            if attempt < 2:
                time.sleep(2 ** attempt)
            else:
                raise


def write_cell(row, col, value):
    return call_mcp("sheet.set_cell_value", {
        "file_id": FILE_ID, "sheet_id": SHEET_ID,
        "row": row, "col": col,
        "value_type": "STRING", "string_value": value
    }, req_id=row)


# ===== 多线程下载 =====
def _download_range(url, start, end, output_path, idx):
    headers = {"Range": f"bytes={start}-{end}", "User-Agent": "Mozilla/5.0"}
    tmp_path = f"{output_path}.part{idx}"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=180) as resp:
            with open(tmp_path, 'wb') as f:
                while True:
                    chunk = resp.read(512 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
        return idx, end - start + 1, None
    except Exception as e:
        return idx, 0, str(e)


def concurrent_download(url, output_path, num_threads=DOWNLOAD_THREADS):
    """多线程并发下载，支持Range请求"""
    # 获取文件大小
    try:
        req = urllib.request.Request(url, headers={"Range": "bytes=0-0", "User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            content_range = resp.headers.get("Content-Range", "")
            if "/" in content_range:
                total_size = int(content_range.split("/")[-1])
            else:
                total_size = int(resp.headers.get("Content-Length", 0))
    except Exception as e:
        logger.warning(f"  获取文件信息失败: {e}，单线程下载")
        return _single_download(url, output_path)

    if total_size == 0:
        return _single_download(url, output_path)

    logger.info(f"  文件大小: {total_size / 1024 / 1024:.1f} MB, {num_threads}线程并发")

    chunk_size = total_size // num_threads
    ranges = []
    for i in range(num_threads):
        start = i * chunk_size
        end = start + chunk_size - 1 if i < num_threads - 1 else total_size - 1
        ranges.append((start, end, i))

    t0 = time.time()
    downloaded = 0
    errors = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = {executor.submit(_download_range, url, s, e, output_path, i): (s, e, i) for s, e, i in ranges}
        for future in concurrent.futures.as_completed(futures):
            idx, bytes_done, err = future.result()
            if err:
                errors.append((idx, err))
            else:
                downloaded += bytes_done
                logger.info(f"  分片 {idx+1}/{num_threads} 完成 ({bytes_done/1024/1024:.1f} MB)")

    logger.info(f"  下载完成: {downloaded / 1024 / 1024:.1f} MB / {time.time()-t0:.1f}s")

    # 重试失败分片
    if errors:
        logger.info(f"  重试 {len(errors)} 个失败分片...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(errors)) as executor:
            for idx, err in errors:
                s, e, _ = ranges[idx]
                executor.submit(_download_range, url, s, e, output_path, idx)

    # 合并分片
    logger.info("  合并分片...")
    with open(output_path, 'wb') as out_f:
        for i in range(num_threads):
            tmp = f"{output_path}.part{i}"
            if os.path.exists(tmp):
                with open(tmp, 'rb') as f:
                    out_f.write(f.read())
                os.remove(tmp)

    final_size = os.path.getsize(output_path)
    logger.info(f"  最终文件大小: {final_size / 1024 / 1024:.1f} MB")
    return final_size == total_size or final_size > 0


def _single_download(url, output_path):
    """单线程下载（Range不支持时回退）"""
    logger.info("  单线程下载...")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=600) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        downloaded = 0
        with open(output_path, 'wb') as f:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if total > 0 and downloaded % (10 * 1024 * 1024) < 1024 * 1024:
                    logger.info(f"  进度: {downloaded/1024/1024:.1f}/{total/1024/1024:.1f} MB")
    return True


# ===== 百度网盘上传 =====
def ensure_baidu_folder(folder_path):
    query = urllib.parse.urlencode({"method": "create", "access_token": BAIDU_ACCESS_TOKEN})
    body = urllib.parse.urlencode({"path": folder_path, "isdir": "1", "size": "0", "block_list": "[]"}).encode()
    req = urllib.request.Request(f"https://pan.baidu.com/rest/2.0/xpan/file?{query}", data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
        if result.get("errno") in [0, -8]:
            logger.info(f"  文件夹就绪: {folder_path}")
    except Exception as e:
        logger.warning(f"  创建文件夹失败: {e}")


def baidu_upload(file_path, remote_path):
    """xpan 分片上传到百度网盘"""
    folder = os.path.dirname(remote_path)
    if folder:
        ensure_baidu_folder(folder)

    file_size = os.path.getsize(file_path)
    logger.info(f"  上传文件大小: {file_size / 1024 / 1024:.1f} MB")

    # 计算分片MD5
    slice_size = 4 * 1024 * 1024
    block_list = []
    with open(file_path, 'rb') as f:
        while True:
            chunk = f.read(slice_size)
            if not chunk:
                break
            block_list.append(hashlib.md5(chunk).hexdigest())

    # precreate
    query = urllib.parse.urlencode({"method": "precreate", "access_token": BAIDU_ACCESS_TOKEN})
    body = urllib.parse.urlencode({
        "path": remote_path, "size": str(file_size), "isdir": "0",
        "autoinit": "1", "block_list": json.dumps(block_list), "ondup": "overwrite",
    }).encode()
    req = urllib.request.Request(f"https://pan.baidu.com/rest/2.0/xpan/file?{query}", data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
    except Exception as e:
        logger.error(f"  precreate失败: {e}")
        return None
    if result.get("errno") != 0:
        logger.error(f"  precreate错误: {result}")
        return None
    uploadid = result.get("uploadid", "")

    # superfile2 分片上传（并发）
    import uuid
    t0 = time.time()

    def upload_slice(i):
        with open(file_path, 'rb') as f:
            f.seek(i * slice_size)
            chunk = f.read(slice_size)
        for attempt in range(3):
            query = urllib.parse.urlencode({
                "method": "upload", "access_token": BAIDU_ACCESS_TOKEN,
                "type": "tmpfile", "uploadid": uploadid,
                "partseq": str(i), "path": remote_path,
            })
            url = f"https://d.pcs.baidu.com/rest/2.0/pcs/superfile2?{query}"
            boundary = uuid.uuid4().hex
            multipart = (
                f"--{boundary}\r\n".encode() +
                b'Content-Disposition: form-data; name="file"; filename="file"\r\n' +
                b"Content-Type: application/octet-stream\r\n\r\n" +
                chunk + f"\r\n--{boundary}--\r\n".encode()
            )
            try:
                req = urllib.request.Request(url, data=multipart, method="POST")
                req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
                with urllib.request.urlopen(req, timeout=120) as resp:
                    json.loads(resp.read().decode())
                return i, True, None
            except Exception as e:
                if attempt < 2:
                    time.sleep(2)
                else:
                    return i, False, str(e)
        return i, False, "max retries"

    # 8线程并发上传
    upload_errors = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(upload_slice, i): i for i in range(len(block_list))}
        for future in concurrent.futures.as_completed(futures):
            i, success, err = future.result()
            if not success:
                upload_errors.append((i, err))
            if (i + 1) % 20 == 0 or i + 1 == len(block_list):
                speed = (i + 1) * slice_size / 1024 / 1024 / (time.time() - t0) if time.time() > t0 else 0
                logger.info(f"    上传进度: {i+1}/{len(block_list)} 分片 ({speed:.1f} MB/s)")

    if upload_errors:
        logger.error(f"  {len(upload_errors)} 个分片上传失败")
        return None

    # create
    query = urllib.parse.urlencode({"method": "create", "access_token": BAIDU_ACCESS_TOKEN})
    body = urllib.parse.urlencode({
        "path": remote_path, "size": str(file_size), "isdir": "0",
        "block_list": json.dumps(block_list), "uploadid": uploadid, "ondup": "overwrite",
    }).encode()
    req = urllib.request.Request(f"https://pan.baidu.com/rest/2.0/xpan/file?{query}", data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
    except Exception as e:
        logger.error(f"  create失败: {e}")
        return None
    if result.get("errno") == 0 or "path" in result:
        path = result.get("path", remote_path)
        logger.info(f"  ✓ 上传成功: {path}")
        return path
    logger.error(f"  create错误: {result}")
    return None


def baidu_create_share(file_path, pwd="yolo"):
    """创建百度网盘分享链接"""
    # 获取 fs_id
    query = urllib.parse.urlencode({"method": "list", "access_token": BAIDU_ACCESS_TOKEN, "dir": os.path.dirname(file_path)})
    req = urllib.request.Request(f"https://pan.baidu.com/rest/2.0/xpan/file?{query}")
    fs_id = None
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        for f in data.get("list", []):
            if f.get("path") == file_path:
                fs_id = f.get("fs_id")
                break
    except Exception as e:
        logger.warning(f"  获取fs_id失败: {e}")
    if not fs_id:
        logger.error("  无法获取fs_id")
        return None

    # 创建分享
    query = urllib.parse.urlencode({"method": "set", "access_token": BAIDU_ACCESS_TOKEN})
    body = urllib.parse.urlencode({
        "fid_list": json.dumps([int(fs_id)]),
        "period": "0", "schannel": "4", "channel_list": "[]", "pwd": pwd,
    }).encode()
    req = urllib.request.Request(f"https://pan.baidu.com/rest/2.0/xpan/share?{query}", data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
        if result.get("errno") == 0 and result.get("link"):
            link = result["link"]
            logger.info(f"  ✓ 分享链接: {link}")
            return link
        logger.error(f"  分享失败: {result}")
        return None
    except Exception as e:
        logger.error(f"  创建分享失败: {e}")
        return None


# ===== 工具函数 =====
def sanitize(name):
    """清理文件名中的非法字符"""
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    name = re.sub(r'\s+', '_', name).strip('_')
    return name[:80] if len(name) > 80 else name


def process_one(ds):
    """处理一个数据集"""
    title = ds["title"]
    url = ds["url"]
    row = ds.get("row")  # 表格行号（可选，用于写回）
    safe_title = sanitize(title)

    logger.info(f"{'='*60}")
    logger.info(f"处理: {title}")
    logger.info(f"  URL: {url[:100]}...")
    if row:
        logger.info(f"  表格行: {row}")

    # 下载
    zip_path = os.path.join(TEMP_DIR, f"{safe_title}.zip")
    logger.info("  开始下载...")
    try:
        if not concurrent_download(url, zip_path):
            logger.error("  下载失败")
            return False
    except Exception as e:
        logger.error(f"  下载异常: {e}")
        return False

    if not os.path.exists(zip_path) or os.path.getsize(zip_path) == 0:
        logger.error("  下载文件不存在或为空")
        return False

    logger.info(f"  下载完成: {os.path.getsize(zip_path) / 1024 / 1024:.1f} MB")

    # 上传百度网盘（不解压，直接上传）
    remote_path = f"/apps/同享AI数据集/{safe_title}.zip"
    logger.info("  上传百度网盘...")
    uploaded_path = baidu_upload(zip_path, remote_path)
    if not uploaded_path:
        if os.path.exists(zip_path):
            os.remove(zip_path)
        return False

    # 创建分享
    logger.info("  创建分享链接...")
    share_link = baidu_create_share(uploaded_path)
    if share_link:
        share_text = f"通过网盘分享的文件：{safe_title}.zip\n链接: {share_link} 提取码: yolo"
    else:
        share_text = f"百度网盘路径: {uploaded_path}（分享暂不可用）"

    # 写回腾讯文档（如果指定了行号）
    if row:
        logger.info(f"  写回腾讯文档行{row}...")
        result = write_cell(row, 1, share_text)
        sc = result.get("result", {}).get("structuredContent", {})
        if sc.get("error", ""):
            logger.error(f"  写回失败: {sc['error']}")
        else:
            logger.info(f"  ✓ 已写回行{row}")

    # 清理本地文件
    if os.path.exists(zip_path):
        os.remove(zip_path)
        logger.info("  本地文件已清理")

    logger.info(f"  ✓ 完成: {title}")
    return True


def main():
    # 检查凭据
    missing = []
    if not BAIDU_ACCESS_TOKEN:
        missing.append("BAIDU_ACCESS_TOKEN")
    if not TENCENT_TOKEN:
        missing.append("TENCENT_TOKEN")
    if missing:
        logger.error(f"缺少环境变量: {', '.join(missing)}")
        sys.exit(1)

    # 读取数据集列表
    json_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "datasets_url.json")
    if not os.path.exists(json_path):
        json_path = "datasets_url.json"
    if not os.path.exists(json_path):
        logger.error(f"配置文件不存在: {json_path}")
        sys.exit(1)

    with open(json_path, "r", encoding="utf-8") as f:
        datasets = json.load(f)

    logger.info(f"共 {len(datasets)} 个数据集待处理")

    success = 0
    failed = 0
    for i, ds in enumerate(datasets, 1):
        logger.info(f"\n[{i}/{len(datasets)}]")
        try:
            if process_one(ds):
                success += 1
            else:
                failed += 1
        except Exception as e:
            logger.error(f"  异常: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
        if i < len(datasets):
            time.sleep(2)

    logger.info(f"\n{'='*60}")
    logger.info(f"完成: 成功 {success}, 失败 {failed}")
    logger.info(f"{'='*60}")

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
