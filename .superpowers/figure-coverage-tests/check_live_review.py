import json
from pathlib import Path
from html.parser import HTMLParser

class EmbeddedData(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.data = ''
    def handle_starttag(self, tag, attrs):
        if tag == 'script' and dict(attrs).get('id') == 'review-data':
            self.active = True
    def handle_endtag(self, tag):
        if tag == 'script':
            self.active = False
    def handle_data(self, data):
        if self.active:
            self.data += data

job = Path('D:/PDF翻译skill实施/.techpack-jobs/figure-coverage-test/88fa7b84c158-20260914T144518Z')
parser = EmbeddedData()
parser.feed((job/'review.html').read_text(encoding='utf-8'))
data = json.loads(parser.data)
assert len(data['items']) == 33
assert len(data['pages']) == 5
assert all(i['review_status'] is None for i in data['items'])
expected = ['ZIPPER GARAGE', 'DNTS @ SS AND POCKET ENTRY', '3G REVERSE COIL ZIPPER', 'BARTACK', 'CSG ACTIVE HT LABEL & SIZE/COO LABEL SEWN INTO WAISTBAND SEAM', 'SHOWN ACTUAL SIZE', '100% POLYESTER', 'VARIABLE SZ FONT CHAMPS SPORT STANDARD', 'VARIABLE COO ENGLISH & FRENCH FONT HELVETICA NEUE', 'VARIABLE CONTENT & CARE', 'CSG044 - CSG ACTIVE HEAT SEAL LABEL']
for source in expected:
    matches = [i for i in data['items'] if i['source_text'] == source]
    assert len(matches) == 1, source
    assert matches[0]['coordinate_confidence'] == 'low'
    assert matches[0]['suggested_translation']
assert not list(job.glob('*.annotated.pdf'))
print('PASS: 5 pages, 33 pending items, all 11 visual additions translated and present exactly once; no PDF export.')
