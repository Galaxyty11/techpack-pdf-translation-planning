import json
from types import SimpleNamespace
import sys
from pathlib import Path
import pytest

ROOT = Path(r'D:/PDF翻译skill实施/.worktrees/figure-annotation-coverage')
sys.path.insert(0, str(ROOT / 'scripts'))
from techpack_pdf.visual_annotations import coverage_request, validate_coverage, Annotation, is_existing_annotation
from techpack_pdf.models import PageType

def request():
    page = SimpleNamespace(page_index=0, page_type=PageType.TECHNICAL_DRAWING, thumbnail='thumbnails/page-0001.png', width=300, height=200, nodes=[])
    return coverage_request(SimpleNamespace(schema_version='1.1', job_id='job', source_sha256='a'*64, glossary_sha256='b'*64, pages=[page]))

def response(req):
    return {**{k:v for k,v in req.items() if k != 'pages'}, 'pages':[dict(page_index=0, checked=True, evidence='Viewed zipper figure and its callout arrows', unresolved=[], annotations=[dict(text='BARTACK', bbox=[10,20,40,30])])]}

def test_exact_binding_and_text():
    req=request()
    value=validate_coverage(req,response(req))
    assert value.pages[0].annotations[0].text == 'BARTACK'

@pytest.mark.parametrize('change', ['job', 'missing', 'duplicate', 'outside', 'empty', 'nan', 'bool_coordinate', 'string_coordinate'])
def test_bad_coverage_rejected(change):
    req=request(); data=response(req)
    if change=='job': data['job_id']='different'
    if change=='missing': data['pages']=[]
    if change=='duplicate': data['pages']*=2
    if change=='outside': data['pages'][0]['annotations'][0]['bbox']=[10,20,400,30]
    if change=='empty': data['pages'][0]['annotations'][0]['text']=' '
    if change=='nan': data['pages'][0]['annotations'][0]['bbox']=[10,20,float('nan'),30]
    if change=='bool_coordinate': data['pages'][0]['annotations'][0]['bbox']=[True,20,40,30]
    if change=='string_coordinate': data['pages'][0]['annotations'][0]['bbox']=['10',20,40,30]
    with pytest.raises(ValueError): validate_coverage(req,data)

def test_dedup_requires_same_location():
    a=Annotation(text='BARTACK',bbox=[10,20,40,30])
    assert is_existing_annotation(a,[SimpleNamespace(text='bartack',bbox=[10,20,40,30])])
    assert not is_existing_annotation(a,[SimpleNamespace(text='bartack',bbox=[100,20,140,30])])

@pytest.mark.parametrize('page_type', ['technical_drawing','construction_detail','label_pack','bom','measurement','sample_review','style_sample','general_info','how_to_measure'])
def test_workflow_blocks_unchecked_pages_and_integrates_production_text(tmp_path, monkeypatch, page_type):
    import techpack_pdf.workflow as w
    from techpack_pdf.glossary import load_glossary
    g=tmp_path/'input-glossary.csv'; g.write_text('source_term,target_term\nbartack,打枣\n',encoding='utf-8')
    monkeypatch.setattr(w, '_snapshot_glossary_path', lambda directory,job:g)
    analysis=w._AnalysisSnapshot.model_validate(dict(schema_version='1.1',job_id='job',source_sha256='a'*64,glossary_sha256='b'*64,parser='mineru',visual_check_required=True,pages=[dict(page_index=0,page_type=page_type,confidence=.99,evidence=['Visual production callout'],thumbnail='thumbnails/page-0001.png',width=300,height=200,nodes=[])],candidates=[]))
    assert w._with_visual_annotations(tmp_path,None,analysis) is None
    req=json.loads((tmp_path/'visual-annotations-request.json').read_text())
    data=response(req); data['pages'][0]['unresolved']=['small dimension unreadable']
    (tmp_path/'visual-annotations-response.json').write_text(json.dumps(data),encoding='utf-8')
    assert w._with_visual_annotations(tmp_path,None,analysis) is None
    data['pages'][0]['unresolved']=[]
    (tmp_path/'visual-annotations-response.json').write_text(json.dumps(data),encoding='utf-8')
    result=w._with_visual_annotations(tmp_path,None,analysis)
    assert len(result.candidates)==1
    c=result.candidates[0]
    assert c.source_text=='BARTACK' and c.should_translate
    assert c.glossary_hits[0].target_term=='打枣'
    assert c.coordinate_confidence.value=='low' and not c.auto_approvable
    assert w._with_visual_annotations(tmp_path,None,analysis)==result
    assert analysis.candidates==[]
    data['pages'][0]['checked']=False
    (tmp_path/'visual-annotations-response.json').write_text(json.dumps(data),encoding='utf-8')
    assert w._with_visual_annotations(tmp_path,None,analysis) is None
