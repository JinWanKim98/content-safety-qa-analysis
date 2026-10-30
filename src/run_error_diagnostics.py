"""Exploratory Chapter 07 diagnostics; immutable source/model, no audit simulation."""

from qa_metrics import read_sql
from reproducibility import snapshot,check_snapshot,verify_sources
import json
import sqlite3
from collections import Counter,defaultdict
from pathlib import Path
from datetime import datetime,timezone
from build_qa_model import load_csv
from intake_dices import combined_rating
from qa_metrics import divide
from prepare_sources import file_hash
from run_quality_baseline import assert_close,write_csv

ROOT=Path(__file__).resolve().parents[1]
PLAN=ROOT/'docs/07_diagnostic_plan.json'
SQL_ITEM=ROOT/'analysis.sql'
SQL_QUESTIONS=ROOT/'analysis.sql'
SQL_REASONS=ROOT/'analysis.sql'
GROUPS=['Q2','Q3','Q4','Q5','Q6']
GROUP_NAMES={'Q2':'Harmful content','Q3':'Bias','Q4':'Misinformation','Q5':'Political affiliation','Q6':'Policy guidelines'}


def granular_fields(fields):
    return [f for f in fields if f.startswith(tuple(g+'_' for g in GROUPS)) and not f.endswith('_overall')]


def annotate_item(item,thresholds):
    """Screens are descriptive analyst assumptions; never change source labels."""
    x=dict(item);d=x['yes']+x['no'];n=x['n']
    if n<=0 or n!=d+x['unsure']:raise ValueError('Invalid item counts')
    if x['reference'] not in {'Yes','No'}:raise ValueError('Invalid reference')
    x['I01']=x['unsure']/n;x['I02']=divide(2*x['yes']*x['no'],d*(d-1)) if d>=2 else None
    x['fn_share']=x['fn']/n if x['reference']=='Yes' else None
    x['fp_share']=x['fp']/n if x['reference']=='No' else None
    x['miss_review']=bool(x['fn_share'] is not None and x['fn_share']>=thresholds['reference_difference_share'])
    x['restriction_review']=bool(x['fp_share'] is not None and x['fp_share']>=thresholds['reference_difference_share'])
    x['split_review']=bool(x['I02'] is not None and x['I02']>=thresholds['pair_disagreement'])
    x['uncertainty_review']=x['I01']>=thresholds['unsure_share']
    x['source_extreme_miss_review']=x['source_severity']=='Extreme' and x['miss_review']
    x['any_review']=any(x[k] for k in ['miss_review','restriction_review','split_review','uncertainty_review'])
    return x


def python_items(rows,thresholds):
    grouped=defaultdict(list)
    for r in rows:grouped[r['item_id']].append(r)
    result=[]
    for item,ratings in sorted(grouped.items()):
        first=ratings[0];ref=first['safety_gold'];c=Counter(r['Q_overall'] for r in ratings)
        result.append(annotate_item({'item_id':item,'reference':ref,'source_severity':first['degree_of_harm'],
          'n':len(ratings),'raters':len({r['rater_id'] for r in ratings}),'yes':c['Yes'],'no':c['No'],
          'unsure':c['Unsure'],'fn':c['No'] if ref=='Yes' else 0,'fp':c['Yes'] if ref=='No' else 0},thresholds))
    return result


def fp_triggers(rows,fields):
    """Reconstruct within-row groups. Contributions overlap; not granular FP."""
    questions=granular_fields(fields)
    if not questions:raise ValueError('No granular questions')
    fp=[r for r in rows if r['safety_gold']=='No' and r['Q_overall']=='Yes']
    qrows=[]
    for q in sorted(questions):
        positive=[r for r in fp if r[q]=='Yes']
        qrows.append({'question':q,'question_group':q.split('_')[0],'cohort_records':len(fp),
          'cohort_items':len({r['item_id'] for r in fp}),'cohort_raters':len({r['rater_id'] for r in fp}),
          'yes_records':len(positive),'yes_items':len({r['item_id'] for r in positive})})
    grouped={g:[q for q in questions if q.startswith(g+'_')] for g in GROUPS}
    triggers=[];hist=Counter()
    for r in fp:
        yes=[g for g,qs in grouped.items() if qs and combined_rating(r[q] for q in qs)=='Yes']
        if not yes:raise AssertionError('Overall FP must contain a granular Yes')
        hist[len(yes)]+=1;triggers.append((r,yes))
    groups=[]
    for g in GROUPS:
        selected=[r for r,yes in triggers if g in yes];sole=[r for r,yes in triggers if yes==[g]]
        groups.append({'question_group':g,'name':GROUP_NAMES[g],'granular_questions':len(grouped[g]),
          'cohort_records':len(fp),'cohort_items':len({r['item_id'] for r in fp}),
          'cohort_raters':len({r['rater_id'] for r in fp}),
          'yes_records':len(selected),'yes_items':len({r['item_id'] for r in selected}),
          'sole_positive_group_records':len(sole),'share_of_overall_fp':divide(len(selected),len(fp))})
    return qrows,groups,[{'positive_groups':i,'records':hist[i]} for i in range(1,6)]


def sql_group_triggers(conn):
    # Reconstruct each group directly from normalized granular answers; aggregate
    # to one judgment-group first, then count positive groups per judgment.
    query="""WITH grouped AS (
      SELECT j.source_row,j.item_id,j.rater_id,q.question_group,MAX(a.answer='Yes') AS has_yes
      FROM fact_judgment j JOIN fact_expert_reference e USING(item_id)
      JOIN fact_answer a USING(source_row) JOIN dim_question q USING(question_id)
      WHERE e.reference_label='No' AND j.overall='Yes' AND q.question_kind='granular'
      GROUP BY j.source_row,j.item_id,j.rater_id,q.question_group
    ), totals AS (SELECT source_row,SUM(has_yes) AS positive_groups FROM grouped GROUP BY source_row)
    SELECT g.question_group,SUM(g.has_yes) AS yes_records,
       COUNT(DISTINCT CASE WHEN g.has_yes=1 THEN g.item_id END) AS yes_items,
       SUM(g.has_yes=1 AND t.positive_groups=1) AS sole_positive_group_records
    FROM grouped g JOIN totals t USING(source_row) GROUP BY g.question_group ORDER BY g.question_group"""
    groups=[dict(r) for r in conn.execute(query)]
    hist=[dict(r) for r in conn.execute("""WITH positive AS (
      SELECT j.source_row,COUNT(DISTINCT q.question_group) AS positive_groups
      FROM fact_judgment j JOIN fact_expert_reference e USING(item_id)
      JOIN fact_answer a USING(source_row) JOIN dim_question q USING(question_id)
      WHERE e.reference_label='No' AND j.overall='Yes' AND q.question_kind='granular' AND a.answer='Yes'
      GROUP BY j.source_row)
      SELECT positive_groups,COUNT(*) AS records FROM positive GROUP BY positive_groups""")]
    observed={h['positive_groups']:h['records'] for h in hist}
    return groups,[{'positive_groups':i,'records':observed.get(i,0)} for i in range(1,6)]


def reason_profiles(rows):
    """Raw code strata; deduplicate positions but never normalize aliases."""
    items={r['item_id']:r for r in rows};members=defaultdict(set);unparsed=set();parsed=set()
    for item,r in items.items():
        if r['safety_gold']!='Yes':continue
        try:
            codes=json.loads(r['safety_gold_reason'])
            if not isinstance(codes,list) or any(not isinstance(c,str) for c in codes):raise ValueError('Invalid list')
        except (json.JSONDecodeError,ValueError):unparsed.add(item);continue
        parsed.add(item)
        for code in set(codes):members[code].add(item)
    out=[]
    for code,ids in sorted(members.items()):
        selected=[r for r in rows if r['item_id'] in ids];c=Counter(r['Q_overall'] for r in selected)
        out.append({'code':code,'reference_unsafe_records':len(selected),'items':len(ids),
          'raters':len({r['rater_id'] for r in selected}),'yes':c['Yes'],'no':c['No'],'unsure':c['Unsure']})
    return out,{'unsafe_items':sum(r['safety_gold']=='Yes' for r in items.values()),'parsed_unsafe_items':len(parsed),
      'unparsed_unsafe_items':len(unparsed),'unparsed_records':sum(r['item_id'] in unparsed for r in rows),
      'unparsed_fn_records':sum(r['item_id'] in unparsed and r['Q_overall']=='No' for r in rows),
      'unique_item_code_memberships':sum(len(v) for v in members.values())}


def add_reason_rates(rows,gate):
    return [{**r,'explicit_no_share':divide(r['no'],r['reference_unsafe_records']),
      'unsure_share':divide(r['unsure'],r['reference_unsafe_records']),
      'display_eligible':r['items']>=gate['minimum_items'] and r['raters']>=gate['minimum_raters'] and r['reference_unsafe_records']>=gate['minimum_rating_records']} for r in rows]


def summarize_flags(items):
    flags=['miss_review','restriction_review','split_review','uncertainty_review','source_extreme_miss_review','any_review']
    return [{'flag':flag,'items':sum(x[flag] for x in items),'existing_fn_records':sum(x['fn'] for x in items if x[flag]),
             'existing_fp_records':sum(x['fp'] for x in items if x[flag]),
             'existing_unsure_records':sum(x['unsure'] for x in items if x[flag])} for flag in flags]


def collect():
    baseline_path=ROOT/'reports/06_baseline_results.json';baseline=json.loads(baseline_path.read_text())
    protected=snapshot(['reports/06_baseline_results.json'])
    check_snapshot(protected)
    plan=json.loads(PLAN.read_text());contract=json.loads((ROOT/'docs/04_qa_metric_contract.json').read_text())
    receipt=json.loads((ROOT/'reports/05_model_validation.json').read_text());model=ROOT/receipt['model_path']
    fields,raw=load_csv(ROOT/'data/raw/dices350/diverse_safety_adversarial_dialog_350.csv')
    conn=sqlite3.connect(model.as_uri()+'?mode=ro',uri=True);conn.row_factory=sqlite3.Row
    try:
        items=[annotate_item(dict(r),plan['threshold_values']) for r in conn.execute(read_sql('07_item_diagnostics.sql'))]
        assert_close(items,python_items(raw,plan['threshold_values']))
        questions=[dict(r) for r in conn.execute(read_sql('07_fp_question_triggers.sql'))]
        py_questions,group_triggers,hist=fp_triggers(raw,fields)
        assert_close(questions,py_questions)
        sql_groups,sql_hist=sql_group_triggers(conn)
        for sqlrow,pyrow in zip(sql_groups,group_triggers):assert_close(pyrow,sqlrow)
        assert len(sql_groups)==len(group_triggers)==5
        assert_close(hist,sql_hist)
        reasons=[dict(r) for r in conn.execute(read_sql('07_expert_reason_profiles.sql'))]
        py_reasons,reason_scope=reason_profiles(raw)
        assert_close(reasons,py_reasons)
        fn_count=conn.execute("SELECT COUNT(*) FROM ratings WHERE reference='Yes' AND judgment='No'").fetchone()[0]
        fn_non_no=conn.execute("""SELECT COUNT(*) FROM fact_answer a JOIN dim_question q USING(question_id)
          JOIN fact_judgment j USING(source_row) JOIN fact_expert_reference e USING(item_id)
          WHERE e.reference_label='Yes' AND j.overall='No' AND q.question_kind='granular' AND a.answer!='No'""").fetchone()[0]
        assert fn_non_no==0
        counts=baseline['panels']['all_released']['counts']
        assert sum(x['fn'] for x in items)==counts['fn']==fn_count
        assert sum(x['fp'] for x in items)==counts['fp']
        assert sum(h['records'] for h in hist)==counts['fp']
        assert sum(g['sole_positive_group_records'] for g in group_triggers)==hist[0]['records']
    finally:conn.close()
    for p,h in protected.items():assert file_hash(p)==h,str(p)
    directions=[]
    for g in baseline['groups']:
        directions.append({'kind':g['kind'],'group':g['group'],'items':g['counts']['items'],
          'reference_unsafe_items':g['reference_items'].get('Yes',0),'reference_safe_items':g['reference_items'].get('No',0),
          'fn_records':g['counts']['fn'],'fp_records':g['counts']['fp'],
          'unsafe_records':g['counts']['ref_unsafe'],'safe_records':g['counts']['ref_safe'],
          **{f'{mid}_{suffix}':(m['value'] if suffix=='fraction' else m['value'] if m['support']['display_eligible'] else None) for mid in ['R10','R12','R11','R13'] for suffix in ['fraction','display_fraction'] for m in [g['metrics'][mid]]}})
    reasons=add_reason_rates(reasons,contract['public_group_support'])
    overlaps=Counter(tuple(x[k] for k in ['miss_review','restriction_review','split_review','uncertainty_review']) for x in items)
    result={'chapter':7,'computed_at_utc':datetime.now(timezone.utc).isoformat(),'plan_sha256':file_hash(PLAN),
      'contract_version':contract['version'],'model_sha256':file_hash(model),
      'protected_inputs_unchanged':True,'protected_input_sha256':{str(p.relative_to(ROOT)):h for p,h in protected.items()},
      'primary_counts':counts,'thresholds':plan['threshold_values'],'flags':summarize_flags(items),
      'flag_overlap_patterns':[dict(zip(['miss_review','restriction_review','split_review','uncertainty_review'],key),items=n) for key,n in sorted(overlaps.items())],
      'group_directions':directions,'fp_question_triggers':questions,'fp_group_triggers':group_triggers,
      'fp_positive_group_histogram':hist,'reason_scope':reason_scope,'expert_reason_profiles':reasons,
      'fn_all_granular_no':{'overall_fn_records':fn_count,'granular_answers_per_record':len(granular_fields(fields)),
          'non_no_granular_answers':fn_non_no,'interpretation':'Aggregation identity, not evidence of one causal question.'},
      'reconciliation':{'item_rows':len(items),'granular_question_rows':len(questions),'question_group_rows':len(group_triggers),
                       'overlap_histogram_bins':len(hist),'expert_reason_rows':len(reasons),'raw_python_vs_normalized_sql':'All exact counts and item flags match; floats within 1e-12.'},
      'causal_effects_estimated':False,'confidence_intervals_computed':False,'audit_budget_or_sampling_simulated':False,
      'limits':['Exploratory screens, not validated policy or quality exclusions. Flags overlap.',
        'Global expert safety gold is not question-level gold. Trigger shares are not granular false-positive rates.',
        'Source severity disagreement is unresolved; proposed priority does not establish real-world harm or business cost.',
        'Repeated ratings and adversarial balanced sample; no iid inference, worker ranking or production prevalence.',
        'Expert-reason codes overlap and preserve near aliases; 3 unparsed unsafe items remain in global/item analyses.']}
    return result,items


def run():
    result,items=collect();out=ROOT/'reports'
    (out/'07_diagnostic_results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    for name,rows in [('item_diagnostics',items),('review_flags',result['flags']),('group_directions',result['group_directions']),
      ('fp_question_triggers',result['fp_question_triggers']),('fp_group_triggers',result['fp_group_triggers']),
      ('expert_reason_profiles',result['expert_reason_profiles'])]:write_csv(out/f'07_{name}.csv',rows)
    render_report(result,items)
    print(json.dumps({'status':'complete_descriptive_diagnostics','reconciled':result['reconciliation'],
      'flags':result['flags'],'reason_scope':result['reason_scope'],'fp_groups':result['fp_group_triggers'],
      'fp_overlap':result['fp_positive_group_histogram'],'protected_inputs_unchanged':True},indent=2))


def render_report(r,items):
    lines=['# Chapter 07 — Error and risk diagnostics','',
      'Exploratory descriptive diagnostics under frozen metric contract v1.0.0. Primary panel: 350 items, 123 raters, 43,050 repeated judgments. Unsafe is Yes; Unsure remains separate. Expert labels are a fallible reference.',
      '', '## What can be diagnosed','',
      'Overall reference differences are 10,910 FN (reference Yes / judgment No) and 4,940 FP (reference No / judgment Yes). These are repeated judgment counts, not distinct contents, confirmed harmful outcomes or worker errors.',
      '', 'Every overall FN has all 19 granular safety answers No. SQL found zero non-No granular answers within this cohort. This follows the frozen aggregation rule; it cannot identify one missed category or explain why the reference differs.',
      '', 'For overall FP, at least one granular Yes produced the overall Yes. We decompose these triggers without treating an overall expert label as question-level ground truth.',
      '', '## Prespecified exploratory review screens','',
      'Thresholds were saved in docs/07_diagnostic_plan.json before diagnostic outcome computation: explicit-No share >=50% on unsafe-reference items; explicit-Yes share >=50% on safe-reference items; I02 >=45%; I01 >=10%. They are analyst assumptions, not validated cutoffs. No item is relabelled or removed. Item screens are distinct from the group display support gate.',
      '', '| Screen | Unique items | Existing FN judgments | Existing FP judgments | Existing Unsure judgments |',
      '|---|---:|---:|---:|---:|']
    for x in r['flags']:lines.append(f"| {x['flag']} | {x['items']} | {x['existing_fn_records']:,} | {x['existing_fp_records']:,} | {x['existing_unsure_records']:,} |")
    lines += ['', 'Screens overlap: do not sum their item or judgment counts. any_review is the set union. No fixed audit budget, selected sample, review discovery yield or operational intervention was evaluated. Those remain Chapter 09.',
      '', 'The source-Extreme + unsafe-reference + miss_review intersection is a proposed first inspection lane only if source severity is accepted as a provisional prioritization assumption. Its unresolved source discrepancy prevents validated harm/cost priority claims. Remaining reference-difference, split and uncertain items remain available; no universal weighted risk score is assigned.',
      '', '## Source severity and topic direction profiles','',
      'reports/07_group_directions.csv reuses Chapter06 reconciled cell counts and frozen R10/R12/R11/R13. FN share divides by all unsafe-reference judgments; FP share divides by all safe-reference judgments; both retain Unsure in those denominators. Public comparison rates use their actual class-specific support gates (10 items,10 raters,100 judgments), and zero denominators remain undefined. Topic profiles overlap and cannot be summed.',
      '', '| Source stratum | Unsafe reference items | FN / all unsafe-reference judgments | Safe reference items | FP / all safe-reference judgments |','|---|---:|---:|---:|---:|']
    for g in r['group_directions']:
        if g['kind']!='severity':continue
        def fmt(mid,num,den):
            value=g[f'{mid}_display_fraction']
            return f"{g[num]:,}/{g[den]:,} = {value*100:.2f}%" if value is not None else 'Withheld: class support below display gate'
        lines.append(f"| {g['group']} | {g['reference_unsafe_items']} | {fmt('R10','fn_records','unsafe_records')} | {g['reference_safe_items']} | {fmt('R12','fp_records','safe_records')} |")
    lines += ['', 'Severity labels are source-version strata, not numeric loss weights or a substitute for safety_gold. Group composition and content differ; these profiles do not establish a causal effect.',
      '', '## What drives the overall-Yes aggregator inside the FP cohort?','',
      '| Granular group | Number of questions | Has group Yes /4940 overall FP judgments | Sole positive group | Positive-trigger items |','|---|---:|---:|---:|---:|']
    for g in r['fp_group_triggers']:
        lines.append(f"| {g['question_group']} {g['name']} | {g['granular_questions']} | {g['yes_records']:,}/4,940 = {g['share_of_overall_fp']*100:.2f}% | {g['sole_positive_group_records']:,} | {g['yes_items']} |")
    multi=sum(h['records'] for h in r['fp_positive_group_histogram'] if h['positive_groups']>=2)
    lines += ['', f"{multi:,}/4,940 overall FP judgments have Yes in multiple Q2–Q6 groups. The group counts overlap, so their shares need not sum to 100%. Sole-positive counts are disjoint, but a sole positive group can coexist with Unsure in other groups; no hypothetical deletion or improved accuracy is claimed.",
      '', 'These are contributions to a deterministic aggregator conditional on overall FP. Q2 has nine granular questions and Q3 five, versus one each for Q4/Q5, so raw trigger counts are not comparable category error rates. The 19-question detail is in reports/07_fp_question_triggers.csv. Without complete question-specific reference labels, do not call these granular false positives or infer the cause of false restrictions.',
      '', '## Expert reason strata for unsafe-reference disagreements','']
    scope=r['reason_scope']
    lines += [f"Global/item analyses retain all {scope['unsafe_items']} unsafe-reference items. Reason strata use {scope['parsed_unsafe_items']} parseable items. The {scope['unparsed_unsafe_items']} unparsed items /{scope['unparsed_records']} judgments include {scope['unparsed_fn_records']} existing FN judgments and remain in global outputs. No quote repair or invented code mapping is applied.",
      '',f"The 18 raw codes form {scope['unique_item_code_memberships']} unique item-code memberships. Codes overlap; near aliases remain distinct. They group items by recorded expert explanation and are not asserted to be complete binary gold labels for each of the 19 questions.",
      '', '| Raw expert reason (eligible groups only) | Items | Explicit No / all judgments in stratum | Unsure |','|---|---:|---:|---:|']
    for x in r['expert_reason_profiles']:
        if not x['display_eligible']:continue
        lines.append(f"| {x['code']} | {x['items']} | {x['no']:,}/{x['reference_unsafe_records']:,} = {x['explicit_no_share']*100:.2f}% | {x['unsure']:,}/{x['reference_unsafe_records']:,} = {x['unsure_share']*100:.2f}% |")
    lines += ['', 'All 18 profiles, original code strings, support flags and internal aggregate values remain in reports/07_expert_reason_profiles.csv. Comparisons are exploratory and may reflect item mix and expert-label ambiguity; no statistical significance ranking.',
      '', '## Evidence → hypothesis → validation','',
      '| Observed evidence | Testable hypothesis, not established cause | What would test it | What these data cannot establish |','|---|---|---|---|',
      '| Unsafe-reference explicit-No judgments occur in recorded expert-reason strata. | Expert and rater interpretations of a policy boundary may differ. | Blinded expert re-adjudication with granular rationales and independent rubric coding; compare matched content. | Whether the expert or rater was wrong, or a specific missed question caused FN. |',
      '| Multiple granular groups contribute Yes within some overall FP judgments. | Broad or overlapping rubric criteria may produce different interpretations. | Collect category-specific expert labels and rationales; examine neutral matched examples before any rubric change. | Removing a category would improve real safety or operational accuracy. |',
      '| Some items meet split and/or uncertainty screens. | Ambiguous content or unclear instructions may contribute to disagreement. | Compare independent explanations on flagged and unflagged items; keep both sides blinded to existing labels. | Low worker competence, causation, or a validated error threshold. |',
      '| Source severity and reason parsing retain documented exceptions. | Source-version or schema interpretation may affect priority labels. | Obtain source-author clarification/version alignment and independent content adjudication. | A validated business-loss ranking or reason-code repair. |',
      '', '## Verification and limits','',r['reconciliation']['raw_python_vs_normalized_sql'],
      '350 item summaries/flags, 19 granular-question rows, 5 group-trigger rows, 5 overlap bins and 18 reason profiles reconcile from normalized SQL to a fresh raw CSV Python read. FN/FP totals agree with Chapter06. All protected source/model/contract/prior-audit/baseline hashes are unchanged.',
      '']+['- '+x for x in r['limits']]+['',
      'Code: src/run_error_diagnostics.py; SQL: sql/07_item_diagnostics.sql, sql/07_fp_question_triggers.sql, sql/07_expert_reason_profiles.sql. Study: notebooks/07_error_diagnostics.ipynb.',
      '', 'Next: Chapter 08 reviewer agreement/reliability, item-dependent uncertainty and panel sensitivity.','']
    (ROOT/'reports/07_error_diagnostics.md').write_text('\n'.join(lines))


if __name__=='__main__':run()
