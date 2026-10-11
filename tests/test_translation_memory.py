from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pymupdf
import pytest

from techpack_pdf import memory
from techpack_pdf.errors import TechpackError
from techpack_pdf.models import FileArtifact, JobManifest
from techpack_pdf.translation import _request_hash, translation_cache_key
from techpack_pdf_cli import main
from test_fast_review_layout import _item


@pytest.fixture
def reviewed(tmp_path):
    source = tmp_path / 'sample.pdf'
    with pymupdf.open() as doc:
        doc.new_page(width=500, height=300)
        doc.save(source)
    glossary = tmp_path / 'terms.csv'
    glossary.write_text('source_term,target_term\n', encoding='utf-8')
    artifact = lambda p: FileArtifact(filename=p.name, path=p, sha256=hashlib.sha256(p.read_bytes()).hexdigest())
    job = JobManifest(job_id='job-test', job_dir=tmp_path, created_at=datetime.now(timezone.utc),
                      source=artifact(source).model_copy(update={'page_count': 1}), glossary=artifact(glossary))
    item = _item([100, 100, 150, 110]).model_copy(update={
        'target_rect': [160, 100, 260, 125], 'font_size': 7,
    }).model_dump(mode='json')
    pipeline = dict(parser='mineru', translation_executor='host_agent', host='codex',
                    execution_mode='main_agent', model='unknown', prompt_version='1.0')
    expected = dict(items=[item], pipeline=pipeline, pages=[dict(page_index=0, width=500, height=300)])
    edited = dict(item, review_status='approved_edited', reviewed_translation='袋口打套结加固',
                  reviewed_source_bbox=item['source_bbox'], reviewed_target_rect=item['target_rect'], reviewed_font_size=7)
    payload = dict(schema_version='1.1', job_id=job.job_id,
                   source=job.source.model_dump(mode='json', exclude={'path'}),
                   glossary=job.glossary.model_dump(mode='json', exclude={'path'}), pipeline=pipeline,
                   items=[edited], blocking_issues=[], review_completed_at='2026-10-11T09:00:00+08:00')
    path = tmp_path / 'review.json'
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    return job, expected, payload, path


def learn(reviewed, path):
    job, expected, payload, review_path = reviewed
    review_path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    return memory.learn_from_review(review_path, job, expected, path)


def request(reviewed, **changes):
    job, _, payload, _ = reviewed
    item = payload['items'][0]
    values = dict(item_id=item['item_id'], source_text=item['source_text'], context=item['source_kind'],
                  page_type=item['page_type'], mode='direct', locked_tokens=[], glossary_terms=[])
    values.update(changes)
    result = dict(schema_version='1.1', job_id=job.job_id, source_sha256=job.source.sha256,
                  glossary_sha256=job.glossary.sha256, attempt=0, items=[values])
    result['request_sha256'] = _request_hash(result)
    return result


def test_learn_deduplicates_and_preserves_distinct_human_corrections(reviewed, tmp_path):
    path = tmp_path / 'memory.md'
    assert learn(reviewed, path)['added'] == 1
    before = path.read_bytes()
    assert learn(reviewed, path)['added'] == 0
    assert path.read_bytes() == before
    reviewed[2]['items'][0]['reviewed_translation'] = '袋口套结补强'
    assert learn(reviewed, path)['added'] == 1
    records = memory.read_memory(path)
    assert [r.corrected_translation for r in records] == ['袋口打套结加固', '袋口套结补强']
    assert records[0].original_translation == '袋口处打枣加固'
    assert records[0].reviewed_at == '2026-10-11T09:00:00+08:00'


@pytest.mark.parametrize('status,text', [('approved', None), ('skipped', '跳过'), ('approved_edited', '袋口处打枣加固')])
def test_position_or_font_edits_and_skips_are_not_learned(reviewed, tmp_path, status, text):
    reviewed[2]['items'][0].update(review_status=status, reviewed_translation=text, reviewed_font_size=9,
                                  reviewed_target_rect=[160, 130, 260, 155])
    assert learn(reviewed, tmp_path / 'memory.md')['added'] == 0
    assert memory.read_memory(tmp_path / 'memory.md') == []


@pytest.mark.parametrize('change', ['job', 'source', 'incomplete', 'immutable', 'tokens'])
def test_invalid_reviews_never_modify_memory(reviewed, tmp_path, change):
    path = tmp_path / 'memory.md'
    learn(reviewed, path)
    before = path.read_bytes()
    payload = reviewed[2]
    if change == 'job':
        payload['job_id'] = 'another-job'
    elif change == 'source':
        payload['source']['sha256'] = '0' * 64
    elif change == 'incomplete':
        payload['items'][0]['review_status'] = None
    elif change == 'immutable':
        payload['items'][0]['source_text'] = 'UNRELATED'
    else:
        payload['items'][0]['reviewed_translation'] = '新增 999 cm'
    with pytest.raises(TechpackError):
        learn(reviewed, path)
    assert path.read_bytes() == before


def test_recall_filters_context_and_marks_glossary_conflicts(reviewed, tmp_path):
    path = tmp_path / 'memory.md'
    learn(reviewed, path)
    recalled = memory.recall(request(reviewed), path)
    assert recalled['matches'][0]['corrected_translation'] == '袋口打套结加固'
    assert recalled['matches'][0]['requires_confirmation'] is False
    assert memory.recall(request(reviewed, page_type='bom'), path)['matches'] == []
    assert memory.recall(request(reviewed, context='description'), path)['matches'] == []
    assert memory.recall(request(reviewed, source_text='BARTACK AT SIDE SEAM'), path)['matches'] == []
    conflict = memory.recall(request(reviewed, glossary_terms=[dict(source_term='BARTACK', target_term='打枣')]), path)
    assert conflict['matches'][0]['requires_confirmation'] is True
    assert 'glossary_conflict' in conflict['matches'][0]['warnings']
    reviewed[2]['items'][0]['reviewed_translation'] = '袋口套结补强'
    learn(reviewed, path)
    assert all(m['requires_confirmation'] for m in memory.recall(request(reviewed), path)['matches'])


@pytest.mark.parametrize('text', [
    '袋口 | 加固\n保留 <br> & 字符',
    '袋口\u2028加固', '袋口\u2029加固', '袋口\v加固', '袋口\f加固',
])
def test_markdown_roundtrip_and_invalid_memory_is_preserved(reviewed, tmp_path, text):
    path = tmp_path / 'memory.md'
    reviewed[2]['items'][0]['reviewed_translation'] = text
    learn(reviewed, path)
    assert memory.read_memory(path)[0].corrected_translation == text
    path.write_text('invalid memory contents', encoding='utf-8')
    with pytest.raises(TechpackError, match='memory'):
        learn(reviewed, path)
    assert path.read_text(encoding='utf-8') == 'invalid memory contents'


def test_memory_changes_invalidate_translation_cache(reviewed, tmp_path):
    path = tmp_path / 'memory.md'
    req = request(reviewed)
    before = memory.recall(req, path)
    assert path.exists()
    assert before['matches'] == []
    learn(reviewed, path)
    after = memory.recall(req, path)
    def key(snapshot):
        return translation_cache_key(req, req['glossary_sha256'], '1.0', 'codex', 'test-model', 'main_agent',
                                     memory_sha256=snapshot['memory_sha256'])
    assert key(before) != key(after)


def test_memory_write_failure_preserves_previous_file(reviewed, tmp_path, monkeypatch):
    path = tmp_path / 'memory.md'
    learn(reviewed, path)
    before = path.read_bytes()
    reviewed[2]['items'][0]['reviewed_translation'] = '新的人工修正'
    def fail(*_args):
        raise OSError('disk full')
    monkeypatch.setattr(memory.os, 'replace', fail)
    with pytest.raises(TechpackError):
        learn(reviewed, path)
    assert path.read_bytes() == before
    assert not path.with_suffix('.md.lock').exists()


def prepare_bound_job(reviewed):
    """Bind real review/request data; upstream files are opaque at this boundary."""
    job, expected, payload, review_path = reviewed
    root = job.job_dir
    def write(name, value):
        (root / name).write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    write('manifest.json', job.model_dump(mode='json'))
    (root / 'input-source.pdf').write_bytes(job.source.path.read_bytes())
    (root / 'input-glossary.csv').write_bytes(job.glossary.path.read_bytes())
    write('translation-request.json', request(reviewed))
    write('analysis.json', {})
    write('translation-response.json', {})
    (root / 'review.html').write_text('Existing review page', encoding='utf-8')
    output = dict(schema_version='1.1', job_id=job.job_id, parser='mineru', translation_executor='host_agent',
                  pages=[dict(page_index=0, width=500, height=300, thumbnail='unused.png')],
                  items=expected['items'], blocking_issues=[])
    write('expected-output.json', dict(schema_version='1.1', job_id=job.job_id,
          source_sha256=job.source.sha256, glossary_sha256=job.glossary.sha256, output=output))
    files = dict(analysis='analysis.json', request='translation-request.json', response='translation-response.json',
                 review_html='review.html', expected_output='expected-output.json')
    digests = {field: hashlib.sha256((root / name).read_bytes()).hexdigest() for field, name in files.items()}
    write('state.json', dict(schema_version='1.1', job_id=job.job_id, source_sha256=job.source.sha256,
                            glossary_sha256=job.glossary.sha256, state='review_ready', revision=4,
                            expected_attempt=0, wait_reason='human_review', artifacts=digests))
    return root


def test_cli_learn_then_recall_does_not_change_job_or_render_pdf(reviewed, tmp_path, capsys, monkeypatch):
    import techpack_pdf.workflow as wf
    root = prepare_bound_job(reviewed)
    path = tmp_path / 'memory.md'
    original = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    def forbidden(*_args, **_kwargs):
        raise AssertionError('Memory must not render or rebuild a job')
    monkeypatch.setattr(wf, 'apply_review', forbidden)
    monkeypatch.setattr(wf, 'plan_review_items', forbidden)
    assert main(['learn-memory', '--job', str(root), '--memory', str(path)]) == 0
    assert json.loads(capsys.readouterr().out)['added'] == 1
    assert main(['recall-memory', '--job', str(root), '--memory', str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['matches'][0]['corrected_translation'] == '袋口打套结加固'
    assert result['request_sha256'] == request(reviewed)['request_sha256']
    assert all((root / name).read_bytes() == data for name, data in original.items())
    assert not list(root.glob('*.annotated.pdf'))


@pytest.mark.parametrize('filename', ['expected-output.json', 'translation-request.json'])
def test_cli_rejects_changed_bound_artifacts(reviewed, tmp_path, capsys, filename):
    root = prepare_bound_job(reviewed)
    path = tmp_path / 'memory.md'
    (root / filename).write_text('{}', encoding='utf-8')
    command = 'learn-memory' if filename == 'expected-output.json' else 'recall-memory'
    assert main([command, '--job', str(root), '--memory', str(path)]) != 0
    assert not path.exists()
    assert json.loads(capsys.readouterr().err)['code'] == 'workflow_artifact_invalid'


def test_busy_memory_preserves_data(reviewed, tmp_path):
    path = tmp_path / 'memory.md'
    learn(reviewed, path)
    before = path.read_bytes()
    lock = path.with_suffix('.md.lock')
    lock.write_text('active writer', encoding='utf-8')
    with pytest.raises(TechpackError) as error:
        learn(reviewed, path)
    assert error.value.code == 'memory_busy'
    assert lock.exists()
    assert path.read_bytes() == before
