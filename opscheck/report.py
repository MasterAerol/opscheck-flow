"""Escaped, dependency-free reports for OpsCheck's tools and workflows."""
from __future__ import annotations

from html import escape
import json
from typing import Any

_DISPLAY_LIMIT = 240
_CSS = """
:root{color-scheme:light;--ink:#142c39;--muted:#65767a;--paper:#f5f6f1;--line:#dfe5de;--teal:#117868;--amber:#9b5817;--red:#aa3f36}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.65 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}a{color:var(--teal);text-underline-offset:4px}main{max-width:1180px;margin:auto;padding:34px 38px 50px}.masthead{display:flex;justify-content:space-between;align-items:center;margin-bottom:28px;gap:16px}.brand{font-weight:780;font-size:23px;letter-spacing:-.8px}.brand-mark{display:inline-grid;place-items:center;background:var(--teal);color:white;width:32px;height:32px;border-radius:9px;margin-right:9px;font-size:20px}.eyebrow{text-transform:uppercase;letter-spacing:1.7px;font-size:11px;font-weight:750;color:var(--muted)}.hero{background:#142f3b;color:#fff;border-radius:20px;padding:31px 35px;position:relative;overflow:hidden}.hero .eyebrow{color:#a7cec4}.hero h1{font-size:clamp(27px,4vw,42px);line-height:1.15;letter-spacing:-1.4px;max-width:780px;margin:12px 0 14px}.hero p{color:#d0dddf;margin:10px 0 0;max-width:850px}.hero .badge{float:right;margin-left:16px}.badge{display:inline-block;border-radius:30px;padding:5px 13px;font-size:12px;font-weight:750;background:#e4f2ea;color:#09634e;border:1px solid #c8e4d7;vertical-align:middle;white-space:normal;overflow-wrap:anywhere}.badge.warning{background:#fff1db;color:#89511b;border-color:#eecf9f}.badge.danger{background:#ffe7e1;color:#943c32;border-color:#ecc3b8}.badge.neutral{background:#eef1f1;color:#465f67;border-color:#dce3e3}.source{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;overflow-wrap:anywhere;white-space:pre-wrap}.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:15px;margin:22px 0 32px}.stat{border:1px solid var(--line);background:#fff;border-radius:13px;padding:17px 20px}.stat strong{display:block;font-size:32px;letter-spacing:-1px;line-height:1.3}.stat span{color:var(--muted);font-size:12px}.stat small{display:block;color:var(--muted);font-size:11px}.stat.alert strong{color:var(--amber)}section{margin-top:26px}.section-heading{display:flex;align-items:center;justify-content:space-between;gap:15px;margin-bottom:12px}h2{font-size:20px;letter-spacing:-.5px;margin:0}h3{font-size:15px;margin:0 0 8px}p{margin:8px 0 16px}.muted{color:var(--muted);font-size:13px}.panel{background:white;border:1px solid var(--line);border-radius:14px;overflow:hidden}.panel-pad{padding:22px 25px}.notice{background:#fff4df;border:1px solid #edd5ac;border-radius:10px;padding:12px 17px;font-size:13px;margin:14px 0}.clean{padding:28px 25px;background:#eef6ef;border:1px solid #d0e2d0;border-radius:13px}.clean strong{font-size:18px;display:block}.clean p{margin:6px 0 0;font-size:13px;color:#48665d}.table-wrap{overflow:auto}table{border-collapse:collapse;width:100%;text-align:left;font-size:13px}th{background:#edf1ed;color:#52686b;text-transform:uppercase;font-size:10px;letter-spacing:1px;padding:12px 16px;white-space:nowrap}td{padding:14px 16px;vertical-align:top;border-top:1px solid #e7ece7;max-width:340px;overflow-wrap:anywhere}tbody tr:hover{background:#fbfcf8}.cell-value{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;white-space:pre-wrap;font-size:12px;overflow-wrap:anywhere}.field{font-weight:650}.issue-code{display:block;font-size:10px;color:var(--muted);margin-top:4px;letter-spacing:.4px}.empty{font-size:12px;font-style:italic;color:var(--muted)}.truncated{font-size:10px;color:var(--amber);font-family:system-ui,sans-serif;white-space:normal}.tag{display:inline-block;padding:3px 10px;margin:4px 6px 4px 0;background:#eef4f0;border:1px solid #d9e4dc;border-radius:7px;overflow-wrap:anywhere;white-space:pre-wrap;font-size:12px}.columns{display:grid;grid-template-columns:1fr 1fr;gap:20px}.workflow-layout{display:grid;grid-template-columns:280px minmax(0,1fr);gap:22px;margin-top:28px}.flow-map{display:grid;gap:12px;align-content:start}.flow-stage{padding:14px 17px;border:1px solid var(--line);border-radius:12px;background:#fff}.flow-stage b{display:block;font-size:13px}.flow-stage small{color:var(--muted);font-size:11px}.flow-stage .num{color:var(--teal);font-size:10px;letter-spacing:1px;font-weight:750}.parallel{display:grid;gap:10px;border:1px dashed #b2c7bf;padding:13px;border-radius:13px;background:#eaf1ea}.parallel>.eyebrow{color:var(--teal);font-size:9px}.parallel .flow-stage{padding:12px}.arrow{text-align:center;color:#81978b;font-size:17px;line-height:1}.workflow-main section:first-child{margin-top:0}.briefing{white-space:pre-wrap;overflow-wrap:anywhere}.action{padding:13px 0;border-top:1px solid var(--line)}.action strong{font-size:13px}.action p{font-size:12px;margin:5px 0 0}.timeline{list-style:none;padding:0;margin:0}.timeline li{padding:11px 0 11px 18px;border-left:2px solid #d3e2da;position:relative;font-size:12px;overflow-wrap:anywhere}.timeline li:before{content:"";height:8px;width:8px;border-radius:50%;background:#309383;position:absolute;left:-5px;top:19px}.timeline span{display:block;color:var(--muted);font-size:11px}.footer{display:flex;justify-content:space-between;gap:16px;margin-top:32px;padding-top:18px;border-top:1px solid var(--line);font-size:11px;color:var(--muted)}.links{display:flex;gap:12px;flex-wrap:wrap}.links a{display:inline-block;font-weight:650;border:1px solid #cadbd3;border-radius:9px;padding:9px 14px;background:#f4f8f3;font-size:12px}.schema-line+.schema-line{margin-top:12px}.limit-note{margin:12px 0;font-size:11px;color:var(--muted)}@media(max-width:700px){main{padding:22px 16px}.masthead{margin-bottom:18px}.masthead>.eyebrow{font-size:9px;letter-spacing:1px}.hero{padding:24px 21px;border-radius:15px}.hero .badge{float:none;margin:0 0 12px}.hero h1{letter-spacing:-.8px}.stats{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin:15px 0 24px}.stat{padding:14px 16px}.stat strong{font-size:27px}.columns{grid-template-columns:1fr}.workflow-layout{grid-template-columns:1fr}.flow-map{grid-template-columns:1fr;gap:8px}.panel-pad{padding:18px}.footer{flex-direction:column;gap:6px}td,th{padding:12px}.table-wrap table{min-width:530px}.section-heading{align-items:flex-start}.section-heading .muted{font-size:11px}}@media print{body{background:white;font-size:11px}main{padding:0;max-width:none}.hero{background:#fff;color:var(--ink);border:1px solid #cbd6d0;padding:20px}.hero p,.hero .eyebrow{color:#435c62}.hero h1{font-size:27px}.stats{margin:15px 0}.stat{padding:12px}.stat strong{font-size:24px}.table-wrap{overflow:visible}table{font-size:10px;min-width:0!important}td{max-width:220px}.workflow-layout{grid-template-columns:220px minmax(0,1fr)}.panel{overflow:visible}tr,.stat,.flow-stage,.action{break-inside:avoid}.footer{margin-top:20px}.links{display:none}a{color:inherit;text-decoration:none}}
"""


def _s(value: Any) -> str:
    return escape(str(value), quote=True)


def _value(value: Any) -> str:
    if value is None:
        return '<span class="empty">Not present</span>'
    value = str(value)
    if not value:
        return '<span class="empty">Empty value</span>'
    extra = ''
    if len(value) > _DISPLAY_LIMIT:
        value = value[:_DISPLAY_LIMIT]
        extra = ' <span class="truncated">[value truncated]</span>'
    return '<span class="cell-value">' + _s(value) + '</span>' + extra


def _badge(status: Any) -> str:
    status = str(status).lower()
    style = 'warning' if status in ('changed', 'running', 'pending', 'waiting', 'waiting_for_approval') else 'danger' if status in ('fail', 'failed', 'blocked', 'rejected') else '' if status in ('pass', 'succeeded', 'unchanged', 'approved') else 'neutral'
    return f'<span class="badge {style}">{_s(status.replace("_", " ").capitalize())}</span>'


def _page(title: str, content: str, flow: bool = False) -> str:
    brand = 'OpsCheck Flow' if flow else 'OpsCheck'
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="color-scheme" content="light">'
            f'<title>{_s(title)} · {_s(brand)}</title><style>{_CSS}</style></head><body><main>'
            '<header class="masthead"><div class="brand"><span class="brand-mark" aria-hidden="true">✓</span>'
            f'{_s(brand)}</div><span class="eyebrow">Local evidence. Clear decisions.</span></header>'
            + content + '<footer class="footer"><span>OpsCheck · Open-source operations toolkit</span>'
            '<span>Generated locally · Source files are never modified</span></footer></main></body></html>')


def _stats(cards: list[tuple[str, Any, str]]) -> str:
    return '<div class="stats">' + ''.join(f'<div class="stat {_s(style)}"><strong>{_s(value)}</strong><span>{_s(label)}</span></div>' for label, value, style in cards) + '</div>'


def _table(headers: list[str], rows: list[str]) -> str:
    return '<div class="panel table-wrap"><table><thead><tr>' + ''.join(f'<th scope="col">{_s(h)}</th>' for h in headers) + '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>'


def _cap(total: int, shown: int, unit: str) -> str:
    if total <= shown:
        return ''
    return f'<div class="notice"><strong>Finding limit reached.</strong> Showing {shown:,} of {total:,} {_s(unit)}. Summary counts include every result; omitted finding details are not included.</div>'


def render_html(result: dict) -> str:
    """Render a validation or comparison result without external dependencies."""
    kind = result['kind']
    summary = result['summary']
    findings = result.get('findings', [])
    status = result['status']
    if kind == 'validate':
        title = 'Your data checks passed.' if status == 'pass' else 'A few things need attention.'
        description = 'Rule-based checks make data issues visible before they reach your next workflow.'
        cards = [('Records checked', summary['rows'], ''), ('Issues found', summary['issues'], 'alert' if summary['issues'] else ''), ('Affected records', summary['affected_rows'], ''), ('Findings shown', summary['reported_findings'], '')]
    elif kind == 'compare':
        title = 'See exactly what changed.' if status == 'changed' else 'No changes detected.'
        description = 'A record-by-record comparison with separate reporting for column changes.'
        cards = [('Added records', summary['added'], ''), ('Removed records', summary['removed'], 'alert' if summary['removed'] else ''), ('Changed records', summary['changed'], 'alert' if summary['changed'] else ''), ('Unchanged records', summary['unchanged'], '')]
    else:
        raise ValueError('Unsupported report kind')
    content = f'<div class="hero">{_badge(status)}<div class="eyebrow">{_s(kind)} report</div><h1>{title}</h1><p>{description}</p><p class="source">Source: {_s(result.get("source", ""))}</p></div>' + _stats(cards)
    if kind == 'validate':
        content += '<section><div class="section-heading"><h2>Validation findings</h2><span class="muted">Logical CSV record numbers</span></div>'
        content += _cap(summary['issues'], len(findings), 'issues')
        if findings:
            rows = []
            for item in findings:
                row = 'Schema' if item.get('row') is None else str(item['row'])
                rows.append('<tr>' + f'<td>{_s(row)}</td><td class="field">{_value(item["column"])}</td><td>{_value(item.get("value"))}</td><td>{_s(item["message"])}<span class="issue-code">{_s(item["code"])}</span></td></tr>')
            content += _table(['Record', 'Column', 'Value', 'Issue'], rows)
        elif status == 'pass':
            content += '<div class="clean"><strong>All configured checks passed.</strong><p>No validation issues were found in this file.</p></div>'
        else:
            content += '<div class="notice">Validation failed. No finding details are included; inspect the summary or rerun with a higher finding limit.</div>'
        content += '</section>'
    else:
        content += f'<p class="muted">{summary["before_rows"]:,} records before → {summary["after_rows"]:,} records after · Matched by <span class="cell-value">{_s(result.get("key", ""))}</span> · {summary["total_changes"]:,} record changes</p>'
        schema = result.get('schema', {})
        content += '<section><div class="section-heading"><h2>Column changes</h2><span class="muted">Counted separately from record changes</span></div>'
        if schema.get('added') or schema.get('removed'):
            content += '<div class="panel panel-pad">'
            for field, label in [('added', 'Added columns'), ('removed', 'Removed columns')]:
                if schema.get(field):
                    content += f'<div class="schema-line"><strong>{label}</strong><div>' + ''.join(f'<span class="tag">{_s(column)}</span>' for column in schema[field]) + '</div></div>'
            content += '</div>'
        else:
            content += '<p class="muted">No columns were added or removed.</p>'
        content += '</section>' + _cap(summary['total_changes'], len(findings), 'record changes')
        for change_kind, heading in [('added', 'Added records'), ('removed', 'Removed records'), ('changed', 'Changed records')]:
            entries = [f for f in findings if f['kind'] == change_kind]
            if not entries:
                continue
            content += f'<section><div class="section-heading"><h2>{heading}</h2><span class="muted">{len(entries):,} shown / {summary[change_kind]:,} total</span></div>'
            rows = []
            for item in entries:
                before, after = item.get('before') or {}, item.get('after') or {}
                fields = item.get('fields') or list(dict.fromkeys([*before, *after]))
                if not fields:
                    rows.append(f'<tr><td>{_value(item["key"])}</td><td colspan="3">Record {_s(change_kind)}</td></tr>')
                for index, field in enumerate(fields):
                    key_cell = f'<td rowspan="{len(fields)}">{_value(item["key"])}</td>' if index == 0 else ''
                    rows.append(f'<tr>{key_cell}<td class="field">{_value(field)}</td><td>{_value(before.get(field))}</td><td>{_value(after.get(field))}</td></tr>')
            content += _table(['Record key', 'Field', 'Before', 'After'], rows) + '</section>'
        if status == 'unchanged':
            content += '<div class="clean"><strong>The compared data is unchanged.</strong><p>Record values and column names match.</p></div>'
    content += '<p class="limit-note">Long values are shortened for display. JSON output preserves full values for included findings. Whitespace in displayed values is preserved.</p>'
    return _page(kind.capitalize() + ' report', content)


def _terminal(value: Any) -> str:
    # JSON string quoting neutralizes newlines, ESC, tabs, controls, and bidi marks.
    return json.dumps(str(value), ensure_ascii=True)


def render_text(result: dict) -> str:
    """Compact terminal output; untrusted strings cannot inject terminal controls."""
    summary = result['summary']
    lines = [f'OpsCheck {_terminal(result["kind"])}: {_terminal(result["status"])}', f'Source: {_terminal(result.get("source", ""))}']
    if result['kind'] == 'validate':
        lines.append(f'{summary["rows"]} records | {summary["issues"]} issues | {summary["affected_rows"]} affected records | {summary["reported_findings"]} reported findings')
        for item in result.get('findings', [])[:5]:
            lines.append(f'  Record {_terminal(item.get("row", "Schema"))}, column {_terminal(item["column"])}: {_terminal(item["message"])}')
        total = summary['issues']
    elif result['kind'] == 'compare':
        lines.append(f'{summary["added"]} added | {summary["removed"]} removed | {summary["changed"]} changed | {summary["unchanged"]} unchanged | {summary["reported_findings"]} reported findings')
        schema = result.get('schema', {})
        for key in ('added', 'removed'):
            if schema.get(key):
                lines.append(f'Columns {key}: ' + ', '.join(_terminal(v) for v in schema[key]))
        for item in result.get('findings', [])[:5]:
            lines.append(f'  {_terminal(item["kind"])} key {_terminal(item["key"])}; fields: ' + ', '.join(_terminal(v) for v in item.get('fields', [])))
        total = summary['total_changes']
    else:
        raise ValueError('Unsupported report kind')
    if total > min(len(result.get('findings', [])), 5):
        lines.append(f'Terminal shows {min(len(result.get("findings", [])), 5)} of {total} findings; summary counts include all results.')
    return '\n'.join(lines)


def _briefing_html(value: Any) -> str:
    if not isinstance(value, dict):
        return f'<p class="briefing">{_s(value)}</p>'
    briefing = value.get('briefing', value)
    if not isinstance(briefing, dict):
        return f'<p class="briefing">{_s(briefing)}</p>'
    summary = briefing.get('summary', briefing.get('text', ''))
    html = f'<p class="briefing">{_s(summary)}</p>'
    for action in briefing.get('actions', []):
        if isinstance(action, dict):
            html += f'<div class="action"><strong>{_s(action.get("title", "Action"))}</strong><p>Priority: {_s(action.get("priority", "unspecified"))}'
            if action.get('evidence_ids'):
                html += ' · Evidence: ' + ', '.join(_s(v) for v in action['evidence_ids'])
            html += '</p></div>'
        else:
            html += f'<div class="action">{_s(action)}</div>'
    if value.get('mode') == 'model' and value.get('rounds') is not None:
        html += f'<p class="muted">Review rounds: {_s(value["rounds"])} · Reviewer approved: {_s(value.get("approved", False))}</p>'
    elif value.get('mode') == 'local':
        html += '<p class="muted">Deterministic briefing; model review not used.</p>'
    history = value.get('review_history', [])
    if history:
        html += '<details><summary>Review history</summary>'
        for item in history:
            html += f'<p class="muted">Round {_s(item.get("round", ""))}: {_s(item.get("feedback", ""))}</p>'
        html += '</details>'
    evidence = value.get('evidence', {})
    if isinstance(evidence, dict) and evidence.get('evidence'):
        html += '<details><summary>Briefing evidence (' + _s(len(evidence['evidence'])) + ' items)</summary>'
        if evidence.get('truncation', {}).get('applied'):
            html += '<p class="muted">This briefing uses a bounded evidence sample. Summary counts include all findings; source reports provide the available finding details.</p>'
        for item in evidence['evidence']:
            details = {key: detail for key, detail in item.items() if key != 'id'}
            html += f'<div class="action"><strong>{_s(item.get("id", "Evidence"))}</strong><p class="cell-value">{_s(json.dumps(details, ensure_ascii=False, default=str))}</p></div>'
        html += '</details>'
    return html


def _revision_html(briefing: dict) -> str:
    revision = briefing.get('revision')
    if not revision:
        return ''
    content = f'<h3>Human revision {_s(revision["number"])}</h3><p class="briefing">Feedback: {_s(revision["feedback"])}</p>'
    content += f'<p class="muted">{_s(revision["sampling_note"])}</p>'
    for section in revision['sections']:
        content += f'<h3>{_s(section["title"])} ({_s(section["total"])} total)</h3>'
        rows = []
        for finding in section['findings']:
            if 'row' in finding:
                rows.append('<tr>' + ''.join(f'<td>{_s(finding.get(key, ""))}</td>'
                            for key in ('row', 'column', 'code', 'value', 'message')) + '</tr>')
            else:
                rows.append('<tr>' + ''.join(f'<td class="cell-value">{_s(json.dumps(finding.get(key), ensure_ascii=False))}</td>'
                            for key in ('key', 'kind', 'fields', 'before', 'after')) + '</tr>')
        headers = ['Row', 'Field', 'Issue', 'Value', 'Explanation'] if 'schema' not in section else ['Order ID', 'Change', 'Fields', 'Before', 'After']
        content += _table(headers, rows) if rows else '<p>No record findings.</p>'
        if 'schema' in section:
            content += f'<p>Column changes: {_s(json.dumps(section["schema"], ensure_ascii=False))}</p>'
    return content


def _approval_html(result: dict) -> str:
    approvals = result.get('approvals', [])
    content = '<section><h2>Approval History</h2>'
    if result.get('failure_reason'):
        content += f'<p class="notice">{_s(result["failure_reason"])}</p>'
    if not approvals:
        return content + '<p>No human approval requested.</p></section>'
    content += f'<p>Approval Status: {_s(approvals[-1]["status"].capitalize())}</p>'
    for item in approvals:
        content += f'<div class="action"><h3>Version {_s(item["iteration"])} · {_badge(item["status"])}</h3>'
        content += f'<p>Reviewer: {_s(item.get("reviewer") or "Unspecified")}</p>'
        content += f'<p class="briefing">Comment: {_s(item.get("comment") or "")}</p>'
        content += f'<p class="muted">Created: {_s(item["created_at"])}<br>Decided: {_s(item.get("decided_at") or "Pending")}</p></div>'
    return content + '</section>'


def render_workflow_html(result: dict) -> str:
    """Render execution evidence separately from underlying business findings."""
    status = str(result.get('status', 'unknown')).lower()
    tasks = result.get('tasks', [])
    results = result.get('results', {})
    quality, changes = results.get('quality_agent', {}), results.get('change_agent', {})
    q, c = quality.get('summary', {}), changes.get('summary', {})
    title = ('Your operations check, connected.' if status == 'succeeded' else
             'Your briefing is ready for approval.' if status == 'waiting_for_approval' else
             'The workflow needs attention.')
    mode = result.get('mode', 'local')
    mode_label = 'Local workflow · Deterministic checks and briefing' if mode == 'local' else 'Model-assisted briefing · Deterministic data checks'
    content = f'<div class="hero">{_badge(status)}<div class="eyebrow">Workflow report</div><h1>{title}</h1><p>Plan the work. Run specialist checks in parallel. Verify the evidence. Prepare a clear briefing.</p><p class="source">Run: {_s(result.get("run_id", ""))}</p></div>'
    content += '<p class="muted" style="margin-top:14px">' + _s(mode_label) + '</p>'
    content += _stats([('Steps completed', f'{sum(t.get("status") == "succeeded" for t in tasks)} / {len(tasks)}', ''), ('Execution attempts', sum(t.get('attempts', 0) for t in tasks), ''), ('Validation issues', q.get('issues', '—'), 'alert' if q.get('issues') else ''), ('Record changes', c.get('total_changes', '—'), 'alert' if c.get('total_changes') else '')])
    content += '<p class="muted">Workflow success means the steps executed, verification passed, and the human approved the briefing. Validation issues and data changes are separate business results.</p>'
    task_map = {t['id']: t for t in tasks}
    labels = {'planner': ('01', 'Plan the workflow', 'Define dependencies and the execution plan'), 'quality_agent': ('02', 'Quality specialist', 'Validate records against configured rules'), 'change_agent': ('03', 'Change specialist', 'Compare snapshots by a stable record key'), 'verifier': ('04', 'Verify the evidence', 'Rerun checks and confirm result consistency'), 'briefing_agent': ('05', 'Prepare the briefing', 'Turn verified findings into next actions')}
    def stage(task_id: str) -> str:
        number, label, detail = labels[task_id]
        task = task_map.get(task_id, {})
        return f'<div class="flow-stage"><span class="num">STEP {number}</span><b>{label}</b><small>{detail}</small><div style="margin-top:8px">{_badge(task.get("status", "pending"))}</div></div>'
    content += '<div class="workflow-layout"><aside class="flow-map" aria-label="Workflow stages">' + stage('planner') + '<div class="arrow" aria-hidden="true">↓</div><div class="parallel"><span class="eyebrow">Parallel checks</span>' + stage('quality_agent') + stage('change_agent') + '</div><div class="arrow" aria-hidden="true">↓</div>' + stage('verifier') + '<div class="arrow" aria-hidden="true">↓</div>' + stage('briefing_agent') + '<div class="flow-stage"><b>Approval gate</b><small>Human decision: approve or request revision</small>' + _badge(result.get('approvals', [{}])[-1].get('status', 'pending') if result.get('approvals') else 'pending') + '<p class="muted">Rejected → Revision agent → Approval gate</p></div></aside><div class="workflow-main">'
    content += '<section><div class="section-heading"><h2>Operations briefing</h2>' + _badge(mode) + '</div><div class="panel panel-pad">'
    if 'briefing_agent' in results:
        content += _briefing_html(results['briefing_agent'])
    else:
        content += '<p class="muted">No briefing is available. Review task status and execution events below.</p>'
    content += _revision_html(results.get('briefing_agent', {}))
    content += _approval_html(result)
    content += '</div></section><section><div class="section-heading"><h2>Check results</h2><span class="muted">Source-level evidence</span></div><div class="panel panel-pad">'
    if quality:
        content += f'<h3>Data quality {_badge(quality.get("status", "unknown"))}</h3><p class="muted">{_s(q.get("rows", 0))} records checked · {_s(q.get("issues", 0))} issues · {_s(q.get("affected_rows", 0))} affected records</p>'
    if changes:
        content += f'<h3>Snapshot changes {_badge(changes.get("status", "unknown"))}</h3><p class="muted">{_s(c.get("added", 0))} added · {_s(c.get("removed", 0))} removed · {_s(c.get("changed", 0))} changed · {_s(c.get("unchanged", 0))} unchanged records</p>'
        schema = changes.get('schema', {})
        if schema.get('added') or schema.get('removed'):
            content += f'<p class="muted">Column changes: {_s(len(schema.get("added", [])))} added, {_s(len(schema.get("removed", [])))} removed (separate from record changes).</p>'
    if not quality and not changes:
        content += '<p class="muted">No completed check outputs are available.</p>'
    content += '<div class="links">'
    if quality:
        content += '<a href="quality.html">View quality report →</a>'
    if changes:
        content += '<a href="changes.html">View changes report →</a>'
    content += '</div></div></section><section><div class="section-heading"><h2>Execution evidence</h2><span class="muted">Attempts persist across resumed runs</span></div>'
    rows = []
    for task in tasks:
        rows.append(f'<tr><td class="field">{_s(task.get("id", ""))}</td><td>{_badge(task.get("status", "unknown"))}</td><td>{_s(task.get("attempts", 0))}</td><td>{_value(task.get("error")) if task.get("error") else "—"}</td></tr>')
    content += _table(['Task', 'Status', 'Attempts', 'Latest error'], rows) + '</section>'
    events = result.get('events', [])
    content += '<section><div class="section-heading"><h2>Run timeline</h2><span class="muted">' + _s(len(events)) + ' events</span></div><div class="panel panel-pad"><ol class="timeline">'
    for event in events:
        event_name = str(event.get('event', 'event')).replace('_', ' ')
        content += f'<li><strong>{_s(event_name.capitalize())}</strong> · {_s(event.get("task") or "workflow")}'
        metadata = {key: value for key, value in event.items() if key not in ('event', 'task')}
        if metadata:
            content += '<span>' + _s(json.dumps(metadata, ensure_ascii=False, default=str)) + '</span>'
        content += '</li>'
    content += '</ol></div></section></div></div>'
    return _page('Workflow report', content, flow=True)
