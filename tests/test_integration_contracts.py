import json
import pytest
from cart_recovery.intelligence.integration_contracts import ROOT, read_contract
@pytest.mark.parametrize('fixture', json.loads((ROOT/'fixtures.json').read_text()), ids=lambda v:v['name'])
def test_shared_fixture(fixture):
    args = dict(merchant='merchant-a',store='store-a',subject='customer-a',episode='episode-a',expected_context_hash=fixture.get('expected_context_hash',''))
    if fixture['valid']:
        read_contract(json.dumps(fixture['payload']), **args)
    else:
        with pytest.raises(Exception):
            read_contract(json.dumps(fixture['payload']), **args)
@pytest.mark.parametrize('raw', ['{"version":"a","version":"b"}', '{"version":"\\ud800"}', '{} {}'])
def test_invalid_json(raw):
    with pytest.raises(Exception):
        read_contract(raw)
