import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ao_toolbox


NEEDS_LIBRARY = unittest.skipUnless(ao_toolbox.LIB.is_dir(), 'cần thư viện repo cục bộ (AO_REPO_LIBRARY)')


class ToolboxTest(unittest.TestCase):
    def test_search_accent_case_and_order_with_fake_data(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / 'fake.json').write_text('{"ten":"BrowserSkill"}', encoding='utf-8')
            rows = [
                {'id': 'browser', 'ten': 'BrowserSkill', 'loai': 'repo', 'mo_ta': 'duyệt web',
                 'dung_khi': 'lấy dữ liệu web', 'tu_khoa': ['lấy dữ liệu web'],
                 'trang_thai': 'da_dung', 'duong_dan': str(folder / 'fake.json')},
                {'id': 'unsafe', 'ten': 'unsafe-scraper', 'loai': 'repo', 'mo_ta': 'giá sản phẩm',
                 'dung_khi': 'kiểm tra giá sản phẩm', 'tu_khoa': ['kiểm tra giá sản phẩm'],
                 'trang_thai': 'khong_nen_dung', 'duong_dan': str(folder / 'fake.json')},
            ]
            self.assertEqual(ao_toolbox.search(rows, 'LAY DU LIEU WEB')[0]['id'], 'browser')
            self.assertEqual(ao_toolbox.search(rows, 'lấy dữ liệu web')[0]['id'], 'browser')
            self.assertNotIn('unsafe', [x['id'] for x in ao_toolbox.search(rows, 'kiem tra gia san pham')])

    @classmethod
    def setUpClass(cls):
        cls.result = ao_toolbox.build()
        cls.rows = cls.result['cong_cu']

    def find(self, query):
        return ao_toolbox.search(self.rows, query)

    @NEEDS_LIBRARY
    def test_real_sources(self):
        self.assertGreaterEqual(sum(x['loai'] == 'repo' and 'repo-library\\repos' in x['duong_dan'] for x in self.rows), 25)
        self.assertGreaterEqual(sum(x['loai'] == 'skill' and x.get('nguon') != 'chú thích tay; chưa xác minh' for x in self.rows), 10)

    @NEEDS_LIBRARY
    def test_web_data(self):
        found = self.find('lấy dữ liệu web')
        browser = next(i for i, x in enumerate(found) if x['id'] == 'browserskill')
        self.assertLess(browser, 3)
        self.assertIn('captcha', found[browser]['canh_bao'])
        self.assertIn('không né', found[browser]['canh_bao'])

    @NEEDS_LIBRARY
    def test_video_excel_post(self):
        self.assertIn('ffmpeg', [x['id'] for x in self.find('doc video')])
        self.assertIn('xlsx', [x['id'] for x in self.find('lam excel')])
        post = self.find('dang bai')
        self.assertTrue(any('APPROVAL BY OWNER' in x.get('canh_bao', '') for x in post))

    @NEEDS_LIBRARY
    def test_unsafe_not_normal_choice(self):
        found = self.find('kiem tra gia san pham')
        self.assertNotIn('unsafe-scraper', [x['id'] for x in found])
        unsafe = next(x for x in self.rows if x['id'] == 'unsafe-scraper')
        self.assertEqual(unsafe['trang_thai'], 'khong_nen_dung')
        self.assertIn('KHÔNG NÊN DÙNG', unsafe['canh_bao'])

    @NEEDS_LIBRARY
    def test_markdown_limit(self):
        md = ao_toolbox.markdown(self.result)
        self.assertLessEqual(len(md.splitlines()), 70)
        self.assertIn('BrowserSkill', md)


if __name__ == '__main__':
    unittest.main()
