"""给 WorkBuddy 的现成提示词。

WorkBuddy 的两条能力都靠「定时任务 + 文件」：既可以把分析请求写进队列
等它回答，也可以让它把 outbox 里的消息转进微信。用户不该自己去猜怎么写
prompt，所以这里生成两段可以直接粘贴的完整指令，参数从当前配置里读：

* 数据目录来自 ``settings.home``，所以打包版和源码版给出的路径都是对的；
* outbox 目录来自通知渠道的配置，用户改过就跟着改；
* 建议页和设置页都调用同一个函数，两处不会各说一套。
"""

from __future__ import annotations

import json
from pathlib import Path


def _home() -> Path:
    from kestrel.settings import settings

    return settings.home


def _bridge_dirs() -> dict[str, Path]:
    """桥目录的实际位置。用户在设置里改过 ``bridge.root`` 就跟着改。

    提示词里写死 `data/bridge` 是最容易出错的一件事：源码运行时它在仓库里，
    打包版在 `~/Library/Application Support/Kestrel`，容器里在 `/data`，
    三者互相看不到。所以每次都从配置现算。
    """
    from kestrel.services import bridge

    return bridge.paths()


def analyse_prompt() -> str:
    """消费分析队列的定时任务提示词。"""
    folders = _bridge_dirs()
    pending = folders["pending"]
    done = folders["done"]
    return f"""你是一个定时任务。每次触发时按下面步骤执行，不要问我问题，直接做。

1. 读取目录 `{pending}` 下所有 `.json` 文件（按文件名排序，最多处理 3 个）。
2. 每个文件的 `instructions` 字段是一份完整的提示词，`input.brief` 是候选股、
   账户和规则的实测数据。**只根据这份 brief 回答**，不要上网查、不要编造价格。
3. 严格按 `instructions` 里的 JSON 结构作答，不要加 Markdown 代码块、不要加解释文字。
   尤其注意：
   - `code` 必须与 brief 里候选股的代码逐字一致；
   - 止损必须低于入场价，且落在 brief 里 `constraints` 给出的区间内；
   - 目标价至少是止损距离的 1.5 倍；
   - 不要给股数，仓位由程序计算；
   - 没有合适的标的就返回空的 `picks` 数组，并在 `pass_reason` 里说明原因。
4. 把 JSON 写入 `{done}/<原文件名>.json`（文件名必须与请求文件同名）。
5. 写完后把原请求文件移动到 `{folders["archive"]}`，避免重复处理。
6. 如果你判断某个请求无法完成，把原因写进 `{folders["failed"]}/<原文件名>.json`。

注意：`done` 里的文件会被程序收走并入库，写到别的位置等于没回答。"""


def notify_prompt() -> str:
    """把 outbox 转进微信的定时任务提示词。"""
    home = _home()
    outbox = "~/.workbuddy/kestrel-outbox"
    try:
        from kestrel.services import channels as channel_service

        conf = channel_service.config_for("workbuddy")
        if conf.get("outbox"):
            outbox = str(conf["outbox"])
    except Exception:  # noqa: BLE001
        pass

    return f"""你是一个定时任务，负责把 Kestrel 的通知转发到微信。

1. 读取目录 `{outbox}` 下所有 `.json` 文件（按文件名排序）。
2. 每个文件的格式是：
   {{"title": "标题", "body": "正文", "url": "链接", "created_at": "时间"}}
3. 把 `title`、`body`、`url` 拼成一条消息发到我的微信（用你自己的发消息能力）。
   如果 `url` 不为空，把它单独放一行，保证可以点击。
4. 发送成功后，把该文件移动到 `{outbox}/sent/`（没有这个目录就创建）。
5. 发送失败就把文件留在原地，下次再试；不要重复发送已成功的消息。

Kestrel 的数据目录是 `{home}`。这些消息来自本机，不需要联网核实内容。"""


def prompts() -> dict[str, str]:
    return {"analyse": analyse_prompt(), "notify": notify_prompt()}


def prompts_json() -> str:
    return json.dumps(prompts(), ensure_ascii=False, indent=2)
