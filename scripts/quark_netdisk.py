#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
夸克网盘客户端（纯标准库，GitHub Action 可用）
===============================================
基于夸克网盘 Web 端逆向 API 实现：
  - 认证：Cookie（含 __pus 关键 token，从浏览器 F12 复制完整 Cookie）
  - 上传：pre → update/hash → auth → OSS分片上传(PUT) → POST合并 → finish
  - 分享：创建分享 → 轮询任务 → 获取分享链接（可设提取码）
  - 文件管理：创建文件夹、文件列表、查找文件

API 域名：
  - drive-pc.quark.cn/1/clouddrive（上传/分享/管理）
  - pan.quark.cn/1/clouddrive（列表）
  - {bucket}.oss-cn-shenzhen.aliyuncs.com（OSS 分片存储）

用法：
  import quark_netdisk
  qk = quark_netdisk.QuarkNetdisk(cookie="完整cookie字符串")
  path = qk.upload_file("/tmp/a.zip", "/同享AI数据集/a_data2.cn.zip")
  share = qk.create_share("/同享AI数据集/a_data2.cn.zip", pwd="yolo")

环境变量：
  QUARK_COOKIE - 夸克网盘完整 Cookie（必填）
"""
import base64
import hashlib
import json
import os
import re
import time
import urllib.parse
import urllib.request
import urllib.error
import uuid
import logging
import mimetypes

logger = logging.getLogger("quark_netdisk")

# ===== 常量 =====
API_HOST = "https://drive-pc.quark.cn/1/clouddrive"
LIST_HOST = "https://pan.quark.cn/1/clouddrive"
OSS_REGION = "oss-cn-shenzhen.aliyuncs.com"
DEFAULT_BUCKET = "ul-zb"
CHUNK_SIZE = 16 * 1024 * 1024  # 16MB 分片（增大以减少请求次数，提升大文件成功率）

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

DEFAULT_HEADERS = {
    "user-agent": UA,
    "accept": "application/json, text/plain, */*",
    "content-type": "application/json",
    "origin": "https://pan.quark.cn",
    "referer": "https://pan.quark.cn/",
    "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}


class QuarkError(Exception):
    pass


class QuarkNetdisk:
    """夸克网盘客户端"""

    def __init__(self, cookie=None):
        self.cookie = cookie or os.environ.get("QUARK_COOKIE", "")
        if not self.cookie:
            raise QuarkError("缺少夸克网盘 Cookie（环境变量 QUARK_COOKIE 或参数 cookie）")
        self.headers = dict(DEFAULT_HEADERS)
        self.headers["cookie"] = self.cookie
        self.root_fid = "0"  # 根目录

    # ===== HTTP 基础 =====
    def _request(self, method, url, data=None, params=None, headers=None, timeout=60):
        """通用 HTTP 请求，返回 JSON"""
        if params:
            url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        req_headers = dict(self.headers)
        if headers:
            req_headers.update(headers)
        body = None
        if data is not None:
            body = json.dumps(data).encode()
        req = urllib.request.Request(url, data=body, method=method, headers=req_headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body_text = e.read().decode(errors="ignore")[:300]
            raise QuarkError(f"HTTP {e.code} {url[:120]}: {body_text}")
        except urllib.error.URLError as e:
            raise QuarkError(f"网络错误 {url[:120]}: {e}")

    def _post_api(self, path, data, params=None, headers=None):
        """POST 到 API_HOST"""
        return self._request("POST", f"{API_HOST}/{path}", data=data, params=params, headers=headers)

    def _get_api(self, path, params=None):
        """GET 到 API_HOST"""
        return self._request("GET", f"{API_HOST}/{path}", params=params)

    # ===== 用户信息 =====
    def get_user_info(self):
        return self._post_api("user/info", {})

    # ===== 文件/文件夹管理 =====
    def list_dir(self, dir_fid="0", page=1, size=50):
        """列出目录内容"""
        params = {
            "pr": "ucpro", "fr": "pc", "uc_param_str": "",
            "pdir_fid": dir_fid, "_page": str(page), "_size": str(size),
            "_fetch_total": "1", "_fetch_sub_dirs": "0",
            "_sort": "file_type:asc,updated_at:desc",
        }
        url = f"{LIST_HOST}/file/sort"
        return self._request("GET", url, params=params)

    def search_file(self, keyword, page=1, size=50):
        """搜索文件"""
        params = {
            "pr": "ucpro", "fr": "pc", "uc_param_str": "",
            "_page": str(page), "_size": str(size), "_fetch_total": "1",
            "_sort": "file_type:desc,updated_at:desc", "_is_hl": "1",
            "q": keyword,
        }
        url = f"{LIST_HOST}/file/search"
        return self._request("GET", url, params=params)

    def create_folder(self, folder_name, parent_fid="0"):
        """创建文件夹，返回 fid"""
        data = {"pdir_fid": parent_fid, "file_name": folder_name, "dir_path": ""}
        result = self._post_api("file", data)
        if result.get("status") != 200:
            raise QuarkError(f"创建文件夹失败: {result.get('message', result)}")
        f = result.get("data", {}).get("file", {})
        return f.get("fid") or f.get("dir_fid")

    def ensure_folder(self, folder_path, parent_fid="0"):
        """
        确保文件夹存在（按路径逐级创建），返回最终 fid。
        folder_path 如 /同享AI数据集 或 同享AI数据集
        """
        path = folder_path.strip().strip("/")
        if not path:
            return parent_fid
        cur_fid = parent_fid
        for part in path.split("/"):
            if not part:
                continue
            found = None
            # 在当前目录查找同名文件夹
            try:
                listing = self.list_dir(cur_fid, size=100)
                for group in listing.get("data", {}).get("list", []):
                    for f in group.get("files", []):
                        if f.get("file_name") == part and f.get("dir") is True:
                            found = f.get("fid")
                            break
                    if found:
                        break
            except Exception:
                pass
            if found:
                cur_fid = found
            else:
                cur_fid = self.create_folder(part, cur_fid)
                logger.info("  已创建文件夹: %s/%s", folder_path, part)
        return cur_fid

    def find_file_fid(self, file_name, dir_fid="0"):
        """在指定目录查找文件，返回 fid 或 None"""
        try:
            listing = self.list_dir(dir_fid, size=100)
            for group in listing.get("data", {}).get("list", []):
                for f in group.get("files", []):
                    if f.get("file_name") == file_name:
                        return f.get("fid")
        except Exception as e:
            logger.warning("  查找文件失败: %s", e)
        return None

    # ===== 上传 =====
    def _get_oss_date(self):
        import datetime
        return datetime.datetime.now(datetime.timezone.utc).strftime('%a, %d %b %Y %H:%M:%S GMT')

    def _pre_upload(self, file_name, file_size, parent_fid, mime_type):
        """预上传：获取 task_id / auth_info / upload_id / obj_key / bucket / callback"""
        now_ms = int(time.time() * 1000)
        data = {
            "ccp_hash_update": True,
            "parallel_upload": True,
            "pdir_fid": parent_fid,
            "dir_name": "",
            "size": file_size,
            "file_name": file_name,
            "format_type": mime_type,
            "l_updated_at": now_ms,
            "l_created_at": now_ms,
        }
        params = {"pr": "ucpro", "fr": "pc", "uc_param_str": ""}
        result = self._post_api("file/upload/pre", data, params=params)
        if result.get("status") != 200:
            raise QuarkError(f"预上传失败: {result.get('message', result)}")
        return result.get("data", {})

    def _update_hash(self, task_id, md5_hex, sha1_hex):
        data = {"task_id": task_id, "md5": md5_hex, "sha1": sha1_hex}
        params = {"pr": "ucpro", "fr": "pc", "uc_param_str": ""}
        result = self._post_api("file/update/hash", data, params=params)
        if result.get("status") != 200:
            raise QuarkError(f"更新哈希失败: {result.get('message', result)}")
        return result

    def _get_upload_auth(self, task_id, mime_type, part_number, auth_info, upload_id,
                         obj_key, bucket, hash_ctx=""):
        """获取分片上传授权（auth_key）"""
        oss_date = self._get_oss_date()
        if hash_ctx:
            auth_meta = (
                f"PUT\n{mime_type}\n{oss_date}\n"
                f"x-oss-date:{oss_date}\n"
                f"x-oss-hash-ctx:{hash_ctx}\n"
                f"x-oss-user-agent:\naliyun-sdk-js/1.0.0 Chrome Mobile 139.0.0.0 on Google Nexus 5 (Android 6.0)\n"
                f"/{bucket}/{obj_key}?partNumber={part_number}&uploadId={upload_id}"
            )
        else:
            auth_meta = (
                f"PUT\n{mime_type}\n{oss_date}\n"
                f"x-oss-date:{oss_date}\n"
                f"x-oss-user-agent:aliyun-sdk-js/1.0.0 Chrome Mobile 139.0.0.0 on Google Nexus 5 (Android 6.0)\n"
                f"/{bucket}/{obj_key}?partNumber={part_number}&uploadId={upload_id}"
            )
        data = {"task_id": task_id, "auth_info": auth_info, "auth_meta": auth_meta}
        result = self._post_api("file/upload/auth", data)
        if result.get("status") != 200:
            raise QuarkError(f"获取上传授权失败: {result.get('message', result)}")
        return result.get("data", {}).get("auth_key", "")

    def _get_complete_auth(self, task_id, mime_type, auth_info, upload_id, obj_key,
                           bucket, xml_data, callback_info):
        """获取 POST 完成合并授权"""
        import datetime
        time.sleep(0.1)
        oss_date = self._get_oss_date()
        xml_md5 = base64.b64encode(hashlib.md5(xml_data.encode('utf-8')).digest()).decode()
        callback_b64 = base64.b64encode(
            json.dumps(callback_info, separators=(',', ':')).encode()).decode()
        auth_meta = (
            f"POST\n{xml_md5}\napplication/xml\n{oss_date}\n"
            f"x-oss-callback:{callback_b64}\n"
            f"x-oss-date:{oss_date}\n"
            f"x-oss-user-agent:aliyun-sdk-js/1.0.0 Chrome 139.0.0.0 on OS X 10.15.7 64-bit\n"
            f"/{bucket}/{obj_key}?uploadId={upload_id}"
        )
        data = {"task_id": task_id, "auth_meta": auth_meta, "auth_info": auth_info}
        result = self._post_api("file/upload/auth", data)
        if result.get("status") != 200:
            raise QuarkError(f"获取POST授权失败: {result.get('message', result)}")
        auth_key = result.get("data", {}).get("auth_key", "")
        upload_url = f"https://{bucket}.{OSS_REGION}/{obj_key}?uploadId={upload_id}"
        headers = {
            "Content-Type": "application/xml",
            "x-oss-date": oss_date,
            "x-oss-user-agent": "aliyun-sdk-js/1.0.0 Chrome 139.0.0.0 on OS X 10.15.7 64-bit",
            "authorization": auth_key,
            "x-oss-callback": callback_b64,
            "Content-MD5": xml_md5,
        }
        return upload_url, headers

    def _oss_put(self, upload_url, data, headers, timeout=300):
        """PUT 分片到 OSS"""
        req = urllib.request.Request(upload_url, data=data, method="PUT", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                etag = resp.headers.get("etag", "").strip('"')
                if not etag:
                    raise QuarkError("上传成功但未获取 ETag")
                return etag
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="ignore")[:200]
            raise QuarkError(f"OSS PUT 失败 {e.code}: {body}")

    def _oss_post(self, upload_url, data, headers, timeout=300):
        """POST 完成合并到 OSS"""
        req = urllib.request.Request(upload_url, data=data, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            # 203 表示合并成功但 callback 失败（文件已上传成功）
            if e.code in (200, 203):
                return e.code
            body = e.read().decode(errors="ignore")[:200]
            raise QuarkError(f"OSS POST 合并失败 {e.code}: {body}")

    def _calc_sha1_state(self, data):
        """计算 data 的 SHA1 中间状态（用于增量哈希 X-Oss-Hash-Ctx）"""
        h0, h1, h2, h3, h4 = (0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0)
        import struct
        data_len = len(data)
        # 只处理完整 64 字节块（分片 4MB 对齐，必然完整）
        full_blocks = data_len - (data_len % 64)
        for i in range(0, full_blocks, 64):
            block = data[i:i + 64]
            w = list(struct.unpack(">16I", block))
            for t in range(16, 80):
                w.append(((w[t - 3] ^ w[t - 8] ^ w[t - 14] ^ w[t - 16]) << 1 |
                          (w[t - 3] ^ w[t - 8] ^ w[t - 14] ^ w[t - 16]) >> 31) & 0xFFFFFFFF)
            a, b, c, d, e = h0, h1, h2, h3, h4
            for t in range(80):
                if t < 20:
                    f, k = (b & c) | ((~b) & d), 0x5A827999
                elif t < 40:
                    f, k = b ^ c ^ d, 0x6ED9EBA1
                elif t < 60:
                    f, k = (b & c) | (b & d) | (c & d), 0x8F1BBCDC
                else:
                    f, k = b ^ c ^ d, 0xCA62C1D6
                temp = (((a << 5) | (a >> 27)) + f + e + k + w[t]) & 0xFFFFFFFF
                e, d, c, b, a = d, c, ((b << 30) | (b >> 2)) & 0xFFFFFFFF, a, temp
            h0 = (h0 + a) & 0xFFFFFFFF
            h1 = (h1 + b) & 0xFFFFFFFF
            h2 = (h2 + c) & 0xFFFFFFFF
            h3 = (h3 + d) & 0xFFFFFFFF
            h4 = (h4 + e) & 0xFFFFFFFF
        processed_bits = full_blocks * 8
        ctx = {
            "hash_type": "sha1",
            "h0": str(h0), "h1": str(h1), "h2": str(h2),
            "h3": str(h3), "h4": str(h4),
            "Nl": str(processed_bits), "Nh": "0", "data": "", "num": "0",
        }
        return base64.b64encode(json.dumps(ctx, separators=(',', ':')).encode()).decode()

    def upload_file(self, file_path, remote_path=None, parent_fid=None, progress_cb=None):
        """
        上传文件到夸克网盘（支持大文件分片）。
        remote_path: 远程路径，如 /同享AI数据集/xxx.zip
        parent_fid: 或直接指定父目录 fid（remote_path 优先）
        返回: 上传后的文件 fid
        """
        if not os.path.exists(file_path):
            raise QuarkError(f"文件不存在: {file_path}")
        file_size = os.path.getsize(file_path)
        file_name = os.path.basename(file_path)

        # 确定父目录
        pdir_fid = parent_fid or self.root_fid
        if remote_path:
            folder_path = os.path.dirname(remote_path.strip("/"))
            if folder_path:
                pdir_fid = self.ensure_folder(folder_path, self.root_fid)
                file_name = os.path.basename(remote_path)

        mime_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        logger.info("  预上传: %s (%d MB) -> dir=%s", file_name, file_size // 1024 // 1024, pdir_fid)

        # 1. 预上传
        pre = self._pre_upload(file_name, file_size, pdir_fid, mime_type)
        task_id = pre.get("task_id")
        if not task_id:
            raise QuarkError(f"预上传未返回 task_id: {pre}")
        auth_info = pre.get("auth_info", "")
        upload_id = pre.get("upload_id", "")
        obj_key = pre.get("obj_key", "")
        bucket = pre.get("bucket") or DEFAULT_BUCKET
        callback_info = pre.get("callback", {})
        logger.info("  task_id=%s bucket=%s", task_id, bucket)

        # 2. 更新哈希（MD5+SHA1）
        logger.info("  计算并更新哈希...")
        md5_hex = hashlib.md5()
        sha1_hex = hashlib.sha1()
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                md5_hex.update(chunk)
                sha1_hex.update(chunk)
        self._update_hash(task_id, md5_hex.hexdigest(), sha1_hex.hexdigest())

        # 3. 分片上传（顺序：第2+分片需要增量哈希）
        parts = (file_size + CHUNK_SIZE - 1) // CHUNK_SIZE
        if parts <= 1:
            logger.info("  单分片上传...")
            self._upload_single(file_path, task_id, mime_type, auth_info, upload_id,
                                obj_key, bucket, callback_info, file_size)
        else:
            logger.info("  多分片上传: %d 片 × 4MB", parts)
            self._upload_multi(file_path, task_id, mime_type, auth_info, upload_id,
                               obj_key, bucket, callback_info, parts, progress_cb)

        # 4. 完成上传
        logger.info("  通知完成...")
        result = self._post_api("file/upload/finish", {"task_id": task_id, "obj_key": obj_key})
        if result.get("status") != 200:
            raise QuarkError(f"完成上传失败: {result.get('message', result)}")

        # 5. 查找文件 fid
        fid = None
        data = result.get("data", {})
        if isinstance(data, dict):
            fid = data.get("fid") or data.get("file", {}).get("fid")
        if not fid:
            fid = self.find_file_fid(file_name, pdir_fid)
        if fid:
            logger.info("  ✓ 上传完成 fid=%s", fid)
        else:
            logger.warning("  上传完成但未获取 fid，可按文件名搜索")
        return fid

    def _upload_single(self, file_path, task_id, mime_type, auth_info, upload_id,
                       obj_key, bucket, callback_info, file_size):
        """单分片上传（<5MB）"""
        with open(file_path, "rb") as f:
            data = f.read()
        auth_key = self._get_upload_auth(task_id, mime_type, 1, auth_info, upload_id, obj_key, bucket)
        oss_date = self._get_oss_date()
        upload_url = f"https://{bucket}.{OSS_REGION}/{obj_key}?partNumber=1&uploadId={upload_id}"
        headers = {
            "Content-Type": mime_type,
            "x-oss-date": oss_date,
            "x-oss-user-agent": "aliyun-sdk-js/1.0.0 Chrome Mobile 139.0.0.0 on Google Nexus 5 (Android 6.0)",
            "authorization": auth_key,
        }
        etag = self._oss_put(upload_url, data, headers)
        # 完成合并
        xml_data = ('<?xml version="1.0" encoding="UTF-8"?>\n<CompleteMultipartUpload>\n'
                    f'<Part>\n<PartNumber>1</PartNumber>\n<ETag>"{etag}"</ETag>\n</Part>\n'
                    '</CompleteMultipartUpload>')
        post_url, post_headers = self._get_complete_auth(
            task_id, mime_type, auth_info, upload_id, obj_key, bucket, xml_data, callback_info)
        self._oss_post(post_url, xml_data.encode(), post_headers)
        logger.info("  单分片上传完成")

    def _upload_multi(self, file_path, task_id, mime_type, auth_info, upload_id,
                      obj_key, bucket, callback_info, parts, progress_cb=None):
        """多分片顺序上传"""
        uploaded_parts = []
        accumulated = b""  # 仅用于计算增量哈希的前序数据流
        # 为节省内存，用流式方式维护 SHA1 增量状态：直接记录前序数据的 SHA1 状态
        # 这里简单做法：每个分片读取前序所有数据计算状态（4MB×N 内存可控）
        # 优化：维护累计字节数和状态，避免重复读取——但SHA1状态必须从头算。
        # 采用增量法：缓存已处理分片的字节流（最多 ~前几个分片），超过 256MB 改用分次计算
        prev_data = b""
        MAX_CACHE = 256 * 1024 * 1024  # 256MB 上限
        for i in range(1, parts + 1):
            offset = (i - 1) * CHUNK_SIZE
            read_size = min(CHUNK_SIZE, os.path.getsize(file_path) - offset)
            with open(file_path, "rb") as f:
                f.seek(offset)
                chunk = f.read(read_size)

            hash_ctx = ""
            if i > 1:
                # 增量哈希：基于前序所有已上传数据
                if len(prev_data) <= MAX_CACHE:
                    hash_ctx = self._calc_sha1_state(prev_data)
                else:
                    # 超大文件：从头计算前序数据的 SHA1 状态
                    hash_ctx = self._calc_sha1_state_from_file(file_path, offset)
            if len(prev_data) <= MAX_CACHE:
                prev_data += chunk

            # 获取授权并上传
            auth_key = self._get_upload_auth(task_id, mime_type, i, auth_info, upload_id,
                                             obj_key, bucket, hash_ctx)
            oss_date = self._get_oss_date()
            upload_url = f"https://{bucket}.{OSS_REGION}/{obj_key}?partNumber={i}&uploadId={upload_id}"
            headers = {
                "Content-Type": mime_type,
                "x-oss-date": oss_date,
                "x-oss-user-agent": "aliyun-sdk-js/1.0.0 Chrome Mobile 139.0.0.0 on Google Nexus 5 (Android 6.0)",
                "authorization": auth_key,
            }
            if hash_ctx:
                headers["X-Oss-Hash-Ctx"] = hash_ctx
            # 重试3次
            etag = None
            for attempt in range(5):
                try:
                    etag = self._oss_put(upload_url, chunk, headers)
                    break
                except Exception as e:
                    if attempt < 4:
                        logger.warning("    分片 %d 失败，重试 %d/5: %s", i, attempt + 1, e)
                        time.sleep(2 ** attempt)
                    else:
                        raise
            uploaded_parts.append((i, etag))
            if progress_cb:
                progress_cb(i, parts)
            if i % 20 == 0 or i == parts:
                logger.info("    分片进度: %d/%d", i, parts)

        # 完成合并（POST）
        xml_parts = "\n".join(
            f'<Part>\n<PartNumber>{n}</PartNumber>\n<ETag>"{e}"</ETag>\n</Part>'
            for n, e in uploaded_parts
        )
        xml_data = ('<?xml version="1.0" encoding="UTF-8"?>\n<CompleteMultipartUpload>\n'
                    + xml_parts + '\n</CompleteMultipartUpload>')
        try:
            post_url, post_headers = self._get_complete_auth(
                task_id, mime_type, auth_info, upload_id, obj_key, bucket, xml_data, callback_info)
            self._oss_post(post_url, xml_data.encode(), post_headers)
            logger.info("  多分片合并完成")
        except Exception as e:
            logger.warning("  POST 合并失败（文件可能已上传成功）: %s", e)

    def _calc_sha1_state_from_file(self, file_path, upto_bytes):
        """从文件头计算 upto_bytes 字节的 SHA1 状态"""
        with open(file_path, "rb") as f:
            data = f.read(upto_bytes)
        return self._calc_sha1_state(data)

    # ===== 分享 =====
    def create_share(self, file_path_or_fid, pwd="", expired_type=1, title=None):
        """
        创建分享链接。
        file_path_or_fid: 网盘文件路径（/同享AI数据集/xxx.zip）或 fid
        pwd: 提取码（空=无密码）
        expired_type: 1=永久, 2=1天, 3=7天, 4=30天（各版本略有差异，1 通常为永久）
        返回: 分享链接 URL
        """
        # 解析 fid
        fid = file_path_or_fid
        if str(file_path_or_fid).startswith("/"):
            fid = self._path_to_fid(file_path_or_fid)
            if not fid:
                raise QuarkError(f"未找到文件: {file_path_or_fid}")
            if not title:
                title = os.path.basename(file_path_or_fid)

        data = {
            "fid_list": [fid],
            "title": title or "数据集",
            "url_type": 1,
            "expired_type": expired_type,
            "share_pwd": pwd,
        }
        if pwd:
            data["share_pwd"] = pwd
        params = {"pr": "ucpro", "fr": "pc", "uc_param_str": ""}
        result = self._post_api("share", data, params=params)
        if result.get("status") != 200:
            raise QuarkError(f"创建分享失败: {result.get('message', result)}")
        data_r = result.get("data", {})
        share_id = data_r.get("share_id")
        task_id = data_r.get("task_id")

        # 异步任务轮询
        if task_id and not share_id:
            share_id = self._wait_share_task(task_id)

        if not share_id:
            raise QuarkError(f"未获取到 share_id: {result}")

        # 设置/获取带密码链接
        try:
            pw_result = self._post_api("share/password", {"share_id": share_id}, params=params)
            if pw_result.get("status") == 200:
                share_url = pw_result.get("data", {}).get("share_url", "")
                if share_url:
                    logger.info("  ✓ 分享链接: %s", share_url)
                    return share_url
        except Exception as e:
            logger.warning("  获取分享链接失败: %s", e)

        # 兜底：通过 share/task 获取
        share_url = f"https://pan.quark.cn/s/{share_id}"
        logger.info("  ✓ 分享链接(兜底): %s", share_url)
        return share_url

    def _wait_share_task(self, task_id, timeout=60):
        """轮询分享任务，返回 share_id"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                result = self._get_api("task", params={
                    "pr": "ucpro", "fr": "pc", "uc_param_str": "",
                    "task_id": task_id, "retry_index": "0",
                })
                data = result.get("data", {})
                if data.get("status") == 1 or data.get("status") == "1":
                    share_id = data.get("share_id") or data.get("share", {}).get("share_id")
                    if share_id:
                        return share_id
                # 任务完成但无 share_id
                if data.get("status") in (1, 2, "1", "2"):
                    share_id = data.get("share_id")
                    if share_id:
                        return share_id
            except Exception as e:
                logger.warning("  轮询任务失败: %s", e)
            time.sleep(2)
        return None

    def _path_to_fid(self, remote_path):
        """将路径转换为 fid（逐级查找）"""
        parts = remote_path.strip("/").split("/")
        cur_fid = self.root_fid
        for i, part in enumerate(parts):
            listing = self.list_dir(cur_fid, size=100)
            found = None
            for group in listing.get("data", {}).get("list", []):
                for f in group.get("files", []):
                    if f.get("file_name") == part:
                        found = f.get("fid")
                        break
                if found:
                    break
            if not found:
                return None
            cur_fid = found
        return cur_fid

    # ===== 分享文本 =====
    def build_share_text(self, share_url, file_name, pwd="yolo"):
        """构造与百度网盘格式一致的分享文本"""
        if pwd:
            return f"通过夸克网盘分享的文件：{file_name}\n链接: {share_url} 提取码: {pwd}"
        return f"通过夸克网盘分享的文件：{file_name}\n链接: {share_url}"


# ===== 便捷函数 =====
def upload_and_share(file_path, remote_path, pwd="yolo", cookie=None):
    """
    一站式：上传文件到夸克网盘并创建分享。
    remote_path: /同享AI数据集/xxx.zip
    返回: (fid, share_text)
    """
    qk = QuarkNetdisk(cookie=cookie)
    fid = qk.upload_file(file_path, remote_path)
    share_url = qk.create_share(remote_path, pwd=pwd)
    share_text = qk.build_share_text(share_url, os.path.basename(remote_path), pwd)
    return fid, share_text


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")
    parser = argparse.ArgumentParser(description="夸克网盘上传/分享工具")
    parser.add_argument("--upload", help="要上传的本地文件路径")
    parser.add_argument("--remote", help="远程路径，如 /同享AI数据集/xxx.zip")
    parser.add_argument("--pwd", default="yolo", help="分享提取码")
    parser.add_argument("--cookie", default=None, help="夸克网盘 Cookie（默认取 QUARK_COOKIE）")
    args = parser.parse_args()

    if args.upload:
        fid, text = upload_and_share(args.upload, args.remote or f"/{os.path.basename(args.upload)}",
                                     pwd=args.pwd, cookie=args.cookie)
        print("FID:", fid)
        print(text)
    else:
        parser.print_help()
