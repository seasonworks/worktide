// Phase 6.5A · Settings 页面下拉选项常量
// 与后端 RUNTIME_KEYS 一一对应；如改集合，请同步 docs/operations/general_settings.md

// GS-1 自动回座时间（分钟）— review 锁定 7 档
export const BREAK_TIMEOUT_MINUTES = [5, 10, 15, 30, 60, 90, 120]

// GS-2 挂机阈值（分钟）— review 锁定 7 档
export const IDLE_THRESHOLD_MINUTES = [1, 3, 5, 10, 15, 30, 60]

// 时区偏移（小时）— 中国默认 +8，给 UTC-12 ~ UTC+14 全集合
export const TZ_OFFSET_HOURS = [
  -12, -11, -10, -9, -8, -7, -6, -5, -4, -3, -2, -1,
  0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14,
]

// GS-11 · 上下班提醒模式
export const CLOCK_REMINDER_MODES = [
  { value: 'group', label: '群发一次 / Group' },
  { value: 'individual', label: '逐个提醒 / Individual' },
]

// GS-11 · 上班提醒提前量（分钟）
export const PRE_CLOCK_IN_MINUTES = [0, 5, 10, 15, 20, 30]

// UI 元信息（后端 group/key/description 之外的展示文案）
// label 走 "中文 / English" 双语风格，与项目其它页面一致
export const KEY_META = {
  // 休息超时
  break_meal_max_seconds:    { label: '吃饭 / Lunch',            unit: '分钟' },
  break_toilet_max_seconds:  { label: '上厕所 / Restroom',       unit: '分钟' },
  break_smoke_max_seconds:   { label: '抽烟 / Smoke',            unit: '分钟' },
  auto_return_after_break_timeout: { label: '自动回座 / Auto Return To Work' },
  // 挂机检测
  idle_threshold_seconds:    { label: '挂机阈值 / Idle Threshold', unit: '分钟' },
  break_pauses_detection:    { label: '休息期间暂停挂机告警 / Pause Idle During Breaks' },
  // 通知开关（review #3 全部 UI 可见）
  notify_break_timeout_enabled:     { label: '自动回座通知 / Break Timeout' },
  notify_break_switch_enabled:      { label: '切换休息通知 / Break Switch' },
  notify_idle_enter_enabled:        { label: '进入挂机通知 / Idle Enter' },
  notify_idle_exit_enabled:         { label: '恢复工作通知 / Idle Exit' },
  notify_clock_in_reminder_enabled: { label: '上班提醒 / Clock-In Reminder' },
  notify_clock_out_reminder_enabled:{ label: '下班提醒 / Clock-Out Reminder' },
  // 上下班提醒
  clock_reminder_mode:              { label: '提醒模式 / Reminder Mode' },
  pre_clock_in_minutes:             { label: '上班提前提醒 / Pre-Clock-In', unit: '分钟' },
  clock_in_reminder_time:           { label: '上班提醒时刻 / Clock-In Time' },
  clock_out_reminder_time:          { label: '下班提醒时刻 / Clock-Out Time' },
  reminders_timezone_offset_hours:  { label: '时区偏移 / TZ Offset (hours)' },
  // 通知开关 · 管理员 @
  admin_telegram_user_ids:          { label: '管理员 TG ID / Admin Telegram IDs（逗号分隔）' },
  // 高级
  offline_threshold_seconds:        { label: '离线判定阈值 / Offline (seconds)', unit: '秒' },
}

// 分组中文/英文 label（与后端 groups 的 id 对齐）
export const GROUP_META = {
  break_timeouts:  { label_zh: '休息超时', label_en: 'Break Timeouts' },
  idle_detection: { label_zh: '挂机检测', label_en: 'Idle Detection' },
  notifications:  { label_zh: '通知开关', label_en: 'Notifications' },
  clock_reminders:{ label_zh: '上下班提醒', label_en: 'Clock Reminders' },
  advanced:       { label_zh: '高级', label_en: 'Advanced' },
}

// 分组渲染顺序（高级放最后）
export const GROUP_ORDER = [
  'break_timeouts',
  'idle_detection',
  'notifications',
  'clock_reminders',
  'advanced',
]
