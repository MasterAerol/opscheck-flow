"""Behavioral safety and evidence-accuracy tests for report presentation."""
import copy
from html.parser import HTMLParser
import unittest

from opscheck.report import render_html, render_text, render_workflow_html


def validation():
    return {'kind': 'validate', 'status': 'fail', 'source': 'orders.csv',
            'summary': {'rows': 15, 'issues': 2, 'affected_rows': 1, 'reported_findings': 2},
            'findings': [{'row': 3, 'column': 'email', 'code': 'type', 'message': 'Invalid email', 'value': 'bad email'},
                         {'row': 3, 'column': 'amount', 'code': 'min', 'message': 'Amount below minimum', 'value': '-5'}]}


def comparison():
    return {'kind': 'compare', 'status': 'changed', 'source': 'old.csv → new.csv', 'key': 'order_id',
            'summary': {'before_rows': 2, 'after_rows': 2, 'added': 1, 'removed': 1, 'changed': 1,
                        'unchanged': 0, 'total_changes': 3, 'reported_findings': 3},
            'schema': {'added': ['notes'], 'removed': ['legacy']},
            'findings': [{'kind': 'added', 'key': 'C', 'before': None, 'after': {'order_id': 'C', 'amount': '30'}, 'fields': ['order_id', 'amount']},
                         {'kind': 'removed', 'key': 'A', 'before': {'order_id': 'A', 'amount': '10'}, 'after': None, 'fields': ['order_id', 'amount']},
                         {'kind': 'changed', 'key': 'B', 'before': {'amount': '20'}, 'after': {'amount': '25'}, 'fields': ['amount']}]}


class ParsedReport(HTMLParser):
    def __init__(self, document):
        super().__init__()
        self.tags, self.data, self.attributes = [], [], []
        self.feed(document)

    def handle_starttag(self, tag, attributes):
        self.tags.append(tag)
        self.attributes.extend(attributes)

    def handle_data(self, data):
        self.data.append(data)


class ReportTests(unittest.TestCase):
    def test_validation_uses_full_counts_and_logical_record_numbers(self):
        parsed = ParsedReport(render_html(validation()))
        text = '\n'.join(parsed.data)
        self.assertIn('15', parsed.data)
        self.assertIn('2', parsed.data)
        self.assertIn('1', parsed.data)
        self.assertIn('3', parsed.data)
        self.assertIn('Logical CSV record numbers', text)
        self.assertIn('Affected records', text)
        self.assertIn('-5', parsed.data)

    def test_all_user_strings_are_text_not_executable_markup(self):
        attack = '<script>alert(1)</script><img src=x onerror="alert(2)">'
        value = validation()
        value['source'] = attack
        value['status'] = attack
        value['findings'] = [dict(row=None, column=attack, code=attack, message=attack, value=attack)]
        parsed = ParsedReport(render_html(value))
        self.assertNotIn('script', parsed.tags)
        self.assertNotIn('img', parsed.tags)
        self.assertFalse(any(key.startswith('on') for key, _ in parsed.attributes))
        self.assertIn(attack, '\n'.join(parsed.data))
        self.assertIn('Schema', parsed.data)

    def test_finding_cap_keeps_true_summary_and_discloses_omissions(self):
        value = validation()
        value['summary'].update(issues=87, affected_rows=13, reported_findings=1)
        value['findings'] = value['findings'][:1]
        output = render_html(value)
        self.assertIn('Showing 1 of 87 issues', output)
        self.assertIn('<strong>87</strong>', output)
        self.assertIn('<strong>13</strong>', output)
        self.assertIn('omitted finding details are not included', output)

    def test_value_truncation_is_visible_and_does_not_mutate_result(self):
        value = validation()
        value['findings'][0]['value'] = 'A' * 300 + 'FULL-VALUE-TAIL'
        original = copy.deepcopy(value)
        output = render_html(value)
        self.assertIn('[value truncated]', output)
        self.assertNotIn('FULL-VALUE-TAIL', output)
        self.assertEqual(value, original)
        self.assertIn('full values for included findings', output)

    def test_empty_pass_is_clean_but_missing_column_is_failure(self):
        value = validation()
        value.update(status='pass', findings=[])
        value['summary'].update(rows=0, issues=0, affected_rows=0, reported_findings=0)
        self.assertIn('All configured checks passed.', render_html(value))
        value.update(status='fail', findings=[dict(row=None, column='required_column', code='missing_column', message='Configured column missing', value='')])
        value['summary'].update(issues=1, reported_findings=1)
        output = render_html(value)
        self.assertNotIn('All configured checks passed.', output)
        self.assertIn('Configured column missing', output)
        self.assertIn('Schema', output)

    def test_zero_reported_findings_does_not_misrepresent_failure(self):
        value = validation()
        value['findings'] = []
        value['summary']['reported_findings'] = 0
        output = render_html(value)
        self.assertIn('Validation failed.', output)
        self.assertIn('Showing 0 of 2 issues', output)
        self.assertNotIn('All configured checks passed.', output)

    def test_comparison_displays_before_after_and_schema_separately(self):
        parsed = ParsedReport(render_html(comparison()))
        text = '\n'.join(parsed.data)
        for item in ['Added records', 'Removed records', 'Changed records', 'Before', 'After', '20', '25', 'Not present', 'notes', 'legacy']:
            self.assertIn(item, parsed.data)
        self.assertIn('Counted separately from record changes', text)
        self.assertIn('3 record changes', text)

    def test_unchanged_comparison_and_schema_only_change(self):
        value = comparison()
        value.update(status='unchanged', findings=[], schema={'added': [], 'removed': []})
        value['summary'].update(added=0, removed=0, changed=0, unchanged=2, total_changes=0, reported_findings=0)
        self.assertIn('The compared data is unchanged.', render_html(value))
        value['status'] = 'changed'
        value['schema']['added'] = ['new field']
        output = render_html(value)
        self.assertIn('new field', output)
        self.assertNotIn('The compared data is unchanged.', output)
        self.assertIn('0 record changes', output)

    def test_comparison_escapes_keys_fields_and_values(self):
        value = comparison()
        attack = '<svg onload=alert(1)>'
        value['source'] = attack
        value['key'] = attack
        value['schema']['added'] = [attack]
        value['findings'] = [{'kind': 'changed', 'key': attack, 'fields': [attack], 'before': {attack: attack}, 'after': {attack: '\t after  '}}]
        output = render_html(value)
        self.assertNotIn('<svg', output)
        self.assertIn('&lt;svg onload=alert(1)&gt;', output)
        self.assertIn('\t after  ', output)
        self.assertIn('white-space:pre-wrap', output)

    def test_terminal_control_characters_are_neutralized(self):
        attack = '\x1b[31m\nforged success\r\t\x00\u202e'
        for value in (validation(), comparison()):
            value['source'] = attack
            value['status'] = attack
            if value['kind'] == 'validate':
                value['findings'][0].update(row=attack, column=attack, message=attack)
            else:
                value['schema']['added'] = [attack]
                value['findings'][0].update(kind=attack, key=attack, fields=[attack])
            output = render_text(value)
            self.assertNotIn('\x1b', output)
            self.assertNotIn('\r', output)
            self.assertNotIn('\t', output)
            self.assertNotIn('\x00', output)
            self.assertNotIn('\u202e', output)
            self.assertIn('\\u001b[31m\\nforged success', output)
            self.assertFalse(any(line.startswith('forged success') for line in output.splitlines()))

    def test_terminal_shows_at_most_five_findings_with_counts(self):
        value = validation()
        value['findings'] = value['findings'] * 4
        value['summary'].update(issues=8, reported_findings=8)
        output = render_text(value)
        self.assertEqual(sum(line.startswith('  Record') for line in output.splitlines()), 5)
        self.assertIn('Terminal shows 5 of 8 findings', output)

    def test_documents_are_standalone_and_responsive(self):
        for output in (render_html(validation()), render_html(comparison())):
            parsed = ParsedReport(output)
            self.assertNotIn('script', parsed.tags)
            self.assertNotIn('link', parsed.tags)
            self.assertIn('@media(max-width:700px)', output)
            self.assertIn('@media print', output)
            self.assertIn('name="viewport"', output)


class WorkflowReportTests(unittest.TestCase):
    def workflow(self):
        return {'run_id': 'test-run', 'status': 'succeeded', 'mode': 'local', 'plan': {}, 'state_dir': '/tmp/run',
                'tasks': [{'id': task, 'status': 'succeeded', 'attempts': 2 if task == 'quality_agent' else 1, 'error': None}
                          for task in ['planner', 'quality_agent', 'change_agent', 'verifier', 'briefing_agent']],
                'events': [{'event': 'task_failed', 'task': 'quality_agent', 'attempt': 1},
                           {'event': 'task_retrying', 'task': 'quality_agent'}, {'event': 'task_succeeded', 'task': 'quality_agent', 'attempt': 2}],
                'results': {'quality_agent': validation(), 'change_agent': comparison(),
                            'briefing_agent': {'mode': 'local', 'rounds': 0, 'approved': True,
                                               'briefing': {'summary': 'Check the two validation issues.', 'actions': [{'title': 'Fix invalid emails', 'priority': 'high', 'evidence_ids': ['V1']}]}}}}

    def test_workflow_success_does_not_hide_validation_failures(self):
        output = render_workflow_html(self.workflow())
        parsed = ParsedReport(output)
        self.assertIn('Succeeded', parsed.data)
        self.assertIn('Fail', parsed.data)
        self.assertIn('Changed', parsed.data)
        self.assertIn('Workflow success means the steps executed', output)
        self.assertIn('5 / 5', parsed.data)
        self.assertIn('6', parsed.data)
        self.assertIn('href="quality.html"', output)
        self.assertIn('href="changes.html"', output)

    def test_retry_timeline_and_parallel_specialists_are_visible(self):
        output = render_workflow_html(self.workflow())
        for item in ['Parallel checks', 'Quality specialist', 'Change specialist', 'Task retrying', 'Task failed', 'Task succeeded', 'Attempts persist across resumed runs', 'Fix invalid emails', 'V1']:
            self.assertIn(item, output)

    def test_local_briefing_does_not_claim_model_review(self):
        output = render_workflow_html(self.workflow())
        self.assertIn('Deterministic briefing; model review not used.', output)
        self.assertNotIn('Reviewer approved:', output)
        self.assertNotIn('Review rounds:', output)

    def test_briefing_evidence_is_reviewable_and_escaped(self):
        value = self.workflow()
        value['results']['briefing_agent']['evidence'] = {
            'evidence': [{'id': 'V1', 'row': 3, 'column': '<script>', 'message': 'Invalid email'}],
            'truncation': {'applied': True}}
        output = render_workflow_html(value)
        self.assertIn('Briefing evidence (1 items)', output)
        self.assertIn('bounded evidence sample', output)
        self.assertIn('Invalid email', output)
        self.assertIn('&lt;script&gt;', output)
        self.assertNotIn('<script>', output)

    def test_run_event_without_task_is_labeled_workflow(self):
        value = self.workflow()
        value['events'] = [{'event': 'run_started', 'task': None}]
        output = render_workflow_html(value)
        self.assertIn('Run started</strong> · workflow', output)

    def test_workflow_all_arbitrary_text_is_escaped(self):
        attack = '<img src=x onerror="alert(1)"><script>x</script>'
        value = self.workflow()
        value['run_id'] = attack
        value['status'] = attack
        value['tasks'][0].update(id=attack, status=attack, error=attack)
        value['events'][0].update(event=attack, task=attack, detail=attack)
        value['results']['briefing_agent']['briefing']['summary'] = attack
        value['results']['briefing_agent']['briefing']['actions'][0].update(title=attack, priority=attack, evidence_ids=[attack])
        parsed = ParsedReport(render_workflow_html(value))
        self.assertNotIn('img', parsed.tags)
        self.assertNotIn('script', parsed.tags)
        self.assertIn(attack, '\n'.join(parsed.data))
        self.assertFalse(any(key.startswith('on') for key, _ in parsed.attributes))

    def test_failed_run_does_not_claim_briefing_or_link_missing_outputs(self):
        value = self.workflow()
        value.update(status='failed', results={})
        value['tasks'][0]['status'] = 'failed'
        output = render_workflow_html(value)
        self.assertIn('No briefing is available', output)
        self.assertIn('No completed check outputs', output)
        self.assertNotIn('href="quality.html"', output)
        self.assertNotIn('href="changes.html"', output)

    def test_model_briefing_has_review_evidence(self):
        value = self.workflow()
        value['mode'] = 'model'
        value['results']['briefing_agent'].update(mode='model', rounds=2, review_history=[{'round': 1, 'approved': False, 'feedback': 'Clarify the affected records.'}, {'round': 2, 'approved': True, 'feedback': 'Grounded in evidence.'}])
        output = render_workflow_html(value)
        self.assertIn('Review rounds: 2', output)
        self.assertIn('Clarify the affected records.', output)
        self.assertIn('Model-assisted briefing', output)


if __name__ == '__main__':
    unittest.main()
