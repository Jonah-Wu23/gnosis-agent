import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from jsonpointer import resolve_pointer

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'experiments/negative_result_offline/independent_review'
MATERIALS = ROOT / 'experiments/feedback_chain/paid/business_review/materials'


def dump(name, value):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def collect_rows(world, book_id, sheet_id):
    sheets = world.get('google_sheets', {})
    if 'rows' in sheets:
        return [row['cells'] for row in sheets['rows'] if row['spreadsheet_id'] == book_id and row['worksheet_id'] == sheet_id]
    return [row['cells'] for book in sheets.get('spreadsheets', [])
            if book.get('id', book.get('spreadsheet_id')) == book_id
            for sheet in book['worksheets'] if sheet.get('id', sheet.get('worksheet_id')) == sheet_id
            for row in sheet.get('rows', [])]


def amounts(value):
    return Decimal(str(value).replace('$', '').replace(',', '').strip())


def records(world, service):
    return [item for group in world.get(service, {}).get('actions', {}).values() for item in group]


def added_messages(record, service='gmail'):
    prior = {item.get('id') or item.get('ts') for item in record['initial_world'].get(service, {}).get('messages', [])}
    return [item for item in record['final_world'].get(service, {}).get('messages', []) if (item.get('id') or item.get('ts')) not in prior]


def independent_checks(record):
    task = record['task']
    initial, final = record['initial_world'], record['final_world']
    result = {}
    def mail(address):
        return [item.get('body_plain', '') for item in added_messages(record) if address in item.get('to', [])]
    def contains(address, literals):
        return any(all(value in body for value in literals) for body in mail(address))
    def rowcheck(key, book, sheet, expected):
        result[key] = any(all(str(row.get(column)) == str(value) for column, value in expected.items()) for row in collect_rows(final, book, sheet))
    def payloads(service):
        return [json.dumps(item.get('params', {}), ensure_ascii=False) for item in records(final, service)]
    if task == 'finance.xero_credit_allocation':
        # 此处核验已写入分配，不把宿主未更新invoice视为Agent未操作。
        expected = {'xcn_002': ('xi_004', 2000), 'xcn_003': ('xi_006', 3500), 'xcn_004': ('xi_009', 1200), 'xcn_005': ('xi_007', 2500)}
        actions = record.get('actual_tool_actions', record.get('actions', []))
        observed = []
        for index, action in enumerate(actions):
            args = action.get('arguments', {})
            if args.get('url', '').endswith('/Allocations') and not action.get('failed'):
                body = args.get('body', {})
                body = json.loads(body) if isinstance(body, str) else body
                for allocation in body.get('Allocations', []):
                    observed.append((args['url'].split('/')[-2], allocation['Invoice']['InvoiceID'], amounts(allocation['Amount']), index))
        for credit, (invoice, amount) in expected.items():
            result['allocation:' + credit] = any(c == credit and i == invoice and a == amount for c, i, a, _ in observed)
        for invoice in initial['xero']['invoices']:
            number = invoice['invoice_number']
            current = next(item for item in final['xero']['invoices'] if item['invoice_id'] == invoice['invoice_id'])
            expected_balance = amounts(invoice['amount_due']) - next((Decimal(amount) for target, amount in expected.values() if target == invoice['invoice_id']), Decimal(0))
            result['balance:' + number] = amounts(current['amount_due']) == expected_balance
        result['credit_limit'] = all(item.get('credit_limit') == next(prior for prior in initial['xero']['contacts'] if prior['contact_id'] == item['contact_id']).get('credit_limit') for item in final['xero']['contacts'])
    elif task == 'finance.xero_multicurrency_payment':
        for invoice, amount in [('xi_3001', 9000), ('xi_3002', 1500000)]:
            result['payment_amount:' + invoice] = any(item.get('invoice_id') == invoice and amounts(item['amount']) == amount for item in final['xero']['payments'])
    elif task == 'finance.xero_quote_to_invoice':
        for name, amount in [('Alpine Solutions', 12600), ('Alpine Solutions Group', 7875), ('Meridian Corp', 18000)]:
            matches = [item for item in final['xero']['invoices'] if item.get('contact_name') == name]
            result['invoice_total:' + name] = any(amounts(item['total']) == amount for item in matches)
            result['invoice_due:' + name] = any(str(item.get('due_date', ''))[:10] == '2026-03-12' for item in matches)
        result['alternative:QU104_total'] = any(item.get('contact_name') == 'Alpine Solutions Group' and amounts(item['total']) == 8505 for item in final['xero']['invoices'])
    elif task == 'finance.qb_void_stale_invoices':
        for identity in ['qi_601', 'qi_602']:
            old = next(item for item in initial['quickbooks']['invoices'] if item['id'] == identity)
            new = next(item for item in final['quickbooks']['invoices'] if item['id'] == identity)
            result['void_reason:' + identity] = any(new.get(field) and new.get(field) != old.get(field) for field in ['note', 'message'])
    elif task == 'finance.employee_reimbursement':
        for employee, row in [('Carol Diaz', 4), ('Dave Kim', 5)]:
            rowcheck('approved:' + str(row), 'ss_reimb', 'ws_pending_reimb', {'Employee': employee, 'Status': 'Approved'})
    elif task == 'hr.airtable_learning_path_assignment':
        for employee, courses in [('EMP-2010', ['LRN-101', 'LRN-102']), ('EMP-3001', ['LRN-201', 'LRN-202', 'LRN-203']), ('EMP-2011', ['LRN-301', 'LRN-302']), ('EMP-2003', ['LRN-101', 'LRN-102'])]:
            fields = [item.get('params', {}).get('fields', {}) for item in records(final, 'airtable')]
            result['courses:' + employee] = any(item.get('Employee ID') == employee and all(course in str(item.get('Courses', '')) for course in courses) for item in fields)
        result['sabbatical_deferral'] = any(item.get('params', {}).get('fields', {}).get('Employee ID') == 'EMP-3003' and not item['params']['fields'].get('Courses') and bool(item['params']['fields'].get('Notes') or item['params']['fields'].get('Status')) for item in records(final, 'airtable'))
        result['correct_employee_email'] = contains('tom.bradford@company.example.com', ['Foundation'])
    elif task == 'hr.benefits_open_enrollment_processing':
        for row, name, plan, tier, cost in [(2, 'Alice Park', 'Premium', 'Family', '$950'), (3, 'Bob Chen', 'Premium', 'Single', '$350'), (4, 'Nadia Petrova', 'Basic', 'Employee+Spouse', '$450')]:
            for field, expected in [('Plan', plan), ('Coverage Tier', tier), ('Monthly Cost', cost)]:
                rowcheck(f'enrollment:{row}:{field}', 'ss_enrollment', 'ws_selections', {'Employee': name, field: expected})
            result['enrollment_status:' + str(row)] = any(item.get('Employee') == name and item.get('Status') not in [None, '', 'Pending'] for item in collect_rows(final, 'ss_enrollment', 'ws_selections'))
        for identity in ['EMP-1002', 'EMP-3001']:
            result['bamboohr:' + identity] = any(identity in payload for payload in payloads('bamboohr'))
        result['excluded_contractor_email'] = not mail('wei.zhang@company.example.com')
    elif task == 'hr.compliance_training_enrollment':
        for name, row, address in [('Alice Park', 2, 'alice.park@company.example.com'), ('Ravi Sundaram', 3, 'ravi.sundaram@company.example.com'), ('Carol Diaz', 5, 'carol.diaz@company.example.com')]:
            rowcheck('enrolled:' + str(row), 'ss_training', 'ws_requirements', {'Employee': name, 'Status': 'Enrolled'})
            result['enrollment_write:' + name] = any(name in payload or address in payload for payload in payloads('bamboohr'))
        result['sales_training_stopped'] = not mail('tom.bradford@company.example.com')
        result['alice_data_privacy'] = contains('alice.park@company.example.com', ['April 8'])
        result['ravi_data_privacy'] = contains('ravi.sundaram@company.example.com', ['April 8', 'April 15'])
    elif task == 'hr.intern_program_coordination':
        for identity, address, name, dates in [('jordan', 'alice.park@company.example.com', 'Jordan', ['June 9', '2026-06-09']), ('ava', 'carlos.reyes@company.example.com', 'Ava', ['June 2', '2026-06-02']), ('taylor', 'bob.chen@company.example.com', 'Taylor', ['June 9', '2026-06-09'])]:
            result['mentor_date:' + identity] = any(name in body and any(date in body for date in dates) for body in mail(address))
        # 顺序与批准措辞需要原文阅读；以独立语义决定表逐执行给值。
        result['legal_first'] = None
        result['mentor_approval'] = None
    elif task == 'hr.offboarding_multi_system':
        result['ian_payroll'] = contains('payroll@company.example.com', ['Ian Costa', '0 days'])
        for name in ['Greg Foster', 'Ian Costa']:
            result['revoke_access:' + name] = any(name in payload for payload in payloads('jira'))
        result['unknown:separation_policy'] = None
    elif task == 'marketing.customer_milestone':
        for identity, name in [('C001', 'Meridian Labs'), ('C006', 'Cascade Analytics'), ('C008', 'Summit Digital')]:
            rowcheck('log_identity:' + identity, 'ss_milestones', 'ws_log', {'customer_id': identity, 'company': name})
    elif task == 'marketing.event_sponsorship_screen':
        names = ['CX Summit', 'EU Support Forum', 'Partner Connect', 'Startup Showcase', 'SaaS Connect', 'Startup Innovation Day']
        values = ['12000', '8000', '15000', '5000', '13000', '6000']
        result['report_all_approved'] = any(all(name in body for name in names) and all(value in body.replace(',', '') for value in values) for body in mail('finance@company.example.com'))
        result['approved_total'] = any('59000' in body.replace(',', '') for body in mail('finance@company.example.com'))
    elif task == 'marketing.news_digest_dedup':
        titles = ['AI automation roundup', 'Support tools comparison 2026', 'AI customer service trends', 'Data policy changes reshape SaaS compliance', 'Enterprise AI adoption trends', 'DevOps platform consolidation trends', '[PAYWALLED] B2B SaaS market size forecast 2026', 'AI product launches January 2026']
        result['verbatim_titles'] = contains('editor@company.example.com', titles)
        result['curator_article_in_log'] = any(row.get('URL') == 'https://news.example.com/ai-product-launches-jan' for row in collect_rows(final, 'ss_newslog', 'ws_log'))
        rowcheck('same_day_embargo', 'ss_newslog', 'ws_log', {'URL': 'https://news.example.com/devops-consolidation-2026'})
        rowcheck('curator_article', 'ss_newslog', 'ws_log', {'URL': 'https://news.example.com/ai-product-launches-jan'})
        result['digest_row_count'] = len(collect_rows(final, 'ss_newslog', 'ws_log')) == 11
        result['digest_count'] = contains('editor@company.example.com', ['8 article'])
        result['digest_slack_count'] = any(item.get('channel_id') == 'ch_content_updates' and '8 article' in item.get('text', '') for item in added_messages(record, 'slack'))
    elif task == 'marketing.campaign_handoff':
        for row, name in [(2, 'Q1 Webinar Series'), (4, 'Partner Promotion'), (6, 'Brand Refresh'), (9, 'Co-Brand Launch')]:
            result['handoff_notes:' + str(row)] = any(item.get('campaign') == name and 'handoff sent on' in item.get('notes', '').lower() for item in collect_rows(final, 'ss_handoff', 'ws_campaigns'))
        creations = [item['params'] for item in records(final, 'google_drive') if item.get('action_key') == 'create_file']
        folders = {item.get('file') for item in creations if item.get('name') == 'ops' and item.get('mimeType') == 'application/vnd.google-apps.folder'}
        documents = [item for item in creations if item.get('folder') in folders and item.get('mimeType') == 'application/vnd.google-apps.document']
        result['archive_copy'] = bool(documents)
        for name in ['Q1 Webinar Series', 'Partner Promotion', 'Brand Refresh', 'Co-Brand Launch']:
            result['document_archive:' + name] = any(name in item.get('name', '') for item in documents)
        result['unknown:document_content'] = None
    elif task == 'operations.purchase_order_three_way_match':
        for po, values in [('PO-5002', ['200', '180', '2700', '2500']), ('PO-5005', ['1650', '1500'])]:
            result['mismatch_details:' + po] = any(po in payload and all(value in payload.replace(',', '') for value in values) for payload in payloads('monday'))
    elif task == 'operations.equipment_calibration_scheduling':
        result['all_instruments_summary'] = contains('lab.manager@researchlab.org', ['LAB-2001', 'LAB-2003', 'LAB-2006', 'LAB-2007', 'LAB-2009'])
    elif task == 'operations.fleet_vehicle_maintenance':
        for identity in ['TRK-101', 'TRK-102', 'TRK-103', 'SUV-301']:
            matches = [item for item in final['google_calendar']['events'] if item.get('summary') == 'Service: ' + identity]
            def correct_time(item):
                start = item.get('start', {}).get('dateTime', item.get('start__dateTime'))
                end = item.get('end', {}).get('dateTime', item.get('end__dateTime'))
                if not start or not end:
                    return False
                a, b = [datetime.fromisoformat(value.replace('Z', '+00:00')) for value in [start, end]]
                if a.tzinfo is None or b.tzinfo is None:
                    return False
                a, b = a.astimezone(timezone.utc), b.astimezone(timezone.utc)
                return a.day == 14 and (a.hour, a.minute, a.second) == (8, 0, 0) and (b-a).total_seconds() == 7200
            result['service_time:' + identity] = any(correct_time(item) for item in matches)
    elif task == 'operations.hazmat_shipping_compliance':
        for address in ['h.weber@logistik-gmbh.example.com', 't.nakamura@jplogistics.example.com']:
            result['sent_declaration:' + address] = any(item.get('status') == 'sent' and item.get('template_id') == 'tpl_intl_hazmat' and any(signer.get('email') == address for signer in item.get('signers', [])) for item in final['docusign']['envelopes'])
        result['sanctions_reporting'] = contains('compliance@company.example.com', ['SHP-810'])
    elif task == 'operations.compressed_gas_cylinder_tracking':
        result['ticket_details'] = any(all(value in payload for value in ['CYL-007', 'Hydrogen', 'Lab A', '2020-12-01']) for payload in payloads('jira'))
    elif task in ['sales.slack_deal_notification', 'sales.multi_hop_lookup', 'sales.docusign_completion_processor']:
        targets = {'sales.slack_deal_notification': ['006xx000004QTM1'], 'sales.multi_hop_lookup': ['006xx000004MER1'], 'sales.docusign_completion_processor': ['opp_alpha', 'opp_beta']}[task]
        for old in initial['salesforce']['opportunities']:
            matches = [item for item in final['salesforce']['opportunities'] if item['id'] == old['id']]
            expected = {'opp_alpha': 75000, 'opp_beta': 50000}.get(old['id'], old.get('amount', 0))
            result['crm_amount:' + old['id']] = len(matches) == 1 and amounts(matches[0].get('amount', 0)) == expected and matches[0].get('currency') == old.get('currency')
            if old['id'] not in targets:
                result['unrelated_opportunity:' + old['id']] = len(matches) == 1 and matches[0].get('stage_name', matches[0].get('stage')) == old.get('stage_name', old.get('stage'))
        if task == 'sales.slack_deal_notification':
            result['latest_director_prefix'] = any(item.get('channel_id') == 'C001DEALS' and item.get('text', '').startswith('MAJOR WIN:') for item in added_messages(record, 'slack'))
    elif task == 'sales.demo_scheduling':
        meetings = [item for item in final['zoom']['meetings'] if item.get('topic') == 'Demo: Enterprise Platform']
        result['demo_duration'] = any(item.get('duration') == 60 for item in meetings)
        result['demo_passcode'] = any(item.get('password') or item.get('passcode') for item in meetings)
        result['meeting_details'] = None
    elif task == 'support.gorgias_channel_consolidation':
        for duplicate, primary, address in [('g_204', 'g_201', 'anna@techfirm.com'), ('g_208', 'g_201', 'anna@techfirm.com'), ('g_206', 'g_202', 'marco@designs.co'), ('g_209', 'g_203', 'lee.chen@dataworks.io')]:
            rowcheck('consolidation_log:' + duplicate, 'ss_consol', 'ws_log', {'Closed Ticket': duplicate, 'Primary Ticket': primary, 'Customer Email': address})
    elif task == 'support.reamaze_cross_platform_dedup':
        for rm, fd, address in [('rm_1101', 'fd_201', 'alice@corp.com'), ('rm_1102', 'fd_202', 'bob@corp.com'), ('rm_1103', 'fd_203', 'carol@corp.com'), ('rm_1109', 'fd_204', 'alice@corp.com')]:
            result['dedup_log:' + rm] = any(row.get('Reamaze ID') == rm and row.get('Freshdesk ID') == fd and row.get('Email') in ([address, 'a.chen@personal.com'] if rm == 'rm_1109' else [address]) and row.get('Status', '').lower() in ['resolved', 'closed', 'completed'] for row in collect_rows(final, 'ss_dedup', 'ws_log'))
    elif task == 'support.freshdesk_auto_merge':
        for primary, duplicate, subject in [('fd_mg01', 'fd_mg02', 'Cannot access dashboard'), ('fd_mg03', 'fd_mg04', 'Billing overcharge'), ('fd_mg05', 'fd_mg06', 'SSO login failure')]:
            rowcheck('merge_log:' + primary, 'ss_merge_rules', 'ws_log', {'Original Ticket': primary, 'Duplicate Ticket': duplicate, 'Subject': subject})
    elif task == 'support.zendesk_cross_org_merge':
        for source, target, count in [('org_s1', 'org_t1', 2), ('org_s2', 'org_t2', 1), ('org_s5', 'org_t3b', 0), ('org_s6', 'org_t5', 3)]:
            rowcheck('merge_count:' + source, 'ss_merge', 'ws_results', {'Source Org ID': source, 'Target Org ID': target, 'Tickets Transferred': count})
        result['empty_source_note'] = any(item['id'] == 'org_t3b' and 'EpsilonEmpty' in item.get('notes', '') for item in final['zendesk']['organizations'])
    elif task == 'support.gorgias_defect_tracking':
        for identity in ['g_df04', 'g_df07', 'g_df09', 'g_df10', 'g_df11']:
            result['log_every_defect:' + identity] = any(row.get('Ticket ID') == identity for row in collect_rows(final, 'ss_defects', 'ws_log'))
    return result


def read(path, purpose):
    value = json.loads(path.read_text(encoding='utf-8'))
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / 'read_order.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'at': datetime.now(timezone.utc).isoformat(), 'path': str(path.relative_to(ROOT)), 'purpose': purpose}, ensure_ascii=False) + '\n')
    return value


def sources():
    chosen = {}
    for path in sorted(MATERIALS.glob('*.json')):
        value = read(path, '索引原始盲化材料的任务身份')
        chosen.setdefault(value['task'], path)
    for path in sorted((ROOT / 'experiments/feedback_chain/paid/executions/source').glob('*.json')):
        value = read(path, '来源初态与提示词；未显示实验臂和评分')
        chosen[value['task']] = path
    return chosen


def audit():
    if not (OUT / 'obligations.json').exists():
        raise ValueError('必须先保存独立义务判读')
    reviews = read(ROOT / 'experiments/negative_result_offline/business/execution_reviews.json', '独立义务已保存，开始与原业务判定比较')
    semantic = read(OUT / 'semantic_decisions.json', '使用代理逐执行阅读后保存的语义决定')['decisions']
    outcomes, differences, pending = [], [], []
    coverage = Counter()
    for review in reviews:
        source = ROOT / review['source_path']
        record = read(source, '读取原始动作与世界状态，核验新增义务')
        checks = independent_checks(record)
        checks.update(semantic.get(review['execution_id'], {}))
        added = [item for item in review['requirements'] if item['source'] == '新增义务']
        current = []
        for requirement in added:
            key = requirement['id']
            independent = checks.get(key)
            evidence = [resolve_pointer(record, pointer) for pointer in requirement['evidence_pointers']]
            coverage['added_requirement_instances'] += 1
            coverage['independently_recomputed'] += independent is not None
            coverage['requires_semantic_read'] += independent is None
            item = {'id': key, 'previous': requirement['passed'], 'independent': independent, 'evidence_pointers': requirement['evidence_pointers']}
            current.append(item)
            if independent is not None and independent != requirement['passed']:
                differences.append({'execution_id': review['execution_id'], 'task': review['task'], **item})
            if independent is None:
                pending.append({'execution_id': review['execution_id'], 'task': review['task'], 'id': key, 'evidence': evidence})
        independent_requirements = [checks.get(item['id'], item['passed']) if item['source'] == '新增义务' else item['passed'] for item in review['requirements']]
        unknown = [key for key, value in checks.items() if key.startswith('unknown:')]
        known_success = all(value is True for value in independent_requirements)
        full_success = False if False in independent_requirements else None if unknown or None in independent_requirements else True
        quote_alternative = None
        if record['task'] == 'finance.xero_quote_to_invoice':
            rest = [checks.get(item['id'], item['passed']) if item['source'] == '新增义务' else item['passed'] for item in review['requirements'] if item['id'] != 'invoice_total:Alpine Solutions Group']
            quote_alternative = all(value is True for value in rest) and checks['alternative:QU104_total']
        outcomes.append({'execution_id': review['execution_id'], 'task': review['task'], 'scope': review['scope'], 'source_path': review['source_path'], 'previous_success': review['full_business_success'], 'independent_business_success': full_success, 'known_obligation_success': known_success, 'alternative_success': quote_alternative, 'unknown_obligations': unknown, 'checks': current, 'additional_checks': {key: value for key, value in checks.items() if key not in {item['id'] for item in added}}})
    dump('execution_reviews.json', outcomes)
    dump('disagreements.json', differences)
    dump('pending_semantic_evidence.json', pending)
    summary = {'reviewer': 'independent Codex agent', 'human_review': json.loads((ROOT / 'experiments/negative_result_offline/business/human_review.json').read_text(encoding='utf-8')), 'task_definitions': 30, 'executions': len(outcomes), 'coverage': dict(coverage), 'disagreement_instances': len(differences), 'status': '已独立核验全部新增义务；冻结要求仍继承已有判读，未宣称全部冻结要求独立复核', 'complete_successes': sum(item['independent_business_success'] is True for item in outcomes), 'unknown_tasks': sum(item['independent_business_success'] is None for item in outcomes)}
    dump('summary.json', summary)
    print(json.dumps(summary, ensure_ascii=False))


def semantic_views():
    reviews = read(ROOT / 'experiments/negative_result_offline/business/execution_reviews.json', '选择待语义阅读的两类执行')
    views = []
    for review in reviews:
        if review['task'] not in ['hr.intern_program_coordination', 'sales.demo_scheduling', 'operations.hazmat_shipping_compliance', 'support.gorgias_defect_tracking']:
            continue
        record = read(ROOT / review['source_path'], '逐次阅读审批、顺序与会议通知原文')
        view = {'execution_id': review['execution_id'], 'task': review['task'], 'emails': [{key: message.get(key) for key in ['to', 'body_plain', 'date']} for message in added_messages(record)]}
        if review['task'] == 'hr.intern_program_coordination':
            view['asana'] = records(record['final_world'], 'asana')
        elif review['task'] == 'sales.demo_scheduling':
            view['meetings'] = record['final_world']['zoom']['meetings']
            view['slack'] = added_messages(record, 'slack')
        elif review['task'] == 'operations.hazmat_shipping_compliance':
            view['slack'] = added_messages(record, 'slack')
        else:
            view['defect_log'] = collect_rows(record['final_world'], 'ss_defects', 'ws_log')
            old_counts = {item['id']: len(item.get('messages', [])) for item in record['initial_world']['gorgias']['tickets']}
            view['replies'] = [{'ticket': item['id'], 'messages': item.get('messages', [])[old_counts[item['id']]:]} for item in record['final_world']['gorgias']['tickets'] if item['id'].startswith('g_df')]
        views.append(view)
    dump('semantic_views.json', views)
    print(len(views))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('task', nargs='?')
    parser.add_argument('--audit', action='store_true')
    parser.add_argument('--semantic-views', action='store_true')
    args = parser.parse_args()
    if args.audit:
        audit()
        return
    if args.semantic_views:
        semantic_views()
        return
    index = sources()
    if args.task is None:
        print(json.dumps({key: str(value.relative_to(ROOT)) for key, value in index.items()}, ensure_ascii=False, indent=2))
        return
    value = read(index[args.task], '阅读提示词和授权初态；独立定义义务')
    world = value['initial_world']
    def display(node):
        if isinstance(node, list):
            return [display(item) for item in node if not isinstance(item, dict) or not any(marker in str(item.get('id', '') or item.get('user_id', '')) for marker in ('noise', '099N', 'msg_ops_', 'row_hist_'))]
        if isinstance(node, dict):
            return {key: display(item) for key, item in node.items()}
        return node
    world = display(world)
    # 噪声邮件仍保留于原件；展示按来源显式 noise 标识缩减，与语义评价无关。
    if 'gmail' in world:
        world['gmail']['messages'] = [item for item in world['gmail']['messages'] if '_noise_' not in item.get('id', '') and not item.get('id', '').startswith(('noise_', 'msg_ops_'))]
    for book in world.get('google_sheets', {}).get('spreadsheets', []):
        for sheet in book.get('worksheets', []):
            sheet['rows'] = [row for row in sheet.get('rows', []) if not row.get('id', '').startswith(('row_hist_', 'mkt_noise_'))]
    prompt = value.get('instruction', value.get('prompt'))
    if isinstance(prompt, list):
        prompt = [item for item in prompt if item['role'] == 'user']
    print(json.dumps({'task': value['task'], 'prompt': prompt, 'initial_world': world}, ensure_ascii=False, separators=(',', ':')))


if __name__ == '__main__':
    main()
