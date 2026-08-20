"""`mi auth` / `mi profile` 命令。"""
from __future__ import annotations

import queue
import sys
import threading
import time
import webbrowser
from typing import Annotated, Optional

import typer

from .. import render
from ..core import const
from ..core.callback import (
    CallbackResult,
    CallbackServer,
    MdnsPublisher,
    local_ips,
    parse_pasted,
    port_available,
    resolve_redirect_host,
)
from ..core.oauth import OAuthClient, build_auth_url, state_for_device
from ..errors import MiCliError, NotAuthenticated, UsageError
from ..render import OutputFormat, mask
from ..store import Profile, config_dir, list_profiles, read_config, write_config
from .context import AppContext, OutputOption, pick_output

app = typer.Typer(help="登录与凭据管理", no_args_is_help=True)
profile_app = typer.Typer(help="多账号/多区域配置", no_args_is_help=True)


def _ctx(ctx: typer.Context) -> AppContext:
    return ctx.obj


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 0:
        return "已过期"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days} 天 {hours} 小时"
    if hours:
        return f"{hours} 小时 {minutes} 分"
    return f"{minutes} 分"


_STDIN_EOF = "__stdin_eof__"


def _collect_code(
    *,
    server: CallbackServer | None,
    result_queue: "queue.Queue[object]",
    expected_state: str,
    wait_seconds: float,
) -> CallbackResult:
    """等回调或等用户粘贴，谁先来算谁。"""
    stop = threading.Event()

    def read_stdin() -> None:
        while not stop.is_set():
            try:
                line = sys.stdin.readline()
            except (OSError, ValueError):
                return
            if not line:
                result_queue.put(_STDIN_EOF)
                return
            if not line.strip():
                continue
            try:
                result_queue.put(parse_pasted(line, expected_state=expected_state))
                return
            except MiCliError as err:
                render.error(err.message)
                render.info("再试一次，或按 Ctrl-C 退出：")

    threading.Thread(target=read_stdin, name="mi-paste", daemon=True).start()

    deadline = time.monotonic() + wait_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            stop.set()
            raise MiCliError(
                f"等待授权超时（{int(wait_seconds)} 秒）",
                hint="用 `mi auth login --manual` 手工粘贴回调地址",
            )
        try:
            item = result_queue.get(timeout=min(1.0, remaining))
        except queue.Empty:
            continue
        if item is _STDIN_EOF:
            if server is None:
                stop.set()
                raise UsageError(
                    "标准输入已结束，也没有本地回调可用；"
                    "请在交互式终端里运行，或用管道传入回调地址"
                )
            # 还有本地回调这条路，继续等。
            continue
        result: CallbackResult = item  # type: ignore[assignment]
        stop.set()
        if result.source == "server":
            # 本地回调一定带 state，不匹配就是别人打过来的请求，直接拒。
            if result.state != expected_state:
                raise MiCliError(
                    "回调的 state 与本次登录不符，已忽略",
                    hint="请重新执行 `mi auth login`",
                )
        elif result.state and result.state != expected_state:
            raise MiCliError("state 不匹配，可能不是本次登录的回调，请重新登录")
        elif not result.state:
            render.warn("粘贴的内容里没有 state，跳过校验")
        return result


@app.command()
def login(
    ctx: typer.Context,
    region: Annotated[
        Optional[str], typer.Option("--region", "-r", help="区域：" + "/".join(const.CLOUD_SERVERS))
    ] = None,
    manual: Annotated[
        bool, typer.Option("--manual", help="不监听端口，只手工粘贴回调地址")
    ] = False,
    no_browser: Annotated[
        bool, typer.Option("--no-browser", help="不自动打开浏览器")
    ] = False,
    no_mdns: Annotated[
        bool, typer.Option("--no-mdns", help="不广播 mDNS")
    ] = False,
    redirect_url: Annotated[
        Optional[str],
        typer.Option(
            "--redirect-url",
            help="自定义 redirect_uri（host 必须是 homeassistant.local:8123）",
        ),
    ] = None,
    device_id: Annotated[
        Optional[str], typer.Option("--device-id", help="自定义 device_id（排查用）")
    ] = None,
    wait: Annotated[
        float, typer.Option("--wait", help="等待授权的秒数")
    ] = 300.0,
    skip_confirm: Annotated[
        bool, typer.Option("--skip-confirm", help="已授权过则跳过确认页")
    ] = False,
) -> None:
    """用小米账号登录。"""
    app_ctx = _ctx(ctx)
    region = region or app_ctx.resolved_region()
    if region not in const.CLOUD_SERVERS:
        raise UsageError(
            f"未知区域 `{region}`，可选：{'、'.join(const.CLOUD_SERVERS)}"
        )

    profile = app_ctx.profile
    identity = profile.identity(region)
    redirect = redirect_url or identity.redirect_url
    if not redirect.startswith(const.REDIRECT_ORIGIN):
        render.warn(
            f"redirect_uri 的 host 不是 {const.REDIRECT_HOST}:{const.REDIRECT_PORT}，"
            "小米服务端会拒绝（invalid redirect uri）"
        )
    device_id = device_id or identity.device_id
    # state 用 HA 的算法，纯粹是为了少一个和上游不一致的变量。
    state = state_for_device(device_id)
    auth_url = build_auth_url(
        redirect_url=redirect,
        device_id=device_id,
        state=state,
        skip_confirm=skip_confirm,
    )
    # 先落盘，换 token 失败时还能用 `mi auth exchange` 拿同一个 code 重试。
    profile.write_pending(
        {
            "region": region,
            "device_id": device_id,
            "redirect_url": redirect,
            "state": state,
            "created_at": int(time.time()),
        }
    )

    result_queue: "queue.Queue[object]" = queue.Queue()
    server: CallbackServer | None = None
    mdns: MdnsPublisher | None = None

    if not manual:
        server, mdns = _try_auto_capture(result_queue, no_mdns=no_mdns)

    try:
        render.info("")
        render.info("[bold]1.[/bold] 在浏览器里打开下面的链接，用小米账号登录并授权：")
        render.raw(auth_url)
        if not no_browser and not _open_browser(auth_url):
            render.warn("没能自动打开浏览器，请手工复制上面的链接")
        if server:
            render.info(
                f"[bold]2.[/bold] 授权后浏览器会跳回 {redirect}，"
                "本机已在 8123 端口等着接收。"
            )
            render.info(
                "[bold]3.[/bold] 如果那个页面打不开（说明 homeassistant.local "
                "没指向本机），把浏览器地址栏里的整段地址粘到这里回车："
            )
        else:
            render.info(
                f"[bold]2.[/bold] 授权后浏览器会跳到 {redirect}，"
                "这个地址多半打不开——没关系，"
            )
            render.info(
                "[bold]3.[/bold] 直接把浏览器地址栏里的整段地址粘到这里回车："
            )
        render.info("")

        result = _collect_code(
            server=server,
            result_queue=result_queue,
            expected_state=state,
            wait_seconds=wait,
        )
        if server:
            _drain(server, result_queue, result)
    finally:
        if server:
            server.close()
        if mdns:
            mdns.close()

    render.info(
        f"拿到授权码（来自{'本地回调' if result.source == 'server' else '粘贴'}），"
        "正在换取 token…"
    )
    try:
        _exchange_and_save(
            app_ctx,
            region=region,
            redirect=redirect,
            device_id=device_id,
            code=result.code,
        )
    except MiCliError as err:
        # 换 token 失败时授权码通常还没被消耗，别让用户白跑一遍浏览器。
        render.error(err.message)
        if err.hint:
            render.info(f"[dim]提示：{err.hint}[/dim]")
        render.info("授权码还在有效期内的话可以直接重试：")
        render.raw(f"  mi -v auth exchange {result.code}")
        raise typer.Exit(code=err.exit_code) from err


def _exchange_and_save(
    app_ctx: AppContext, *, region: str, redirect: str, device_id: str, code: str
) -> None:
    """用授权码换 token 并落盘。

    授权、换 token 两步里的 client_id / redirect_uri / device_id 必须完全一致，
    否则服务端返回 96002 invalid request。
    """
    profile = app_ctx.profile
    trace = render.raw if app_ctx.verbose else None
    with OAuthClient(
        region, redirect_url=redirect, timeout=app_ctx.timeout, trace=trace
    ) as client:
        token = client.exchange_code(code, device_id)
        auth = token.to_auth(region, device_id)
        try:
            info = client.user_profile(token.access_token)
            auth.uid = str(info.get("userId") or "") or None
            auth.nickname = info.get("miliaoNick") or info.get("nickname")
        except MiCliError as err:
            render.warn(f"账号信息获取失败（不影响登录）：{err.message}")

    profile.write_auth(auth)
    profile.clear_pending()
    render.success(
        f"登录成功：{auth.nickname or '未知昵称'}"
        f"（uid {auth.uid or '未知'}，区域 {region}，profile {profile.name}）"
    )
    render.info(f"凭据已保存到 {profile.auth_path}（权限 0600）")
    render.info(
        f"有效期约 {_fmt_duration(auth.expires_at - time.time())}，到期前会自动续期"
    )


@app.command()
def exchange(
    ctx: typer.Context,
    code_or_url: Annotated[
        str, typer.Argument(help="授权码，或浏览器回调地址（整段粘贴）")
    ],
) -> None:
    """用授权码换 token。

    登录时换 token 那一步失败（比如网络抖动）而授权码还没过期时，可以用这个
    命令重试，不必再走一遍浏览器；参数取自上一次 `mi auth login` 的记录。
    """
    app_ctx = _ctx(ctx)
    profile = app_ctx.profile
    pending = profile.read_pending()
    if not pending:
        raise UsageError(
            "没有找到上一次登录的记录，先执行 `mi auth login`",
        )
    result = parse_pasted(code_or_url, expected_state=pending.get("state"))
    age = int(time.time()) - int(pending.get("created_at", 0))
    if age > 600:
        render.warn(f"上一次登录是 {age // 60} 分钟前发起的，授权码可能已过期")
    _exchange_and_save(
        app_ctx,
        region=pending["region"],
        redirect=pending["redirect_url"],
        device_id=pending["device_id"],
        code=result.code,
    )


def _drain(
    server: CallbackServer,
    result_queue: "queue.Queue[object]",
    used: CallbackResult,
) -> None:
    """本地回调和粘贴可能同时到达，丢掉多余的那份。"""
    while True:
        try:
            result_queue.get_nowait()
        except queue.Empty:
            return


def _open_browser(url: str) -> bool:
    try:
        return webbrowser.open(url)
    except Exception:
        return False


def _try_auto_capture(
    result_queue: "queue.Queue[object]", *, no_mdns: bool
) -> tuple[CallbackServer | None, MdnsPublisher | None]:
    """尽力让 homeassistant.local:8123 落到本机。失败就静默回退到粘贴。"""
    if not port_available(const.REDIRECT_PORT):
        render.warn(
            f"{const.REDIRECT_PORT} 端口被占用（本机在跑 Home Assistant？），"
            "只能用粘贴方式登录"
        )
        return None, None

    server: CallbackServer | None = None
    try:
        server = CallbackServer(result_queue=result_queue)
    except OSError as err:
        render.warn(f"监听 {const.REDIRECT_PORT} 失败：{err}")
        return None, None
    server.start()

    resolved = resolve_redirect_host()
    mine = set(local_ips())
    if resolved and set(resolved) & mine:
        render.info(
            f"[dim]{const.REDIRECT_HOST} 已解析到本机（{', '.join(resolved)}）[/dim]"
        )
        return server, None
    if resolved:
        render.warn(
            f"{const.REDIRECT_HOST} 解析到 {', '.join(resolved)}，不是本机；"
            "回调会落到那台机器上"
        )
        return server, None

    if no_mdns:
        return server, None
    address = next((ip for ip in local_ips() if ip != "127.0.0.1"), "127.0.0.1")
    publisher = MdnsPublisher(address)
    failure = publisher.start()
    if failure:
        render.info(f"[dim]mDNS 不可用（{failure}），可以直接用粘贴方式[/dim]")
        return server, None
    render.info(f"[dim]已通过 mDNS 把 {const.REDIRECT_HOST} 指向 {address}[/dim]")
    return server, publisher


@app.command()
def status(
    ctx: typer.Context,
    check: Annotated[
        bool, typer.Option("--check", help="调用一次云端接口验证 token 是否真的有效")
    ] = False,
    output: OutputOption = None,
) -> None:
    """查看当前登录状态。"""
    app_ctx = _ctx(ctx)
    profile = app_ctx.profile
    auth = profile.read_auth()
    if auth is None:
        raise NotAuthenticated(f"profile `{profile.name}` 尚未登录")

    now = time.time()
    data = {
        "profile": profile.name,
        "region": f"{auth.region}（{const.CLOUD_SERVERS.get(auth.region, '未知')}）",
        "nickname": auth.nickname or "-",
        "uid": auth.uid or "-",
        "device_id": auth.device_id,
        "access_token": mask(auth.access_token),
        "expires_in": _fmt_duration(auth.expires_at - now),
        "refresh_due": "是" if auth.needs_refresh else "否",
        "path": str(profile.auth_path),
    }
    if check:
        with app_ctx.session() as session:
            info = session.user_profile()
            data["check"] = f"有效（{info.get('miliaoNick', '')}）"
    render.output(data, pick_output(app_ctx, output))


@app.command()
def refresh(ctx: typer.Context) -> None:
    """立刻刷新 access_token。"""
    app_ctx = _ctx(ctx)
    with app_ctx.session() as session:
        auth = session.refresh_token(force=True)
    render.success(
        f"已刷新，有效期约 {_fmt_duration(auth.expires_at - time.time())}"
    )


@app.command()
def whoami(ctx: typer.Context, output: OutputOption = None) -> None:
    """打印当前账号信息（实时查询）。"""
    app_ctx = _ctx(ctx)
    with app_ctx.session() as session:
        info = session.user_profile()
        auth = session.auth
    render.output(
        {
            "nickname": info.get("miliaoNick") or "-",
            "uid": str(info.get("userId") or auth.uid or "-"),
            "region": auth.region,
            "profile": app_ctx.profile_name,
        },
        pick_output(app_ctx, output),
    )


@app.command()
def logout(
    ctx: typer.Context,
    purge: Annotated[
        bool, typer.Option("--purge", help="连同设备清单、spec 缓存一起删除")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="不询问")] = False,
) -> None:
    """退出登录，删除本地凭据。"""
    app_ctx = _ctx(ctx)
    profile = app_ctx.profile
    if not profile.exists():
        render.info("本来就没有登录信息")
        return
    if not yes and render.is_tty():
        target = "整个 profile 目录" if purge else "登录凭据"
        typer.confirm(f"确认删除 {profile.name} 的{target}？", abort=True)
    if purge:
        profile.purge()
    else:
        profile.clear_auth()
    render.success(f"已清除 profile `{profile.name}` 的凭据")


@profile_app.command("list")
def profile_list(ctx: typer.Context, output: OutputOption = None) -> None:
    """列出所有 profile。"""
    app_ctx = _ctx(ctx)
    default = read_config(app_ctx.root).get("profile", "default")
    rows = []
    for name in list_profiles(app_ctx.root) or [default]:
        auth = Profile(name, root=app_ctx.root).read_auth()
        rows.append(
            {
                "profile": name + (" *" if name == default else ""),
                "region": auth.region if auth else "-",
                "nickname": (auth.nickname if auth else None) or "-",
                "已登录": "是" if auth else "否",
            }
        )
    render.output(rows, pick_output(app_ctx, output))


@profile_app.command("use")
def profile_use(ctx: typer.Context, name: str) -> None:
    """设置默认 profile。"""
    app_ctx = _ctx(ctx)
    config = read_config(app_ctx.root)
    config["profile"] = name
    write_config(config, app_ctx.root)
    render.success(f"默认 profile 已设为 `{name}`")


@profile_app.command("remove")
def profile_remove(
    ctx: typer.Context,
    name: str,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="不询问")] = False,
) -> None:
    """删除一个 profile 的全部本地数据。"""
    app_ctx = _ctx(ctx)
    profile = Profile(name, root=app_ctx.root)
    if not profile.path.exists():
        raise UsageError(f"profile `{name}` 不存在")
    if not yes and render.is_tty():
        typer.confirm(f"确认删除 profile `{name}` 的全部数据？", abort=True)
    profile.purge()
    render.success(f"已删除 profile `{name}`")


@profile_app.command("path")
def profile_path(ctx: typer.Context) -> None:
    """打印配置目录位置。"""
    print(_ctx(ctx).root or config_dir())
