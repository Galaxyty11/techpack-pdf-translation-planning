import json
from pathlib import Path
p=Path(r'D:/PDF翻译skill实施/output/installed-feec452-test/jobs/e898c5d57555-20260917T083119Z')
q=json.loads((p/'translation-request.json').read_text(encoding='utf-8'))
translations={
'p001-i041':'腰围松量',
'p001-i052':'腰高',
'p001-i063':'脚口弹力绳外露半长',
'p001-i074':'腰围拉量',
'p001-i085':'坐围 档上3"',
'p001-i096':'横档 裆下1"',
'p001-i107':'脚口松量',
'p001-i118':'下摆切线高（上装）/脚口切线高（下装）',
'p001-i129':'前浪含腰',
'p001-i140':'后浪含腰',
'p001-i151':'内长',
'p001-i162':'前袋开口宽',
'p001-i173':'前袋袋布长',
'p001-i184':'前袋袋布宽',
'p001-i195':'侧缝开衩长',
'p001-i206':'侧缝开衩宽',
'p001-i217':'点到点测量',
'p001-i218':'8/27/25 更新后浪放码：2XL、3XL 和 4XL',
'p001-i219':'IT 基础版型 1802283',
}
assert set(translations)=={i['item_id'] for i in q['items']}
r={k:q[k] for k in ['schema_version','job_id','source_sha256','glossary_sha256','request_sha256','attempt']}
r['items']=[dict(item_id=i['item_id'],translated_text=translations[i['item_id']],preserved_tokens=i['locked_tokens'],glossary_terms_used=[t['source_term'] for t in i['glossary_terms']],mode=i['mode'],warnings=[],translator=dict(host='codex',execution_mode='main_agent',model='unknown',agent_role=None,prompt_version='1.0')) for i in q['items']]
(p/'translation-response.json').write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8')
print('Saved translations:',len(r['items']))
