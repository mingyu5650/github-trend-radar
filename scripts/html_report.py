"""Render a self-contained, readable HTML version of the radar report."""

import html
from typing import Any, Mapping


SOURCE_LABELS = {
    "github_top": "GitHub 总 Star 榜",
    "ossinsight_24h": "24 小时新增榜",
    "ossinsight_7d": "7 日新增榜",
    "github_trending_weekly": "GitHub 周趋势",
    "github_details": "GitHub 仓库详情",
    "github_star_fallback": "累计 Star 兜底",
    "github_readme": "GitHub README",
    "watchlist_details": "观察清单详情",
    "local history": "本地历史快照",
    "local history 24h": "本地历史快照（过去24小时）",
    "local history 7d": "本地历史快照（过去7天）",
    "OSSInsight 24h external": "OSSInsight 外部24小时榜",
    "OSSInsight 7d external": "OSSInsight 外部7日榜",
}
STATUS_LABELS = {
    "ok": "正常",
    "partial": "部分可用",
    "degraded": "不可用",
}
EVIDENCE_TYPE_LABELS = {
    "ranking_data": "榜单数据",
    "calculation": "计算结果",
    "comparison": "对比结果",
    "source_record": "来源记录",
}
PERIOD_LABELS = {
    "24h": "过去24小时",
    "7d": "过去7天",
    "weekly": "每周",
}
METRIC_LABELS = {
    "total_stars": "当前累计 Star",
    "daily_growth": "24小时新增 Star",
    "weekly_growth": "7日新增 Star",
    "acceleration": "24h 增速相对7日日均",
}


def _text(value: Any, default: str = "—") -> str:
    if value is None or value == "":
        return default
    if isinstance(value, list):
        return "；".join(_text(item, "") for item in value if item not in (None, "")) or default
    if isinstance(value, Mapping):
        return "；".join(f"{_text(k, '')}：{_text(v, '')}" for k, v in value.items()) or default
    if isinstance(value, float):
        return f"{value:,.2f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _evidence_text(value: Any) -> str:
    """Render evidence entries with Chinese labels."""

    if value is None:
        return "—"
    if isinstance(value, list):
        return "；".join(
            _evidence_text(item)
            for item in value
            if item not in (None, "")
        ) or "—"
    if not isinstance(value, Mapping):
        return _text(value)
    labels = {
        "type": "类型",
        "source": "来源",
        "period": "周期",
        "repo": "仓库",
        "value": "数值",
        "unit": "单位",
        "metric": "指标",
        "detail": "说明",
        "status": "状态",
    }

    def localized_value(key: str, item: Any) -> str:
        mappings = {
            "type": EVIDENCE_TYPE_LABELS,
            "source": SOURCE_LABELS,
            "period": PERIOD_LABELS,
            "metric": METRIC_LABELS,
            "status": STATUS_LABELS,
        }
        mapping = mappings.get(key, {})
        return _text(mapping.get(item, item), "")

    return "；".join(
        f"{labels.get(key, _text(key, ''))}：{localized_value(key, item)}"
        for key, item in value.items()
        if item not in (None, "")
    ) or "—"


def _e(value: Any, default: str = "—") -> str:
    return html.escape(_text(value, default), quote=True)


def _evidence(value: Any) -> str:
    return html.escape(_evidence_text(value), quote=True)


def source_status_text(value: Any) -> str:
    """Render source status entries with Chinese labels."""

    if not isinstance(value, list):
        return _text(value)
    rows = []
    for entry in value:
        if not isinstance(entry, Mapping):
            rows.append(_text(entry))
            continue
        labels = {"source": "来源", "status": "状态"}
        pieces = [
            f"{labels.get(key, _text(key, ''))}：{_text((SOURCE_LABELS if key == 'source' else STATUS_LABELS).get(item, item), '')}"
            for key, item in entry.items()
            if item not in (None, "")
        ]
        rows.append("；".join(pieces))
    return "；".join(rows) or "—"


def _repo(row: Mapping[str, Any]) -> str:
    name = _text(row.get("repo") or row.get("full_name") or row.get("repository"), "")
    if not name:
        return "—"
    url = row.get("repo_url") or f"https://github.com/{name}"
    return f'<a href="{html.escape(str(url), quote=True)}" target="_blank" rel="noopener">{html.escape(name)}</a>'


def _table(headers, rows, css_class="") -> str:
    rows = list(rows)
    head = "".join(f"<th>{html.escape(label)}</th>" for label in headers)
    body = "".join("<tr>{}</tr>".format("".join(f"<td>{cell}</td>" for cell in row)) for row in rows)
    if not body:
        body = f'<tr><td colspan="{len(headers)}" class="empty">暂无可用数据</td></tr>'
    return f'<div class="table-wrap"><table class="{css_class}"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _ranking(rows, metric_label):
    output = []
    is_growth = metric_label in {"24h 新增 Star", "7d 新增 Star"}
    for index, row in enumerate(rows, 1):
        value = row.get("value")
        metric = _e(value)
        if metric_label != "当前累计 Star" and isinstance(value, (int, float)):
            metric = "+" + metric
        cells = [
            str(index), _repo(row), _e(row.get("primary_category")),
            _e(row.get("repository_type")), _e(row.get("purpose")),
            _e(row.get("problem_solved")),
        ]
        if is_growth:
            cells.append(_e(row.get("total_stars"), "缺失"))
        cells.extend((metric, _e(row.get("data_scope"))))
        output.append(tuple(cells))
    headers = ["#", "仓库", "分类", "类型", "用途与定位", "主要解决问题"]
    if is_growth:
        headers.append("当前累计 Star")
    headers.extend((metric_label, "数据口径"))
    return _table(headers, output, "ranking")


def _list(value):
    items = value if isinstance(value, list) else ([] if value in (None, "") else [value])
    return "<ul>{}</ul>".format("".join(f"<li>{_e(item)}</li>" for item in items)) if items else '<p class="empty">暂无</p>'


def build_html_report(model: Mapping[str, Any]) -> str:
    metadata = model.get("metadata", {})
    rankings = model.get("rankings", {})
    overview = model.get("overview", {})
    statuses = metadata.get("source_status", [])
    date = _text(metadata.get("date"), "未知日期")
    status_cards = "".join(
        '<div class="status-card"><span>{}</span><strong class="status {}">{}</strong></div>'.format(
            _e(SOURCE_LABELS.get(x.get("source"), x.get("source"))),
            _e(x.get("status"), "unknown"),
            _e(STATUS_LABELS.get(x.get("status"), "未知")),
        )
        for x in statuses
    )
    category_rows = [(_e(x.get("category")), _e(x.get("facts")), _e(x.get("change")), _e(x.get("confidence"))) for x in model.get("category_trends", [])]
    reusable = "".join(
        '<article class="project"><header><h3>{}</h3><b>{} 分</b></header><p class="purpose">{}</p><dl><dt>主要解决问题</dt><dd>{}</dd><dt>可复用内容</dt><dd>{}</dd><dt>累计 Star</dt><dd>{}</dd><dt>评分信号覆盖</dt><dd>{}</dd><dt>许可证 / 维护</dt><dd>{} / {}</dd><dt>描述来源</dt><dd>{}</dd><dt>风险</dt><dd>{}</dd><dt>建议动作</dt><dd>{}</dd></dl></article>'.format(
            _repo(x), _e(x.get("score"), "0"), _e(x.get("purpose")), _e(x.get("problem_solved")), _e(x.get("reuse")), _e(x.get("total_stars")), _e(x.get("score_confidence")), _e(x.get("license")), _e(x.get("maintenance")), _e(x.get("description_source")), _e(x.get("risks")), _e(x.get("actions")))
        for x in model.get("reusable_projects", [])
    ) or '<p class="empty">暂无复用候选</p>'
    featured = "".join('<article class="brief"><h3>{}</h3><p><b>事实：</b>{}</p><p><b>研判：</b>{}</p><p><b>建议：</b>{}</p></article>'.format(_repo(x), _e(x.get("facts")), _e(x.get("inferences")), _e(x.get("actions"))) for x in model.get("featured_projects", [])) or '<p class="empty">暂无重点项目解读</p>'
    history_rows = [(_repo(x), _e(x.get("facts")), _e(x.get("inferences")), _e(x.get("confidence")), _e(x.get("replacement")), _e(x.get("actions"))) for x in model.get("history", [])]
    market_rows = [(_e(x.get("conclusion")), _evidence(x.get("evidence")), _e(x.get("change")), _e(x.get("confidence")), _e(x.get("consecutive_periods") or x.get("periods"))) for x in model.get("market_trends", [])]
    watch_rows = [(_repo(x), _e(x.get("reason") or x.get("观察理由")), _e(x.get("actions") or x.get("下一步动作"))) for x in model.get("watchlist", [])]
    quality = model.get("data_quality", {})
    if isinstance(quality, Mapping):
        quality_labels = {
            "source_status": "来源状态",
            "query_time": "查询时间",
            "periods": "查询周期",
            "warnings": "降级与警告",
            "missing": "缺失",
            "conflicts": "冲突",
            "manual_inferences": "人工推断",
        }
        quality_rows = [
            (
                _e(quality_labels.get(key, key)),
                _e(source_status_text(value)) if key == "source_status" else _e(value),
            )
            for key, value in quality.items()
        ]
    else:
        quality_rows = [
            (_e(x.get("item") or x.get("项目")), _e(x.get("description") or x.get("说明")))
            for x in quality if isinstance(x, Mapping)
        ]
    ok_count = sum(1 for x in statuses if x.get("status") == "ok")

    return f'''<!-- Generated by Codex html-report -->
<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>GitHub 开源趋势雷达 · {html.escape(date)}</title>
<style>
:root{{--bg:#f5f7fb;--surface:#fff;--text:#171a22;--muted:#667085;--border:#dfe5ef;--primary:#0066ff;--secondary:#1a3a5c;--success:#087f5b;--warn:#b45309;--danger:#c92a2a;--font:'PingFang SC','Hiragino Sans GB','Microsoft YaHei',system-ui,sans-serif;--mono:Menlo,Consolas,monospace;--max:1240px;--radius:14px;--shadow:0 8px 24px rgba(15,23,42,.07)}}
*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;overflow-x:hidden;background:var(--bg);color:var(--text);font:15px/1.72 var(--font)}}a{{color:var(--primary);text-decoration:none}}a:hover{{text-decoration:underline}}.container,.content{{max-width:var(--max);margin:auto;padding:0 24px}}.cover{{position:relative;overflow:hidden;padding:70px 24px 56px;color:#fff;text-align:center;background:linear-gradient(135deg,#1e1e1e,#132a4a 52%,#1a3a5c)}}.cover:before{{content:'';position:absolute;inset:-50%;background:radial-gradient(circle at 25% 40%,rgba(0,102,255,.32),transparent 38%),radial-gradient(circle at 75% 65%,rgba(77,171,247,.18),transparent 36%)}}.cover .container{{position:relative}}.eyebrow{{font:700 12px var(--mono);letter-spacing:.14em;color:#79b8ff}}h1{{font-size:clamp(30px,5vw,52px);line-height:1.14;margin:18px 0 12px}}.subtitle{{max-width:720px;margin:auto;color:rgba(255,255,255,.78);font-size:17px}}.meta{{margin-top:20px;color:rgba(255,255,255,.58)}}.toc{{position:sticky;top:0;z-index:20;background:rgba(255,255,255,.96);border-bottom:1px solid var(--border);box-shadow:0 2px 12px rgba(15,23,42,.05)}}.toc .container{{display:flex;gap:8px;overflow:auto;padding-top:12px;padding-bottom:12px}}.toc a{{white-space:nowrap;padding:5px 10px;border-radius:999px;color:var(--muted);font-size:13px}}.toc a:hover{{background:#eaf2ff;color:var(--primary);text-decoration:none}}section{{padding:48px 0}}section+section{{border-top:1px solid var(--border)}}h2{{display:inline-block;margin:0 0 24px;padding-bottom:9px;border-bottom:3px solid var(--primary);font-size:25px}}h3{{margin:0 0 10px;font-size:17px}}.metrics,.statuses{{display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));gap:12px}}.metric,.status-card,.callout,.project,.brief{{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow)}}.metric{{padding:18px}}.metric span{{display:block;color:var(--muted);font-size:12px}}.metric strong{{font-size:25px}}.status-card{{display:flex;min-width:0;align-items:center;justify-content:space-between;gap:8px;padding:12px 14px}}.status-card span{{min-width:0;overflow-wrap:anywhere}}.status{{white-space:nowrap;font:700 12px var(--font)}}.status.ok{{color:var(--success)}}.status.partial{{color:var(--warn)}}.status.degraded{{color:var(--danger)}}.overview{{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:22px 0}}.callout{{padding:18px 20px;border-left:4px solid var(--primary)}}.callout.warn{{border-left-color:var(--warn)}}.callout.success{{border-left-color:var(--success)}}ul{{margin:8px 0 0;padding-left:20px}}.table-wrap{{overflow:auto;max-width:100%;max-height:650px;margin:18px 0;border:1px solid var(--border);border-radius:var(--radius);background:var(--surface)}}table{{width:100%;min-width:920px;border-collapse:collapse;font-size:13px}}thead{{position:sticky;top:0;z-index:2}}th{{background:#1e293b;color:#fff;text-align:left;white-space:nowrap}}th,td{{padding:11px 13px;border-bottom:1px solid var(--border);vertical-align:top}}tbody tr:nth-child(even){{background:#f8fafc}}.ranking td:nth-child(5),.ranking td:nth-child(6){{min-width:250px}}.empty{{color:var(--muted);text-align:center}}.projects{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}}.project,.brief{{padding:20px}}.project header{{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}}.project header h3{{min-width:0}}.project header b{{display:inline-flex;flex:0 0 auto;align-items:center;justify-content:center;align-self:flex-start;min-width:64px;height:36px;color:#fff;background:var(--primary);border-radius:999px;padding:0 12px;font:700 12px/1 var(--font);white-space:nowrap}}.purpose{{font-weight:650}}dl{{display:grid;grid-template-columns:110px 1fr;gap:7px 12px}}dt{{color:var(--muted);font-size:12px}}dd{{margin:0}}.briefs{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}}footer{{padding:40px 24px;background:#1e1e1e;color:#a0a0a0}}footer h2{{color:#f0f0f0}}@media(max-width:820px){{.overview,.projects{{grid-template-columns:1fr}}section{{padding:34px 0}}.cover{{padding:52px 18px 42px}}dl{{grid-template-columns:1fr}}}}@media print{{.toc{{display:none}}body{{background:#fff}}section{{break-inside:avoid}}}}
</style></head><body>
<header class="cover"><div class="container"><div class="eyebrow">GITHUB TREND RADAR</div><h1>GitHub 开源趋势与项目复用雷达</h1><p class="subtitle">把热度、增长、复用价值与数据质量放在同一张决策地图中。</p><div class="meta">{html.escape(date)} · 查询时间 {_e(metadata.get("query_time"))}</div></div></header>
<nav class="toc"><div class="container"><a href="#overview">今日速览</a><a href="#rankings">核心榜单</a><a href="#categories">分类趋势</a><a href="#reuse">复用项目</a><a href="#featured">重点解读</a><a href="#history">退榜替代</a><a href="#market">市场走向</a><a href="#watchlist">观察清单</a><a href="#quality">数据质量</a></div></nav><main>
<section id="overview"><div class="content"><h2>今日速览</h2><div class="metrics"><div class="metric"><span>合并仓库</span><strong>{len(model.get("repositories", []))}</strong></div><div class="metric"><span>正常来源</span><strong>{ok_count}/{len(statuses)}</strong></div><div class="metric"><span>复用候选</span><strong>{len(model.get("reusable_projects", []))}</strong></div><div class="metric"><span>重点解读</span><strong>{len(model.get("featured_projects", []))}</strong></div></div><div class="overview"><div class="callout"><h3>事实</h3>{_list(overview.get("facts"))}</div><div class="callout warn"><h3>研判</h3>{_list(overview.get("inferences"))}</div><div class="callout success"><h3>行动</h3>{_list(overview.get("actions"))}</div></div><h3>来源状态</h3><div class="statuses">{status_cards}</div></div></section>
<section id="rankings"><div class="content"><h2>核心排行榜</h2><h3>当前累计 Star</h3>{_ranking(rankings.get("total", []), "当前累计 Star")}<h3>过去 24 小时新增</h3>{_ranking(rankings.get("daily", []), "24h 新增 Star")}<h3>过去 7 天新增</h3>{_ranking(rankings.get("weekly", []), "7d 新增 Star")}<h3>增长加速度</h3>{_ranking(rankings.get("acceleration", []), "24h / 7日日均（倍）")}</div></section>
<section id="categories"><div class="content"><h2>分类趋势</h2>{_table(("分类","事实","上期变化","可信度"), category_rows)}</div></section><section id="reuse"><div class="content"><h2>可复用项目榜</h2><div class="callout"><b>阅读提示</b><p>评分用于候选筛选，不等于直接采用结论；请结合许可证、维护状态、风险和实际集成验证。</p></div><div class="projects">{reusable}</div></div></section>
<section id="featured"><div class="content"><h2>重点项目解读</h2><div class="briefs">{featured}</div></div></section><section id="history"><div class="content"><h2>历史热门、退榜与替代</h2>{_table(("仓库","事实","推断原因","可信度","替代关系","行动建议"), history_rows)}</div></section><section id="market"><div class="content"><h2>市场走向</h2>{_table(("结论","数据证据","上期变化","可信度","连续周期"), market_rows)}</div></section><section id="watchlist"><div class="content"><h2>个人观察清单</h2>{_table(("仓库","观察理由","下一步动作"), watch_rows)}</div></section><section id="quality"><div class="content"><h2>数据口径与异常</h2>{_table(("项目","说明"), quality_rows)}</div></section></main>
<footer><div class="container"><h2>数据来源</h2><p>GitHub Search、GitHub Repository/README/Release、OSSInsight、GitHub Trending 与本地历史快照。具体来源状态及降级信息见“数据口径与异常”。</p><p>本报告由 GitHub 开源趋势雷达自动生成。</p></div></footer></body></html>'''


__all__ = ["build_html_report"]
