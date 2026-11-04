"""Synthetic stdin/stdout participant for C5 cross-repository acceptance."""
import json
import sys
import tempfile
from pathlib import Path
from .integration_privacy import PrivacyStore, PrivacyStop


def main():
    raw = sys.stdin.buffer.read(200001)
    task = json.loads(raw)
    if task['operation'] != 'erase' or task['privacy_generation'] != 2:
        raise ValueError('synthetic first erasure required')
    with tempfile.TemporaryDirectory(prefix='c5-privacy-') as folder:
        store = PrivacyStore(Path(folder)/'fixture.sqlite')
        for kind in ['input_snapshot','journal','result','plan','judgment','pending_job']:
            store.put(task['merchant_id'],task['store_id'],artifact_id=kind,subject=task['subject_key'],generation=1,kind=kind,sources=task['source_keys'],payload={'private':'synthetic derivative'},expires=200,now=100)
        receipt, _ = store.apply(task['merchant_id'],task['store_id'],raw,now=101)
        try:
            store.read(task['merchant_id'],task['store_id'],'result',now=102)
        except PrivacyStop:
            pass
        else:
            raise AssertionError('erased result still readable')
        assert store.apply(task['merchant_id'],task['store_id'],raw,now=102)[0] == receipt
        print(json.dumps(receipt))


if __name__ == '__main__':
    main()
