"""输出适配：xlsx 之外的 data.json、可导入日历的 .ics，以及手机日历可选接口。"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .connectors.base import SourceNotConfigured
from .models import ResolvedRecord
from .settings import TZ

_APP = "ddl-agent"


def _payload(records: list[ResolvedRecord], generated_at: datetime | None = None) -> dict:
    return {
        "generated_at": (generated_at or datetime.now(TZ)).isoformat(timespec="seconds"),
        "counts": {
            "total": len(records),
            "confirmed": sum(1 for record in records if record.status == "confirmed"),
            "uncertain": sum(1 for record in records if record.status == "uncertain"),
            "missing_deadline": sum(1 for record in records if record.status == "missing_deadline"),
        },
        "records": [
            {
                "task_name": record.task_name,
                "course": record.course,
                "deadline": record.deadline_iso,
                "status": record.status,
                "source": record.source,
                "confidence": round(record.confidence, 4),
                "evidence": record.evidence_ids,
                "note": record.resolution_note,
                "updated_at": record.updated_at,
            }
            for record in records
        ],
    }


def write_data_json(records: list[ResolvedRecord], path: str | Path,
                    generated_at: datetime | None = None) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = _payload(records, generated_at)
    _atomic_write(target, json.dumps(payload, ensure_ascii=False, indent=2))
    return target


def write_ics(records: list[ResolvedRecord], path: str | Path,
              generated_at: datetime | None = None) -> Path:
    stamp = (generated_at or datetime.now(TZ)).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:-//{_APP}//DDL Calendar//EN",
        "CALSCALE:GREGORIAN",
    ]
    for record in records:
        if record.status != "confirmed" or not record.deadline_iso:
            continue
        deadline = datetime.fromisoformat(record.deadline_iso)
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=TZ)
        start = deadline.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        uid = hashlib.sha1(
            f"{record.course}|{record.task_name}|{record.deadline_iso}".encode("utf-8")
        ).hexdigest()[:20]
        summary = f"[{record.course or 'DDL'}] {record.task_name}"
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}@{_APP}",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{start}",
            f"SUMMARY:{_escape(summary)}",
            f"DESCRIPTION:{_escape(record.resolution_note or record.source)}",
            "STATUS:CONFIRMED",
            "BEGIN:VALARM",
            "TRIGGER:-P1D",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{_escape('明天截止：' + record.task_name)}",
            "END:VALARM",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(target, "\r\n".join(lines) + "\r\n")
    return target


def export_outputs(records: list[ResolvedRecord], out_dir: str | Path,
                   generated_at: datetime | None = None) -> list[Path]:
    out = Path(out_dir)
    return [
        write_data_json(records, out / "data.json", generated_at),
        write_ics(records, out / "deadlines.ics", generated_at),
        write_calendar_html(records, out / "calendar.html", generated_at),
    ]


def write_calendar_html(records: list[ResolvedRecord], path: str | Path,
                        generated_at: datetime | None = None) -> Path:
    """生成自包含的日历页面，双击即可离线打开。"""
    payload = _payload(records, generated_at)
    embedded = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(target, _CALENDAR_TEMPLATE.replace("__PAYLOAD__", embedded))
    return target


class PhoneCalendarWriter:
    """预留：把记录写进手机系统日历。

    默认关闭；需要真机验证后再启用。未配置 agent_factory 时显式报错，不静默假装成功。
    """

    def __init__(self, enabled: bool = False, agent_factory=None):
        self.enabled = enabled
        self.agent_factory = agent_factory

    def write(self, records: list[ResolvedRecord]) -> dict:
        if not self.enabled:
            return {"ok": False, "skipped": True,
                    "reason": "手机系统日历写入默认关闭（需真机验证后再启用）"}
        if self.agent_factory is None:
            raise SourceNotConfigured("手机系统日历写入未配置手机 agent；本功能未经真机验证")
        agent = self.agent_factory()
        task = (
            "打开系统日历，按下面条目逐条新建日程（标题、开始时间），已存在同名同时间的不重复创建：\n"
            + "\n".join(
                f"- {record.task_name} @ {record.deadline_iso}"
                for record in records
                if record.status == "confirmed" and record.deadline_iso
            )
        )
        agent.run(task)
        ok = getattr(agent, "last_status", None) not in ("failed", "timeout")
        return {"ok": ok, "skipped": False, "reason": "已调用手机日历（未经真机验证）"}


def _atomic_write(path: Path, content: str) -> None:
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _escape(value: str) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


_CALENDAR_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DDL 日历</title>
<style>
:root{--bg:#f5f6f8;--card:#fff;--text:#1f2937;--muted:#6b7280;--line:#e5e7eb;--accent:#2563eb;--warn:#b45309}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:"Microsoft YaHei","PingFang SC",system-ui,sans-serif}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;padding:24px 32px 8px}
h1{font-size:22px;margin:0}
.meta{margin:6px 0 0;color:var(--muted);font-size:13px}
.actions button{border:1px solid var(--line);background:var(--card);color:var(--text);border-radius:8px;padding:6px 12px;cursor:pointer}
.actions button:hover{border-color:var(--accent);color:var(--accent)}
.stats{display:flex;gap:12px;padding:0 32px 12px;flex-wrap:wrap}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 16px;font-size:13px;color:var(--muted)}
.stat b{display:block;font-size:20px;color:var(--text)}
main{margin:0 32px;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.weekdays,.grid{display:grid;grid-template-columns:repeat(7,1fr)}
.weekdays div{padding:10px;text-align:center;font-size:12px;color:var(--muted);border-bottom:1px solid var(--line)}
.grid .cell{min-height:104px;border-right:1px solid var(--line);border-bottom:1px solid var(--line);padding:6px}
.cell:nth-child(7n){border-right:0}
.cell.empty{background:#fafafa}
.day{font-size:12px;color:var(--muted);margin-bottom:4px}
.event{background:#eff6ff;border-left:3px solid var(--accent);border-radius:4px;padding:3px 6px;margin-bottom:4px;font-size:12px;line-height:1.35;word-break:break-all}
.event .time{color:var(--accent);font-weight:600;margin-right:4px}
.pending{margin:20px 32px 40px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 20px}
.pending h2{font-size:15px;margin:0 0 10px}
.pending ul{margin:0;padding-left:18px;font-size:13px;color:var(--muted)}
.pending li{margin:4px 0}
@media (max-width:720px){header{padding:16px}main{margin:0 12px}.stats{padding:0 12px 12px}.pending{margin:16px 12px 32px}.grid .cell{min-height:84px}}
</style>
</head>
<body>
<header>
  <div>
    <h1>DDL 日历</h1>
    <p class="meta" id="meta"></p>
  </div>
  <div class="actions">
    <button id="prev" type="button">上一月</button>
    <button id="today" type="button">本月</button>
    <button id="next" type="button">下一月</button>
  </div>
</header>
<div class="stats" id="stats"></div>
<main>
  <div class="weekdays"><div>日</div><div>一</div><div>二</div><div>三</div><div>四</div><div>五</div><div>六</div></div>
  <div class="grid" id="grid"></div>
</main>
<section class="pending">
  <h2>待确认 / 缺少时间</h2>
  <ul id="pending"></ul>
</section>
<script id="payload" type="application/json">__PAYLOAD__</script>
<script>
(function () {
  var data = JSON.parse(document.getElementById('payload').textContent);
  var records = data.records || [];
  var counts = data.counts || {};
  var confirmed = records.filter(function (r) { return r.status === 'confirmed' && r.deadline; });
  var pending = records.filter(function (r) { return r.status !== 'confirmed'; });

  var grid = document.getElementById('grid');
  var meta = document.getElementById('meta');
  var stats = document.getElementById('stats');
  var pendingList = document.getElementById('pending');

  function pad(n) { return String(n).padStart(2, '0'); }
  function dayKey(y, m, d) { return y + '-' + pad(m + 1) + '-' + pad(d); }

  var now = new Date();
  var base = (data.generated_at || '').slice(0, 7).split('-');
  var firstMonth = confirmed.length
    ? confirmed.map(function (r) { return r.deadline.slice(0, 7); }).sort()[0].split('-')
    : base;
  var viewYear = firstMonth.length === 2 ? parseInt(firstMonth[0], 10) : now.getFullYear();
  var viewMonth = firstMonth.length === 2 ? parseInt(firstMonth[1], 10) - 1 : now.getMonth();

  function render() {
    var byDate = {};
    confirmed.forEach(function (record) {
      var key = record.deadline.slice(0, 10);
      (byDate[key] = byDate[key] || []).push(record);
    });
    Object.keys(byDate).forEach(function (key) {
      byDate[key].sort(function (a, b) { return a.deadline.localeCompare(b.deadline); });
    });

    grid.textContent = '';
    var offset = new Date(viewYear, viewMonth, 1).getDay();
    var days = new Date(viewYear, viewMonth + 1, 0).getDate();
    var total = Math.ceil((offset + days) / 7) * 7;

    for (var index = 0; index < total; index += 1) {
      var cell = document.createElement('div');
      cell.className = 'cell';
      var dayNumber = index - offset + 1;
      if (dayNumber < 1 || dayNumber > days) {
        cell.className = 'cell empty';
        grid.appendChild(cell);
        continue;
      }
      var label = document.createElement('div');
      label.className = 'day';
      label.textContent = dayNumber;
      cell.appendChild(label);

      (byDate[dayKey(viewYear, viewMonth, dayNumber)] || []).forEach(function (record) {
        var event = document.createElement('div');
        event.className = 'event';
        event.title = [record.course, record.source, record.note].filter(Boolean).join(' · ');
        var time = document.createElement('span');
        time.className = 'time';
        time.textContent = record.deadline.slice(11, 16);
        event.appendChild(time);
        event.appendChild(document.createTextNode(
          (record.course ? record.course + ' ' : '') + record.task_name
        ));
        cell.appendChild(event);
      });
      grid.appendChild(cell);
    }

    meta.textContent = '生成时间 ' + (data.generated_at || '').replace('T', ' ') +
      ' · 共 ' + (counts.total || 0) + ' 条';

    stats.textContent = '';
    [
      ['共收录', counts.total || 0],
      ['已确认', counts.confirmed || 0],
      ['待确认', counts.uncertain || 0],
      ['缺时间', counts.missing_deadline || 0]
    ].forEach(function (item) {
      var box = document.createElement('div');
      box.className = 'stat';
      var value = document.createElement('b');
      value.textContent = item[1];
      box.appendChild(value);
      box.appendChild(document.createTextNode(item[0]));
      stats.appendChild(box);
    });

    pendingList.textContent = '';
    if (!pending.length) {
      var empty = document.createElement('li');
      empty.textContent = '暂无待确认项';
      pendingList.appendChild(empty);
    }
    pending.forEach(function (record) {
      var item = document.createElement('li');
      var status = record.status === 'uncertain' ? '待确认' : '缺截止时间';
      item.textContent = record.task_name + ' · ' + status +
        (record.note ? ' · ' + record.note : '');
      pendingList.appendChild(item);
    });
  }

  document.getElementById('prev').addEventListener('click', function () {
    viewMonth -= 1;
    if (viewMonth < 0) { viewMonth = 11; viewYear -= 1; }
    render();
  });
  document.getElementById('next').addEventListener('click', function () {
    viewMonth += 1;
    if (viewMonth > 11) { viewMonth = 0; viewYear += 1; }
    render();
  });
  document.getElementById('today').addEventListener('click', function () {
    viewYear = now.getFullYear();
    viewMonth = now.getMonth();
    render();
  });

  render();
  // 页面文件由每轮检索覆盖写入，定时重载即可看到最新结果
  setTimeout(function () { location.reload(); }, 120000);
})();
</script>
</body>
</html>
"""
