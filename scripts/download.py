#!/usr/bin/env python3
"""
GitHub Action 自包含下载脚本：Roboflow → 百度网盘 → 腾讯文档写回

特性：
  - 多线程并发下载（32线程 + Range 请求）
  - 百度网盘 xpan 并发分片上传（8线程）
  - 自动创建分享链接（提取码 yolo）
  - 写回腾讯文档表格
  - 从 datasets.json 读取待处理列表
  - 从环境变量读取所有凭据

环境变量：
  ROBOFLOW_API_KEY  - Roboflow API Key
  BAIDU_ACCESS_TOKEN - 百度网盘 Access Token
  TENCENT_TOKEN     - 腾讯文档 MCP Token
  FILE_ID           - 腾讯文档 file_id
  SHEET_ID          - 腾讯文档 sheet_id

用法：
  python3 github_action_download.py
"""
import hashlib
import json
import os
import shutil
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
import zipfile
import concurrent.futures
import threading
import uuid

# ===== 凭据（从环境变量读取）=====
ROBOFLOW_API_KEY = os.environ.get("ROBOFLOW_API_KEY", "")
BAIDU_ACCESS_TOKEN = os.environ.get("BAIDU_ACCESS_TOKEN", "")
TENCENT_TOKEN = os.environ.get("TENCENT_TOKEN", "")
FILE_ID = os.environ.get("FILE_ID", "")
SHEET_ID = os.environ.get("SHEET_ID", "")
MCP_URL = "https://docs.qq.com/openapi/mcp"

WORK_DIR = os.environ.get("WORK_DIR", "/tmp/dataset_downloads")
DOWNLOAD_THREADS = int(os.environ.get("DOWNLOAD_THREADS", "32"))
UPLOAD_CONCURRENCY = int(os.environ.get("UPLOAD_CONCURRENCY", "8"))
CHUNK_SIZE = 4 * 1024 * 1024  # 4MB


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


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
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def write_cell(row, col, value):
    result = call_mcp("sheet.set_cell_value", {
        "file_id": FILE_ID, "sheet_id": SHEET_ID,
        "row": row, "col": col,
        "value_type": "STRING", "string_value": value
    }, req_id=row)
    sc = result.get("result", {}).get("structuredContent", {})
    return not sc.get("error", "")


# ===== 多线程并发下载 =====
def _download_range(url, start, end, output_path, idx):
    headers = {"Range": f"bytes={start}-{end}"}
    tmp_path = f"{output_path}.part{idx}"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=120) as resp:
            with open(tmp_path, 'wb') as f:
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
        return idx, end - start + 1, None
    except Exception as e:
        return idx, 0, str(e)


def concurrent_download(url, output_path, num_threads=DOWNLOAD_THREADS):
    # 获取文件大小
    try:
        req = urllib.request.Request(url, headers={"Range": "bytes=0-0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            content_range = resp.headers.get("Content-Range", "")
            if "/" in content_range:
                total_size = int(content_range.split("/")[-1])
            else:
                total_size = int(resp.headers.get("Content-Length", 0))
    except Exception as e:
        log(f"  无法获取文件信息: {e}，回退单线程")
        return _single_thread_download(url, output_path)

    if total_size == 0:
        return _single_thread_download(url, output_path)

    log(f"  文件大小: {total_size / 1024 / 1024:.1f} MB, {num_threads}线程并发下载")

    chunk_size = total_size // num_threads
    ranges = []
    for i in range(num_threads):
        start = i * chunk_size
        end = start + chunk_size - 1 if i < num_threads - 1 else total_size - 1
        ranges.append((start, end, i))

    t0 = time.time()
    downloaded_bytes = 0
    errors = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = {executor.submit(_download_range, url, s, e, output_path, i): (s, e, i) for s, e, i in ranges}
        for future in concurrent.futures.as_completed(futures):
            idx, bytes_done, err = future.result()
            if err:
                errors.append((idx, err))
            else:
                downloaded_bytes += bytes_done

    # 重试失败分片
    if errors:
        log(f"  重试 {len(errors)} 个失败分片...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(errors)) as executor:
            futures = {}
            for idx, err in errors:
                s, e, _ = ranges[idx]
                futures[executor.submit(_download_range, url, s, e, output_path, idx)] = idx
            for future in concurrent.futures.as_completed(futures):
                idx, bytes_done, err = future.result()
                if not err:
                    downloaded_bytes += bytes_done

    # 合并分片
    log("  合并分片...")
    with open(output_path, 'wb') as out_f:
        for i in range(num_threads):
            tmp_path = f"{output_path}.part{i}"
            if os.path.exists(tmp_path):
                with open(tmp_path, 'rb') as f:
                    out_f.write(f.read())
                os.remove(tmp_path)

    actual_size = os.path.getsize(output_path)
    if actual_size != total_size:
        log(f"  文件大小不匹配: {actual_size} vs {total_size}")
        return False

    speed = downloaded_bytes / (time.time() - t0) / 1024 / 1024 if time.time() > t0 else 0
    log(f"  下载完成: {downloaded_bytes / 1024 / 1024:.1f} MB ({speed:.1f} MB/s)")
    return True


def _single_thread_download(url, output_path):
    log("  单线程下载中...")
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=600) as resp:
        with open(output_path, 'wb') as f:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
    return True


# ===== Roboflow 下载 =====
def parse_roboflow_url(url):
    import re
    match = re.match(r'https?://universe\.roboflow\.com/([^/]+)/([^/?#]+)', url)
    if not match:
        return None, None, None
    workspace = match.group(1)
    project = match.group(2)
    ver_match = re.search(r'/dataset/(\d+)', url)
    version = int(ver_match.group(1)) if ver_match else None
    return workspace, project, version


def download_roboflow(workspace, project, version_num=None, output_dir=WORK_DIR):
    if not version_num:
        try:
            url = f"https://api.roboflow.com/{workspace}/{project}?api_key={ROBOFLOW_API_KEY}"
            with urllib.request.urlopen(url, timeout=30) as resp:
                data = json.loads(resp.read().decode())
            versions = data.get("versions", [])
            if not versions:
                log("  该项目没有可用版本")
                return None
            version_num = len(versions)
        except Exception as e:
            log(f"  获取版本信息失败: {e}")
            return None

    log(f"  下载版本: v{version_num}")

    # 获取下载链接
    try:
        api_url = f"https://api.roboflow.com/{workspace}/{project}/{version_num}/yolov8?api_key={ROBOFLOW_API_KEY}"
        with urllib.request.urlopen(api_url, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        download_link = data.get("export", {}).get("link") or data.get("link")
        if not download_link:
            log("  数据集正在生成中，等待30秒...")
            time.sleep(30)
            with urllib.request.urlopen(api_url, timeout=30) as resp:
                data = json.loads(resp.read().decode())
            download_link = data.get("export", {}).get("link") or data.get("link")
        if not download_link:
            log(f"  无法获取下载链接")
            return None
    except Exception as e:
        log(f"  获取下载链接失败: {e}")
        return None

    os.makedirs(output_dir, exist_ok=True)
    zip_path = os.path.join(output_dir, f"{workspace}_{project}_v{version_num}.zip")
    extract_dir = os.path.join(output_dir, f"{workspace}_{project}_v{version_num}")

    log(f"  开始下载...")
    if not concurrent_download(download_link, zip_path):
        return None

    # 解压
    os.makedirs(extract_dir, exist_ok=True)
    with zipfile.ZipFile(zip_path, 'r') as zf:
        zf.extractall(extract_dir)
    os.remove(zip_path)
    log(f"  解压完成: {extract_dir}")
    return extract_dir


def compress_dataset(source_dir, zip_path):
    if os.path.exists(zip_path):
        os.remove(zip_path)
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(source_dir):
            for file in files:
                filepath = os.path.join(root, file)
                arcname = os.path.relpath(filepath, source_dir)
                zf.write(filepath, arcname)
    return zip_path


# ===== 百度网盘 xpan 并发分片上传 =====
def ensure_baidu_folder(folder_path, access_token):
    query = urllib.parse.urlencode({"method": "create", "access_token": access_token})
    body = urllib.parse.urlencode({"path": folder_path, "isdir": "1", "size": "0", "block_list": "[]"}).encode()
    req = urllib.request.Request(f"https://pan.baidu.com/rest/2.0/xpan/file?{query}", data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
        if result.get("errno") in [0, -8]:
            log(f"  文件夹就绪: {folder_path}")
    except Exception as e:
        log(f"  创建文件夹失败: {e}")


def baidu_upload_xpan(file_path, remote_path, access_token):
    """百度网盘 xpan 并发分片上传（8线程）"""
    file_size = os.path.getsize(file_path)
    slice_size = CHUNK_SIZE
    log(f"  xpan并发上传: {file_size / 1024 / 1024:.1f} MB, {UPLOAD_CONCURRENCY}线程")

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
        "path": remote_path, "size": str(file_size), "isdir": "0",
        "autoinit": "1", "block_list": json.dumps(block_list), "ondup": "overwrite",
    }).encode()
    req = urllib.request.Request(f"https://pan.baidu.com/rest/2.0/xpan/file?{query_params}", data=body_data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
    except Exception as e:
        log(f"  precreate 失败: {e}")
        return None

    if result.get("errno") != 0:
        log(f"  precreate 错误: errno={result.get('errno')}")
        return None

    uploadid = result.get("uploadid", "")
    if not uploadid:
        return None
    log(f"  precreate 成功")

    # Step 2: 并发分片上传
    total_parts = len(block_list)
    uploaded_parts = 0
    upload_lock = threading.Lock()
    failed_parts = []
    t0 = time.time()

    def _upload_part(part_seq):
        nonlocal uploaded_parts
        offset = part_seq * slice_size
        read_size = min(slice_size, file_size - offset)
        with open(file_path, 'rb') as f:
            f.seek(offset)
            chunk = f.read(read_size)

        for attempt in range(3):
            query = urllib.parse.urlencode({
                "method": "upload", "access_token": access_token, "type": "tmpfile",
                "uploadid": uploadid, "partseq": str(part_seq), "path": remote_path,
            })
            superfile_url = f"https://d.pcs.baidu.com/rest/2.0/pcs/superfile2?{query}"
            boundary = uuid.uuid4().hex
            multipart_body = (
                f"--{boundary}\r\n".encode() +
                b'Content-Disposition: form-data; name="file"; filename="file"\r\n' +
                b"Content-Type: application/octet-stream\r\n\r\n" + chunk +
                f"\r\n--{boundary}--\r\n".encode()
            )
            try:
                req = urllib.request.Request(superfile_url, data=multipart_body, method="POST")
                req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
                with urllib.request.urlopen(req, timeout=120) as resp:
                    json.loads(resp.read().decode())
                with upload_lock:
                    uploaded_parts += 1
                    if uploaded_parts % 20 == 0 or uploaded_parts == total_parts:
                        elapsed = time.time() - t0
                        speed = uploaded_parts * slice_size / 1024 / 1024 / elapsed if elapsed > 0 else 0
                        log(f"    进度: {uploaded_parts}/{total_parts} ({speed:.1f} MB/s)")
                return True
            except Exception as e:
                if attempt < 2:
                    time.sleep(2)
                else:
                    log(f"    分片 {part_seq} 失败: {e}")
                    return False
        return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=UPLOAD_CONCURRENCY) as executor:
        futures = {executor.submit(_upload_part, i): i for i in range(total_parts)}
        for future in concurrent.futures.as_completed(futures):
            if not future.result():
                failed_parts.append(futures[future])

    if failed_parts:
        log(f"  {len(failed_parts)} 个分片失败: {failed_parts[:10]}")
        return None

    speed = file_size / 1024 / 1024 / (time.time() - t0) if time.time() > t0 else 0
    log(f"  上传完成: {speed:.1f} MB/s (并发{UPLOAD_CONCURRENCY})")

    # Step 3: create 合并
    create_query = urllib.parse.urlencode({"method": "create", "access_token": access_token})
    create_body = urllib.parse.urlencode({
        "path": remote_path, "size": str(file_size), "isdir": "0",
        "block_list": json.dumps(block_list), "uploadid": uploadid, "ondup": "overwrite",
    }).encode()
    req = urllib.request.Request(f"https://pan.baidu.com/rest/2.0/xpan/file?{create_query}", data=create_body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
    except Exception as e:
        log(f"  create 合并失败: {e}")
        return None

    if result.get("errno") == 0 or "path" in result:
        path = result.get("path", remote_path)
        log(f"  ✓ 上传成功: {path}")
        return path
    else:
        log(f"  create 错误: errno={result.get('errno')}")
        return None


def baidu_create_share(file_path, access_token, pwd="yolo"):
    """创建百度网盘分享链接"""
    # 获取 fs_id
    list_query = urllib.parse.urlencode({"method": "list", "access_token": access_token, "dir": os.path.dirname(file_path)})
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
        log(f"  获取 fs_id 失败: {e}")

    if not fs_id:
        return None

    share_query = urllib.parse.urlencode({"method": "set", "access_token": access_token})
    share_url = f"https://pan.baidu.com/rest/2.0/xpan/share?{share_query}"
    share_body = urllib.parse.urlencode({
        "fid_list": json.dumps([int(fs_id)]), "period": "0",
        "schannel": "4", "channel_list": "[]", "pwd": pwd,
    }).encode()
    req = urllib.request.Request(share_url, data=share_body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
        if result.get("errno") == 0 and result.get("link"):
            link = result["link"]
            log(f"  ✓ 分享链接: {link} (提取码: {pwd})")
            return link
        else:
            log(f"  分享失败: errno={result.get('errno')}")
            return None
    except Exception as e:
        log(f"  创建分享失败: {e}")
        return None


# ===== 主流程 =====
def process_dataset(ds):
    """处理单个数据集：下载 → 压缩 → 上传 → 分享 → 写回"""
    row = ds["row"]
    workspace = ds["workspace"]
    project = ds["project"]
    version_num = ds.get("version")
    title = ds.get("name", f"{workspace}/{project}")

    log(f"{'='*50}")
    log(f"处理: {title} (行{row})")
    log(f"  Workspace: {workspace}, Project: {project}, Version: {version_num}")

    # 1. 下载
    dataset_dir = download_roboflow(workspace, project, version_num)
    if not dataset_dir:
        return False

    # 2. 压缩
    safe_title = title.replace(" ", "_").replace("/", "_").replace(":", "_")[:80]
    zip_path = os.path.join(WORK_DIR, f"{safe_title}.zip")
    log("  正在压缩...")
    compress_dataset(dataset_dir, zip_path)
    log(f"  ZIP 大小: {os.path.getsize(zip_path) / 1024 / 1024:.1f} MB")

    # 清理解压目录
    shutil.rmtree(dataset_dir, ignore_errors=True)

    # 3. 上传到百度网盘
    remote_path = f"/apps/同享AI数据集/{title}.zip"
    folder = os.path.dirname(remote_path)
    ensure_baidu_folder(folder, BAIDU_ACCESS_TOKEN)
    uploaded_path = baidu_upload_xpan(zip_path, remote_path, BAIDU_ACCESS_TOKEN)
    if not uploaded_path:
        return False

    # 4. 创建分享
    share_link = baidu_create_share(uploaded_path, BAIDU_ACCESS_TOKEN)
    if share_link:
        share_text = f"通过网盘分享的文件：{title}\n链接: {share_link} 提取码: yolo"
    else:
        share_text = f"百度网盘路径: {uploaded_path}（分享功能暂不可用）"

    # 5. 写回腾讯文档（列1 = 网盘下载地址）
    log("  写回腾讯文档...")
    if write_cell(row, 1, share_text):
        log(f"  ✓ 已写回表格行{row}")
    else:
        log(f"  ✗ 写回表格失败")

    # 清理 ZIP（用户要求不清理空间则保留）
    if os.environ.get("KEEP_FILES") != "1":
        if os.path.exists(zip_path):
            os.remove(zip_path)

    return True


def main():
    # 读取 datasets.json
    datasets_file = os.environ.get("DATASETS_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "datasets.json"))
    if not os.path.exists(datasets_file):
        log(f"错误: 找不到 datasets.json: {datasets_file}")
        sys.exit(1)

    with open(datasets_file, "r", encoding="utf-8") as f:
        datasets = json.load(f)

    log(f"共 {len(datasets)} 个数据集待处理")
    log(f"下载线程: {DOWNLOAD_THREADS}, 上传并发: {UPLOAD_CONCURRENCY}")

    success = 0
    failed = 0
    for i, ds in enumerate(datasets, 1):
        log(f"\n[{i}/{len(datasets)}]")
        try:
            if process_dataset(ds):
                success += 1
            else:
                failed += 1
        except Exception as e:
            log(f"  异常: {e}")
            failed += 1
        time.sleep(1)

    log(f"\n{'='*50}")
    log(f"完成: 成功 {success} 个, 失败 {failed} 个")
    log(f"{'='*50}")

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
