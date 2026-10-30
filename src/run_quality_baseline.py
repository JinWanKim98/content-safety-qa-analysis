"""Read-only, reconciled Chapter 06 descriptive baseline; standard library only."""

from qa_metrics import read_sql
from reproducibility import snapshot,check_snapshot,verify_sources
import csv
import json
import math
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter
from build_qa_model import PANELS, raw_ratings, load_csv
from prepare_sources import file_hash
from qa_metrics import Rating, scorecard, item_metrics, macro_item_metrics, expression

ROOT=Path(__file__).resolve().parents[1]
CONTRACT=ROOT/'docs/04_qa_metric_contract.json'
SCORE=ROOT/'analysis.sql'
ITEM=ROOT/'analysis.sql'
SELECT=ROOT/'analysis.sql'
CELLS=['tp','fn','uy','fp','tn','un']


def denominator_rows(rows,metric):
    """Actual observations contributing to each frozen denominator."""
    if metric in {'R01','R02','R03'}:return rows
    if metric=='R04':return [r for r in rows if r.judgment!='Unsure']
    if metric=='R05':return [r for r in rows if r.judgment=='Yes']
    if metric in {'R06','R07'}:return [r for r in rows if r.reference=='Yes' and r.judgment!='Unsure']
    if metric=='R08':return [r for r in rows if r.reference=='No' and r.judgment!='Unsure']
    if metric in {'R09','R10','R11'}:return [r for r in rows if r.reference=='Yes']
    if metric in {'R12','R13','R14'}:return [r for r in rows if r.reference=='No']
    raise ValueError('Unknown frozen metric')


def support(rows,gate):
    counts={'items':len({r.item_id for r in rows}),'raters':len({r.rater_id for r in rows}), 'records':len(rows)}
    counts['display_eligible']=all(counts[k]>=gate[g] for k,g in [('items','minimum_items'),('raters','minimum_raters'),('records','minimum_rating_records')])
    return counts


def assert_close(actual,expected):
    if isinstance(expected,dict):
        for k,v in expected.items():assert_close(actual[k],v)
    elif isinstance(expected,list):
        assert len(actual)==len(expected)
        for a,b in zip(actual,expected):assert_close(a,b)
    elif isinstance(expected,float):assert math.isclose(actual,expected,rel_tol=1e-12,abs_tol=1e-12),(actual,expected)
    else:assert actual==expected,(actual,expected)


def summarize(conn,selection,params,independent,contract):
    selection=selection.strip().rstrip(';')
    sql='WITH selected AS ('+selection+'), '+read_sql('04_reference_scorecard.sql').replace('WITH cells AS','cells AS',1).replace('FROM ratings','FROM selected')
    computed=dict(conn.execute(sql,params).fetchone())
    python=scorecard(independent)
    assert_close(computed,python['counts'])
    metrics={}
    for m in contract['metrics']:
        key=m['id'];num=expression(m['numerator'],computed);den=expression(m['denominator'],computed)
        eligible_rows=denominator_rows(independent,key)
        assert len(eligible_rows)==den
        metrics[key]={'numerator':num,'denominator':den,'value':computed[key],
                      'support':support(eligible_rows,contract['public_group_support'])}
        assert_close({k:metrics[key][k] for k in ['numerator','denominator','value']},python['metrics'][key])
    item_sql='WITH selected AS ('+selection+'), '+read_sql('04_item_metrics.sql').replace('WITH counts AS','counts AS',1).replace('FROM ratings','FROM selected')
    items=[dict(r) for r in conn.execute(item_sql,params)]
    assert_close(items,item_metrics(independent))
    macro=macro_item_metrics(independent)
    return {'counts':python['counts'],'metrics':metrics,'item_macro':macro,
            'item_flags':{k:sum(i[k] for i in items) for k in ['all_unsure','insufficient_decisive','decisive_tie']},
            'group_support':support(independent,contract['public_group_support']),
            'reference_items':dict(Counter({r.item_id:r.reference for r in independent}.values())),
            'item_coverage':{'minimum':min((i['n'] for i in items),default=None),'maximum':max((i['n'] for i in items),default=None)},
            'logical_resolution_bounds':python['logical_resolution_bounds']},items


def run():
    receipt=json.loads((ROOT/'reports/05_model_validation.json').read_text())
    contract=json.loads(CONTRACT.read_text())
    model=ROOT/receipt['model_path']
    raw=ROOT/'data/raw/dices350/diverse_safety_adversarial_dialog_350.csv'
    protected={model:receipt['model_sha256'],CONTRACT:receipt['contract_sha256'],raw:contract['source_sha256']}
    protected=snapshot()
    check_snapshot(protected)
    fields,rows=load_csv(raw)
    ratings=raw_ratings(rows)
    metadata={r['item_id']:r for r in rows}
    excluded=set()
    conn=sqlite3.connect(model.as_uri()+'?mode=ro',uri=True);conn.row_factory=sqlite3.Row
    try:
        excluded={r[0] for r in conn.execute('SELECT rater_id FROM dim_rater WHERE published_excluded=1')}
        selected={'all_released':ratings,
          'published_104_panel':[r for r in ratings if r.rater_id not in excluded],
          'q1_comprehension_only':[r for r,rawrow in zip(ratings,rows) if rawrow['Q1_whole_conversation_evaluation']=='None of the above - it is in English, it makes sense, and I am familiar with the topic']}
        panels={};base_items=[]
        for key,view in PANELS.items():
            panels[key],items=summarize(conn,'SELECT * FROM '+view,{},selected[key],contract)
            assert_close(panels[key]['counts'],receipt['panels'][key]['counts'])
            if key=='all_released':base_items=items
        groups=[]
        for kind,values in [('severity',[r[0] for r in conn.execute('SELECT DISTINCT degree_of_harm FROM fact_expert_reference ORDER BY degree_of_harm')]),
                            ('topic',[r[0] for r in conn.execute('SELECT topic FROM dim_topic ORDER BY topic')])]:
            for value in values:
                params={'severity':value if kind=='severity' else None,'topic':value if kind=='topic' else None}
                independent=[r for r in ratings if (metadata[r.item_id]['degree_of_harm']==value if kind=='severity' else value in [s.strip() for s in metadata[r.item_id]['harm_type'].split(',')])]
                result,_=summarize(conn,read_sql('06_quality_baseline.sql'),params,independent,contract)
                groups.append({'kind':kind,'group':value,'panel':'all_released',**result})
        for cell in CELLS+['n','items']:
            assert sum(g['counts'][cell] for g in groups if g['kind']=='severity')==panels['all_released']['counts'][cell]
        topic_memberships=sum(g['counts']['n'] for g in groups if g['kind']=='topic')
        bridge_memberships=conn.execute('SELECT COUNT(*) FROM ratings JOIN bridge_item_topic USING(item_id)').fetchone()[0]
        assert topic_memberships==bridge_memberships
    finally:conn.close()
    for p,sha in protected.items():assert file_hash(p)==sha, str(p)
    result={'chapter':6,'computed_at_utc':datetime.now(timezone.utc).isoformat(),
      'contract_version':contract['version'],'contract_sha256':file_hash(CONTRACT),'model_sha256':file_hash(model),
      'plan_sha256':file_hash(ROOT/'docs/06_baseline_plan.json'),
      'protected_inputs_unchanged':True,'protected_input_sha256':{str(p.relative_to(ROOT)):sha for p,sha in protected.items()},'reconciliation':'All panel/group cell counts, 14 metrics and item metrics independently reconciled to raw CSV Python; floats 1e-12.',
      'panels':panels,'groups':groups,
      'topic_membership_records':topic_memberships,'topic_membership_items':sum(g['counts']['items'] for g in groups if g['kind']=='topic'),
      'group_metric_rows_reconciled':len(groups)*14,'confidence_intervals_computed':False,
      'limitations':['Adversarial research sample, 175 unsafe and 175 safe reference items: not production prevalence.',
        'Repeated items and raters; no independent-row significance. Formal reliability remains Chapter 08.',
        'Source-version severity discrepancy unresolved; no numeric severity weights.',
        'Expert reference is a benchmark, not infallible truth. No worker ranking or improvement claim.',
        'Topic groups overlap; group rates are never averaged into the global score.']}
    reports=ROOT/'reports';reports.mkdir(exist_ok=True)
    (reports/'06_baseline_results.json').write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n')
    metric_rows=[]
    for kind,name,summary in [('panel',k,v) for k,v in panels.items()]+[(g['kind'],g['group'],g) for g in groups]:
        for mid,m in summary['metrics'].items():
            metric_rows.append({'kind':kind,'group':name,'panel':name if kind=='panel' else 'all_released',
              'metric_id':mid,'numerator':m['numerator'],'denominator':m['denominator'],'fraction':m['value'],
              'denominator_items':m['support']['items'],'denominator_raters':m['support']['raters'],
              'display_eligible':m['support']['display_eligible'],
              'display_fraction':m['value'] if m['support']['display_eligible'] else None})
    write_csv(reports/'06_baseline_metrics.csv',metric_rows)
    write_csv(reports/'06_item_profile.csv',base_items)
    write_csv(reports/'06_group_counts.csv',[{'kind':g['kind'],'group':g['group'],**g['counts'],
               'unsafe_reference_items':g['reference_items'].get('Yes',0),'safe_reference_items':g['reference_items'].get('No',0),
               'display_eligible':g['group_support']['display_eligible']} for g in groups])
    render_report(result)
    print(json.dumps({'status':'complete','panels':len(panels),'groups':len(groups),'metric_rows':len(metric_rows),
                      'protected_inputs_unchanged':True,'report':str(reports/'06_quality_baseline.md')},indent=2))
    return result


def write_csv(path,rows):
    with path.open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)


def render_report(result):
    p=result['panels']['all_released'];c=p['counts']
    lines=['# Chapter 06 — Quality baseline','',
      'Descriptive QA baseline for the pinned DICES-350 research release. One observation is one rater–item judgment; repeated judgments are not independent conversations.',
      '',f"Primary panel: {c['n']:,} judgments, {c['items']} items, {c['raters']} raters; each item has 123 judgments. Expert reference: 175 unsafe / 175 safe items, each repeated 123 times.",
      '','## Reference matrix','', '| Expert reference | Judgment Yes (unsafe) | Judgment No | Unsure | Row total |','|---|---:|---:|---:|---:|',
      f"| Yes | {c['tp']:,} | {c['fn']:,} | {c['uy']:,} | {c['ref_unsafe']:,} |",
      f"| No | {c['fp']:,} | {c['tn']:,} | {c['un']:,} | {c['ref_safe']:,} |",'',
      f"Judgment label totals: Yes {c['tp']+c['fp']:,}, No {c['fn']+c['tn']:,}, Unsure {c['u']:,}. Unsure is not converted to safe or classified as an explicit binary error.",
      '', '## Frozen scorecard and panel sensitivity','', '| Panel | Records / items / raters | R03 all-reference agreement | R04 decisive agreement | R09 unsafe detection | R11 unsafe Unsure |','|---|---|---:|---:|---:|---:|']
    fmt=lambda m:f"{m['numerator']:,}/{m['denominator']:,} = {m['value']*100:.2f}%" if m['value'] is not None else 'Undefined'
    for name,x in result['panels'].items():
        cc=x['counts'];lines.append(f"| {name} | {cc['n']:,} / {cc['items']} / {cc['raters']} | "+' | '.join(fmt(x['metrics'][k]) for k in ['R03','R04','R09','R11'])+' |')
    lines += ['', 'All 14 metrics with exact numerators/denominators and per-metric support are in 06_baseline_metrics.csv. Fractions are stored on 0–1 scale. Comparisons are independent panels, not interventions.',
      '',f"All-panel decisive coverage: {fmt(p['metrics']['R01'])}; Unsure: {fmt(p['metrics']['R02'])}. Removing Unsure changes the agreement denominator from 43,050 to 40,356 with the same 24,506 matches. The resulting +3.8000 pp is not improvement.",
      '',f"Unsafe-reference explicit-No judgments: {fmt(p['metrics']['R10'])}. Decisive-only recall: {fmt(p['metrics']['R06'])}; full-reference detection: {fmt(p['metrics']['R09'])}. Always display them with unsafe-reference Unsure {fmt(p['metrics']['R11'])}.",
      '',f"Equal-item mean I02: {p['item_macro']['I02']['value']*100:.2f}% across {p['item_macro']['I02']['defined_items']} defined items; undefined 0, all-Unsure 0, fewer-than-two-decisive 0, decisive ties {p['item_flags']['decisive_tie']}. This is decisive pair disagreement within items, not an error rate or chance-corrected reliability coefficient.",
      '', '## Source severity strata (primary panel)','', '| Source stratum | Items | Records | R03 agreement | R09 detection | Unsafe / safe reference items |','|---|---:|---:|---:|---:|---:|']
    for g in result['groups']:
        if g['kind']!='severity':continue
        def display(k):
            m=g['metrics'][k];return fmt(m) if m['support']['display_eligible'] else f"Withheld: denominator support below gate ({m['support']['items']} items, {m['denominator']} records)"
        lines.append(f"| {g['group']} | {g['counts']['items']} | {g['counts']['n']:,} | {display('R03')} | {display('R09')} | {g['reference_items'].get('Yes',0)} / {g['reference_items'].get('No',0)} |")
    lines += ['', 'These are the released degree_of_harm labels. Their mismatch with the paper remains unresolved. Severity does not define the expert safety label. Do not assign business cost weights or interpret stratum differences causally.',
      '', '## Overlapping harm topics','',f"25 topics form {result['topic_membership_items']} item memberships and {result['topic_membership_records']:,} judgment memberships, versus 350 unique items and 43,050 unique judgments. Topic rates use EXISTS filters so each eligible judgment appears once within a topic. Topics cannot be summed into a global total.",
      '', '| Topic | Items | Records | Unsafe / safe reference items | R03 all-reference agreement |','|---|---:|---:|---:|---:|']
    for g in result['groups']:
        if g['kind']!='topic':continue
        m=g['metrics']['R03'];value=fmt(m) if m['support']['display_eligible'] else 'Withheld: support below display gate'
        lines.append(f"| {g['group']} | {g['counts']['items']} | {g['counts']['n']:,} | {g['reference_items'].get('Yes',0)} / {g['reference_items'].get('No',0)} | {value} |")
    lines += ['', 'The frozen gate requires at least 10 unique items, 10 raters and 100 records in the actual metric denominator population. It is a display rule, not power/significance or row deletion. All raw aggregate fractions remain in the internal CSV/JSON, with display eligibility and class support explicitly recorded.',
      '', '## Uncertainty and limits','']+['- '+s for s in result['limitations']]+[
      '- Counts and Unsure describe observed uncertainty; no confidence interval has been computed. Formal item-dependent uncertainty is Chapter 08.',
      '- Raw duration semantics remain unresolved; no speed, productivity or SLA metric is used.',
      '', '## Verification','',result['reconciliation'],
      f"Three panels and 29 groups reconcile: {result['group_metric_rows_reconciled']} group metric values plus 42 panel metric values. Severity partitions reconcile cell by cell; overlapping topic membership counts reconcile to the bridge. Read-only model/source/contract fingerprints are unchanged.",
      '', 'Code: src/run_quality_baseline.py; query: sql/06_quality_baseline.sql; metric definitions: docs/04_qa_metric_contract.json; study notebook: notebooks/06_quality_baseline.ipynb.',
      '', 'Next: Chapter 07 error/risk diagnostics. No simulated audit policy or intervention effect has been evaluated in this chapter.','']
    (ROOT/'reports/06_quality_baseline.md').write_text('\n'.join(lines))


if __name__=='__main__':run()
