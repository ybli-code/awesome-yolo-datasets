"""
移动文件相关命令
"""

from typing import List

import typer

from ..utils import (get_client, handle_api_error, print_error, print_info,
                     print_success)


def move_files(
    source_paths: List[str],
    target_path: str,
    use_id: bool = False
):
    """移动文件到指定文件夹"""
    try:
        with get_client() as client:
            if not client.is_logged_in():
                print_error("未登录，请先使用 quarkpan auth login 登录")
                raise typer.Exit(1)

            # 创建路径解析器
            from ...services.name_resolver import NameResolver
            resolver = NameResolver(client.files)

            # 解析源文件路径或ID
            if use_id:
                file_ids = source_paths
                print_info(f"使用文件ID移动: {', '.join(file_ids)}")
            else:
                file_ids = []
                for path in source_paths:
                    try:
                        file_id, _ = resolver.resolve_path(path)
                        file_ids.append(file_id)
                        print_info(f"解析路径 '{path}' -> {file_id}")
                    except Exception as e:
                        print_error(f"无法解析路径 '{path}': {e}")
                        raise typer.Exit(1)

            # 解析目标文件夹路径或ID
            if use_id:
                target_folder_id = target_path
                print_info(f"目标文件夹ID: {target_folder_id}")
            else:
                try:
                    target_folder_id, target_type = resolver.resolve_path(target_path)

                    # 检查目标是否为文件夹
                    if target_type != 'folder':
                        print_error(f"目标路径不是文件夹: {target_path}")
                        raise typer.Exit(1)

                    print_info(f"目标文件夹: {target_path} -> {target_folder_id}")
                except Exception as e:
                    print_error(f"无法解析目标路径 '{target_path}': {e}")
                    raise typer.Exit(1)

            # 显示移动信息
            print_info("📦 开始移动文件...")
            print_info(f"   源文件数量: {len(file_ids)}")
            print_info(f"   目标文件夹: {target_path if not use_id else target_folder_id}")

            # 执行移动
            result = client.move_files(
                file_ids=file_ids,
                target_folder_id=target_folder_id
            )

            if result and result.get('status') == 200:
                data = result.get('data', {})
                task_id = data.get('task_id')
                finish = data.get('finish', False)

                if finish:
                    print_success("文件移动完成!")
                else:
                    print_success(f"文件移动完成! (任务ID: {task_id})")

                # 显示移动结果
                print_info(f"\n📊 移动结果:")
                print_info(f"   移动文件数: {len(file_ids)}")
                print_info(f"   状态: {'同步完成' if finish else '异步完成'}")

                if not use_id:
                    print_info(f"\n💡 提示: 文件已移动到 '{target_path}'")
            else:
                print_error("移动失败")
                raise typer.Exit(1)

    except Exception as e:
        handle_api_error(e, "移动文件")
        raise typer.Exit(1)


def move_to_folder(
    source_paths: List[str],
    folder_name: str,
    parent_folder: str = "/",
    create_folder: bool = True,
    use_id: bool = False
):
    """移动文件到指定名称的文件夹（如果不存在则创建）"""
    # TODO: 实现 use_id 参数功能
    _ = use_id  # 参数将在未来实现中使用
    try:
        with get_client() as client:
            if not client.is_logged_in():
                print_error("未登录，请先使用 quarkpan auth login 登录")
                raise typer.Exit(1)

            # 构造目标文件夹路径
            if parent_folder == "/":
                target_folder_path = f"/{folder_name}"
            else:
                target_folder_path = f"{parent_folder.rstrip('/')}/{folder_name}"

            print_info(f"目标文件夹路径: {target_folder_path}")

            # 初始化路径解析器
            try:
                from ...services.name_resolver import NameResolver
                resolver = NameResolver(client.files)
            except Exception as e:
                print_error(f"无法初始化路径解析器: {e}")
                raise typer.Exit(1)

            # 检查目标文件夹是否存在
            try:
                target_folder_id, _ = resolver.resolve_path(target_folder_path)
                print_info(f"找到现有文件夹: {target_folder_path}")
            except:
                if create_folder:
                    # 创建文件夹
                    print_info(f"创建新文件夹: {folder_name}")

                    # 解析父文件夹ID
                    if parent_folder == "/":
                        parent_folder_id = "0"
                    else:
                        try:
                            parent_folder_id, _ = resolver.resolve_path(parent_folder)
                        except Exception as e:
                            print_error(f"无法解析父文件夹路径 '{parent_folder}': {e}")
                            raise typer.Exit(1)

                    # 创建文件夹
                    create_result = client.create_folder(folder_name, parent_folder_id)
                    if create_result and create_result.get('status') == 200:
                        target_folder_id = create_result.get('data', {}).get('fid')
                        print_success(f"文件夹创建成功: {folder_name}")
                    else:
                        print_error(f"创建文件夹失败: {folder_name}")
                        raise typer.Exit(1)
                else:
                    print_error(f"目标文件夹不存在: {target_folder_path}")
                    print_info("使用 --create-folder 自动创建文件夹")
                    raise typer.Exit(1)

            # 调用移动函数
            move_files(source_paths, target_folder_id, use_id=True)

    except Exception as e:
        handle_api_error(e, "移动文件到文件夹")
        raise typer.Exit(1)
