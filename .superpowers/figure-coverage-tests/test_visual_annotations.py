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
    page = req['pages'][0]
    groups = ['ALL VISIBLE GROUPS'] if page['required_business_area'] == 'bom_color_groups' else []
    return {
        **{k:v for k,v in req.items() if k not in {'pages', 'required_stage'}},
        'production_stage': req['required_stage'],
        'stage_evidence': 'Stage was determined from the bound page types.',
        'pages':[dict(
            page_index=0,
            business_area=page['required_business_area'],
            checked=True,
            evidence='Viewed zipper figure and its callout arrows',
            unresolved=[],
            annotations=[dict(text='BARTACK', bbox=[10,20,40,30])],
            protected_item_ids=[],
            visible_color_groups=groups,
            required_color_groups=groups,
            checked_color_groups=groups,
        )],
    }

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
    assert is_existing_annotation(a,[SimpleNamespace(text='bartack',bbox=[10,20,40,30],should_translate=True)])
    assert not is_existing_annotation(a,[SimpleNamespace(text='bartack',bbox=[10,20,40,30],should_translate=False)])
    assert not is_existing_annotation(a,[SimpleNamespace(text='bartack',bbox=[100,20,140,30],should_translate=True)])

def test_request_exposes_existing_selection_status_and_business_scope():
    page = SimpleNamespace(
        page_index=0,
        page_type=PageType.BOM,
        thumbnail='thumbnails/page-0001.png',
        width=300,
        height=200,
        nodes=[SimpleNamespace(
            text='MAIN FABRIC',
            bbox=[10,20,80,30],
            should_translate=False,
            decision_reason='skipped_admin',
        )],
    )
    req = coverage_request(SimpleNamespace(
        schema_version='1.1', job_id='job', source_sha256='a'*64,
        glossary_sha256='b'*64, pages=[page],
    ))

    assert req['required_stage'] == 'ambiguous'
    assert req['pages'][0]['required_business_area'] == 'bom_color_groups'
    assert req['pages'][0]['existing_annotations'][0]['should_translate'] is False
    assert req['pages'][0]['existing_annotations'][0]['decision_reason'] == 'skipped_admin'

def test_stage_requires_explicit_document_evidence():
    pages = [
        SimpleNamespace(
            page_index=0,
            page_type=PageType.BOM,
            thumbnail='thumbnails/page-0001.png',
            width=300,
            height=200,
            nodes=[SimpleNamespace(
                text='MAIN FABRIC',
                bbox=[10,20,80,30],
                should_translate=True,
                decision_reason='page_rule',
            )],
        ),
        SimpleNamespace(
            page_index=1,
            page_type=PageType.MEASUREMENT,
            thumbnail='thumbnails/page-0002.png',
            width=300,
            height=200,
            nodes=[],
        ),
    ]
    analysis = SimpleNamespace(
        schema_version='1.1',
        job_id='job',
        source_sha256='a'*64,
        glossary_sha256='b'*64,
        pages=pages,
    )

    request_without_heading = coverage_request(analysis)
    assert request_without_heading['required_stage'] == 'ambiguous'
    assert request_without_heading['stage_evidence'] == []

    pages[0].nodes.append(SimpleNamespace(
        text='BULK PRODUCTION STAGE',
        bbox=[10,40,100,50],
        should_translate=False,
        decision_reason='skipped_admin',
    ))
    request_with_heading = coverage_request(analysis)
    assert request_with_heading['required_stage'] == 'bulk'
    assert request_with_heading['stage_evidence'] == ['BULK PRODUCTION STAGE']

def test_bulk_bom_requires_every_visible_color_group_checked():
    page = SimpleNamespace(page_index=0, page_type=PageType.BOM, thumbnail='thumbnails/page-0001.png', width=300, height=200, nodes=[])
    req = coverage_request(SimpleNamespace(schema_version='1.1', job_id='job', source_sha256='a'*64, glossary_sha256='b'*64, pages=[page]))
    data = response(req)
    data['production_stage'] = 'ambiguous'
    data['stage_evidence'] = 'Only a BOM page is present, so the stage is ambiguous.'
    data['pages'][0].update(
        business_area='bom_color_groups',
        visible_color_groups=['BLACK', 'NAVY'],
        required_color_groups=['BLACK'],
        checked_color_groups=['BLACK'],
    )

    with pytest.raises(ValueError):
        validate_coverage(req, data)

    data['pages'][0]['required_color_groups'] = ['BLACK', 'NAVY']
    data['pages'][0]['checked_color_groups'] = ['BLACK', 'NAVY']
    assert validate_coverage(req, data).production_stage == 'ambiguous'

def test_skipped_extracted_node_can_be_resubmitted_by_visual_review(tmp_path, monkeypatch):
    import techpack_pdf.workflow as w
    from techpack_pdf.glossary import load_glossary
    from techpack_pdf.matching import MatchedNode
    from techpack_pdf.models import CoordinateConfidence
    from techpack_pdf.selection import (
        PageClassification,
        PageNode,
        SelectionPage,
        select_candidates,
    )

    glossary_path = tmp_path / 'input-glossary.csv'
    glossary_path.write_text('source_term,target_term\nbartack,打枣\n', encoding='utf-8')
    glossary = load_glossary(glossary_path)
    monkeypatch.setattr(w, '_snapshot_glossary_path', lambda directory, job: glossary_path)
    matched = MatchedNode(
        mineru_index=0,
        native_index=0,
        text='BARTACK',
        source_bbox=(10.0, 20.0, 40.0, 30.0),
        mineru_bbox=(10.0, 20.0, 40.0, 30.0),
        coordinate_confidence=CoordinateConfidence.HIGH,
        similarity=1.0,
        distance_ratio=0.0,
        auto_approvable=True,
    )
    selected = select_candidates(
        SelectionPage(
            0,
            PageClassification(PageType.TECHNICAL_DRAWING, .99, ('title:technical drawing',)),
            (PageNode(matched, 'table_header'),),
        ),
        glossary,
    )[0]
    assert selected.should_translate is False
    analysis = w._AnalysisSnapshot.model_validate(dict(
        schema_version='1.1',
        job_id='job',
        source_sha256='a'*64,
        glossary_sha256='b'*64,
        parser='mineru',
        visual_check_required=True,
        pages=[dict(
            page_index=0,
            page_type='technical_drawing',
            confidence=.99,
            evidence=['title:technical drawing'],
            thumbnail='thumbnails/page-0001.png',
            width=300,
            height=200,
            nodes=[dict(text='BARTACK', bbox=[10,20,40,30], field_role='table_header')],
        )],
        candidates=[w._candidate_snapshot(selected).model_dump(mode='json')],
    ))
    assert w._with_visual_annotations(tmp_path, None, analysis) is None
    req = json.loads((tmp_path / 'visual-annotations-request.json').read_text())
    data = response(req)
    (tmp_path / 'visual-annotations-response.json').write_text(
        json.dumps(data, ensure_ascii=False),
        encoding='utf-8',
    )

    result = w._with_visual_annotations(tmp_path, None, analysis)

    assert len(result.candidates) == 2
    assert result.candidates[0].should_translate is False
    assert result.candidates[1].source_text == 'BARTACK'
    assert result.candidates[1].should_translate is True

def test_visual_review_can_protect_existing_print_artwork_trademark(tmp_path, monkeypatch):
    import techpack_pdf.workflow as w
    from techpack_pdf.glossary import load_glossary
    from techpack_pdf.matching import MatchedNode
    from techpack_pdf.models import CoordinateConfidence
    from techpack_pdf.selection import (
        PageClassification,
        PageNode,
        SelectionPage,
        select_candidates,
    )

    glossary_path = tmp_path / 'input-glossary.csv'
    glossary_path.write_text('source_term,target_term\n', encoding='utf-8')
    glossary = load_glossary(glossary_path)
    monkeypatch.setattr(w, '_snapshot_glossary_path', lambda directory, job: glossary_path)
    matched = MatchedNode(
        mineru_index=0,
        native_index=0,
        text='NIKE',
        source_bbox=(10.0, 20.0, 50.0, 32.0),
        mineru_bbox=(10.0, 20.0, 50.0, 32.0),
        coordinate_confidence=CoordinateConfidence.HIGH,
        similarity=1.0,
        distance_ratio=0.0,
        auto_approvable=True,
    )
    selected = select_candidates(
        SelectionPage(
            0,
            PageClassification(PageType.PRINT_ARTWORK, .99, ('title:print artwork',)),
            (PageNode(matched, 'body'),),
        ),
        glossary,
    )[0]
    assert selected.should_translate is True
    analysis = w._AnalysisSnapshot.model_validate(dict(
        schema_version='1.1',
        job_id='job',
        source_sha256='a'*64,
        glossary_sha256='b'*64,
        parser='mineru',
        visual_check_required=True,
        pages=[dict(
            page_index=0,
            page_type='print_artwork',
            confidence=.99,
            evidence=['title:print artwork'],
            thumbnail='thumbnails/page-0001.png',
            width=300,
            height=200,
            nodes=[dict(text='NIKE', bbox=[10,20,50,32], field_role='body')],
        )],
        candidates=[w._candidate_snapshot(selected).model_dump(mode='json')],
    ))
    assert w._with_visual_annotations(tmp_path, None, analysis) is None
    req = json.loads((tmp_path / 'visual-annotations-request.json').read_text())
    data = response(req)
    data['pages'][0]['annotations'] = []
    data['pages'][0]['protected_item_ids'] = ['p001-i001']
    (tmp_path / 'visual-annotations-response.json').write_text(
        json.dumps(data, ensure_ascii=False),
        encoding='utf-8',
    )

    result = w._with_visual_annotations(tmp_path, None, analysis)

    assert len(result.candidates) == 1
    assert result.candidates[0].should_translate is False
    assert result.candidates[0].decision_reason.value == 'skipped_code'

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
