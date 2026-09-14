# -*- coding: utf-8 -*-
"""在 quark_client 中添加 openssl_sha1.py：用 C 级 OpenSSL 快速计算 SHA1 增量状态
OSS 分片上传必须带 X-Oss-Hash-Ctx 头（否则 400 NoHashContext）。
此模块用 ctypes 调 libcrypto 的 SHA1_CTX，447MB/s，替代纯 Python SHA1（5-10分钟）。
"""
import ctypes
import ctypes.util
import json
import base64
import os
import sys
import glob


class SHA_CTX(ctypes.Structure):
    _fields_ = [
        ("h0", ctypes.c_uint32), ("h1", ctypes.c_uint32),
        ("h2", ctypes.c_uint32), ("h3", ctypes.c_uint32), ("h4", ctypes.c_uint32),
        ("Nl", ctypes.c_uint32), ("Nh", ctypes.c_uint32),
        ("data", ctypes.c_ubyte * 64),
        ("num", ctypes.c_uint32)
    ]


_lib = None


def _load_libcrypto():
    global _lib
    if _lib is not None:
        return _lib
    for name in ('crypto', 'libcrypto'):
        p = ctypes.util.find_library(name)
        if p:
            try:
                _lib = ctypes.CDLL(p)
                return _lib
            except Exception:
                pass
    for dll in ('libcrypto-3-x64.dll', 'libcrypto-1_1-x64.dll',
                'libcrypto-3.dll', 'libcrypto-1_1.dll', 'libcrypto.so.3',
                'libcrypto.so.1.1', 'libcrypto.dylib'):
        try:
            _lib = ctypes.CDLL(dll)
            return _lib
        except Exception:
            pass
    py_dir = os.path.dirname(sys.executable)
    for pattern in ('libcrypto*.dll', 'libcrypto*.so*', 'libcrypto*.dylib'):
        for p in glob.glob(os.path.join(py_dir, 'DLLs', pattern)) + \
                 glob.glob(os.path.join(py_dir, pattern)):
            try:
                _lib = ctypes.CDLL(p)
                return _lib
            except Exception:
                pass
    return None


def _ensure_funcs():
    lib = _load_libcrypto()
    if lib is None:
        raise RuntimeError("无法加载 libcrypto，无法计算 SHA1 增量状态")
    lib.SHA1_Init.argtypes = [ctypes.POINTER(SHA_CTX)]
    lib.SHA1_Init.restype = ctypes.c_int
    lib.SHA1_Update.argtypes = [ctypes.POINTER(SHA_CTX), ctypes.c_void_p, ctypes.c_size_t]
    lib.SHA1_Update.restype = ctypes.c_int
    return lib


def _ctx_to_hash_ctx(ctx: SHA_CTX) -> str:
    """把 SHA_CTX 状态序列化为 OSS X-Oss-Hash-Ctx 格式（base64 JSON）"""
    state = {
        "hash_type": "sha1",
        "h0": str(ctx.h0), "h1": str(ctx.h1),
        "h2": str(ctx.h2), "h3": str(ctx.h3), "h4": str(ctx.h4),
        "Nl": str(ctx.Nl), "Nh": str(ctx.Nh),
        "data": "", "num": "0"
    }
    hash_json = json.dumps(state, separators=(',', ':'))
    return base64.b64encode(hash_json.encode('utf-8')).decode('utf-8')


def compute_hash_ctxs(file_path, parts, chunk_size):
    """一次流式扫描，计算每个分片起始位置的 hash_ctx（O(n)，447MB/s）
    parts: [(part_number, part_size), ...]
    返回: {part_number: hash_ctx}，part 1 为 None（OSS 对首个分片不要求 hash_ctx）
    """
    lib = _ensure_funcs()
    ctx = SHA_CTX()
    lib.SHA1_Init(ctypes.byref(ctx))

    hash_ctxs = {}
    read_buf = bytearray(chunk_size + 1)
    with open(file_path, 'rb') as f:
        for part_number, part_size in parts:
            if part_number > 1:
                # 记录当前状态作为 part_number 的起始 hash_ctx
                hash_ctxs[part_number] = _ctx_to_hash_ctx(ctx)
            data = f.read(part_size)
            # 用 create_string_buffer 传递数据
            buf = ctypes.create_string_buffer(data, len(data))
            lib.SHA1_Update(ctypes.byref(ctx), buf, len(data))
            if part_number == 1:
                hash_ctxs[part_number] = None
    return hash_ctxs
