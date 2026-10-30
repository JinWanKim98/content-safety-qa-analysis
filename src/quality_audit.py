"""Preserve DICES raw data; audit annotations and compare independent QA cohorts."""

from qa_metrics import read_sql
import argparse
import ast
import csv
import json
import re
import sqlite3
from collections import Counter,defaultdict
from datetime import datetime,timezone
from pathlib import Path

from intake_dices import combined_rating, schema_from_readme
from prepare_sources import file_hash

ROOT=Path(__file__).resolve().parents[1]
RAW=ROOT/'data/raw/dices350/diverse_safety_adversarial_dialog_350.csv'
Q1_OK='None of the above - it is in English, it makes sense, and I am familiar with the topic'
COHORTS={'all_released':'1=1',
         'drop_invalid_reason_items':"a.item_id NOT IN (SELECT item_id FROM audit_flags WHERE reason_status NOT IN ('valid_empty','valid_nonempty'))",
         'published_104_panel':'f.published_excluded=0',
         'q1_comprehension_only':'f.q1_ok=1',
         'drop_unsure':"a.Q_overall!='Unsure'"}


def parse_reason(text):
    """Return a status and parsed codes. Never repair or evaluate source strings."""
    try:value=json.loads(text)
    except json.JSONDecodeError:return 'invalid_json',None
    if not isinstance(value,list) or any(not isinstance(v,str) or not v.strip() for v in value):
        return 'invalid_shape',None
    return ('valid_nonempty' if value else 'valid_empty'),value


def flags_for(row,granular,excluded):
    status,_=parse_reason(row['safety_gold_reason'])
    labels=granular+['Q2_harmful_content_overall','Q3_bias_overall','Q6_policy_guidelines_overall','Q_overall']
    invalid=sum(row[k] not in {'Yes','No','Unsure'} for k in labels)
    invalid+=row['safety_gold'] not in {'Yes','No'}
    mismatch=0
    groups={'Q_overall':granular,**{f'Q{x}_{name}_overall':[k for k in granular if k.startswith(f'Q{x}_')]
             for x,name in [('2','harmful_content'),('3','bias'),('6','policy_guidelines')]}}
    if not invalid:
        mismatch=sum(row[name]!=combined_rating(row[k] for k in keys) for name,keys in groups.items())
    try:
        duration=int(row['answer_time_ms']);stamp=int(row['answer_timestamp'])
        invalid_time=int(duration<0 or stamp<0)
    except ValueError:invalid_time=1
    return {'reason_status':status,'q1_ok':int(row['Q1_whole_conversation_evaluation']==Q1_OK),
            'published_excluded':int(row['rater_id'] in excluded),
            'invalid_labels':invalid,'aggregate_mismatches':mismatch,'invalid_time':invalid_time}


def build_workspace(conn,rows,fields,excluded):
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('CREATE TABLE raw_annotations(source_row INTEGER PRIMARY KEY,'+
                 ','.join('"'+k+'" TEXT NOT NULL' for k in fields)+')')
    conn.executemany('INSERT INTO raw_annotations VALUES('+','.join('?' for _ in range(len(fields)+1))+')',
                     [(n,*(row[k] for k in fields)) for n,row in enumerate(rows,1)])
    conn.execute('CREATE INDEX audit_item ON raw_annotations(item_id)')
    conn.execute('CREATE INDEX audit_rater ON raw_annotations(rater_id)')
    conn.execute('CREATE TABLE audit_flags(source_row INTEGER PRIMARY KEY REFERENCES raw_annotations(source_row),'
                 'item_id TEXT NOT NULL,rater_id TEXT NOT NULL,reason_status TEXT NOT NULL,q1_ok INTEGER NOT NULL,'
                 'published_excluded INTEGER NOT NULL,invalid_labels INTEGER NOT NULL,'
                 'aggregate_mismatches INTEGER NOT NULL,invalid_time INTEGER NOT NULL)')
    granular=[k for k in fields if k.startswith(('Q2_','Q3_','Q4_','Q5_','Q6_')) and not k.endswith('_overall')]
    flags=[]
    for n,row in enumerate(rows,1):
        flag=flags_for(row,granular,excluded);flags.append(flag)
        conn.execute('INSERT INTO audit_flags VALUES(?,?,?,?,?,?,?,?,?)',
                     (n,row['item_id'],row['rater_id'],*flag.values()))
    conn.commit()
    return flags


def diagnostic(rows):
    n=len(rows);yes=sum(r['Q_overall']=='Yes' for r in rows);no=sum(r['Q_overall']=='No' for r in rows)
    unsure=sum(r['Q_overall']=='Unsure' for r in rows)
    matches=sum(r['Q_overall']==r['safety_gold'] for r in rows)
    unique={r['item_id']:r for r in rows}
    return {'records':n,'items':len(unique),'raters':len({r['rater_id'] for r in rows}),
            'yes':yes,'no':no,'unsure':unsure,'exact_reference_matches':matches,
            'reference_unsafe_rows':sum(r['safety_gold']=='Yes' for r in rows),
            'reference_unsafe_items':sum(r['safety_gold']=='Yes' for r in unique.values()),
            'yes_share_pct':100*yes/n if n else None,'unsure_share_pct':100*unsure/n if n else None,
            'exact_reference_match_all_retained_pct':100*matches/n if n else None,
            'exact_reference_match_decisive_only_pct':100*matches/(yes+no) if yes+no else None}


def selected(rows,flags,name):
    bad_items={r['item_id'] for r,f in zip(rows,flags) if f['reason_status'] not in {'valid_empty','valid_nonempty'}}
    predicates={'all_released':lambda r,f:True,
                'drop_invalid_reason_items':lambda r,f:r['item_id'] not in bad_items,
                'published_104_panel':lambda r,f:not f['published_excluded'],
                'q1_comprehension_only':lambda r,f:bool(f['q1_ok']),
                'drop_unsure':lambda r,f:r['Q_overall']!='Unsure'}
    return [r for r,f in zip(rows,flags) if predicates[name](r,f)]


def compare_cohorts(conn,rows,flags):
    results=[]
    for name,where in COHORTS.items():
        sql=conn.execute('''SELECT COUNT(*) AS records,COUNT(DISTINCT a.item_id) AS items,
          COUNT(DISTINCT a.rater_id) AS raters,SUM(a.Q_overall='Yes') AS yes,SUM(a.Q_overall='No') AS no,
          SUM(a.Q_overall='Unsure') AS unsure,SUM(a.Q_overall=a.safety_gold) AS exact_reference_matches,
          SUM(a.safety_gold='Yes') AS reference_unsafe_rows,
          COUNT(DISTINCT CASE WHEN a.safety_gold='Yes' THEN a.item_id END) AS reference_unsafe_items
          FROM raw_annotations a JOIN audit_flags f USING(source_row) WHERE '''+where).fetchone()
        expected=diagnostic(selected(rows,flags,name))
        assert all((sql[k] if sql[k] is not None else 0)==expected[k] for k in sql.keys()),name
        results.append({'cohort':name,**expected,'python_sql_equal':True})
    base=results[0]
    for r in results:
        r['removed_records']=base['records']-r['records']
        r['removed_items']=base['items']-r['items']
        r['diagnostic_match_delta_pp']=(r['exact_reference_match_all_retained_pct']-base['exact_reference_match_all_retained_pct']) if r['records'] else None
    return results


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,default=ROOT/'data/work/ch03_annotation_audit.sqlite')
    args=parser.parse_args();database=args.database.resolve()
    if not database.is_relative_to((ROOT/'data/work').resolve()):
        raise ValueError('Keep private audit database under data/work')
    if database.exists():raise FileExistsError('Preserving the existing QA workspace; choose a new --database filename')
    manifest=json.loads((ROOT/'reports/02_acquisition_manifest.json').read_text())
    for record in manifest['files']:assert file_hash(ROOT/record['path'])==record['sha256'],record['path']
    before=file_hash(RAW)
    with RAW.open(encoding='utf-8-sig',newline='') as stream:
        reader=csv.DictReader(stream);fields=reader.fieldnames;rows=list(reader)
    assert fields==json.loads((ROOT/'reports/02_dices_intake.json').read_text())['columns']
    text=(ROOT/'references/google_dices/350_README.md').read_text()
    excluded=set(ast.literal_eval(re.search(r'removed_raters_350 = (\[[^\n]+\])',text).group(1)))
    database.parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(database) as conn:
        flags=build_workspace(conn,rows,fields,excluded);conn.row_factory=sqlite3.Row
        parts=re.split(r'^-- name: (\w+)\s*$',read_sql('03_annotation_audit.sql'),flags=re.M)
        audits={parts[n]:[dict(r) for r in conn.execute(parts[n+1])] for n in range(1,len(parts),2)}
        assert audits['join_cardinality'][0]=={'joined_records':len(rows),'unique_source_rows':len(rows)}
        assert not list(conn.execute('PRAGMA foreign_key_check'))
        assert conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        cohorts=compare_cohorts(conn,rows,flags)
    items=defaultdict(list)
    for row in rows:items[row['item_id']].append(row)
    # Public report contains only counts, status and hashes; identifiers stay private.
    exceptions=[];vocabulary=Counter()
    for group in items.values():
        row=group[0];status,codes=parse_reason(row['safety_gold_reason'])
        if codes is not None:vocabulary.update(set(codes))
        if status not in {'valid_empty','valid_nonempty'}:
            exceptions.append({'reason_status':status,'raw_reason':row['safety_gold_reason'],
                               'safety_gold':row['safety_gold'],'affected_records':len(group),
                               'raw_reason_sha256':__import__('hashlib').sha256(row['safety_gold_reason'].encode()).hexdigest()})
    after=file_hash(RAW);assert before==after
    for record in manifest['files']:assert file_hash(ROOT/record['path'])==record['sha256']
    payload={'chapter':3,'created_at_utc':datetime.now(timezone.utc).isoformat(),
             'data_kind':'actual_full_DICES350_annotation_QA','raw_sha256_before':before,'raw_sha256_after':after,
             'raw_unchanged':True,'private_workspace':str(database.relative_to(ROOT)),
             'workspace_sha256':file_hash(database),'workspace_is_final_model':False,
             'audits':audits,'cohorts':cohorts,'strict_reason_exceptions':exceptions,
             'valid_reason_code_item_counts':dict(sorted(vocabulary.items())),
             'checks':{'all_five_cohort_integer_counts_match_sql_python':True,'one_to_one_flag_join':True,
                       'sqlite_integrity':'ok','foreign_key_violations':0,'source_bytes_preserved':True},
             'plan_sha256':file_hash(ROOT/'docs/03_quality_audit_plan.json'),
             'code_hashes':{name:file_hash(ROOT/name) for name in ['src/quality_audit.py','analysis.sql']},
             'diagnostic_rate_definition':'Exact Q_overall==safety_gold matches / all retained records; Unsure retained in denominator. Separate decisive-only denominator is explicitly labelled. Not the final Chapter 04 scorecard.',
             'comparisons_are_cumulative':False,'independent_row_inference_performed':False,
             'individual_rater_or_demographic_rankings_published':False}
    (ROOT/'reports/03_qa_audit_results.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n')
    with (ROOT/'reports/03_rule_impact.csv').open('w',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(cohorts[0]));writer.writeheader();writer.writerows(cohorts)
    print(json.dumps({'audits':audits,'cohorts':cohorts,'raw_unchanged':True},indent=2))


if __name__=='__main__':main()
