import time


def to_int(value):
    try:
        return int(value)
    except Exception:
        return None


def to_seconds_ts(value):
    """将接口时间戳转换为秒，兼容毫秒时间戳。"""
    ts = to_int(value)
    if ts is None:
        return None
    if ts > 10**12:
        ts = ts // 1000
    return ts


def estimate_realtime_ap(
    ap_info: dict, current_ap: int, max_ap: int, now_ts: int | None = None
):
    """根据接口快照与时间字段估算实时理智和满理智剩余时间。"""
    if current_ap >= max_ap:
        return current_ap, 0

    now = now_ts or int(time.time())

    full_ts = to_int(
        ap_info.get("recoverTime")
        or ap_info.get("completeRecoveryTime")
        or ap_info.get("fullRecoveryTime")
    )
    if full_ts:
        remain = max(0, full_ts - now)
        missing = (remain + 359) // 360
        estimated = max(0, max_ap - missing)
        return max(current_ap, min(max_ap, estimated)), remain

    last_add_ts = to_int(ap_info.get("lastApAddTime") or ap_info.get("lastRecoverTime"))
    if last_add_ts:
        gained = max(0, (now - last_add_ts) // 360)
        estimated = min(max_ap, current_ap + gained)
        remain = max(0, (max_ap - estimated) * 360)
        return estimated, remain

    return current_ap, None


def extract_status(data: dict):
    """从不同形态的接口返回中提取统一状态结构。"""
    payload = data.get("data", {}) if isinstance(data, dict) else {}
    status_info = payload.get("status") or payload.get("current") or payload
    if not isinstance(status_info, dict):
        return None

    ap_info = status_info.get("ap", {})
    ap = ap_info.get("current")
    max_ap = ap_info.get("max")
    if ap is None or max_ap is None:
        return None

    ap_val = to_int(ap)
    max_ap_val = to_int(max_ap)
    if ap_val is None or max_ap_val is None:
        return None
    current_ts = to_seconds_ts(payload.get("currentTs"))
    ap_estimated, ap_full_eta = estimate_realtime_ap(
        ap_info, ap_val, max_ap_val, current_ts
    )

    return {
        "ap": ap_val,
        "max_ap": max_ap_val,
        "ap_realtime": ap_estimated,
        "ap_full_eta": ap_full_eta,
    }


def evaluate_reminder_state(status: dict | None, reminded: bool):
    """根据理智状态返回提醒状态机标记。"""
    if not isinstance(status, dict):
        return {"is_full": False, "should_notify": False, "should_reset": True}

    ap = to_int(status.get("ap_realtime"))
    max_ap = to_int(status.get("max_ap"))
    if ap is None or max_ap is None or max_ap <= 0:
        return {"is_full": False, "should_notify": False, "should_reset": True}

    is_full = ap >= max_ap
    return {
        "is_full": is_full,
        "should_notify": is_full and not reminded,
        "should_reset": not is_full,
    }
