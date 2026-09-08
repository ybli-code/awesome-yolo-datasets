"""
分享相关命令
"""

import os
import re
from typing import List, Optional, Set

import typer
from rich.console import Console
from rich.table import Table

from ..utils import get_client, handle_api_error, print_error, print_info, print_success, print_warning


def extract_share_links_from_file(file_path: str) -> List[str]:
    """
    从文件中提取夸克网盘分享链接

    Args:
        file_path: 文件路径

    Returns:
        提取到的分享链接列表
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"文件不存在: {file_path}")

    if not os.path.isfile(file_path):
        raise ValueError(f"路径不是文件: {file_path}")

    # 夸克网盘分享链接的正则表达式
    quark_link_pattern = r'https://pan\.quark\.cn/s/[a-zA-Z0-9]+'

    links = []

    try:
        # 尝试不同的编码格式读取文件
        encodings = ['utf-8', 'gbk', 'gb2312', 'utf-16']
        content = None

        for encoding in encodings:
            try:
                with open(file_path, 'r', encoding=encoding) as f:
                    content = f.read()
                break
            except UnicodeDecodeError:
                continue

        if content is None:
            raise ValueError(f"无法读取文件 {file_path}，不支持的编码格式")

        # 使用正则表达式提取所有匹配的链接
        matches = re.findall(quark_link_pattern, content)
        links.extend(matches)

    except Exception as e:
        raise ValueError(f"读取文件失败: {e}")

    return links


def deduplicate_links(links: List[str]) -> List[str]:
    """
    去重分享链接并打印日志

    Args:
        links: 原始链接列表

    Returns:
        去重后的链接列表
    """
    if not links:
        return []

    original_count = len(links)
    unique_links = list(dict.fromkeys(links))  # 保持顺序的去重
    duplicate_count = original_count - len(unique_links)

    if duplicate_count > 0:
        print_info(f"发现 {duplicate_count} 个重复链接，已自动去重")

    return unique_links


def validate_share_links(links: List[str]) -> List[str]:
    """
    验证分享链接格式并过滤无效链接

    Args:
        links: 链接列表

    Returns:
        有效的链接列表
    """
    if not links:
        return []

    valid_links = []
    invalid_count = 0

    # 验证正则表达式 - 夸克网盘分享ID通常是8-16位字母数字组合
    strict_pattern = r'^https://pan\.quark\.cn/s/[a-zA-Z0-9]{8,16}$'

    for link in links:
        # 清理链接（移除可能的参数和空白字符）
        clean_link = link.strip().split('?')[0].split(' ')[0]

        if re.match(strict_pattern, clean_link):
            valid_links.append(clean_link)
        else:
            invalid_count += 1
            print_warning(f"跳过无效链接: {link}")

    if invalid_count > 0:
        print_info(f"跳过了 {invalid_count} 个无效链接")

    return valid_links


def create_share(
    file_paths: List[str],
    title: str = "",
    expire_days: int = 0,
    password: Optional[str] = None,
    use_id: bool = False,
    check_duplicates: bool = True,
    force_new: bool = False
):
    """创建分享链接"""
    console = Console()

    try:
        with get_client() as client:
            if not client.is_logged_in():
                print_error("未登录，请先使用 quarkpan auth login 登录")
                raise typer.Exit(1)

            # 解析文件路径或ID
            if use_id:
                file_ids = file_paths
                print_info(f"使用文件ID创建分享: {', '.join(file_ids)}")
            else:
                # 使用路径解析器将路径转换为ID
                from ...services.name_resolver import NameResolver
                resolver = NameResolver(client.files)

                file_ids = []
                for path in file_paths:
                    try:
                        file_id, _ = resolver.resolve_path(path)
                        file_ids.append(file_id)
                        print_info(f"解析路径 '{path}' -> {file_id}")
                    except Exception as e:
                        print_error(f"无法解析路径 '{path}': {e}")
                        raise typer.Exit(1)

            # 显示分享参数
            print_info("📤 创建分享链接...")
            if title:
                print_info(f"   标题: {title}")
            if expire_days > 0:
                print_info(f"   有效期: {expire_days} 天")
            else:
                print_info("   有效期: 永久")
            if password:
                print_info(f"   提取码: {password}")
            else:
                print_info("   提取码: 无")

            # 使用智能批量分享功能
            def progress_callback(current, total, file_id, result):
                status_icon = {
                    'created': '🆕',
                    'reused': '✅',
                    'failed': '❌'
                }.get(result['status'], '❓')

                status_text = {
                    'created': '创建新分享',
                    'reused': '复用现有分享',
                    'failed': '分享失败'
                }.get(result['status'], '未知状态')

                print_info(f"[{current}/{total}] {status_icon} {status_text}: {file_id}")
                if result.get('share_url'):
                    print_info(f"    链接: {result['share_url']}")
                if result.get('message'):
                    if result['status'] == 'failed':
                        print_warning(f"    {result['message']}")
                    else:
                        print_info(f"    {result['message']}")

            # 创建分享
            result = client.shares.smart_batch_create_shares(
                file_ids=file_ids,
                title=title,
                expire_days=expire_days,
                password=password,
                check_duplicates=check_duplicates and not force_new,
                progress_callback=progress_callback
            )

            if result.get('status') == 200:
                data = result.get('data', {})
                total = data.get('total', 0)
                new_created = data.get('new_created', 0)
                reused = data.get('reused', 0)
                failed = data.get('failed', 0)

                print_success(f"批量分享完成!")
                print_info(f"📊 统计: 总计 {total}, 新建 {new_created}, 复用 {reused}, 失败 {failed}")

                # 显示成功的分享信息
                successful_results = [r for r in data.get('results', []) if r['status'] in ['created', 'reused']]

                if successful_results:
                    table = Table(title="分享结果")
                    table.add_column("状态", style="cyan")
                    table.add_column("分享链接", style="green")
                    table.add_column("标题", style="yellow")

                    for share_result in successful_results:
                        status_text = "🆕 新建" if share_result['status'] == 'created' else "✅ 复用"
                        table.add_row(
                            status_text,
                            share_result.get('share_url', 'N/A'),
                            share_result.get('title', 'N/A')
                        )

                    console.print(table)

                if failed > 0:
                    print_warning(f"有 {failed} 个文件分享失败，请检查文件是否存在或权限是否正确")
            else:
                print_error(f"批量分享失败: {result.get('message', '未知错误')}")
                raise typer.Exit(1)

    except Exception as e:
        handle_api_error(e, "创建分享")
        raise typer.Exit(1)


def list_my_shares(page: int = 1, size: int = 20):
    """列出我的分享"""
    console = Console()

    try:
        with get_client() as client:
            if not client.is_logged_in():
                print_error("未登录，请先使用 quarkpan auth login 登录")
                raise typer.Exit(1)

            print_info(f"📋 获取我的分享列表 (第{page}页)...")

            result = client.get_my_shares(page=page, size=size)

            if result and result.get('status') == 200:
                data = result.get('data', {})
                shares = data.get('list', [])
                metadata = result.get('metadata', {})
                total = metadata.get('_total', 0)

                if not shares:
                    print_warning("暂无分享")
                    return

                print_success(f"找到 {total} 个分享")

                # 创建表格
                table = Table(title=f"我的分享 (第{page}页，共{total}个)")
                table.add_column("序号", style="cyan", width=4)
                table.add_column("标题", style="green", width=18)
                table.add_column("分享链接", style="bright_blue", width=35)
                table.add_column("类型", style="yellow", width=4)
                table.add_column("文件数", style="yellow", width=6)
                table.add_column("创建时间", style="blue", width=12)
                table.add_column("状态", style="magenta", width=6)
                table.add_column("访问量", style="dim", width=6)

                for i, share in enumerate(shares, 1):
                    # 格式化创建时间
                    created_at = share.get('created_at', 0)
                    if created_at:
                        import datetime
                        create_time = datetime.datetime.fromtimestamp(created_at / 1000)
                        time_str = create_time.strftime('%m-%d %H:%M')
                    else:
                        time_str = "未知"

                    # 状态
                    status = "正常" if share.get('status') == 1 else "已失效"

                    # 类型（文件夹或文件）
                    first_file = share.get('first_file', {})
                    is_dir = first_file.get('dir', False)
                    file_type = "📁" if is_dir else "📄"

                    # 分享链接（完整显示）
                    share_url = share.get('share_url', '')

                    # 访问量
                    click_pv = share.get('click_pv', 0)

                    table.add_row(
                        str(i),
                        share.get('title', '无标题')[:16],  # 稍微缩短标题
                        share_url,  # 完整显示分享链接
                        file_type,
                        str(share.get('file_num', 0)),
                        time_str,
                        status,
                        str(click_pv)
                    )

                console.print(table)

                # 显示详细统计信息
                print_info(f"\n📊 统计信息:")
                total_clicks = sum(share.get('click_pv', 0) for share in shares)
                total_saves = sum(share.get('save_pv', 0) for share in shares)
                total_downloads = sum(share.get('download_pv', 0) for share in shares)
                print_info(f"   总访问量: {total_clicks}")
                print_info(f"   总保存量: {total_saves}")
                print_info(f"   总下载量: {total_downloads}")

                # 分页信息
                total_pages = (total + size - 1) // size
                if total_pages > 1:
                    print_info(f"\n📄 第 {page}/{total_pages} 页")
                    if page < total_pages:
                        print_info(f"使用 'quarkpan shares --page {page + 1}' 查看下一页")
            else:
                print_error("获取分享列表失败")
                raise typer.Exit(1)

    except Exception as e:
        handle_api_error(e, "获取分享列表")
        raise typer.Exit(1)


def save_share(
    share_url: str,
    target_folder: str = "/来自：分享/",
    create_folder: bool = True,
    save_all: bool = True,
    wait_completion: bool = True,
    timeout: int = 60
):
    """转存分享文件"""
    try:
        with get_client() as client:
            if not client.is_logged_in():
                print_error("未登录，请先使用 quarkpan auth login 登录")
                raise typer.Exit(1)

            print_info(f"🔗 解析分享链接: {share_url}")

            # 解析目标文件夹
            target_folder_id = "0"  # 默认根目录
            target_folder_name = None

            if target_folder != "/":
                # 解析目标文件夹路径
                from ...services.name_resolver import NameResolver
                resolver = NameResolver(client.files)

                try:
                    target_folder_id, _ = resolver.resolve_path(target_folder)
                    print_info(f"目标文件夹: {target_folder}")
                except:
                    if create_folder:
                        # 自动创建文件夹
                        target_folder_name = target_folder.split('/')[-1]
                        print_info(f"将创建新文件夹: {target_folder_name}")
                    else:
                        print_error(f"目标文件夹不存在: {target_folder}")
                        print_info("使用 --create-folder 自动创建文件夹")
                        raise typer.Exit(1)

            print_info("📥 开始转存...")
            if wait_completion:
                print_info("⏳ 等待转存任务完成...")

            result = client.save_shared_files(
                share_url=share_url,
                target_folder_id=target_folder_id,
                target_folder_name=target_folder_name,
                save_all=save_all,
                wait_for_completion=wait_completion,
                timeout=timeout
            )

            if result:
                share_info = result.get('share_info', {})
                file_count = share_info.get('file_count', 0)
                print_success(f"转存成功! 共转存 {file_count} 个文件")

                # 显示转存的文件信息
                files = share_info.get('files', [])
                if files and len(files) <= 10:  # 只显示前10个文件
                    print_info("转存的文件:")
                    for file_info in files:
                        file_name = file_info.get('file_name', '未知文件')
                        file_size = file_info.get('size', 0)
                        if file_size > 0:
                            size_str = _format_size(file_size)
                            print_info(f"  📄 {file_name} ({size_str})")
                        else:
                            print_info(f"  📁 {file_name}")
            else:
                print_error("转存失败")
                raise typer.Exit(1)

    except Exception as e:
        handle_api_error(e, "转存分享")
        raise typer.Exit(1)


def batch_save_shares(
    share_urls: List[str],
    target_folder: str = "/来自：分享/",
    save_all: bool = True,
    wait_completion: bool = True,
    create_subfolder: bool = False,
    from_file: Optional[str] = None
):
    """批量转存分享链接"""
    try:
        with get_client() as client:
            if not client.is_logged_in():
                print_error("未登录，请先使用 quarkpan auth login 登录")
                raise typer.Exit(1)

            # 如果指定了文件，从文件中提取链接
            if from_file:
                print_info(f"📄 读取文件: {from_file}")

                try:
                    # 提取链接
                    extracted_links = extract_share_links_from_file(from_file)
                    print_info(f"🔍 提取到 {len(extracted_links)} 个分享链接")

                    if not extracted_links:
                        print_warning("文件中未找到任何夸克网盘分享链接")
                        raise typer.Exit(0)

                    # 去重
                    unique_links = deduplicate_links(extracted_links)

                    # 验证链接格式
                    valid_links = validate_share_links(unique_links)

                    if not valid_links:
                        print_error("没有找到有效的分享链接")
                        raise typer.Exit(1)

                    print_success(f"✅ 处理完成，共 {len(valid_links)} 个有效链接")
                    share_urls = valid_links

                except FileNotFoundError as e:
                    print_error(str(e))
                    raise typer.Exit(1)
                except ValueError as e:
                    print_error(str(e))
                    raise typer.Exit(1)

            elif not share_urls:
                print_error("请提供分享链接或使用 --from 参数指定文件")
                raise typer.Exit(1)

            print_info(f"🔗 准备批量转存 {len(share_urls)} 个分享链接")

            # 解析目标文件夹
            target_folder_id = "0"  # 默认根目录
            if target_folder != "/":
                try:
                    from ...services.name_resolver import NameResolver
                    resolver = NameResolver(client.files)
                    target_folder_id, _ = resolver.resolve_path(target_folder)
                    print_info(f"目标文件夹: {target_folder} -> {target_folder_id}")
                except Exception as e:
                    print_error(f"无法解析目标文件夹路径 '{target_folder}': {e}")
                    raise typer.Exit(1)

            print_info("📥 开始批量转存...")
            if wait_completion:
                print_info("⏳ 等待所有转存任务完成...")

            # 进度回调函数
            def progress_callback(current, total, url, result):
                if result.get('success'):
                    print_success(f"[{current}/{total}] ✅ 转存成功: {url}")
                else:
                    print_error(f"[{current}/{total}] ❌ 转存失败: {url} - {result.get('error', '未知错误')}")

            results = client.batch_save_shares(
                share_urls=share_urls,
                target_folder_id=target_folder_id,
                create_subfolder=create_subfolder,
                save_all=save_all,
                wait_for_completion=wait_completion,
                progress_callback=progress_callback
            )

            # 统计结果
            success_count = sum(1 for r in results if r.get('success'))
            failed_count = len(results) - success_count

            print_info(f"\n📊 批量转存完成:")
            print_success(f"成功: {success_count}")
            if failed_count > 0:
                print_error(f"❌ 失败: {failed_count}")

            # 显示失败的链接
            failed_urls = [r['url'] for r in results if not r.get('success')]
            if failed_urls:
                print_warning("\n失败的分享链接:")
                for url in failed_urls:
                    print_warning(f"  - {url}")

    except Exception as e:
        handle_api_error(e, "批量转存分享")
        raise typer.Exit(1)


def _format_size(size: int) -> str:
    """格式化文件大小"""
    if size < 1024:
        return f"{size} B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    elif size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    else:
        return f"{size / (1024 * 1024 * 1024):.1f} GB"
