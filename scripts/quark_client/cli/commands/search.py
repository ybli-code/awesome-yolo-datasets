"""
文件搜索命令
"""

from typing import List, Optional

import typer
from rich import print as rprint
from rich.console import Console
from rich.table import Table

from ..utils import (format_file_size, format_timestamp, get_client,
                     get_file_type_icon, handle_api_error, print_error,
                     print_info, print_warning, truncate_text)

search_app = typer.Typer(help="🔍 文件搜索")
console = Console()


@search_app.callback(invoke_without_command=True)
def search_main(
    ctx: typer.Context,
    keyword: Optional[str] = typer.Argument(None, help="搜索关键词"),
    folder_id: str = typer.Option("0", "--folder", "-f", help="搜索范围文件夹ID，默认全盘搜索"),
    page: int = typer.Option(1, "--page", "-p", help="页码"),
    size: int = typer.Option(20, "--size", "-s", help="每页数量"),
    show_details: bool = typer.Option(False, "--details", "-d", help="显示详细信息"),
    extensions: Optional[List[str]] = typer.Option(None, "--ext", "-e", help="文件扩展名过滤 (如: pdf, doc, mp4)"),
    min_size: Optional[str] = typer.Option(None, "--min-size", help="最小文件大小 (如: 1MB, 100KB)"),
    max_size: Optional[str] = typer.Option(None, "--max-size", help="最大文件大小 (如: 100MB, 1GB)")
):
    """搜索文件"""
    if ctx.invoked_subcommand is not None:
        return

    if not keyword:
        rprint("[red]❌ 请提供搜索关键词[/red]")
        rprint("使用: [cyan]quarkpan search \"关键词\"[/cyan]")
        raise typer.Exit(1)

    # 直接在这里实现搜索逻辑
    do_search(keyword, folder_id, page, size, show_details, extensions, min_size, max_size)


def do_search(
    keyword: str,
    folder_id: str = "0",
    page: int = 1,
    size: int = 20,
    show_details: bool = False,
    file_extensions: Optional[List[str]] = None,
    min_size: Optional[str] = None,
    max_size: Optional[str] = None
):
    """搜索文件"""
    try:
        with get_client() as client:
            if not client.is_logged_in():
                print_error("未登录，请先使用 quarkpan auth login 登录")
                raise typer.Exit(1)

            print_info(f"正在搜索: {keyword}")

            # 解析文件大小
            min_size_bytes = parse_file_size(min_size) if min_size else None
            max_size_bytes = parse_file_size(max_size) if max_size else None

            # 执行搜索
            if file_extensions or min_size_bytes or max_size_bytes:
                # 高级搜索
                results = client.search_files_advanced(
                    keyword=keyword,
                    folder_id=folder_id,
                    page=page,
                    size=size,
                    file_extensions=file_extensions,
                    min_size=min_size_bytes,
                    max_size=max_size_bytes
                )
            else:
                # 基础搜索
                results = client.search_files(
                    keyword=keyword,
                    folder_id=folder_id,
                    page=page,
                    size=size
                )

            if not results or 'data' not in results:
                print_error("搜索失败")
                raise typer.Exit(1)

            file_list = results['data'].get('list', [])
            # 搜索API的total在metadata中
            total = results.get('metadata', {}).get('_total', len(file_list))

            # 显示搜索结果
            search_scope = "全盘" if folder_id == "0" else f"文件夹 {folder_id}"
            rprint(f"\n🔍 搜索结果: [bold]{keyword}[/bold] (范围: {search_scope})")

            if not file_list:
                print_warning("没有找到匹配的文件")
                return

            # 显示过滤条件
            filters = []
            if file_extensions:
                filters.append(f"扩展名: {', '.join(file_extensions)}")
            if min_size:
                filters.append(f"最小: {min_size}")
            if max_size:
                filters.append(f"最大: {max_size}")

            if filters:
                rprint(f"[dim]过滤条件: {' | '.join(filters)}[/dim]")

            if show_details:
                # 详细表格视图
                table = Table(title=f"第{page}页，共{total}个结果")
                table.add_column("序号", style="dim", width=4)
                table.add_column("类型", style="cyan", width=4)
                table.add_column("名称", style="white", min_width=25)
                table.add_column("大小", style="green", width=10)
                table.add_column("修改时间", style="yellow", width=16)
                table.add_column("ID", style="dim", width=8)

                for i, file_info in enumerate(file_list, (page - 1) * size + 1):
                    name = file_info.get('file_name', '未知')
                    size_bytes = file_info.get('size', 0)
                    file_type = file_info.get('file_type', 0)
                    updated_at = file_info.get('updated_at', '')
                    fid = file_info.get('fid', '')

                    is_folder = file_type == 0
                    type_icon = get_file_type_icon(name, is_folder)
                    size_str = "-" if is_folder else format_file_size(size_bytes)
                    time_str = format_timestamp(updated_at) if updated_at else "-"
                    short_id = fid[:8] + "..." if len(fid) > 8 else fid

                    table.add_row(
                        str(i),
                        type_icon,
                        truncate_text(name, 30),
                        size_str,
                        time_str,
                        short_id
                    )

                console.print(table)
            else:
                # 简洁列表视图
                rprint(f"[dim]第{page}页，共{total}个结果[/dim]\n")

                for i, file_info in enumerate(file_list, (page - 1) * size + 1):
                    name = file_info.get('file_name', '未知')
                    file_type = file_info.get('file_type', 0)
                    size_bytes = file_info.get('size', 0)

                    is_folder = file_type == 0
                    type_icon = get_file_type_icon(name, is_folder)

                    if is_folder:
                        rprint(f"  {i:2d}. {type_icon} {name}")
                    else:
                        size_str = format_file_size(size_bytes)
                        rprint(f"  {i:2d}. {type_icon} {name} [dim]({size_str})[/dim]")

            # 显示分页信息
            if total > size:
                total_pages = (total + size - 1) // size
                rprint(f"\n[dim]第 {page}/{total_pages} 页，共 {total} 个结果[/dim]")
                if page < total_pages:
                    rprint(f"[dim]使用 --page {page + 1} 查看下一页[/dim]")

    except Exception as e:
        handle_api_error(e, "搜索文件")
        raise typer.Exit(1)


def parse_file_size(size_str: str) -> Optional[int]:
    """解析文件大小字符串，返回字节数"""
    if not size_str:
        return None

    size_str = size_str.upper().strip()

    # 提取数字和单位
    import re
    match = re.match(r'^(\d+(?:\.\d+)?)\s*([KMGT]?B?)$', size_str)
    if not match:
        return None

    number = float(match.group(1))
    unit = match.group(2) or 'B'

    # 转换为字节
    multipliers = {
        'B': 1,
        'KB': 1024,
        'MB': 1024 ** 2,
        'GB': 1024 ** 3,
        'TB': 1024 ** 4,
        'K': 1024,
        'M': 1024 ** 2,
        'G': 1024 ** 3,
        'T': 1024 ** 4
    }

    return int(number * multipliers.get(unit, 1))


if __name__ == "__main__":
    search_app()
