"""
认证管理命令
"""

import typer
from rich import print as rprint

from ..utils import get_client, print_error, print_info, print_success

auth_app = typer.Typer(help="🔐 认证管理")


@auth_app.command()
def login(
    force: bool = typer.Option(False, "--force", "-f", help="强制重新登录"),
    method: str = typer.Option("auto", "--method", "-m", help="登录方式: auto, api, simple"),
    api: bool = typer.Option(False, "--api", help="使用API登录"),
    simple: bool = typer.Option(False, "--simple", help="使用简化登录")
):
    """🔐 登录夸克网盘

    支持多种登录方式：
    • auto: 自动选择最佳方式 (默认)
    • api: 纯API登录，轻量快速
    • simple: 简化登录，手动指导
    """
    try:
        with get_client(auto_login=False) as client:
            # 检查当前登录状态
            if not force and client.is_logged_in():
                rprint("[green]✅ 已经登录，无需重复登录[/green]")
                rprint("使用 [cyan]--force[/cyan] 强制重新登录")
                return

            # 确定登录方式
            if api:
                method = "api"
            elif simple:
                method = "simple"

            # 显示登录方式信息
            method_info = {
                "auto": "🚀 自动选择最佳登录方式",
                "api": "⚡ API登录 - 轻量快速，无需浏览器",
                "simple": "📝 简化登录 - 手动指导，完全无依赖"
            }

            print_info(f"正在登录夸克网盘... {method_info.get(method, method)}")

            if method == "api":
                rprint("[dim]将自动生成二维码，请使用夸克APP扫描[/dim]")
            elif method == "simple":
                rprint("[dim]将提供详细的手动登录指导[/dim]")
            else:
                rprint("[dim]将自动选择最适合的登录方式[/dim]")

            # 执行登录
            cookies = client.login(force_relogin=force, method=method)

            if cookies:
                print_success("登录成功！")

                # 验证登录状态
                if client.is_logged_in():
                    print_info("登录状态验证通过")

                    # 尝试获取用户信息
                    try:
                        storage = client.get_storage_info()
                        if storage and 'data' in storage:
                            print_info("账户信息获取成功")
                        else:
                            rprint("[yellow]⚠️ 无法获取账户信息，但登录成功[/yellow]")
                    except Exception:
                        rprint("[yellow]⚠️ 无法获取账户信息，但登录成功[/yellow]")
                else:
                    rprint("[yellow]⚠️ 登录可能未完全成功，请重试[/yellow]")
            else:
                print_error("登录失败，未获取到有效凭证")
                raise typer.Exit(1)

    except KeyboardInterrupt:
        rprint("\n[yellow]⚠️ 登录被用户取消[/yellow]")
        raise typer.Exit(1)
    except Exception as e:
        print_error(f"登录失败: {e}")
        rprint("\n[dim]可能的解决方案:[/dim]")
        rprint("[dim]1. 检查网络连接[/dim]")
        rprint("[dim]2. 尝试使用 --manual 手动登录[/dim]")
        rprint("[dim]3. 确保浏览器正常工作[/dim]")
        raise typer.Exit(1)


@auth_app.command()
def logout():
    """登出夸克网盘"""
    try:
        with get_client(auto_login=False) as client:
            if not client.is_logged_in():
                rprint("[yellow]⚠️ 当前未登录[/yellow]")
                return

            print_info("正在登出...")
            client.logout()
            print_success("已成功登出")

    except Exception as e:
        print_error(f"登出失败: {e}")
        raise typer.Exit(1)


@auth_app.command()
def status():
    """检查登录状态"""
    try:
        with get_client(auto_login=False) as client:
            if client.is_logged_in():
                print_success("已登录")

                # 尝试获取账户信息
                try:
                    storage = client.get_storage_info()
                    if storage and 'data' in storage:
                        data = storage['data']
                        total = data.get('total', 0)
                        used = data.get('used', 0)

                        from ..utils import format_file_size

                        rprint(f"[dim]总容量: {format_file_size(total)}[/dim]")
                        rprint(f"[dim]已使用: {format_file_size(used)}[/dim]")
                        rprint(f"[dim]剩余: {format_file_size(total - used)}[/dim]")
                    else:
                        rprint("[yellow]⚠️ 无法获取存储信息[/yellow]")
                except Exception as e:
                    rprint(f"[yellow]⚠️ 获取存储信息失败: {e}[/yellow]")
            else:
                rprint("[red]❌ 未登录[/red]")
                rprint("使用 [cyan]quarkpan auth login[/cyan] 登录")
                raise typer.Exit(1)

    except Exception as e:
        print_error(f"检查状态失败: {e}")
        raise typer.Exit(1)


@auth_app.command()
def info():
    """显示认证相关信息"""
    rprint("""
[bold blue]🔐 认证管理[/bold blue]

[bold]可用命令:[/bold]
  [cyan]login[/cyan]   - 登录夸克网盘
  [cyan]logout[/cyan]  - 登出
  [cyan]status[/cyan]  - 检查登录状态

[bold]登录选项:[/bold]
  [cyan]--qr[/cyan]      - 使用二维码登录 (默认)
  [cyan]--manual[/cyan]  - 使用手动登录
  [cyan]--force[/cyan]   - 强制重新登录

[bold]示例:[/bold]
  [dim]# 二维码登录[/dim]
  quarkpan auth login

  [dim]# 手动登录[/dim]
  quarkpan auth login --manual

  [dim]# 强制重新登录[/dim]
  quarkpan auth login --force

  [dim]# 检查状态[/dim]
  quarkpan auth status

  [dim]# 登出[/dim]
  quarkpan auth logout

[bold]说明:[/bold]
- 二维码登录: 自动提取二维码，使用夸克APP扫描
- 手动登录: 打开浏览器，手动完成登录流程
- 登录凭证会自动保存，下次使用时无需重新登录
""")


if __name__ == "__main__":
    auth_app()
