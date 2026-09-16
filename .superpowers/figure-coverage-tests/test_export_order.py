import sys
import json
from pathlib import Path

sys.path.insert(0, 'D:/PDF翻译skill实施/.worktrees/figure-annotation-coverage/scripts')
import techpack_pdf.workflow as w


def test_visual_additions_keep_existing_translation_contract_order(monkeypatch):
    directory = Path('D:/PDF翻译skill实施/.techpack-jobs/figure-coverage-test/88fa7b84c158-20260914T144518Z')
    job = w._load_job(directory)
    analysis = w._effective_analysis(directory, job)
    request_bytes = (directory / 'translation-request.json').read_bytes()
    request = json.loads(request_bytes)
    assert request_bytes == w._canonical_request_bytes(directory, analysis, job)
    ids = [c.item_id for c in analysis.candidates if c.should_translate]
    assert len(ids) == len(set(ids)) == 33
    assert sorted(ids) == [item['item_id'] for item in request['items']]
    expected = w._load_model(directory, 'expected-output.json', w._ExpectedOutputSnapshot)
    state = w._load_state(directory, job)
    # Isolate the expensive layout renderer, preserving its original item order.
    def render(directory, job, rebuilt_analysis, translations):
        assert [c.item_id for c in rebuilt_analysis.candidates if c.should_translate] == [i.item_id for i in expected.output.items]
        return expected.output
    monkeypatch.setattr(w, '_trusted_output', render)
    w._verify_apply_integrity_closure(directory, job, state, expected)
