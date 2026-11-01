import copy,hashlib,json,unittest
from verify import ROOT,validate,verify_pair,verify_bundle,canonical,loads
class ContractSemantics(unittest.TestCase):
    def setUp(self):
        self.fixtures=loads((ROOT/'conformance.json').read_text())
        self.request=copy.deepcopy(next(x['value'] for x in self.fixtures if x['name']=='complete-request'))
        self.result=copy.deepcopy(next(x['value'] for x in self.fixtures if x['name']=='complete-review-bundle')['judgments'][0])
    def test_response_binding_and_evidence(self):
        verify_pair(self.request,self.result)
        for field in ['snapshot_sha256','policy_sha256','resolved_model']:
            bad=copy.deepcopy(self.result);bad['binding'][field]='0'*64
            with self.assertRaises(Exception):verify_pair(self.request,bad)
        bad=copy.deepcopy(self.result);bad['input_evidence_refs']=[dict(id='foreign',revision=1,sha256='a'*64)]
        with self.assertRaises(Exception):verify_pair(self.request,bad)
    def test_same_key_different_payload_changes_identity(self):
        other=copy.deepcopy(self.request);other['deadline_ms']+=1
        self.assertEqual(other['idempotency_key'],self.request['idempotency_key'])
        self.assertNotEqual(hashlib.sha256(canonical(other)).hexdigest(),self.result['request_sha256'])
        with self.assertRaises(Exception):verify_pair(other,self.result)
    def test_canonical_order_and_array_order(self):
        self.assertEqual(canonical({'b':2,'a':'é'}),b'{"a":"\xc3\xa9","b":2}')
        self.assertNotEqual(canonical([1,2]),canonical([2,1]))
        for bad in ['{"x":1,"x":2}','{"p":NaN}','{"p":Infinity}']:
            with self.assertRaises(Exception):loads(bad)
    def test_complete_bundle_must_match_request(self):
        b=copy.deepcopy(next(x['value'] for x in self.fixtures if x['name']=='complete-review-bundle'))
        verify_bundle(self.request,b)
        b['consumed_instance_ids']=[];b['unused_instance_ids']=b['expected_instance_ids']
        with self.assertRaises(Exception):verify_bundle(self.request,b)
    def test_noul_missing_output_is_not_zero(self):
        r=copy.deepcopy(next(x['value'] for x in self.fixtures if x['name']=='TF06-valid'))
        r['service_status']='unavailable';r.pop('answer');r['error_code']='timeout';r['disposition']='requires_review'
        validate('judgment-result.schema.json',r)
        r['disposition']='pass'
        with self.assertRaises(Exception):validate('judgment-result.schema.json',r)
if __name__=='__main__':unittest.main()
