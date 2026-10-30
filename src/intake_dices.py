"""Full-file DICES-350 intake; preserve all raw rows and emit aggregate metadata."""
import ast
import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from prepare_sources import file_hash

ROOT=Path(__file__).resolve().parents[1]
RAW=ROOT/'data/raw/dices350/diverse_safety_adversarial_dialog_350.csv'


def combined_rating(values):
    values=list(values)
    if not values or any(v not in {'Yes','No','Unsure'} for v in values):
        raise ValueError('Cannot aggregate empty or invalid safety answers')
    if 'Yes' in values:return 'Yes'
    if 'Unsure' in values:return 'Unsure'
    return 'No'


def schema_from_readme(text):
    names=re.findall(r'^\d+\. `([^`]+)`:',text,flags=re.M)
    if len(names)!=41 or len(set(names))!=41:
        raise ValueError('Unexpected official schema; review source version')
    return names


def scan(path,declared_fields,removed_raters):
    before=file_hash(path)
    with path.open(encoding='utf-8-sig',newline='') as stream:
        reader=csv.DictReader(stream)
        fields=reader.fieldnames
        if fields is None or len(fields)!=41 or len(set(fields))!=41:
            raise ValueError('Expected 41 unique CSV headers')
        discrepancies=[{'position':i,'documented':a,'actual':b}
                       for i,(a,b) in enumerate(zip(declared_fields,fields),1) if a!=b]
        allowed=[{'position':37,'documented':'Q6_policy_guidelines_other_type','actual':'Q6_policy_guidelines_other'}]
        if discrepancies!=allowed:
            raise ValueError('Unexpected schema change; no silent rename')
        rows=[]
        for row in reader:
            if None in row or any(v is None for v in row.values()):
                raise ValueError('Malformed CSV record')
            rows.append(row)
    missing={name:sum(not r[name].strip() for r in rows) for name in fields}
    if any(missing[k] for k in ['id','item_id','rater_id']):
        raise ValueError('Missing key')
    pairs=Counter((r['item_id'],r['rater_id']) for r in rows)
    items=defaultdict(list);raters=defaultdict(list)
    for row in rows:
        items[row['item_id']].append(row)
        raters[row['rater_id']].append(row)
    granular=[k for k in fields if k.startswith(('Q2_','Q3_','Q4_','Q5_','Q6_')) and not k.endswith('_overall')]
    aggregated=['Q2_harmful_content_overall','Q3_bias_overall','Q6_policy_guidelines_overall','Q_overall']
    labels=granular+aggregated
    invalid_labels={k:sum(r[k] not in {'Yes','No','Unsure'} for r in rows) for k in labels}
    invalid_gold=sum(r['safety_gold'] not in {'Yes','No'} for r in rows)
    mismatches={k:0 for k in aggregated}
    if not any(invalid_labels.values()):
        groups={'Q_overall':granular,**{f'Q{x}_{family}_overall':[k for k in granular if k.startswith(f'Q{x}_')]
                for x,family in [('2','harmful_content'),('3','bias'),('6','policy_guidelines')]}}
        for target,columns in groups.items():
            mismatches[target]=sum(r[target]!=combined_rating(r[k] for k in columns) for r in rows)
    else:
        mismatches={k:None for k in aggregated}
    item_constant=['context','response','degree_of_harm','harm_type','safety_gold','safety_gold_reason']
    nonconstant={k:sum(len({r[k] for r in group})>1 for group in items.values()) for k in item_constant}
    examples=[group[0] for group in items.values()]
    bad_reasons=Counter();gold_reason_shapes=Counter();topics=set();topic_mention_count=0
    for row in examples:
        try:
            reason=json.loads(row['safety_gold_reason'])
            if not isinstance(reason,list) or any(not isinstance(v,str) for v in reason):
                raise ValueError('Not a string list')
            shape='empty_list' if not reason else 'nonempty_list'
        except (json.JSONDecodeError,ValueError):
            shape='invalid_json_list';bad_reasons[row['safety_gold']]+=1
        gold_reason_shapes[(row['safety_gold'],shape)]+=1
        tokens=[v.strip() for v in row['harm_type'].split(',') if v.strip()]
        topics.update(tokens);topic_mention_count+=len(tokens)
    invalid_reason_strings={r['safety_gold_reason'] for r in examples if _invalid_reason(r['safety_gold_reason'])}
    removed=set(removed_raters)
    timestamps=[];times=[];invalid_time=0
    for row in rows:
        try:
            duration=int(row['answer_time_ms']);stamp=int(row['answer_timestamp'])
            if duration<0:raise ValueError('Negative duration')
            times.append(duration);timestamps.append(stamp)
        except ValueError:invalid_time+=1
    source_reference_counts=Counter(r['safety_gold'] for r in examples)
    result={'file_sha256_before':before,'rows':len(rows),'columns':fields,'column_count':len(fields),
            'unique_items':len(items),'unique_raters':len(raters),
            'duplicate_row_ids':len(rows)-len({r['id'] for r in rows}),
            'duplicate_item_rater_extra_rows':sum(n-1 for n in pairs.values()),
            'rows_per_item_histogram':dict(sorted(Counter(map(len,items.values())).items())),
            'rows_per_rater_histogram':dict(sorted(Counter(map(len,raters.values())).items())),
            'complete_item_rater_grid':len(pairs)==len(rows)==len(items)*len(raters),
            'missing_cells_by_field':missing,'total_missing_cells':sum(missing.values()),
            'schema_discrepancies':discrepancies,'invalid_safety_labels_by_field':invalid_labels,
            'invalid_expert_reference_labels':invalid_gold,'granular_question_count':len(granular),
            'aggregate_reconstruction_mismatches':mismatches,
            'item_field_nonconstant_counts':nonconstant,
            'overall_label_counts':dict(Counter(r['Q_overall'] for r in rows)),
            'expert_reference_item_counts':dict(source_reference_counts),
            'expert_reason_shape_counts':[{'gold':gold,'shape':shape,'items':n}
                                        for (gold,shape),n in sorted(gold_reason_shapes.items())],
            'invalid_expert_reason_json_items':sum(bad_reasons.values()),
            'invalid_expert_reason_json_rows':sum(len(group) for group in items.values()
                if group[0]['safety_gold_reason'] in invalid_reason_strings),
            'harm_type_distinct_raw_combinations':len({r['harm_type'] for r in examples}),
            'harm_type_atomic_tokens_after_comma_split':len(topics),'harm_type_token_mentions':topic_mention_count,
            'severity_item_counts':dict(Counter(r['degree_of_harm'] for r in examples)),
            'q1_response_counts':dict(Counter(r['Q1_whole_conversation_evaluation'] for r in rows)),
            'phase_counts':dict(Counter(r['phase'] for r in rows)),
            'source_exclusion_panel':{'documented_removed_count':len(removed),
                'removed_ids_found':len(removed & raters.keys()),'removed_ids_absent_count':len(removed-raters.keys()),
                'remaining_raters':len(raters.keys()-removed),'remaining_rows':sum(r['rater_id'] not in removed for r in rows),
                'applied_to_raw':False},
            'time_fields':{'invalid_or_negative_rows':invalid_time,
                'answer_time_ms_min':min(times) if times else None,'answer_time_ms_max':max(times) if times else None,
                'answer_timestamp_min_utc':datetime.fromtimestamp(min(timestamps),timezone.utc).isoformat() if timestamps else None,
                'answer_timestamp_max_utc':datetime.fromtimestamp(max(timestamps),timezone.utc).isoformat() if timestamps else None,
                'meaning':'Annotation-question duration per schema; timestamps are rating activity, not conversation creation or staff shifts'}}
    result['file_sha256_after']=file_hash(path)
    if result['file_sha256_after']!=before:raise RuntimeError('Raw file changed')
    return result


def _invalid_reason(text):
    try:
        value=json.loads(text)
        return not isinstance(value,list) or any(not isinstance(x,str) for x in value)
    except json.JSONDecodeError:
        return True


def main():
    manifest=json.loads((ROOT/'reports/02_acquisition_manifest.json').read_text())
    for row in manifest['files']:
        assert file_hash(ROOT/row['path'])==row['sha256'],row['path']
    text=(ROOT/'references/google_dices/350_README.md').read_text()
    removed=ast.literal_eval(re.search(r'removed_raters_350 = (\[[^\n]+\])',text).group(1))
    result=scan(RAW,schema_from_readme(text),removed)
    result.update(chapter=2,source_revision=manifest['revision'],data_kind='actual_full_DICES_350_CSV',
                  csv_rows_modified=False,cleaning_rules_applied=False,
                  expected_dimensions_match=(result['rows'],result['unique_items'],result['unique_raters'],result['column_count'])==(43050,350,123,41),
                  open_chapter03_items=['Preserve and flag 3 malformed JSON expert-reason strings; no silent repair',
                     'Review complete annotation QA and rule impacts before metric computation'],
                  quality_performance_metrics_computed=False)
    if not result['expected_dimensions_match']:
        raise ValueError('Actual dimensions differ from the pinned DICES-350 expectations; review before publishing intake.')
    (ROOT/'reports/02_dices_intake.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    dictionary=[]
    for index,name in enumerate(result['columns'],1):
        role='safety judgment' if name.startswith('Q') and name!='Q1_whole_conversation_evaluation' else 'metadata'
        dictionary.append({'position':index,'column':name,'documented_column':schema_from_readme(text)[index-1],
                           'role':role,'missing_cells':result['missing_cells_by_field'][name]})
    with (ROOT/'docs/02_fields.csv').open('w',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(dictionary[0]));writer.writeheader();writer.writerows(dictionary)
    print(json.dumps({k:result[k] for k in ['rows','column_count','unique_items','unique_raters',
          'complete_item_rater_grid','total_missing_cells','schema_discrepancies',
          'aggregate_reconstruction_mismatches','invalid_expert_reason_json_items',
          'invalid_expert_reason_json_rows','harm_type_distinct_raw_combinations',
          'harm_type_atomic_tokens_after_comma_split','source_exclusion_panel','time_fields']},indent=2))


if __name__=='__main__':main()
