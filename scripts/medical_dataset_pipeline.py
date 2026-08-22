#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
UCSF-PDGM & UPENN-GBM 医学影像数据集 全流程下载整理工具
============================================================
功能：从 GitHub Release 高速下载 → BIDS 规范整理 → 打包压缩 → 上传百度网盘 → 创建分享链接

使用场景：
1. Colab 运行（推荐）：利用 Colab 美国服务器高速下载+上传百度网盘
2. 本地运行：需配置代理加速 GitHub 访问

百度网盘凭据来自 dataset-drive-organizer 技能内置配置。
"""

import os
import sys
import re
import json
import time
import hashlib
import urllib.request
import urllib.parse
import urllib.error
import subprocess
import logging
import shutil
import tarfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ============================================================
# 配置
# ============================================================

# 百度网盘 Access Token（来自 dataset-drive-organizer 技能）
BAIDU_ACCESS_TOKEN = os.environ.get("BAIDU_ACCESS_TOKEN",
    "123.485e6f4d655137d4e223b7a53aeaf38e.YHXJvDs6OVmxeNAVp_-hixv6GuYQc4740J4C-sL.6LhX4A")

# 百度网盘 BDUSS 认证（备选方案，无需 Access Token）
BAIDU_BDUSS = os.environ.get("BAIDU_BDUSS", "")
BAIDU_APP_ID = os.environ.get("BAIDU_APP_ID", "266719")  # 百度网盘官方 app_id

# 工作目录（优先级：命令行参数 > 环境变量 > /mnt(GitHub Actions) > /content(Colab) > 本地）
WORK_DIR = os.environ.get("WORK_DIR", "")
if WORK_DIR:
    BASE_DIR = os.path.join(WORK_DIR, 'medical_datasets')
elif os.path.exists('/mnt'):
    BASE_DIR = '/mnt/medical_datasets'  # GitHub Actions: 使用 /mnt 资源盘（额外14GB）
elif os.path.exists('/content'):
    BASE_DIR = '/content/medical_datasets'  # Colab
else:
    BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'medical_datasets')

UCSF_DIR = os.path.join(BASE_DIR, 'UCSF-PDGM')
UPENN_DIR = os.path.join(BASE_DIR, 'UPENN-GBM')
PACKAGE_DIR = os.path.join(BASE_DIR, 'packages')

# 百度网盘远程目录
BAIDU_REMOTE_DIR = '/apps/医学影像数据集'

# 下载配置
MAX_WORKERS = 16  # 并发下载线程数
CHUNK_SIZE = 65536
MAX_RETRIES = 5

# GitHub release 配置
GITHUB_RELEASES = {
    'ucsf-pdgm': {
        'name': 'UCSF-PDGM',
        'tag': 'ucsf-pdgm',
        'output_dir': UCSF_DIR,
        'description': 'UCSF 术前弥漫性胶质瘤 MRI 数据集（495例，含IDH/MGMT分子标记）',
    },
    'upenn-gbm': {
        'name': 'UPENN-GBM',
        'tag': 'upenn-gbm',
        'output_dir': UPENN_DIR,
        'description': 'UPenn 新发胶质母细胞瘤 MRI 数据集（630例，含放射组学特征）',
    },
}

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}

# ============================================================
# 日志
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger('medical_dataset')

# ============================================================
# 第一部分：从 GitHub Release 下载
# ============================================================

def get_release_assets(tag):
    """从 GitHub expanded_assets 页面获取所有下载链接（带重试和备用方案）"""
    url = f'https://github.com/data-nih/tcia/releases/expanded_assets/{tag}'
    logger.info(f'获取文件清单: {url}')
    
    # 方案1: expanded_assets HTML 页面（重试5次，递增等待）
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=60) as resp:
                html = resp.read().decode('utf-8')
            break
        except Exception as e:
            wait = 2 ** (attempt + 1)  # 2, 4, 8, 16, 32 秒
            logger.warning(f'请求失败 (尝试 {attempt+1}/5): {e}, {wait}秒后重试...')
            time.sleep(wait)
    else:
        # 方案2: 备用 - 使用 GitHub API 获取 release assets
        logger.warning('expanded_assets 页面失败，尝试 GitHub API...')
        api_url = f'https://api.github.com/repos/data-nih/tcia/releases/tags/{tag}'
        try:
            req = urllib.request.Request(api_url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=60) as resp:
                release_data = json.loads(resp.read().decode('utf-8'))
            assets = []
            for a in release_data.get('assets', []):
                assets.append({'name': a['name'], 'url': a['browser_download_url']})
            logger.info(f'通过 GitHub API 获取到 {len(assets)} 个文件')
            return assets
        except Exception as e:
            logger.error(f'GitHub API 也失败: {e}')
            logger.error('无法获取文件清单，请检查网络连接')
            return []
    
    pattern = r'href="(/data-nih/tcia/releases/download/[^"]+)"'
    matches = re.findall(pattern, html)
    
    assets = []
    seen = set()
    for m in matches:
        if m in seen:
            continue
        seen.add(m)
        filename = urllib.parse.unquote(m.split('/')[-1])
        download_url = f'https://github.com{m}'
        assets.append({'name': filename, 'url': download_url})
    
    logger.info(f'找到 {len(assets)} 个文件')
    return assets

def parse_filename(filename):
    """解析文件名，提取 subject ID 和 modality"""
    name_no_ext = filename
    for ext in ['.nii.gz', '.gqi.fz', '.qsdr.fz', '.sz', '.zip', '.csv', '.tsv', '.json', '.bval', '.bvec']:
        if name_no_ext.endswith(ext):
            name_no_ext = name_no_ext[:-len(ext)]
            break
    parts = name_no_ext.split('_', 1)
    subject_id = parts[0] if parts else 'unknown'
    modality = parts[1] if len(parts) > 1 else 'unknown'
    return subject_id, modality

def get_target_path(output_dir, filename):
    """根据文件名生成 BIDS 风格目标路径"""
    subject_id, modality = parse_filename(filename)
    
    if any(filename.endswith(ext) for ext in ['.csv', '.tsv', '.json', '.txt']):
        return os.path.join(output_dir, filename)
    
    mod_lower = modality.lower()
    if 'seg' in mod_lower or 'label' in mod_lower:
        subdir = 'labels'
    elif 'dwi' in mod_lower or 'diff' in mod_lower or mod_lower in ['gqi.fz', 'qsdr.fz', 'sz']:
        subdir = 'dwi'
    elif 'asl' in mod_lower or 'perf' in mod_lower:
        subdir = 'perf'
    else:
        subdir = 'anat'
    
    return os.path.join(output_dir, subject_id, subdir, filename)

def download_file(url, output_path, max_retries=MAX_RETRIES):
    """下载单个文件，支持断点续传"""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    
    if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
        try:
            req = urllib.request.Request(url, headers=HEADERS, method='HEAD')
            with urllib.request.urlopen(req, timeout=30) as resp:
                remote_size = int(resp.headers.get('Content-Length', 0))
            local_size = os.path.getsize(output_path)
            if remote_size > 0 and local_size >= remote_size:
                return 'skipped', local_size
        except:
            pass
    
    resume_pos = os.path.getsize(output_path) if os.path.exists(output_path) else 0
    
    for attempt in range(max_retries):
        try:
            headers = dict(HEADERS)
            if resume_pos > 0:
                headers['Range'] = f'bytes={resume_pos}-'
            
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=180) as resp:
                status = resp.status
                if status in [200, 206]:
                    mode = 'ab' if (resume_pos > 0 and status == 206) else 'wb'
                    if mode == 'wb':
                        resume_pos = 0
                    
                    downloaded = resume_pos
                    with open(output_path, mode) as f:
                        while True:
                            chunk = resp.read(CHUNK_SIZE)
                            if not chunk:
                                break
                            f.write(chunk)
                            downloaded += len(chunk)
                    return 'downloaded', downloaded
                elif status == 416:
                    return 'skipped', os.path.getsize(output_path)
                else:
                    time.sleep(2 * (attempt + 1))
                    resume_pos = 0
        except Exception as e:
            time.sleep(2 * (attempt + 1))
            if os.path.exists(output_path):
                resume_pos = os.path.getsize(output_path)
    
    return 'failed', 0

def download_dataset(config, skip_dwi=False):
    """下载完整数据集"""
    name = config['name']
    tag = config['tag']
    output_dir = config['output_dir']
    
    logger.info(f'{"="*60}')
    logger.info(f'开始下载: {name}')
    logger.info(f'输出目录: {output_dir}')
    logger.info(f'{"="*60}')
    
    # 1. 获取文件清单
    assets = get_release_assets(tag)
    if not assets:
        logger.error(f'{name}: 未找到文件')
        return None
    
    # 统计
    subjects = set()
    modalities = {}
    for a in assets:
        sid, mod = parse_filename(a['name'])
        subjects.add(sid)
        modalities[mod] = modalities.get(mod, 0) + 1
    
    logger.info(f'受试者: {len(subjects)}')
    logger.info(f'模态分布:')
    for mod, count in sorted(modalities.items()):
        logger.info(f'  {mod}: {count}')
    
    # 2. 准备下载任务
    tasks = []
    dwi_exts = ['.sz', '.gqi.fz', '.qsdr.fz']
    skipped_dwi = 0
    for a in assets:
        if skip_dwi and any(a['name'].endswith(ext) for ext in dwi_exts):
            skipped_dwi += 1
            continue
        target = get_target_path(output_dir, a['name'])
        tasks.append({'url': a['url'], 'output': target, 'name': a['name']})
    
    if skipped_dwi > 0:
        logger.info(f'跳过 {skipped_dwi} 个 DWI 大文件 (skip_dwi=True)')
    logger.info(f'待下载: {len(tasks)} 个文件 ({MAX_WORKERS} 线程)')
    
    # 3. 多线程下载
    results = {'downloaded': 0, 'skipped': 0, 'failed': 0}
    failed_files = []
    total_bytes = 0
    completed = 0
    start_time = time.time()
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(download_file, t['url'], t['output']): t for t in tasks}
        
        for future in as_completed(futures):
            t = futures[future]
            completed += 1
            try:
                status, size = future.result()
                total_bytes += size
                if status == 'downloaded':
                    results['downloaded'] += 1
                elif status == 'skipped':
                    results['skipped'] += 1
                else:
                    results['failed'] += 1
                    failed_files.append(t['name'])
            except Exception as e:
                results['failed'] += 1
                failed_files.append(t['name'])
            
            if completed % 100 == 0 or completed == len(tasks):
                elapsed = time.time() - start_time
                speed = total_bytes / elapsed / 1024 / 1024 if elapsed > 0 else 0
                eta = (len(tasks) - completed) * (elapsed / completed) if completed > 0 else 0
                logger.info(f'进度: {completed}/{len(tasks)} | '
                      f'新下载: {results["downloaded"]} | '
                      f'已存在: {results["skipped"]} | '
                      f'失败: {results["failed"]} | '
                      f'速度: {speed:.1f}MB/s | '
                      f'剩余: {eta/60:.1f}分钟')
    
    # 清理0字节文件
    for root, dirs, files in os.walk(output_dir):
        for fn in files:
            fp = os.path.join(root, fn)
            if os.path.getsize(fp) == 0:
                os.remove(fp)
    
    # 汇总
    elapsed = time.time() - start_time
    logger.info(f'{"="*50}')
    logger.info(f'{name} 下载完成!')
    logger.info(f'总文件数: {len(tasks)}')
    logger.info(f'新下载: {results["downloaded"]}')
    logger.info(f'已存在(跳过): {results["skipped"]}')
    logger.info(f'失败: {results["failed"]}')
    logger.info(f'总数据量: {total_bytes/1024/1024/1024:.2f} GB')
    logger.info(f'总耗时: {elapsed/60:.1f} 分钟')
    logger.info(f'平均速度: {total_bytes/elapsed/1024/1024:.1f} MB/s')
    
    if failed_files:
        fail_file = os.path.join(output_dir, '_failed_files.txt')
        with open(fail_file, 'w') as f:
            f.write('\n'.join(failed_files))
        logger.warning(f'失败列表: {fail_file} ({len(failed_files)}个)')
    
    return results

# ============================================================
# 第二部分：打包压缩
# ============================================================

def package_dataset(source_dir, output_path):
    """将数据集目录打包为 tar.gz"""
    logger.info(f'打包: {source_dir} -> {output_path}')
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    
    with tarfile.open(output_path, 'w:gz') as tar:
        tar.add(source_dir, arcname=os.path.basename(source_dir))
    
    size = os.path.getsize(output_path)
    logger.info(f'打包完成: {size/1024/1024:.1f} MB')
    return output_path

# ============================================================
# 第三部分：百度网盘上传（xpan 分片 + PCS 回退）
# ============================================================

def ensure_baidu_folder(folder_path, access_token):
    """确保百度网盘文件夹存在"""
    parts = [p for p in folder_path.strip('/').split('/') if p]
    current = ''
    for part in parts:
        current += '/' + part
        query = urllib.parse.urlencode({
            'method': 'create', 'access_token': access_token,
            'path': current, 'isdir': '1',
        })
        url = f'https://pan.baidu.com/rest/2.0/xpan/file?{query}'
        try:
            req = urllib.request.Request(url, method='POST')
            with urllib.request.urlopen(req, timeout=15) as resp:
                json.loads(resp.read().decode())
        except:
            pass

def ensure_baidu_folder_bduss(folder_path, bduss, app_id):
    """确保百度网盘文件夹存在（BDUSS 认证）"""
    parts = [p for p in folder_path.strip('/').split('/') if p]
    current = ''
    for part in parts:
        current += '/' + part
        query = urllib.parse.urlencode({
            'method': 'create', 'path': current,
            'isdir': '1', 'app_id': app_id,
        })
        url = f'https://pan.baidu.com/rest/2.0/xpan/file?{query}'
        try:
            req = urllib.request.Request(url, method='POST')
            req.add_header('Cookie', f'BDUSS={bduss}')
            with urllib.request.urlopen(req, timeout=15) as resp:
                json.loads(resp.read().decode())
        except:
            pass

def baidu_upload_xpan(file_path, remote_path, access_token):
    """百度网盘 xpan 分片上传（precreate → superfile2 → create）"""
    logger.info('使用 xpan 分片上传...')
    
    file_size = os.path.getsize(file_path)
    slice_size = 4 * 1024 * 1024  # 4MB per slice
    
    # 计算分片 MD5
    block_list = []
    with open(file_path, 'rb') as f:
        while True:
            chunk = f.read(slice_size)
            if not chunk:
                break
            block_list.append(hashlib.md5(chunk).hexdigest())
    
    # Step 1: precreate
    query_params = urllib.parse.urlencode({'method': 'precreate', 'access_token': access_token})
    body_data = urllib.parse.urlencode({
        'path': remote_path, 'size': str(file_size),
        'isdir': '0', 'autoinit': '1',
        'block_list': json.dumps(block_list), 'ondup': 'overwrite',
    }).encode()
    
    req = urllib.request.Request(
        f'https://pan.baidu.com/rest/2.0/xpan/file?{query_params}',
        data=body_data, method='POST'
    )
    req.add_header('Content-Type', 'application/x-www-form-urlencoded')
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
    except Exception as e:
        logger.warning(f'precreate 失败: {e}')
        return None
    
    if result.get('errno') != 0:
        logger.warning(f'precreate 错误: errno={result.get("errno")}')
        return None
    
    uploadid = result.get('uploadid', '')
    if not uploadid:
        return None
    logger.info(f'precreate 成功, uploadid={uploadid[:20]}...')
    
    # Step 2: superfile2 分片上传
    import uuid
    t0 = time.time()
    total_parts = len(block_list)
    
    with open(file_path, 'rb') as f:
        for i in range(total_parts):
            chunk = f.read(slice_size)
            part_seq = i
            
            for attempt in range(3):
                query = urllib.parse.urlencode({
                    'method': 'upload', 'access_token': access_token,
                    'type': 'tmpfile', 'uploadid': uploadid,
                    'partseq': str(part_seq), 'path': remote_path,
                })
                superfile_url = f'https://d.pcs.baidu.com/rest/2.0/pcs/superfile2?{query}'
                
                boundary = uuid.uuid4().hex
                multipart_body = (
                    f'--{boundary}\r\n'.encode() +
                    b'Content-Disposition: form-data; name="file"; filename="file"\r\n' +
                    b'Content-Type: application/octet-stream\r\n\r\n' +
                    chunk +
                    f'\r\n--{boundary}--\r\n'.encode()
                )
                
                try:
                    req = urllib.request.Request(superfile_url, data=multipart_body, method='POST')
                    req.add_header('Content-Type', f'multipart/form-data; boundary={boundary}')
                    with urllib.request.urlopen(req, timeout=120) as resp:
                        json.loads(resp.read().decode())
                    break
                except Exception as e:
                    if attempt < 2:
                        time.sleep(2)
                    else:
                        logger.error(f'分片 {part_seq} 上传失败: {e}')
                        return None
            
            if (i + 1) % 10 == 0 or (i + 1) == total_parts:
                elapsed = time.time() - t0
                speed = (i + 1) * slice_size / 1024 / 1024 / elapsed if elapsed > 0 else 0
                logger.info(f'  分片进度: {i+1}/{total_parts} ({speed:.1f} MB/s)')
    
    # Step 3: create 合并
    create_query = urllib.parse.urlencode({'method': 'create', 'access_token': access_token})
    create_body = urllib.parse.urlencode({
        'path': remote_path, 'size': str(file_size),
        'isdir': '0', 'block_list': json.dumps(block_list),
        'uploadid': uploadid, 'ondup': 'overwrite',
    }).encode()
    
    req = urllib.request.Request(
        f'https://pan.baidu.com/rest/2.0/xpan/file?{create_query}',
        data=create_body, method='POST'
    )
    req.add_header('Content-Type', 'application/x-www-form-urlencoded')
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
    except Exception as e:
        logger.error(f'create 合并失败: {e}')
        return None
    
    if result.get('errno') == 0 or 'path' in result:
        path = result.get('path', remote_path)
        logger.info(f'上传成功: {path}')
        return path
    else:
        logger.error(f'create 错误: errno={result.get("errno")}')
        return None

def baidu_upload_pcs(file_path, remote_path, access_token=None, bduss=None, app_id=None):
    """百度网盘 PCS 简单上传（使用 curl，更稳定，支持重试）"""
    logger.info('使用 PCS 简单上传 (curl)...')
    
    if bduss and app_id:
        # 使用 BDUSS + app_id 认证
        upload_url = 'https://c.pcs.baidu.com/rest/2.0/pcs/file'
        params = f'method=upload&path={urllib.parse.quote(remote_path)}&ondup=overwrite&app_id={app_id}'
        cookie_header = f'BDUSS={bduss}'
    else:
        # 使用 Access Token 认证
        if not access_token:
            logger.error('未设置 BAIDU_ACCESS_TOKEN 或 BAIDU_BDUSS')
            return None
        upload_url = 'https://d.pcs.baidu.com/rest/2.0/pcs/file'
        params = f'method=upload&access_token={access_token}&path={urllib.parse.quote(remote_path)}&ondup=overwrite'
        cookie_header = None
    
    # 使用 curl 上传（比 Python urllib 更稳定）
    cmd = [
        'curl', '-s', '-X', 'POST',
        '--connect-timeout', '30',
        '--max-time', '7200',  # 最大2小时
        '--retry', '5',  # 重试5次
        '--retry-delay', '10',  # 重试间隔10秒
        '--retry-connrefused',
        '-H', 'Content-Type: application/octet-stream',
        '--data-binary', f'@{file_path}',
        f'{upload_url}?{params}',
    ]
    if cookie_header:
        cmd.extend(['-H', f'Cookie: {cookie_header}'])
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
        if result.returncode == 0 and result.stdout:
            data = json.loads(result.stdout)
            if 'path' in data:
                logger.info(f'上传成功: {data["path"]}')
                return data['path']
            else:
                logger.error(f'上传返回异常: {data}')
        else:
            logger.error(f'curl 上传失败: returncode={result.returncode}')
            if result.stderr:
                logger.error(f'stderr: {result.stderr[:300]}')
            if result.stdout:
                logger.error(f'stdout: {result.stdout[:300]}')
    except subprocess.TimeoutExpired:
        logger.error('上传超时（超过2小时）')
    except Exception as e:
        logger.error(f'PCS 上传失败: {e}')
    
    return None

def baidu_upload(file_path, remote_path, access_token=None, bduss=None, app_id=None):
    """上传文件到百度网盘（自动选择最佳方式）"""
    # 优先使用 BDUSS 认证（无需 Access Token，不会过期）
    if bduss and app_id:
        logger.info('使用 BDUSS 认证方式上传')
        folder = os.path.dirname(remote_path)
        if folder:
            ensure_baidu_folder_bduss(folder, bduss, app_id)
        
        file_size = os.path.getsize(file_path)
        logger.info(f'文件大小: {file_size/1024/1024:.1f} MB')
        
        return baidu_upload_pcs(file_path, remote_path, bduss=bduss, app_id=app_id)
    
    # 使用 Access Token 认证
    if not access_token:
        logger.error('未设置 BAIDU_ACCESS_TOKEN 或 BAIDU_BDUSS')
        return None
    
    folder = os.path.dirname(remote_path)
    if folder:
        ensure_baidu_folder(folder, access_token)
    
    file_size = os.path.getsize(file_path)
    logger.info(f'文件大小: {file_size/1024/1024:.1f} MB')
    
    result = baidu_upload_xpan(file_path, remote_path, access_token)
    if result:
        return result
    
    logger.info('尝试 PCS 简单上传...')
    return baidu_upload_pcs(file_path, remote_path, access_token=access_token)

def baidu_create_share(file_path, access_token=None, pwd='yolo', bduss=None, app_id=None):
    """创建百度网盘分享链接（支持 Access Token 或 BDUSS 认证）"""
    fs_id = None
    
    if bduss and app_id:
        # 使用 BDUSS + PCS API 获取 fs_id
        list_query = urllib.parse.urlencode({
            'method': 'list', 'path': os.path.dirname(file_path),
            'app_id': app_id,
        })
        list_url = f'https://pcs.baidu.com/rest/2.0/pcs/file?{list_query}'
        try:
            req = urllib.request.Request(list_url)
            req.add_header('Cookie', f'BDUSS={bduss}')
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
            for f in data.get('list', []):
                if f.get('path') == file_path:
                    fs_id = f.get('fs_id')
                    break
        except Exception as e:
            logger.warning(f'获取 fs_id 失败: {e}')
    else:
        # 使用 Access Token + xpan API 获取 fs_id
        list_query = urllib.parse.urlencode({
            'method': 'list', 'access_token': access_token,
            'dir': os.path.dirname(file_path),
        })
        list_url = f'https://pan.baidu.com/rest/2.0/xpan/file?{list_query}'
        try:
            req = urllib.request.Request(list_url)
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
            for f in data.get('list', []):
                if f.get('path') == file_path:
                    fs_id = f.get('fs_id')
                    break
        except Exception as e:
            logger.warning(f'获取 fs_id 失败: {e}')
    
    if not fs_id:
        logger.error('无法获取文件 fs_id')
        return None
    
    # 创建分享（xpan API，需要 access_token）
    if access_token:
        share_query = urllib.parse.urlencode({'method': 'set', 'access_token': access_token})
    else:
        # BDUSS 方式尝试创建分享（可能不支持，返回 None）
        share_query = urllib.parse.urlencode({'method': 'set', 'app_id': app_id})
    
    share_url = f'https://pan.baidu.com/rest/2.0/xpan/share?{share_query}'
    share_body = urllib.parse.urlencode({
        'fid_list': json.dumps([int(fs_id)]),
        'period': '0', 'schannel': '4',
        'channel_list': '[]', 'pwd': pwd,
    }).encode()
    
    req = urllib.request.Request(share_url, data=share_body, method='POST')
    req.add_header('Content-Type', 'application/x-www-form-urlencoded')
    if bduss and not access_token:
        req.add_header('Cookie', f'BDUSS={bduss}')
    
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            if result.get('errno') == 0 and result.get('link'):
                link = result['link']
                logger.info(f'分享链接: {link} (提取码: {pwd})')
                return link
            else:
                logger.error(f'分享失败: errno={result.get("errno")}, msg={result.get("errmsg", "")}')
                return None
    except Exception as e:
        logger.error(f'创建分享失败: {e}')
        return None

# ============================================================
# 主流程
# ============================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(description='UCSF/UPenn 医学影像数据集 全流程下载整理工具')
    parser.add_argument('--skip-download', action='store_true', help='跳过下载，直接打包上传')
    parser.add_argument('--skip-upload', action='store_true', help='跳过百度网盘上传')
    parser.add_argument('--dataset', choices=['all', 'ucsf-pdgm', 'upenn-gbm'], default='all', help='指定数据集')
    parser.add_argument('--baidu-token', default=None, help='自定义百度网盘 Access Token')
    parser.add_argument('--skip-dwi', action='store_true', help='跳过DWI大文件(.sz/.gqi.fz/.qsdr.fz)，仅下载结构像+分割标签')
    args = parser.parse_args()
    
    if args.baidu_token:
        global BAIDU_ACCESS_TOKEN
        BAIDU_ACCESS_TOKEN = args.baidu_token
    
    os.makedirs(BASE_DIR, exist_ok=True)
    os.makedirs(PACKAGE_DIR, exist_ok=True)
    
    logger.info(f'{"#"*60}')
    logger.info(f'UCSF/UPenn 医学影像数据集 全流程下载整理工具')
    logger.info(f'工作目录: {BASE_DIR}')
    logger.info(f'并发线程: {MAX_WORKERS}')
    logger.info(f'跳过下载: {args.skip_download}')
    logger.info(f'跳过上传: {args.skip_upload}')
    logger.info(f'{"#"*60}')
    
    # 确定要处理的数据集
    if args.dataset == 'all':
        datasets = list(GITHUB_RELEASES.keys())
    else:
        datasets = [args.dataset]
    
    share_results = {}
    
    for key in datasets:
        config = GITHUB_RELEASES[key]
        name = config['name']
        output_dir = config['output_dir']
        
        logger.info(f'\n{"="*60}')
        logger.info(f'处理数据集: {name}')
        logger.info(f'输出目录: {output_dir}')
        logger.info(f'{"="*60}')
        
        # 打印当前磁盘使用情况
        try:
            disk = shutil.disk_usage(os.path.dirname(output_dir))
            logger.info(f'磁盘空间: 总计 {disk.total/1024**3:.1f}GB, 已用 {disk.used/1024**3:.1f}GB, 可用 {disk.free/1024**3:.1f}GB')
        except:
            pass
        
        # 1. 下载
        if not args.skip_download:
            dl_result = download_dataset(config, skip_dwi=args.skip_dwi)
            if dl_result is None:
                logger.error(f'{name}: 下载失败，跳过打包和上传')
                share_results[name] = {'error': 'download failed (cannot get file list)'}
                # 清理可能存在的空目录
                if os.path.exists(output_dir):
                    shutil.rmtree(output_dir, ignore_errors=True)
                continue
        else:
            logger.info(f'跳过下载: {name}')
        
        # 检查目录是否存在
        if not os.path.exists(output_dir):
            logger.error(f'{name}: 输出目录不存在，跳过')
            share_results[name] = {'error': 'output directory not found'}
            continue
        
        # 2. 打包
        package_path = os.path.join(PACKAGE_DIR, f'{name}.tar.gz')
        if not os.path.exists(package_path):
            package_dataset(output_dir, package_path)
        else:
            logger.info(f'包已存在: {package_path}')
        
        # 3. 上传百度网盘
        if not args.skip_upload:
            remote_path = f'{BAIDU_REMOTE_DIR}/{name}.tar.gz'
            logger.info(f'上传到百度网盘: {remote_path}')
            uploaded_path = baidu_upload(package_path, remote_path, 
                                        access_token=BAIDU_ACCESS_TOKEN,
                                        bduss=BAIDU_BDUSS, app_id=BAIDU_APP_ID)
            
            if uploaded_path:
                # 4. 创建分享链接
                share_link = baidu_create_share(uploaded_path, 
                                               access_token=BAIDU_ACCESS_TOKEN,
                                               bduss=BAIDU_BDUSS, app_id=BAIDU_APP_ID)
                share_results[name] = {
                    'package': package_path,
                    'remote_path': uploaded_path,
                    'share_link': share_link,
                    'password': 'yolo',
                }
            else:
                logger.error(f'{name}: 上传失败')
                share_results[name] = {'package': package_path, 'error': 'upload failed'}
        else:
            share_results[name] = {'package': package_path, 'skipped_upload': True}
        
        # 5. 流式清理：删除原始下载目录（释放空间），打包文件保留到最后
        if os.path.exists(output_dir):
            logger.info(f'清理原始下载目录: {output_dir}')
            shutil.rmtree(output_dir, ignore_errors=True)
            logger.info('已清理，释放磁盘空间')
        
        # 打印清理后的磁盘使用情况
        try:
            disk = shutil.disk_usage(os.path.dirname(package_path))
            logger.info(f'清理后磁盘: 可用 {disk.free/1024**3:.1f}GB')
        except:
            pass
    
    # 最终汇总
    logger.info(f'\n{"#"*60}')
    logger.info(f'全部任务完成!')
    logger.info(f'{"#"*60}')
    
    for name, result in share_results.items():
        logger.info(f'\n{name}:')
        logger.info(f'  本地包: {result.get("package", "N/A")}')
        if 'share_link' in result:
            logger.info(f'  百度网盘: {result["share_link"]}')
            logger.info(f'  提取码: {result["password"]}')
        elif 'error' in result:
            logger.info(f'  状态: {result["error"]}')
        else:
            logger.info(f'  状态: 跳过上传')
    
    # 保存结果到 JSON
    result_file = os.path.join(BASE_DIR, 'download_results.json')
    with open(result_file, 'w', encoding='utf-8') as f:
        json.dump(share_results, f, ensure_ascii=False, indent=2)
    logger.info(f'\n结果已保存: {result_file}')

if __name__ == '__main__':
    main()
