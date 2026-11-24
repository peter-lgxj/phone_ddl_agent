"""输出适配：xlsx 之外的 data.json、可导入日历的 .ics，以及手机日历可选接口。"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .models import ResolvedRecord
from .settings import TZ

_APP = "ddl-agent"
_ICAL_TZID = "Asia/Shanghai"


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
                "sources": record.sources,
                "confidence": round(record.confidence, 4),
                "evidence": record.evidence_ids,
                "note": record.resolution_note,
                "inferred": record.inferred,
                "inference_basis": record.inference_basis,
                "superseded_by": record.superseded_by,
                "conflicts": record.conflicts,
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
        "BEGIN:VTIMEZONE",
        f"TZID:{_ICAL_TZID}",
        "BEGIN:STANDARD",
        "DTSTART:19700101T000000",
        "TZOFFSETFROM:+0800",
        "TZOFFSETTO:+0800",
        "TZNAME:CST",
        "END:STANDARD",
        "END:VTIMEZONE",
    ]
    for record in records:
        if record.status != "confirmed" or not record.deadline_iso:
            continue
        deadline = datetime.fromisoformat(record.deadline_iso)
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=TZ)
        start = deadline.astimezone(TZ).strftime("%Y%m%dT%H%M%S")
        uid = hashlib.sha1(
            f"{record.course}|{record.task_name}|{record.deadline_iso}".encode("utf-8")
        ).hexdigest()[:20]
        summary = f"[{record.course or 'DDL'}] {record.task_name}"
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}@{_APP}",
            f"DTSTAMP:{stamp}",
            f"DTSTART;TZID={_ICAL_TZID}:{start}",
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
    """手机系统日历写入已按只读约束永久禁用。

    保留类名与 ``enabled`` 参数仅为兼容旧配置；即使配置打开也不会调用手机，
    避免任何路径在用户不知情时创建/修改系统日历。
    """

    def __init__(self, enabled: bool = False, agent_factory=None):
        self.enabled = enabled
        self.agent_factory = agent_factory

    def write(self, records: list[ResolvedRecord]) -> dict:
        return {
            "ok": False,
            "skipped": True,
            "reason": "只读约束：手机系统日历写入已禁用，不会创建或修改系统日历",
        }


def _atomic_write(path: Path, content: str) -> None:
    tmp = path.with_name(f"{path.name}.tmp")
    # 必须按字节写入：文本模式的 newline 转换会把 ICS 的 CRLF 再转成 \r\r\n
    tmp.write_bytes(content.encode("utf-8"))
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
:root{
  --bg:#f3f6fb;--card:#fff;--text:#16233a;--muted:#64748b;--line:#e5eaf2;
  --accent:#2563eb;--accent-soft:#eff5ff;--warn:#b45309;
  --radius:14px;
}
*{box-sizing:border-box}
html,body{margin:0}
body{background:var(--bg);color:var(--text);font:14px/1.6 "PingFang SC","Microsoft YaHei",system-ui,-apple-system,sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:1120px;margin:0 auto;padding:20px 24px 40px}
header{display:flex;flex-wrap:wrap;gap:16px;align-items:flex-end;justify-content:space-between;margin-bottom:14px}
h1{font-size:21px;margin:0;letter-spacing:.2px}
.meta{margin:4px 0 0;color:var(--muted);font-size:12.5px}
.monthnav{display:flex;align-items:center;gap:6px}
.monthnav button{border:1px solid var(--line);background:var(--card);color:var(--text);border-radius:9px;width:34px;height:34px;font-size:16px;cursor:pointer;line-height:1;transition:border-color .15s,color .15s}
.monthnav button:hover{border-color:var(--accent);color:var(--accent)}
.monthnav button.ghost{width:auto;padding:0 12px;font-size:13px}
.monthnav strong{min-width:118px;text-align:center;font-size:15px;font-weight:650;letter-spacing:.3px}
.stats{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:11px;padding:8px 14px;font-size:12.5px;color:var(--muted);display:flex;align-items:baseline;gap:8px}
.stat b{font-size:17px;color:var(--text)}
.calendar{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);overflow:hidden}
.weekdays,.grid{display:grid;grid-template-columns:repeat(7,minmax(0,1fr))}
.weekdays div{padding:11px 10px;text-align:center;font-size:12px;color:var(--muted);border-bottom:1px solid var(--line);background:#fbfcfe}
.weekdays div:first-child,.weekdays div:last-child{color:#94a3b8}
.cell{min-height:108px;border-right:1px solid var(--line);border-bottom:1px solid var(--line);padding:7px 8px 8px;display:flex;flex-direction:column;gap:4px}
.cell:nth-child(7n){border-right:0}
.cell.blank{background:#fbfcfe}
.cell.weekend{background:#fdfefe}
.cell.today{background:#f5f9ff}
.day{font-size:12px;color:var(--muted);display:flex;align-items:center;min-height:20px}
.day b{font-weight:600}
.cell.today .day b{background:var(--accent);color:#fff;border-radius:50%;width:21px;height:21px;display:grid;place-items:center;font-size:11.5px}
.event{display:flex;gap:6px;align-items:flex-start;background:var(--accent-soft);border:0;border-left:3px solid var(--accent);border-radius:6px;padding:4px 7px;font:inherit;font-size:12px;line-height:1.35;cursor:pointer;text-align:left;width:100%;color:var(--text);transition:background .15s}
.event:hover{background:#e2edff}
.event.past{background:#f8fafc;border-left-color:#cbd5e1;color:#94a3b8}
.event .time{color:var(--accent);font-weight:650;flex:none;font-variant-numeric:tabular-nums}
.event.past .time{color:#94a3b8}
.event .text{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.more{border:0;background:transparent;color:var(--accent);font-size:11.5px;padding:0 2px;cursor:pointer;text-align:left;font-family:inherit}
.more:hover{text-decoration:underline}
.empty{padding:56px 24px;text-align:center;color:var(--muted);background:var(--card);border:1px dashed var(--line);border-radius:var(--radius)}
.empty b{display:block;color:var(--text);font-size:15px;margin-bottom:6px}
.pending{margin-top:16px;background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:16px 18px}
.pending h2{font-size:14px;margin:0 0 10px;font-weight:650}
.pending ul{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:8px}
.pending li{display:flex;align-items:center;gap:9px;flex-wrap:wrap;font-size:13px;border:1px solid var(--line);border-radius:9px;padding:8px 11px;cursor:pointer}
.pending li:hover{border-color:#cfe0f7;background:#fbfdff}
.chip{font-size:11.5px;font-weight:600;padding:2px 8px;border-radius:999px;flex:none}
.chip.uncertain{background:#fff7ed;color:var(--warn)}
.chip.missing{background:#f1f5f9;color:#475569}
.src{color:var(--muted);font-size:12px}
.note{color:var(--muted);font-size:12.5px}
.toast{position:fixed;left:50%;bottom:22px;transform:translate(-50%,14px);background:#16233a;color:#fff;padding:9px 16px;border-radius:10px;font-size:12.5px;opacity:0;pointer-events:none;transition:opacity .2s,transform .2s}
.toast.show{opacity:1;transform:translate(-50%,0)}
dialog{border:0;border-radius:14px;padding:0;max-width:min(440px,calc(100vw - 32px));width:100%;box-shadow:0 24px 60px -20px rgba(16,35,58,.45)}
dialog::backdrop{background:rgba(16,35,58,.35)}
.dlg-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;padding:18px 20px 10px}
.dlg-head h2{font-size:16px;margin:0;line-height:1.4}
.dlg-head button{border:0;background:#f1f5f9;color:var(--muted);width:30px;height:30px;border-radius:9px;font-size:17px;cursor:pointer;flex:none}
.dlg-head button:hover{background:#e2e8f0;color:var(--text)}
.dlg-body{padding:0 20px 20px;display:grid;grid-template-columns:56px 1fr;gap:8px 12px;font-size:13px;margin:0}
.dlg-body dt{color:var(--muted)}
.dlg-body dd{margin:0;word-break:break-word}
.dlg-list{padding:0 20px 20px;margin:0;list-style:none;display:flex;flex-direction:column;gap:8px}
.dlg-list li{border:1px solid var(--line);border-radius:9px;padding:8px 10px;font-size:13px;cursor:pointer}
.dlg-list li:hover{background:#fbfdff;border-color:#cfe0f7}
@media (max-width:760px){
  .wrap{padding:14px 12px 28px}
  h1{font-size:18px}
  header{gap:10px;align-items:center}
  .cell{min-height:86px;padding:5px 5px 6px}
  .event{font-size:11px;padding:3px 5px;gap:4px}
  .weekdays div{padding:8px 4px;font-size:11px}
  .monthnav strong{min-width:96px;font-size:14px}
}
@media (prefers-reduced-motion:reduce){*{transition:none !important;animation:none !important}}
</style>
</head>
<body>
<div class="wrap">
<header>
  <div>
    <h1>DDL 日历</h1>
    <p class="meta" id="meta"></p>
  </div>
  <div class="monthnav">
    <button id="prev" type="button" aria-label="上个月">‹</button>
    <strong id="month-label"></strong>
    <button id="next" type="button" aria-label="下个月">›</button>
    <button id="today" class="ghost" type="button">今天</button>
  </div>
</header>
<div class="stats" id="stats"></div>
<div class="calendar" id="calendar">
  <div class="weekdays"><div>日</div><div>一</div><div>二</div><div>三</div><div>四</div><div>五</div><div>六</div></div>
  <div class="grid" id="grid"></div>
</div>
<div class="empty" id="empty" hidden><b>还没有已确认的截止时间</b>待确认或缺少时间的条目会列在下方。</div>
<section class="pending">
  <h2>待确认 / 缺少时间</h2>
  <ul id="pending"></ul>
</section>
</div>

<dialog id="detail">
  <div class="dlg-head">
    <h2 id="detail-title"></h2>
    <button type="button" id="detail-close" aria-label="关闭">×</button>
  </div>
  <dl class="dlg-body" id="detail-body"></dl>
  <ul class="dlg-list" id="detail-list" hidden></ul>
</dialog>
<div class="toast" id="toast" role="status" aria-live="polite"></div>

<script id="payload" type="application/json">__PAYLOAD__</script>
<script>
(function () {
  'use strict';
  var data = JSON.parse(document.getElementById('payload').textContent);
  var records = data.records || [];
  var counts = data.counts || {};
  var grid = document.getElementById('grid');
  var meta = document.getElementById('meta');
  var stats = document.getElementById('stats');
  var pendingList = document.getElementById('pending');
  var monthLabel = document.getElementById('month-label');
  var emptyBox = document.getElementById('empty');
  var calendarBox = document.getElementById('calendar');
  var dialog = document.getElementById('detail');

  function pad(n) { return String(n).padStart(2, '0'); }
  function dayKey(y, m, d) { return y + '-' + pad(m + 1) + '-' + pad(d); }

  var now = new Date();
  var view = startView();

  function confirmedRecords() {
    return records.filter(function (r) { return r.status === 'confirmed' && r.deadline; });
  }

  function startView() {
    var months = confirmedRecords().map(function (r) { return r.deadline.slice(0, 7); }).sort();
    var current = now.getFullYear() + '-' + pad(now.getMonth() + 1);
    var month = (months.length && months.indexOf(current) === -1) ? months[0] : current;
    var parts = month.split('-');
    return {year: parseInt(parts[0], 10), month: parseInt(parts[1], 10) - 1};
  }

  function shift(delta) {
    var m = view.month + delta;
    var y = view.year;
    if (m < 0) { m = 11; y -= 1; }
    if (m > 11) { m = 0; y += 1; }
    view = {year: y, month: m};
    render();
  }

  function eventButton(record) {
    var button = document.createElement('button');
    button.type = 'button';
    button.className = 'event';
    if (new Date(record.deadline).getTime() < now.getTime()) button.className += ' past';
    button.title = [record.course, record.task_name, record.source].filter(Boolean).join(' · ');
    var time = document.createElement('span');
    time.className = 'time';
    time.textContent = record.deadline.slice(11, 16);
    var text = document.createElement('span');
    text.className = 'text';
    text.textContent = (record.course ? record.course + ' ' : '') + record.task_name;
    button.appendChild(time);
    button.appendChild(text);
    button.addEventListener('click', function () { showRecord(record); });
    return button;
  }

  function showRecord(record) {
    if (!dialog || !dialog.showModal) return;
    document.getElementById('detail-title').textContent = record.task_name;
    var body = document.getElementById('detail-body');
    body.textContent = '';
    [
      ['课程', record.course],
      ['截止', record.deadline ? record.deadline.replace('T', ' ').slice(0, 16) : ''],
      ['来源', record.source],
      ['状态', record.status === 'confirmed' ? '已确认' : (record.status === 'uncertain' ? '待确认' : '缺少时间')],
      ['备注', record.note]
    ].forEach(function (pair) {
      if (!pair[1]) return;
      var dt = document.createElement('dt');
      dt.textContent = pair[0];
      var dd = document.createElement('dd');
      dd.textContent = pair[1];
      body.appendChild(dt);
      body.appendChild(dd);
    });
    document.getElementById('detail-list').hidden = true;
    dialog.showModal();
  }

  function showDay(key, list) {
    if (!dialog || !dialog.showModal) return;
    document.getElementById('detail-title').textContent = key + ' · ' + list.length + ' 个截止';
    document.getElementById('detail-body').textContent = '';
    var ul = document.getElementById('detail-list');
    ul.textContent = '';
    ul.hidden = false;
    list.forEach(function (record) {
      var li = document.createElement('li');
      li.textContent = record.deadline.slice(11, 16) + '  ' +
        (record.course ? record.course + ' ' : '') + record.task_name;
      li.addEventListener('click', function () { showRecord(record); });
      ul.appendChild(li);
    });
    dialog.showModal();
  }

  function render() {
    var confirmed = confirmedRecords();
    var byDate = {};
    confirmed.forEach(function (record) {
      var key = record.deadline.slice(0, 10);
      (byDate[key] = byDate[key] || []).push(record);
    });
    Object.keys(byDate).forEach(function (key) {
      byDate[key].sort(function (a, b) { return a.deadline.localeCompare(b.deadline); });
    });

    monthLabel.textContent = view.year + ' 年 ' + (view.month + 1) + ' 月';
    grid.textContent = '';

    var offset = new Date(view.year, view.month, 1).getDay();
    var days = new Date(view.year, view.month + 1, 0).getDate();
    var total = Math.ceil((offset + days) / 7) * 7;

    for (var index = 0; index < total; index += 1) {
      var cell = document.createElement('div');
      var dayNumber = index - offset + 1;
      if (dayNumber < 1 || dayNumber > days) {
        cell.className = 'cell blank';
        grid.appendChild(cell);
        continue;
      }
      var key = dayKey(view.year, view.month, dayNumber);
      cell.className = 'cell' + ((index % 7 === 0 || index % 7 === 6) ? ' weekend' : '');
      if (dayNumber === now.getDate() && view.month === now.getMonth() && view.year === now.getFullYear()) {
        cell.className += ' today';
      }
      var label = document.createElement('div');
      label.className = 'day';
      var number = document.createElement('b');
      number.textContent = dayNumber;
      label.appendChild(number);
      cell.appendChild(label);

      var list = byDate[key] || [];
      list.slice(0, 3).forEach(function (record) { cell.appendChild(eventButton(record)); });
      if (list.length > 3) {
        var more = document.createElement('button');
        more.type = 'button';
        more.className = 'more';
        more.textContent = '还有 ' + (list.length - 3) + ' 条';
        more.addEventListener('click', function () { showDay(key, list); });
        cell.appendChild(more);
      }
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

    var pending = records.filter(function (r) { return r.status !== 'confirmed'; });
    pendingList.textContent = '';
    if (!pending.length) {
      var empty = document.createElement('li');
      empty.className = 'note';
      empty.textContent = '暂无待确认项';
      pendingList.appendChild(empty);
    }
    pending.forEach(function (record) {
      var li = document.createElement('li');
      var chip = document.createElement('span');
      chip.className = 'chip ' + (record.status === 'uncertain' ? 'uncertain' : 'missing');
      chip.textContent = record.status === 'uncertain' ? '待确认' : '缺时间';
      var name = document.createElement('span');
      name.textContent = (record.course ? record.course + ' ' : '') + record.task_name;
      li.appendChild(chip);
      li.appendChild(name);
      if (record.source) {
        var src = document.createElement('span');
        src.className = 'src';
        src.textContent = record.source;
        li.appendChild(src);
      }
      if (record.note) {
        var note = document.createElement('span');
        note.className = 'note';
        note.textContent = record.note;
        li.appendChild(note);
      }
      li.addEventListener('click', function () { showRecord(record); });
      pendingList.appendChild(li);
    });

    var hasConfirmed = confirmed.length > 0;
    emptyBox.hidden = hasConfirmed;
    calendarBox.hidden = !hasConfirmed;
  }

  document.getElementById('prev').addEventListener('click', function () { shift(-1); });
  document.getElementById('next').addEventListener('click', function () { shift(1); });
  document.getElementById('today').addEventListener('click', function () {
    view = {year: now.getFullYear(), month: now.getMonth()};
    render();
  });
  document.getElementById('detail-close').addEventListener('click', function () { dialog.close(); });
  dialog.addEventListener('click', function (event) {
    if (event.target === dialog) dialog.close();
  });

  render();

  // 每轮扫描会覆盖写入本文件：定时比对 generated_at，有变化就地重绘，
  // 避免旧版整页 reload 把用户正在看的月份重置掉。
  function refreshFromDisk() {
    fetch(location.href, {cache: 'no-store'}).then(function (response) {
      return response.text();
    }).then(function (html) {
      var doc = new DOMParser().parseFromString(html, 'text/html');
      var node = doc.getElementById('payload');
      if (!node) return;
      var next = JSON.parse(node.textContent);
      if (!next || next.generated_at === data.generated_at) return;
      data = next;
      records = data.records || [];
      counts = data.counts || {};
      render();
      var toast = document.getElementById('toast');
      toast.textContent = '日历已更新';
      toast.className = 'toast show';
      setTimeout(function () { toast.className = 'toast'; }, 2600);
    }).catch(function () { /* 离线 file:// 打开时无法读回，忽略 */ });
  }
  setInterval(refreshFromDisk, 60000);
})();
</script>
</body>
</html>
"""
