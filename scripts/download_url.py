#!/usr/bin/env python3

"""

GitHub Actions: 通用URL数据集下载 → 网盘上传（百度/夸克自动选择）→ 写回腾讯文档

支持直接下载URL（如Harvard Dataverse、HuggingFace、OpenDataLab等）

不解压，直接重命名上传。



网盘选择（按环境变量自动判断）：

  1. QUARK_COOKIE 非空 → 上传到夸克网盘 + 创建分享（提取码 yolo）

  2. BAIDU_ACCESS_TOKEN 非空 → 上传到百度网盘 + 创建分享（提取码 yolo）

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



# 尝试导入夸克网盘客户端（QuarkPan成熟库，同目录 quark_client/）

try:

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    from quark_client import QuarkClient

    QUARK_AVAILABLE = True

except ImportError:

    QUARK_AVAILABLE = False



# ===== 配置（从 GitHub Secrets 读取）=====

BAIDU_ACCESS_TOKEN = os.environ.get("BAIDU_ACCESS_TOKEN", "")

QUARK_COOKIE = os.environ.get("QUARK_COOKIE", "")

TENCENT_TOKEN = os.environ.get("TENCENT_TOKEN", "")



# 网盘后端选择：夸克优先，其次百度

NETDISK = "quark" if (QUARK_COOKIE and QUARK_AVAILABLE) else ("baidu" if BAIDU_ACCESS_TOKEN else "")

FILE_ID = os.environ.get("FILE_ID", "DUWVWRHN4bVhWb3Rp")

SHEET_ID = os.environ.get("SHEET_ID", "BB08J2")

MCP_URL = "https://docs.qq.com/openapi/mcp"



TEMP_DIR = "/tmp/dataset_downloads"

os.makedirs(TEMP_DIR, exist_ok=True)



# ===== RELAY 快链路（坑#117复核版: runner→GitHub Release→T4传夸克）=====

RELAY_MODE = os.environ.get("RELAY_MODE", "0") == "1"

GH_RELAY_TOKEN = os.environ.get("GH_RELAY_TOKEN", "")

RELAY_REPO = os.environ.get("GITHUB_REPOSITORY", "ybli-code/awesome-yolo-datasets")

RELAY_MANIFEST = {"expected": 0, "items": []}



def relay_stage(zip_path, safe_title, row):

    """RELAY 模式: 计算 md5 登记 manifest, zip 改 ASCII 名保留待 finalize 统一上传
    （坑: GitHub asset 中文名触发 already_exists, 必须 job{row}.zip ASCII 命名）"""

    import hashlib

    h = hashlib.md5()

    with open(zip_path, "rb") as f:

        for chunk in iter(lambda: f.read(1 << 22), b""):

            h.update(chunk)

    ascii_name = f"job{row}.zip"

    new_path = os.path.join(TEMP_DIR, ascii_name)

    if os.path.abspath(new_path) != os.path.abspath(zip_path):

        os.rename(zip_path, new_path)

    RELAY_MANIFEST["items"].append({

        "zip": ascii_name,

        "safe_title": safe_title,

        "row": row,

        "md5": h.hexdigest(),

        "size": os.path.getsize(new_path),

    })

    logger.info(f"RELAY: 已登记 {ascii_name} "
                f"({os.path.getsize(new_path)/1048576:.0f}MB, md5={h.hexdigest()[:8]}...)")



def relay_finalize():

    """RELAY 收尾: 创建 Release(tag=relay-<run_id>) 逐个上传 zip, body=manifest"""

    if not RELAY_MODE or not RELAY_MANIFEST["items"]:

        return

    import hashlib  # noqa

    import subprocess

    tag = f"relay-{os.environ.get('GITHUB_RUN_ID') or int(time.time())}"

    body = json.dumps(RELAY_MANIFEST, ensure_ascii=False)

    logger.info(f"RELAY: 创建 Release {tag} ({len(RELAY_MANIFEST['items'])} 资产)...")

    req = urllib.request.Request(
        f"https://api.github.com/repos/{RELAY_REPO}/releases",
        data=json.dumps({"tag_name": tag, "target_commitish": "main",
                         "name": tag, "body": body, "draft": False}).encode(),
        method="POST",
        headers={"Authorization": f"token {GH_RELAY_TOKEN}",
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json"})

    with urllib.request.urlopen(req, timeout=60) as r:

        rel = json.loads(r.read().decode())

    upload_url = rel["upload_url"].split("{")[0]

    for it in RELAY_MANIFEST["items"]:

        p = os.path.join(TEMP_DIR, it["zip"])

        logger.info(f"RELAY: 上传 {it['zip']} ({it['size']/1048576:.0f}MB)...")

        ok = False

        for attempt in range(3):

            res = subprocess.run(
                ["curl", "-sS", "--max-time", "3600",
                 "-H", f"Authorization: token {GH_RELAY_TOKEN}",
                 "-H", "Content-Type: application/zip",
                 "--data-binary", f"@{p}",
                 f"{upload_url}?name={urllib.parse.quote(it['zip'])}"],
                capture_output=True, text=True, timeout=3660)

            try:

                asset = json.loads(res.stdout)

                if asset.get("state") == "uploaded" and asset.get("size") == it["size"]:

                    ok = True

                    break

                logger.warning(f"RELAY: 第{attempt+1}次上传异常: {str(res.stdout)[:200]}")

            except Exception as e:

                logger.warning(f"RELAY: 第{attempt+1}次解析失败: {e} {res.stderr[:100]}")

            time.sleep(5)

        if not ok:

            raise RuntimeError(f"RELAY asset 上传失败: {it['zip']}")

    logger.info("RELAY: 全部资产上传完成, T4 将在 2 分钟内开始处理")



DOWNLOAD_THREADS = 16

CHUNK_SIZE = 4 * 1024 * 1024



# ===== 日志 =====

logging.basicConfig(

    level=logging.INFO,

    format="[%(asctime)s] %(levelname)s %(message)s",

    datefmt="%H:%M:%S",

)

logger = logging.getLogger("gh_url_download")

try:

    os.makedirs(TEMP_DIR, exist_ok=True)

    _fh = logging.FileHandler(os.path.join(TEMP_DIR, "run.log"), encoding="utf-8")

    _fh.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", "%H:%M:%S"))

    logger.addHandler(_fh)

except Exception as _e:

    print("FileHandler fail:", _e)





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





# ===== Google Drive 下载链接解析（处理大文件 confirm token）=====

class _NoRedirect(urllib.request.HTTPRedirectHandler):

    """禁止自动重定向，手动处理 Location"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):

        return None





def resolve_google_drive(url):

    """解析 Google Drive 分享链接，返回 (最终下载URL, is_google)。



    流程（手动控制重定向）：

    1. GET drive.google.com/uc?export=download&id=FILEID（不跟随重定向）

    2. 若 3xx 且带 Location -> 小文件直链，直接返回 Location

    3. 若 200 HTML -> 提取 confirm token -> drive.usercontent.google.com 下载直链

    4. 解析失败 -> raise ValueError（带响应片段便于诊断）

    """

    if "drive.google.com" not in url and "docs.google.com" not in url:

        return url, False

    m = re.search(r"[?&]id=([\w-]+)", url)

    if not m:

        m = re.search(r"/file/d/([\w-]+)", url)

    if not m:

        raise ValueError("无法从Google Drive链接提取file id: " + url[:80])

    file_id = m.group(1)



    probe = "https://drive.google.com/uc?export=download&id=" + file_id

    req = urllib.request.Request(probe, headers={"User-Agent": "Mozilla/5.0"})

    opener = urllib.request.build_opener(_NoRedirect())

    with opener.open(req, timeout=60) as resp:

        status = resp.status

        headers = dict(resp.headers)

        body = resp.read(2 * 1024 * 1024).decode(errors="ignore")



    loc = headers.get("Location", "")

    if status in (301, 302, 303, 307, 308) and loc:

        logger.info("  Google Drive 重定向: %s", loc[:120])

        return loc, True



    m2 = re.search(r'name="confirm"\s+value="([\w-]+)"', body)

    if not m2:

        m2 = re.search(r"confirm=([0-9A-Za-z\-_]+)", body)

    if m2:

        final = ("https://drive.usercontent.google.com/download?id=" + file_id

                 + "&export=download&confirm=" + m2.group(1))

        logger.info("  Google Drive confirm已解析: %s", final[:120])

        return final, True



    # 可能触发 virus scan / 需要登录 / 文件已删除

    raise ValueError(

        "Google Drive 解析失败: status=%s, body片段=%s" % (status, body[:200].replace("\n", " "))

    )





# ===== 多线程下载 =====

def _download_range(url, start, end, output_path, idx):

    headers = {"Range": f"bytes={start}-{end}", "User-Agent": "Mozilla/5.0"}

    tmp_path = f"{output_path}.part{idx}"

    try:

        req = urllib.request.Request(url, headers=headers)

        with urllib.request.urlopen(req, timeout=90) as resp:

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

    # 获取文件大小（探测后使用最终URL，避免后续分片每次走重定向）

    final_url = url

    try:

        req = urllib.request.Request(url, headers={"Range": "bytes=0-0", "User-Agent": "Mozilla/5.0"})

        with urllib.request.urlopen(req, timeout=30) as resp:

            content_type = resp.headers.get("Content-Type", "")

            content_range = resp.headers.get("Content-Range", "")

            if "/" in content_range:

                total_size = int(content_range.split("/")[-1])

            else:

                total_size = int(resp.headers.get("Content-Length", 0))

            if "text/html" in content_type and total_size < 2 * 1024 * 1024:

                raise RuntimeError(

                    "下载链接返回HTML而非文件 (Content-Type=%s, size=%s)。请检查链接有效性。"

                    % (content_type, total_size)

                )

            final_url = resp.geturl() or url

            if final_url != url:

                logger.info("  已解析最终下载URL: %s...", final_url[:120])

    except Exception as e:

        logger.warning(f"  获取文件信息失败: {e}，单线程下载")

        return _single_download(url, output_path)



    if total_size == 0:

        return _single_download(url, output_path)



    # S3/对象存储对并发 Range 严重限速（实测16线程全卡死，单线程秒回），

    # 一律使用最终URL单线程流式下载

    logger.info("  使用最终URL单线程流式下载（规避对象存储并发限速）")

    return _single_download(final_url, output_path)



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

        futures = {executor.submit(_download_range, final_url, s, e, output_path, i): (s, e, i) for s, e, i in ranges}

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

                executor.submit(_download_range, final_url, s, e, output_path, idx)



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





# ===== 夸克网盘上传（QuarkPan成熟库）=====

def quark_get_or_create_folder(client, folder_name="同享AI数据集"):

    """查找或创建文件夹，返回fid"""

    resp = client.list_files(folder_id="0", page=1, size=100)

    for f in resp.get("data", {}).get("list", []):

        if f.get("file_name") == folder_name:

            return f.get("fid")

    result = client.files.create_folder(folder_name, parent_id="0")

    return result.get("data", {}).get("fid") or result.get("fid")





def quark_find_file_fid(client, folder_fid, file_name, max_pages=30):

    """用 search_files 按文件名直接定位 fid（比翻 list 页高效）。



    网盘文件夹已上千文件，翻 list 页既慢又浪费；search 接口一次请求即可。

    仍保留 max_pages 兜底：若 search 不可用或返回为空，回退翻 list。

    """

    # 优先用 search（全局搜，按文件名精确匹配）

    try:

        res = client.search_files(file_name)

        items = res.get("data", {}).get("list", []) or res.get("list", [])

        for it in items:

            if it.get("file_name") == file_name:

                return it.get("fid")

    except Exception as e:

        logger.warning(f"  search_files 异常，回退翻list: {e}")



    # 兜底：翻 list 页

    for page in range(1, max_pages + 1):

        try:

            resp = client.list_files(folder_id=folder_fid, page=page, size=100)

        except Exception as e:

            logger.warning(f"  list_files page={page} 异常: {e}")

            break

        files = resp.get("data", {}).get("list", [])

        if not files:

            break

        for f in files:

            if f.get("file_name") == file_name:

                return f.get("fid")

        if len(files) < 100:

            break

    return None





def quark_upload_and_share(zip_path, safe_title, pwd="yolo"):
    """上传文件到夸克网盘并创建分享，返回分享文本（整体重试3次）"""
    last_err = None
    for attempt in range(3):
        try:
            return _quark_upload_and_share_once(zip_path, safe_title, pwd)
        except Exception as e:
            last_err = e
            logger.warning(f"  上传/分享第{attempt+1}次失败: {str(e)[:120]}，12s后重试")
            time.sleep(12)
    raise RuntimeError(f"夸克上传/分享失败: {last_err}")


def _quark_upload_and_share_once(zip_path, safe_title, pwd="yolo"):

    """上传文件到夸克网盘并创建分享，返回分享文本"""

    client = QuarkClient(cookies=QUARK_COOKIE, auto_login=False)

    folder_fid = quark_get_or_create_folder(client)

    if not folder_fid:

        raise RuntimeError("无法获取/创建 同享AI数据集 文件夹")

    logger.info(f"  目标文件夹fid: {folder_fid[:16]}...")



    # 上传文件（QuarkPan自动处理分片/哈希/OSS授权）

    file_name = os.path.basename(zip_path)

    logger.info(f"  上传中: {file_name} ({os.path.getsize(zip_path)/1024/1024:.1f} MB)")

    client.upload.upload_file(zip_path, parent_folder_id=folder_fid)



    # 查找上传后的文件fid（对象存储有一致性延迟，翻页+指数退避重试）

    file_fid = None

    for attempt, wait in enumerate([0, 5, 10, 20]):

        if wait:

            logger.info(f"  等待{wait}s后第{attempt+1}次查找fid...")

            time.sleep(wait)

        file_fid = quark_find_file_fid(client, folder_fid, file_name)

        if file_fid:

            break

        logger.warning(f"  第{attempt+1}次未找到文件fid")

    if not file_fid:

        raise RuntimeError("上传后未找到文件fid（已分页重试4次）")



    # 创建分享（永久+提取码）

    logger.info("  创建分享链接...")

    share = client.shares.create_share(

        file_ids=[file_fid],

        title=f"{safe_title}_data2.cn",

        expire_days=0,

        password=pwd

    )

    share_url = share.get("share_url", "")

    passcode = share.get("passcode", pwd)

    if not share_url:

        raise RuntimeError(f"分享创建失败: {share}")

    return f"通过夸克网盘分享的文件：{safe_title}_data2.cn.zip\n链接: {share_url} 提取码: {passcode}"





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

    logger.info(f"  网盘后端: {NETDISK}")

    if row:

        logger.info(f"  表格行: {row}")



    # 下载（文件名规范: {列0标题}_data2.cn.zip）

    zip_path = os.path.join(TEMP_DIR, f"{safe_title}_data2.cn.zip")

    logger.info("  开始下载...")

    try:

        real_url, is_google = resolve_google_drive(url)

        if is_google:

            logger.info("  Google Drive 已解析: %s", real_url[:120])

        if is_google:

            # Google Drive 大文件：单线程流式下载，避免并发 Range 被限速/节流

            logger.info("  Google Drive 使用单线程流式下载")

            ok = _single_download(real_url, zip_path)

        else:

            ok = concurrent_download(real_url, zip_path)

        if not ok:

            logger.error("  下载失败")

            return False

    except Exception as e:

        logger.error(f"  下载异常: {e}")

        return False



    if not os.path.exists(zip_path) or os.path.getsize(zip_path) == 0:

        logger.error("  下载文件不存在或为空")

        return False



    logger.info(f"  下载完成: {os.path.getsize(zip_path) / 1024 / 1024:.1f} MB")



    # RELAY 快链路（坑#117复核版）: 登记 manifest, 由 T4 从 Release 拉取并传夸克,
    # 分享链接经 t4_tasks/done_*.json 回传, 本机轮询后写表
    if RELAY_MODE and GH_RELAY_TOKEN:

        relay_stage(zip_path, safe_title, row)

        return True



    # 上传网盘（不解压，直接上传）

    if NETDISK == "quark":

        logger.info("  上传夸克网盘...")

        try:

            share_text = quark_upload_and_share(zip_path, safe_title)

        except Exception as e:

            logger.error(f"  夸克上传/分享失败: {e}")

            if os.path.exists(zip_path):

                os.remove(zip_path)

            return False

    elif NETDISK == "baidu":

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

    else:

        logger.error("  未配置任何网盘凭据（QUARK_COOKIE 或 BAIDU_ACCESS_TOKEN）")

        if os.path.exists(zip_path):

            os.remove(zip_path)

        return False



    # 写回腾讯文档（如果指定了行号）

    if row:

        logger.info(f"  写回腾讯文档行{row}...")

        # 坑#70复发修复: 行号超过表格容量时 set_cell_value 静默失效 → 写回前先扩容

        try:

            _info = call_mcp("sheet.get_sheet_info", {"file_id": FILE_ID, "sheet_id": SHEET_ID}, req_id=row)

            _sheets = _info.get("result", {}).get("structuredContent", {}).get("sheets", [])

            _rc = next((s.get("row_count", 0) for s in _sheets if s.get("sheet_id") == SHEET_ID), 0)

            if _rc and _rc < row + 10:

                _need = row + 500 - _rc

                call_mcp("sheet.insert_dimension", {"file_id": FILE_ID, "sheet_id": SHEET_ID,

                         "dimension_type": "row", "index": _rc, "count": _need, "direction": "before"}, req_id=row)

                logger.info(f"  已扩容: {_rc} -> {_rc + _need} 行")

        except Exception as _e:

            logger.warning(f"  扩容检查失败(继续写回): {_e}")

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

    if not TENCENT_TOKEN:

        missing.append("TENCENT_TOKEN")

    if NETDISK == "quark":

        if not QUARK_COOKIE:

            missing.append("QUARK_COOKIE")

    elif NETDISK == "baidu":

        if not BAIDU_ACCESS_TOKEN:

            missing.append("BAIDU_ACCESS_TOKEN")

    else:

        missing.append("QUARK_COOKIE 或 BAIDU_ACCESS_TOKEN")

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

    RELAY_MANIFEST["expected"] = len(datasets)





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



    # RELAY 收尾: 上传 Release（失败 exit 1 让 Action 标红）

    try:

        relay_finalize()

        if RELAY_MODE:

            shutil.rmtree(TEMP_DIR, ignore_errors=True)

    except Exception as e:

        logger.error(f"RELAY finalize 失败: {e}")

        import traceback

        traceback.print_exc()

        sys.exit(1)

    if failed > 0:

        sys.exit(1)





if __name__ == "__main__":

    main()

