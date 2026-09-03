def _text_element(content: str) -> dict:
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def _hr_element() -> dict:
    return {"tag": "hr"}


def build_accepted_card(short_id: str, user_text: str) -> dict:
    preview = user_text[:100]
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "🔍 已受理"},
            "template": "blue",
        },
        "elements": [
            _text_element(f"**Case** `{short_id}`"),
            _text_element(f"**内容** {preview}"),
            _text_element("**状态** 处理中…"),
        ],
    }


_MAX_REPORT_CHARS = 3500


def build_progress_card(short_id: str, user_text: str, current_action: str) -> dict:
    preview = user_text[:100]
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "🔍 排查中"},
            "template": "blue",
        },
        "elements": [
            _text_element(f"**Case** `{short_id}`"),
            _text_element(f"**内容** {preview}"),
            _hr_element(),
            _text_element(f"**当前** {current_action}"),
        ],
    }


def build_done_card(
    *,
    short_id: str,
    report_text: str,
    tool_call_count: int,
    duration_seconds: float,
) -> dict:
    truncated = len(report_text) > _MAX_REPORT_CHARS
    body = report_text[:_MAX_REPORT_CHARS]
    elements = [
        _text_element(f"**Case** `{short_id}`"),
        _text_element(f"**耗时** {duration_seconds:.0f}s，调用了 {tool_call_count} 次工具"),
        _hr_element(),
        _text_element(body),
    ]
    if truncated:
        elements.append(_text_element(f"_（报告过长已截断，完整内容见 case `{short_id}`）_"))
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "✅ 处理完成"},
            "template": "green",
        },
        "elements": elements,
    }


def build_failed_card(short_id: str, user_text: str, reason: str) -> dict:
    preview = user_text[:100]
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "⚠️ 处理失败"},
            "template": "red",
        },
        "elements": [
            _text_element(f"**Case** `{short_id}`"),
            _text_element(f"**内容** {preview}"),
            _hr_element(),
            _text_element(f"**原因** {reason}"),
        ],
    }


def build_decline_card(short_id: str, user_text: str, reply_text: str) -> dict:
    preview = user_text[:100]
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "🙃 这个不归我管"},
            "template": "yellow",
        },
        "elements": [
            _text_element(f"**Case** `{short_id}`"),
            _text_element(f"**内容** {preview}"),
            _hr_element(),
            _text_element(reply_text),
        ],
    }


def build_warning_card(short_id: str, user_text: str, reply_text: str) -> dict:
    preview = user_text[:100]
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "🚫 拒绝执行"},
            "template": "red",
        },
        "elements": [
            _text_element(f"**Case** `{short_id}`"),
            _text_element(f"**内容** {preview}"),
            _hr_element(),
            _text_element(reply_text),
        ],
    }


def build_locked_out_card(short_id: str, remaining_minutes: int) -> dict:
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "🚫 冷却中"},
            "template": "red",
        },
        "elements": [
            _text_element(f"**Case** `{short_id}`"),
            _text_element(f"你之前触发了破坏性指令警告，还在冷却期内，还剩约 {remaining_minutes} 分钟不受理新请求。"),
        ],
    }
