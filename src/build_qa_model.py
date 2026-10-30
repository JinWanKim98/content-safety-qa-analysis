"""Build the actual normalized DICES model and reconcile frozen metrics.

The CSV is read independently again for Python scorecards; SQL reads normalized
tables. Raw data, prior audit DB and the Chapter 04 contract are preserved.
"""

from qa_metrics import read_sql
from reproducibility import snapshot,check_snapshot,verify_sources
import argparse
import ast
import csv
import hashlib
import json
import math
import re
import sqlite3
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path

from intake_dices import combined_rating
from prepare_sources import file_hash
from qa_metrics import Rating,validate,scorecard,item_metrics,macro_item_metrics,expression
from quality_audit import parse_reason,Q1_OK

ROOT=Path(__file__).resolve().parents[1]
RAW=ROOT/'data/raw/dices350/diverse_safety_adversarial_dialog_350.csv'
SCHEMA=ROOT/'analysis.sql'
SCORE_SQL=ROOT/'analysis.sql'
ITEM_SQL=ROOT/'analysis.sql'
PANELS={'all_released':'ratings','published_104_panel':'ratings_published_104','q1_comprehension_only':'ratings_q1'}
ITEM_FIELDS=['context','response','safety_gold','degree_of_harm','harm_type','safety_gold_reason']
RATER_FIELDS=['rater_gender','rater_race','rater_raw_race','rater_age','rater_education']
Q1_DOMAIN={Q1_OK,'It is nonsensical (repetitive, incomprehensible, gibberish)',
           'I am not familiar with the topic of the conversation','It is in a language other than English'}


def raw_ratings(rows):
    return [Rating(r['id'],r['item_id'],r['rater_id'],r['safety_gold'],r['Q_overall']) for r in rows]


def canonical_values(row,fields):
    return json.dumps([row[k] for k in fields],ensure_ascii=False,separators=(',',':'))


def create_model(conn,rows,fields,excluded=(),source_sha='synthetic',contract_sha='synthetic',revision='synthetic'):
    rows=list(rows);validate(raw_ratings(rows));excluded=set(excluded)
    safety=[k for k in fields if k.startswith(('Q2_','Q3_','Q4_','Q5_','Q6_')) or k=='Q_overall']
    granular=[k for k in safety if not k.endswith('_overall')]
    if len(safety)!=23 or len(granular)!=19:raise ValueError('Unexpected safety question structure')
    items={};raters={}
    for n,row in enumerate(rows,1):
        if any(k not in row or row[k] is None for k in fields):raise ValueError('Malformed source record')
        if row['Q1_whole_conversation_evaluation'] not in Q1_DOMAIN:raise ValueError('Invalid Q1 domain')
        if any(row[k] not in {'Yes','No','Unsure'} for k in safety):raise ValueError('Invalid safety answer')
        for target,columns in {'Q_overall':granular,**{f'Q{x}_{family}_overall':[k for k in granular if k.startswith(f'Q{x}_')]
                for x,family in [('2','harmful_content'),('3','bias'),('6','policy_guidelines')]}}.items():
            if row[target]!=combined_rating(row[k] for k in columns):raise ValueError('Stored aggregate does not match granular answers')
        for mapping,key,columns,label in [(items,row['item_id'],ITEM_FIELDS,'item'),(raters,row['rater_id'],RATER_FIELDS,'rater')]:
            values=tuple(row[k] for k in columns)
            if key in mapping and mapping[key][1]!=values:raise ValueError('Conflicting '+label+' metadata')
            mapping.setdefault(key,(n,values))
    conn.executescript(read_sql('05_create_qa_model.sql'))
    contract=json.loads((ROOT/'docs/04_qa_metric_contract.json').read_text())
    conn.execute('INSERT INTO model_config VALUES(1,?,?,?,?,?)',
                 (source_sha,contract_sha,contract['version'],revision,json.dumps(fields)))
    # Preserve all 41 values in source order. The immutable CSV preserves bytes;
    # these arrays preserve logical values without repeating field names per row.
    source=[]
    for n,row in enumerate(rows,1):
        raw=canonical_values(row,fields)
        source.append((n,row['id'],raw,hashlib.sha256(raw.encode()).hexdigest()))
    conn.executemany('INSERT INTO source_record VALUES(?,?,?,?)',source)
    for item,(n,values) in items.items():
        context,response,gold,severity,topics,reason=values
        text_hash=hashlib.sha256(json.dumps([context,response],ensure_ascii=False).encode()).hexdigest()
        conn.execute('INSERT INTO dim_item VALUES(?,?,?,?,?)',(item,context,response,text_hash,n))
        status,codes=parse_reason(reason)
        conn.execute('INSERT INTO fact_expert_reference VALUES(?,?,?,?,?,?,?)',(item,gold,severity,topics,reason,status,n))
        for topic in sorted({t.strip() for t in topics.split(',') if t.strip()}):
            conn.execute('INSERT OR IGNORE INTO dim_topic VALUES(?)',(topic,))
            conn.execute('INSERT INTO bridge_item_topic VALUES(?,?)',(item,topic))
        if codes is not None:
            for index,code in enumerate(codes):
                conn.execute('INSERT OR IGNORE INTO dim_reason_code VALUES(?)',(code,))
                conn.execute('INSERT INTO fact_expert_reason VALUES(?,?,?)',(item,index,code))
    for rater,(n,values) in raters.items():
        conn.execute('INSERT INTO dim_rater VALUES(?,?,?,?,?,?,?,?)',(rater,*values,int(rater in excluded),n))
    questions=[]
    for position,column in enumerate(fields,1):
        if column not in safety:continue
        group=column.split('_')[0] if column!='Q_overall' else 'overall'
        kind='aggregate' if column.endswith('_overall') else 'granular'
        alias='Q6_policy_guidelines_other_type' if column=='Q6_policy_guidelines_other' else None
        conn.execute('INSERT INTO dim_question VALUES(?,?,?,?,?,?)',(position,column,group,kind,position,alias))
        questions.append((position,column))
    judgments=[];answers=[]
    for n,row in enumerate(rows,1):
        duration=int(row['answer_time_ms']);timestamp=int(row['answer_timestamp'])
        judgments.append((n,row['id'],row['item_id'],row['rater_id'],row['phase'],duration,timestamp,
                          row['Q1_whole_conversation_evaluation'],int(row['Q1_whole_conversation_evaluation']==Q1_OK),row['Q_overall']))
        answers.extend((n,q,row[column]) for q,column in questions)
        if len(judgments)>=1000:
            conn.executemany('INSERT INTO fact_judgment VALUES(?,?,?,?,?,?,?,?,?,?)',judgments)
            conn.executemany('INSERT INTO fact_answer VALUES(?,?,?)',answers)
            judgments.clear();answers.clear()
    if judgments:
        conn.executemany('INSERT INTO fact_judgment VALUES(?,?,?,?,?,?,?,?,?,?)',judgments)
        conn.executemany('INSERT INTO fact_answer VALUES(?,?,?)',answers)
    conn.execute('CREATE VIEW item_metrics AS '+read_sql('04_item_metrics.sql').rstrip().rstrip(';'))
    conn.commit()
    return len(rows)


def model_checks(conn,expected_records,expected_items=None,expected_raters=None):
    tables=['source_record','dim_item','dim_rater','fact_expert_reference','dim_topic','bridge_item_topic',
            'dim_reason_code','fact_expert_reason','dim_question','fact_judgment','fact_answer']
    counts={t:conn.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in tables}
    if counts['source_record']!=expected_records or counts['fact_judgment']!=expected_records:
        raise AssertionError('Source/judgment count differs')
    if counts['fact_expert_reference']!=counts['dim_item']:raise AssertionError('Expert reference cardinality')
    if expected_items is not None and counts['dim_item']!=expected_items:raise AssertionError('Item count differs')
    if expected_raters is not None and counts['dim_rater']!=expected_raters:raise AssertionError('Rater count differs')
    if counts['dim_question']!=23 or counts['fact_answer']!=23*expected_records:
        raise AssertionError('Missing or multiplied safety answers')
    if conn.execute('SELECT COUNT(*) FROM ratings').fetchone()[0]!=expected_records:
        raise AssertionError('Rating join changed cardinality')
    if conn.execute('SELECT COUNT(*) FROM rating_reporting').fetchone()[0]!=expected_records:
        raise AssertionError('Reporting join changed cardinality')
    if list(conn.execute('PRAGMA foreign_key_check')):raise AssertionError('Foreign-key violation')
    if conn.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise AssertionError('SQLite integrity')
    bad_q1=conn.execute('SELECT COUNT(*) FROM fact_judgment WHERE q1_ok != (q1_value=?)',(Q1_OK,)).fetchone()[0]
    if bad_q1:raise AssertionError('Q1 flag changed')
    invalid_reason_rows=conn.execute("SELECT COUNT(*) FROM fact_expert_reason r JOIN fact_expert_reference e USING(item_id) WHERE e.reason_status NOT IN ('valid_empty','valid_nonempty')").fetchone()[0]
    if invalid_reason_rows:raise AssertionError('Invented parsed reason codes')
    # Independent SQL reconstruction of each stored aggregate, including Q_overall.
    mismatches={}
    for target,group in [('Q2_harmful_content_overall','Q2'),('Q3_bias_overall','Q3'),('Q6_policy_guidelines_overall','Q6'),('Q_overall',None)]:
        condition="q.question_kind='granular'"+('' if group is None else ' AND q.question_group=?')
        parameters=[] if group is None else [group]
        query='''WITH reconstructed AS (
          SELECT a.source_row,CASE WHEN MAX(a.answer='Yes') THEN 'Yes'
          WHEN MAX(a.answer='Unsure') THEN 'Unsure' ELSE 'No' END AS expected
          FROM fact_answer a JOIN dim_question q USING(question_id) WHERE '''+condition+'''
          GROUP BY a.source_row)
          SELECT COUNT(*) FROM reconstructed r JOIN fact_answer a USING(source_row)
          JOIN dim_question q USING(question_id) WHERE q.source_column=? AND a.answer!=r.expected'''
        mismatches[target]=conn.execute(query,parameters+[target]).fetchone()[0]
    if any(mismatches.values()):raise AssertionError('SQL aggregate reconstruction differs')
    stored_mismatch=conn.execute("SELECT COUNT(*) FROM fact_judgment j JOIN fact_answer a USING(source_row) JOIN dim_question q USING(question_id) WHERE q.source_column='Q_overall' AND a.answer!=j.overall").fetchone()[0]
    if stored_mismatch:raise AssertionError('Stored Q_overall copies differ')
    return {'table_counts':counts,'reporting_rows':expected_records,'foreign_key_violations':0,
            'sqlite_integrity':'ok','sql_aggregate_mismatches':mismatches,'all_source_records_retained':True}


def panel_python_rows(rows,excluded,panel):
    if panel=='all_released':return rows
    if panel=='published_104_panel':return [r for r in rows if r['rater_id'] not in excluded]
    if panel=='q1_comprehension_only':return [r for r in rows if r['Q1_whole_conversation_evaluation']==Q1_OK]
    raise ValueError('Unknown panel')


def equal(a,b):
    return (a is None and b is None) or (a is not None and b is not None and math.isclose(a,b,rel_tol=1e-12,abs_tol=1e-12))


def reconcile(conn,independent_rows,excluded):
    contract=json.loads((ROOT/'docs/04_qa_metric_contract.json').read_text());results={};comparison_rows=[]
    for panel,view in PANELS.items():
        selected=raw_ratings(panel_python_rows(independent_rows,excluded,panel))
        py=scorecard(selected);sql=dict(conn.execute(read_sql('04_reference_scorecard.sql').replace('FROM ratings','FROM '+view)).fetchone())
        assert all(value==sql[key] for key,value in py['counts'].items()),(panel,'counts')
        for metric in contract['metrics']:
            key=metric['id'];value=py['metrics'][key]
            assert equal(value['value'],sql[key]),(panel,key)
            assert value['numerator']==expression(metric['numerator'],sql)
            assert value['denominator']==expression(metric['denominator'],sql)
            comparison_rows.append({'panel':panel,'metric_id':key,'numerator':value['numerator'],
                                    'denominator':value['denominator'],'sql_value':sql[key],
                                    'python_value':value['value'],'equal':True})
        items=[dict(r) for r in conn.execute(read_sql('04_item_metrics.sql').replace('FROM ratings','FROM '+view))]
        py_items=item_metrics(selected)
        assert len(items)==len(py_items)
        for a,b in zip(items,py_items):
            assert a.keys()==b.keys() and all(equal(a[k],b[k]) if k in ['I01','I02'] else a[k]==b[k] for k in a),(panel,'item values')
        macro=macro_item_metrics(selected)
        for key in ['I01','I02']:
            defined=[r[key] for r in items if r[key] is not None]
            assert macro[key]['defined_items']==len(defined)
            assert equal(macro[key]['value'],sum(defined)/len(defined) if defined else None)
        for key,sqlkeys in [('B01',['agreement_lower','agreement_upper']),('B02',['detection_lower','detection_upper'])]:
            assert all(equal(a,sql[b]) for a,b in zip(py['logical_resolution_bounds'][key],sqlkeys))
        results[panel]={'counts':py['counts'],'metrics':py['metrics'],'logical_resolution_bounds':py['logical_resolution_bounds'],
                        'item_metrics_checked':len(items),'macro_item_metrics':macro,'all_python_sql_equal':True}
    return results,comparison_rows


def load_csv(path):
    with path.open(encoding='utf-8-sig',newline='') as stream:
        reader=csv.DictReader(stream);fields=reader.fieldnames;rows=list(reader)
    if any(None in r or any(v is None for v in r.values()) for r in rows):raise ValueError('Malformed CSV')
    return fields,rows


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'data/processed/project02.sqlite')
    args=parser.parse_args();target=args.output.resolve()
    if not target.is_relative_to((ROOT/'data/processed').resolve()):raise ValueError('Keep the private model under data/processed')
    building=target.with_name(target.name+'.building')
    if target.exists() or building.exists():raise FileExistsError('Preserving the existing model/build. Choose a new --output filename.')
    manifest=verify_sources()
    contract_path=ROOT/'docs/04_qa_metric_contract.json';contract=json.loads(contract_path.read_text())
    protected=snapshot(model=False)
    check_snapshot(protected)
    assert file_hash(RAW)==contract['source_sha256']
    fields,rows=load_csv(RAW)
    assert fields==manifest['csv_columns'] and len(rows)==43050
    text=(ROOT/'references/google_dices/350_README.md').read_text()
    excluded=set(ast.literal_eval(re.search(r'removed_raters_350 = (\[[^\n]+\])',text).group(1)))
    target.parent.mkdir(parents=True,exist_ok=True)
    print('Building source records, dimensions and 23 safety answers per judgment...',flush=True)
    conn=sqlite3.connect(building)
    try:
        create_model(conn,rows,fields,excluded,contract['source_sha256'],file_hash(contract_path),manifest['revision'])
        conn.row_factory=sqlite3.Row
        checks=model_checks(conn,len(rows),350,123)
        for stored,row in zip(conn.execute('SELECT * FROM source_record ORDER BY source_row'),rows):
            values=json.loads(stored['raw_json']);assert dict(zip(fields,values))==row
            assert hashlib.sha256(stored['raw_json'].encode()).hexdigest()==stored['raw_values_sha256']
        # Re-read original CSV for the independent Python path, not normalized rows.
        print('Re-reading raw CSV for independent Python/SQL reconciliation...',flush=True)
        second_fields,independent_rows=load_csv(RAW);assert second_fields==fields
        panels,reconciliation=reconcile(conn,independent_rows,excluded)
    finally:
        conn.close()
    verify=sqlite3.connect(building.as_uri()+'?mode=ro',uri=True)
    try:
        model_checks(verify,len(rows),350,123)
    finally:
        verify.close()
    assert all(file_hash(ROOT/p)==sha for p,sha in protected.items())
    building.rename(target)
    result={'chapter':5,'created_at_utc':datetime.now(timezone.utc).isoformat(),'data_kind':'actual_full_DICES350_normalized_model',
            'model_path':str(target.relative_to(ROOT)),'model_bytes':target.stat().st_size,'model_sha256':file_hash(target),
            'contract_version':contract['version'],'contract_sha256':file_hash(contract_path),
            'source_revision':manifest['revision'],'source_and_configuration_fingerprints':{str(p.relative_to(ROOT)):h for p,h in protected.items()},
            'all_protected_inputs_unchanged':True,'source_logical_values_roundtrip_equal':True,
            'checks':checks,'panels':panels,'reconciled_rating_metric_rows':len(reconciliation),
            'all_panels_recomputed_from_raw':True,'all_full_source_python_sql_equal':True,
            'individual_or_demographic_rankings_produced':False,'confidence_intervals_computed':False,
            'code_hashes':{name:file_hash(ROOT/name) for name in ['src/build_qa_model.py','analysis.sql',
                        'src/qa_metrics.py','analysis.sql','analysis.sql']}}
    (ROOT/'reports/05_model_validation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    with (ROOT/'reports/05_metric_reconciliation.csv').open('w',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(reconciliation[0]));writer.writeheader();writer.writerows(reconciliation)
    print(json.dumps({'table_counts':checks['table_counts'],'rating_metrics_reconciled':len(reconciliation),
                      'panels':3,'base_six_cells':{k:panels['all_released']['counts'][k] for k in ['tp','fn','uy','fp','tn','un']},
                      'model_sha256':result['model_sha256'],'protected_inputs_unchanged':True},indent=2))


if __name__=='__main__':main()
