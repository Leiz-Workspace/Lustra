# coding=UTF-8
"""
Lustra: gender filter tier 0 + safety blacklist tests (offline)
docs/plans/gender-filter-model.md: AC-G01..AC-G05, AC-S01..AC-S03
Run: python -m unittest lustra.tests.test_gender
"""
#########################################
#
#

import json
import pathlib
import tempfile
from multiprocessing.dummy import current_process
from unittest import TestCase, mock

from lustra.logger import Logger

Logger.init(True, True)

from lustra import safety  # noqa: E402
from lustra.cmdargs import prepare_arglist  # noqa: E402
from lustra.downloaders import _make_downloader  # noqa: E402
from lustra.extensions import Extensions  # noqa: E402
from lustra.gender import GenderConf, GenderLabel, decide, infer_from_tags, rules_for_module  # noqa: E402
from lustra.module import ProcModule  # noqa: E402
from lustra.network import HTMLContent  # noqa: E402

H, M_, L_ = GenderConf.HIGH, GenderConf.MEDIUM, GenderConf.LOW
F, MA, B, N, U = GenderLabel.ONLY_FEMALE, GenderLabel.ONLY_MALE, GenderLabel.BOTH, GenderLabel.NONE, GenderLabel.UNKNOWN

# (module, tags, expected label, expected confidence, required flags)
CASES = [
    # danbooru (completeness .95)
    ('db', '1girl 1boy hetero', B, H, set()),
    ('db', '1girl solo', F, H, set()),
    ('db', '2girls yuri', F, H, set()),
    ('db', 'multiple_girls', F, H, set()),
    ('db', '1boy male_focus', MA, H, set()),
    ('db', 'multiple_boys yaoi', MA, H, set()),
    ('db', 'yuri', F, M_, set()),
    ('db', '1girl penis', B, H, set()),
    ('db', '1girl futanari solo', F, H, {'futa'}),
    ('db', 'futanari futa_with_male 1boy', B, H, {'futa'}),
    ('db', 'trap 1boy', MA, H, {'trap'}),
    ('db', 'otoko_no_ko', MA, H, {'trap'}),
    ('db', 'reverse_trap', F, H, {'trapFemale'}),
    ('db', '1girl 1other', F, M_, {'other'}),
    ('db', '1other', N, M_, {'other'}),
    ('db', 'no_humans scenery', N, H, set()),
    ('db', 'scenery sky cloud', U, None, set()),
    ('db', 'furry_female solo', F, H, {'anthro'}),
    ('db', '1girl disembodied_penis', B, H, set()),
    ('db', 'photo_(medium) 1girl', F, H, {'real'}),
    # rule34 (completeness .85: count tags alone only reach medium)
    ('rx', '1girls female_only', F, H, set()),
    ('rx', '1girls solo', F, M_, set()),
    ('rx', 'female', F, M_, set()),
    ('rx', '1boy male_only', MA, H, set()),
    ('rx', 'straight', B, H, set()),
    ('rx', 'hetero', B, H, set()),
    ('rx', 'female male', B, H, set()),
    ('rx', 'female_only 1boy', MA, L_, set()),
    ('rx', 'futa_only', F, H, {'futa'}),
    ('rx', 'futanari 1futa solo', F, M_, {'futa'}),
    ('rx', 'cuntboy', MA, M_, {'futaMale'}),
    ('rx', 'femboy 1boy', MA, M_, {'trap'}),
    ('rx', '1girls male_pov', B, H, {'pov'}),
    ('rx', 'female penis futanari', F, M_, {'futa'}),
    ('rx', 'lesbian', F, M_, set()),
    ('rx', 'ambiguous_gender', N, M_, {'other'}),
    # e621 (completeness .92, pair tags, solo marker)
    ('en', 'male solo anthro', MA, H, {'anthro'}),
    ('en', 'female solo', F, H, set()),
    ('en', 'male/female duo', B, H, set()),
    ('en', 'male/male', MA, H, set()),
    ('en', 'female/female', F, H, set()),
    ('en', 'gynomorph/male', B, H, {'futa'}),
    ('en', 'intersex solo', F, H, {'futa'}),
    ('en', 'andromorph', MA, H, {'futaMale'}),
    ('en', 'ambiguous_gender solo', N, M_, {'other'}),
    ('en', 'female male_pov', B, H, {'pov'}),
    # generic / unmeasured sites
    ('xb', '1girl solo', F, M_, set()),
    ('rp', 'Some_Character Series_Name', U, None, set()),
    ('rp', 'yuri', F, M_, set()),
]


class GenderRulesTests(TestCase):
    def test_table(self) -> None:
        for module, tags, label, conf, flags in CASES:
            with self.subTest(module=module, tags=tags):
                res = infer_from_tags(tags.split(), rules_for_module(module))
                self.assertEqual(label, res.label)
                self.assertEqual(conf, res.conf)
                self.assertTrue(flags.issubset(res.flags), f'flags {sorted(res.flags)} lack {sorted(flags)}')

    def test_evidence_and_case(self) -> None:
        res = infer_from_tags(['1Girl', '1BOY', 'hetero'], rules_for_module('db'))
        self.assertEqual((B, H), (res.label, res.conf))
        self.assertIn('1girl', res.evidence)
        self.assertIn('1boy', res.evidence)

    def test_decide_threshold_and_strict(self) -> None:
        db = rules_for_module('db')
        futa = infer_from_tags(['1girl', 'futanari', 'solo'], db)
        self.assertEqual((True, F), decide(futa, {'only_female'}, M_, False))
        self.assertEqual((False, U), decide(futa, {'only_female'}, M_, True))  # AC-G03
        rx_medium = infer_from_tags(['1girls', 'solo'], rules_for_module('rx'))
        self.assertTrue(decide(rx_medium, {'only_female'}, M_, False)[0])
        self.assertEqual((False, U), decide(rx_medium, {'only_female'}, H, False))
        self.assertEqual((True, U), decide(rx_medium, {'unknown'}, H, False))


class SafetyTests(TestCase):
    def test_every_listed_tag_is_blocked(self) -> None:
        for tag in sorted(safety.BLOCKED_TAGS | safety.BLOCKED_TOKENS):
            with self.subTest(tag=tag):
                self.assertTrue(safety.is_blocked([tag]))
                self.assertTrue(safety.is_blocked([tag.upper()]))

    def test_token_matching(self) -> None:
        for tag in ('younger_female', 'Young', 'young_male', 'onee-shota', 'loli_(style)', 'cub/adult', 'very_young', 'teen_titans'):
            with self.subTest(tag=tag):
                self.assertTrue(safety.is_blocked(['1girl', tag]))
        for tag in ('cat_ears', 'adult', 'mature_female', 'childhood_friend', 'youkai', 'kidney', 'cube', 'scenery'):
            with self.subTest(tag=tag):
                self.assertFalse(safety.is_blocked([tag]))

    def test_extensions_can_only_add(self) -> None:
        self.assertFalse(safety.is_blocked(['some_site_specific_tag']))
        safety.register_extra_tags(['Some_Site_Specific_Tag'])
        self.assertTrue(safety.is_blocked(['some_site_specific_tag']))
        public = [n for n in dir(safety) if not n.startswith('_')]
        self.assertFalse([n for n in public if any(w in n.lower() for w in ('remove', 'unregister', 'disable', 'clear', 'allow'))])


POSTS = [
    (1, '1girl solo'), (2, '1boy male_focus'), (3, '1girl 1boy hetero'), (4, 'no_humans scenery'),
    (5, 'younger_female 1girl'), (6, 'aged_down 1girl solo'), (7, 'landscape'),
]


def _db_posts_json() -> bytes:
    return json.dumps([{
        'id': i, 'created_at': '2026-11-16T04:24:19.090-05:00', 'tag_string': tags, 'file_ext': 'jpg', 'md5': f'{i:032d}',
        'file_url': f'https://cdn.donmai.us/original/{i}.jpg', 'large_file_url': f'https://cdn.donmai.us/sample/{i}.jpg',
        'preview_file_url': f'https://cdn.donmai.us/180x180/{i}.jpg', 'rating': 'g', 'score': 1,
    } for i, tags in POSTS]).encode()


class GenderPipelineTests(TestCase):
    def _downloader_with_items(self):
        dwn = _make_downloader(ProcModule.DB)
        items = dwn._get_all_post_tags(_db_posts_json())
        dwn.items_raw_per_task[:] = items
        for it in items:
            info = dwn._extract_item_info(it)
            dwn.item_info_dict_per_task[info.id] = info
        return dwn

    def test_filters_only_female(self) -> None:  # AC-G04, AC-S01
        dwn = self._downloader_with_items()
        dwn.gender_select = frozenset({'only_female'})
        parents: set[str] = {'5'}
        dwn._filter_items_safety(parents)
        self.assertEqual(2, dwn.safety_blocked_count)
        self.assertNotIn('5', parents)
        dwn._filter_items_by_gender(parents)
        kept = [dwn._extract_id(h) for h in dwn.items_raw_per_task]
        self.assertEqual(['1'], kept)
        self.assertEqual({'2', '3', '4', '5', '6', '7'}, dwn.filtered_out_ids_cache)
        self.assertEqual(4, dwn.gender_filtered_count)
        self.assertEqual({'only_female': 1, 'only_male': 1, 'both': 1, 'none': 1, 'unknown': 1}, dwn.gender_stats)

    def test_no_filter_no_change(self) -> None:  # AC-G05: without -gender behavior is unchanged (safety too)
        dwn = self._downloader_with_items()
        before = list(dwn.items_raw_per_task)
        dwn._filter_items_safety(set())
        dwn._filter_items_by_gender(set())
        self.assertEqual(before, dwn.items_raw_per_task)
        self.assertEqual(0, dwn.safety_blocked_count)

    def test_cli_args_and_conflicts(self) -> None:
        with _make_downloader(ProcModule.DB) as dwn:
            me = current_process()
            me.killed = False
            dwn.reset_root_thread(me)
            dwn.no_conflict_delay = True
            dwn._parse_args(prepare_arglist(['-module', 'db', '-gender', 'only_female,both', '-gender_conf', 'high', '-gender_strict', 'x']), False)
            self.assertEqual(frozenset({'only_female', 'both'}), dwn.gender_select)
            self.assertEqual((H, True), (dwn.gender_min_conf, dwn.gender_strict))
        with self.assertRaises(SystemExit):
            prepare_arglist(['-module', 'db', '-gender', 'girls_only', 'x'])

    def test_preview_never_returns_blocked_posts(self) -> None:  # AC-S02
        from lustra import server
        page = HTMLContent(None, _db_posts_json())  # type: ignore[arg-type]
        with mock.patch('lustra.download_db.DownloaderDb.fetch_html', return_value=page), mock.patch.object(server, 'notify'):
            res = server.m_search_preview({'module': 'db', 'tags': 'scenery', 'args': ['-gender', 'only_female']})
        ids = [it['id'] for it in res['items']]
        self.assertEqual(2, res['blockedCount'])
        self.assertNotIn('5', ids)
        self.assertNotIn('6', ids)
        self.assertNotIn('/5.jpg', json.dumps(res))
        by_id = {it['id']: it for it in res['items']}
        self.assertFalse(by_id['1']['excluded'])
        self.assertEqual(['gender'], by_id['2']['excludedBy'])
        self.assertEqual('only_female', by_id['1']['gender']['label'])
        with mock.patch('lustra.download_db.DownloaderDb.fetch_html', return_value=page), mock.patch.object(server, 'notify'):
            res2 = server.m_search_preview({'module': 'db', 'tags': 'scenery', 'args': []})
        self.assertEqual(7, len(res2['items']))  # no filter -> no blacklist, unchanged behavior (product decision)
        self.assertFalse(res2['genderEnabled'])


class ExtensionGenderManifestTests(TestCase):
    def test_extension_rules_and_safety_tags(self) -> None:  # AC-S03 (extension adds tags)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict('os.environ', {'LUSTRA_DATA_DIR': tmp}):
            Extensions.ensure_loaded()
            src = pathlib.Path(tmp) / 'src' / 'gsite'
            src.mkdir(parents=True)
            (src / 'gender_rules.json').write_text(json.dumps({'completeness': 0.95, 'female': {'high': ['onna']}, 'male': {'high': ['otoko']}}))
            (src / 'manifest.json').write_text(json.dumps({
                'id': 'gsite', 'name': 'G', 'abbr': 'gq', 'version': '1.0.0', 'entry': 'downloader.py:DownloaderGq',
                'genderRules': 'gender_rules.json', 'safetyTags': ['site_only_minor_tag']}))
            (src / 'downloader.py').write_text('from lustra.download_db import DownloaderDb\n\n\nclass DownloaderGq(DownloaderDb):\n'
                                               "    def _get_module_abbr(self) -> str:\n        return 'gq'\n")
            Extensions.install_from_dir(src)
            self.assertTrue(safety.is_blocked(['site_only_minor_tag']))
            res = infer_from_tags(['onna'], rules_for_module('gq'))
            self.assertEqual((F, H), (res.label, res.conf))
            with _make_downloader(ProcModule.PROC_MODULES_BY_NAME['gq']) as dwn:
                self.assertIn('gender_tags', dwn.CAPABILITIES)
            Extensions.uninstall('gsite')

#
#
#########################################
