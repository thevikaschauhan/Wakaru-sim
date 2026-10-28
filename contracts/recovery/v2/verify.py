#!/usr/bin/env python3
"""Strict AF01 schemas plus cross-field semantics. Requires jsonschema==4.26.0."""
import hashlib,json,math,sys
from datetime import datetime
from pathlib import Path
from jsonschema import Draft202012Validator,FormatChecker
ROOT=Path(__file__).resolve().parent
CAT=json.loads((ROOT/'judgment-catalog.json').read_text())
def no_duplicates(pairs):
    out={}
    for k,v in pairs:
        if k in out: raise ValueError('duplicate key: '+k)
        out[k]=v
    return out
def loads(raw):return json.loads(raw,object_pairs_hook=no_duplicates,parse_constant=lambda v: (_ for _ in ()).throw(ValueError(v)))
def canonical(value):
    # Semantic request identities use ASCII keys, integer revisions/money and decimal
    # strings for measured values. Probability results are not request identities.
    def check(v):
        if isinstance(v,float):raise ValueError('request identity uses integers or decimal strings')
        if isinstance(v,dict):
            for k,x in v.items():
                if not k.isascii():raise ValueError('non-ASCII identity key')
                check(x)
        elif isinstance(v,list):
            for x in v:check(x)
    check(value)
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
def validate(name,v):
    schema=loads((ROOT/name).read_text());Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema,format_checker=FormatChecker()).validate(v)
    semantic(v)
def semantic(v):
    def safe_text(x):
        if isinstance(x,str):
            if '\x00' in x:raise ValueError('NUL text is not persistable')
            x.encode('utf-8')
        elif isinstance(x,dict):
            for k,item in x.items():safe_text(k);safe_text(item)
        elif isinstance(x,list):
            for item in x:safe_text(item)
    safe_text(v)
    version=v.get('schema_version')
    if version in ['recovery_journey_v2','recovery_context_v2'] and datetime.fromisoformat(v['expires_at'])<=datetime.fromisoformat(v['abandoned_at']):raise ValueError('invalid expiry')
    if version=='recovery_context_v2':
        if v['conversation']['merchant_id']!=v['merchant_id'] or v['conversation']['journey_id']!=v['journey_id'] or v['conversation']['revision']!=v['conversation_revision']:raise ValueError('foreign conversation')
        if any(e['merchant_id']!=v['merchant_id'] or e['store_id']!=v['store_id'] or datetime.fromisoformat(e['received_at'])>datetime.fromisoformat(v['cutoff_at']) for e in v['events']):raise ValueError('foreign or future evidence')
        for w in v['allowed_windows']:
            if not datetime.fromisoformat(w['starts_at'])<datetime.fromisoformat(w['ends_at'])<=datetime.fromisoformat(v['expires_at']):raise ValueError('invalid window')
    if version=='followup_judgment_request_v1':
        ids=[q['instance_id'] for q in v['questions']]
        if len(ids)!=len(set(ids)):raise ValueError('duplicate question')
        for q in v['questions']:
            if q['task_id']=='TF08' and set(q['options'])!=set(v['state']['allowed_window_ids']+['no_preference']):raise ValueError('foreign window')
            if q['required'] and q['applicability']=='not_applicable':raise ValueError('inconsistent applicability')
        if len(canonical(v['state']))>v['max_input_bytes']:raise ValueError('input overflow')
        if hashlib.sha256(canonical(v['state'])).hexdigest()!=v['semantic_input_sha256']:raise ValueError('semantic input hash mismatch')
    if version=='followup_judgment_result_v1' and v['disposition']=='pass' and v['service_status']!='evaluated':raise ValueError('unavailable cannot pass')
    if version=='followup_judgment_result_v1' and 'answer' in v:
        a=v['answer']
        if 'probabilities' in a:
            p=a['probabilities']
            if abs(sum(p.values())-1)>1e-6:raise ValueError('distribution sum')
            if a['type']=='choice' and (a['choice'] not in p or p[a['choice']]+1e-6<max(p.values())):raise ValueError('choice not maximum')
            if a['type']=='score' and abs(sum(int(k)*x for k,x in p.items())-a['value'])>1e-6:raise ValueError('score weighted value')
        if v['disposition']=='pass':
            task=v['task_id']; confidence=a.get('confidence',0);choice=a.get('choice');p=a.get('probabilities',{})
            good={'TF01':choice in ['useful_change','no_useful_change'] and confidence>=.80,
                  'TF02':choice in ['explicit_report','behavior_only'] and confidence>=.85,
                  'TF03':a.get('value',0)>=2 and confidence>=.80,
                  'TF04':choice in ['adds_useful_information','answers_prior_question'] and confidence>=.85,
                  'TF05':choice=='supported' and p.get('supported',0)>=.95 and confidence>=.90,
                  'TF06':a.get('yes_probability',1)<=.05,
                  'TF07':choice=='consistent' and p.get('consistent',0)>=.95 and confidence>=.90,
                  'TF08':False}[task]
            if not good:raise ValueError('rehearsal threshold not met')
    if version=='followup_semantic_bundle_v1':
        expected=set(v['expected_instance_ids']); completed=set(v['completed_instance_ids']); consumed=set(v['consumed_instance_ids']);unused=set(v['unused_instance_ids'])
        for key in ['expected_instance_ids','completed_instance_ids','consumed_instance_ids','unused_instance_ids']:
            if len(v[key])!=len(set(v[key])):raise ValueError('duplicate coverage')
        if completed!=expected or consumed&unused or consumed|unused!=completed:raise ValueError('incomplete check coverage')
        rows={j['instance_id']:j for j in v['judgments']}
        if len(rows)!=len(v['judgments']) or set(rows)!=completed:raise ValueError('judgment coverage mismatch')
        for j in rows.values():
            semantic(j)
            for key in ['binding','request_sha256','semantic_input_sha256']:
                if j[key]!=v[key]:raise ValueError('stale or foreign binding')
        if v['disposition']=='pass':
            c=v['coverage']
            if not all(c[x] for x in ['history_complete','evidence_complete']) or c['missing_paths']:raise ValueError('incomplete input')
            if v['binding']['phase']=='draft' and not(c['full_text_checked'] and c['claim_extraction_complete']):raise ValueError('incomplete draft coverage')
            if any(rows[x]['disposition'] not in ['pass','not_applicable'] for x in consumed):raise ValueError('failed check averaged away')
def verify_pair(request,result):
    validate('judgment-request.schema.json',request);validate('judgment-result.schema.json',result)
    if request['binding']!=result['binding'] or request['semantic_input_sha256']!=result['semantic_input_sha256']:raise ValueError('binding conflict')
    if hashlib.sha256(canonical(request)).hexdigest()!=result['request_sha256']:raise ValueError('request hash conflict')
    q=next((q for q in request['questions'] if q['instance_id']==result['instance_id']),None)
    if not q or q['task_id']!=result['task_id']:raise ValueError('foreign check')
    if 'answer' in result and q['type']=='choice' and set(q['options'])!=set(result['answer']['probabilities']):raise ValueError('foreign options')
    refs=request['state']['fact_refs']+request['state']['delta_refs']+request['state']['conversation_refs']+[x['source_ref'] for x in request['state']['spans']]
    if any(r not in refs for r in result['input_evidence_refs']):raise ValueError('foreign source')
def verify_bundle(request,bundle):
    validate('judgment-request.schema.json',request);validate('semantic-bundle.schema.json',bundle)
    questions={q['instance_id']:q for q in request['questions']}
    if set(bundle['expected_instance_ids'])!=set(questions):raise ValueError('request coverage mismatch')
    if not {q['instance_id'] for q in questions.values() if q['required']}<=set(bundle['consumed_instance_ids']):raise ValueError('mandatory check not consumed')
    for result in bundle['judgments']:verify_pair(request,result)
    if bundle['disposition']=='pass' and request['binding']['phase']=='draft':
        required=[q for q in questions.values() if q['required']]
        if not {'TF04','TF06','TF07'}<={q['task_id'] for q in required}:raise ValueError('mandatory draft task missing')
        claims=set(request['state']['coverage']['claim_ids'])
        if claims!={q['subject_id'] for q in required if q['task_id']=='TF05'}:raise ValueError('claim check coverage missing')
def main():
    manifest=loads((ROOT/'manifest.json').read_text())
    for name,digest in manifest['sha256'].items():
        if hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=digest:raise ValueError('manifest mismatch '+name)
    cases=loads((ROOT/'conformance.json').read_text())
    for c in cases:
        try:validate(c['schema'],c['value']);ok=True
        except Exception:ok=False
        if ok!=c['valid']:raise ValueError('fixture failed '+c['name'])
    if len(sys.argv)>2:validate(sys.argv[1],loads(Path(sys.argv[2]).read_text()))
    print(f'PASS: {len(cases)} conformance fixtures; {len(manifest["sha256"])} pinned artifacts')
if __name__=='__main__':main()
