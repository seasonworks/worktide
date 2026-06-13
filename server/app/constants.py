from enum import Enum


class EmployeeStatus(str, Enum):
    """员工实时状态。

    - online：在线且有活动
    - idle：挂机（空闲时间超过阈值）
    - offline：离线（长时间未收到上报）
    """

    online = "online"
    idle = "idle"
    offline = "offline"


class WorkState(str, Enum):
    """考勤/工时状态（由 Telegram 控制，与活动状态 EmployeeStatus 正交）。"""

    off_shift = "off_shift"      # 下班
    working = "working"          # 在岗
    break_meal = "break_meal"    # 吃饭
    break_toilet = "break_toilet"  # 厕所
    break_smoke = "break_smoke"  # 抽烟


class BreakType(str, Enum):
    meal = "meal"
    toilet = "toilet"
    smoke = "smoke"


class BreakEndReason(str, Enum):
    """break 结束原因，用于区分主动回座与系统强制超时。"""

    manual_return = "manual_return"  # 主动回座
    timeout = "timeout"              # 系统强制超时（auto_ended=true）
    shift_end = "shift_end"          # 下班时一并结束
    switched = "switched"            # 切换到另一类型 break
    admin_force = "admin_force"      # 后台强制恢复在岗（auto_ended=true）


# break_type → 对应的休息态 work_state
BREAK_TYPE_TO_WORK_STATE = {
    BreakType.meal: WorkState.break_meal,
    BreakType.toilet: WorkState.break_toilet,
    BreakType.smoke: WorkState.break_smoke,
}

# break_type → settings 中对应的最大时长配置键
BREAK_TYPE_TO_MAX_KEY = {
    BreakType.meal: "break_meal_max_seconds",
    BreakType.toilet: "break_toilet_max_seconds",
    BreakType.smoke: "break_smoke_max_seconds",
}
