"""只生成文件/解码计算，不调用播放器。合成 NCM 不等于真实官方下载样本。"""
import base64
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from Cryptodome.Cipher import AES
from Cryptodome.Util.Padding import pad
from mutagen.mp3 import MP3
from mutagen.flac import FLAC
from mutagen.id3 import TXXX, TBPM
from djtool.audio_tasks import AudioCancelled, AudioError, run_file_process
from djtool.intake import import_audio
from djtool.library import Library
from djtool.ncm import extract, read_metadata, music_id
from djtool.tracks import effective_bpm


def fixture_ncm(audio, destination, meta=None, padding=29, artwork=b''):
    """独立构造标准字段和逐字节掩码，覆盖提取时的块边界与封面填充。"""
    key = b'fixture-key-for-container-tests'
    box = list(range(256))
    j = 0
    for i in range(256):
        j = (j + box[i] + key[i % len(key)]) % 256
        box[i], box[j] = box[j], box[i]
    data = bytearray(audio.read_bytes())
    for index in range(len(data)):
        i = (index + 1) % 256
        data[index] ^= box[(box[i] + box[(box[i] + i) % 256]) % 256]
    encrypted_key = AES.new(bytes.fromhex('687a4852416d736f356b496e62617857'),AES.MODE_ECB).encrypt(pad(b'neteasecloudmusic'+key,16))
    key_data = bytes(x ^ 0x64 for x in encrypted_key)
    text = b'music:' + json.dumps(meta or {},ensure_ascii=False).encode()
    encrypted_meta = AES.new(bytes.fromhex('2331346c6a6b5f215c5d2630553c2728'),AES.MODE_ECB).encrypt(pad(text,16))
    metadata = bytes(x ^ 0x63 for x in b"163 key(Don't modify):"+base64.b64encode(encrypted_meta))
    u32 = lambda number: struct.pack('<I', number)
    destination.write_bytes(b'CTENFDAM\x00\x00' + u32(len(key_data)) + key_data +
                           u32(len(metadata)) + metadata + b'\x00'*5 +
                           u32(len(artwork)+padding)+u32(len(artwork))+artwork+b'\x00'*padding+data)


class IntakeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.samples = tempfile.TemporaryDirectory()
        cls.mp3 = Path(cls.samples.name)/'original.mp3'
        cls.flac = Path(cls.samples.name)/'original.flac'
        for path, codec in [(cls.mp3,'libmp3lame'),(cls.flac,'flac')]:
            subprocess.run(['ffmpeg','-v','error','-nostdin','-f','lavfi','-i','sine=duration=3',
                            '-c:a',codec,str(path)],check=True)
        tags=MP3(cls.mp3)
        tags.tags.add(TXXX(encoding=3,desc='original-custom',text='keep me'))
        tags.tags.add(TBPM(encoding=3,text='93'))
        tags.save()

    @classmethod
    def tearDownClass(cls):
        cls.samples.cleanup()

    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.root=Path(self.temporary.name)
        self.library=Library(self.root/'library')

    def tearDown(self):
        self.temporary.cleanup()

    def online_song(self, number='123', duration=3):
        return {'provider':'netease','remote_id':number,'url':f'https://music.163.com/song?id={number}',
                'title':'Same title','artist':'Fixture','duration':duration,
                'openapi_scope':'fixture','availability_reason':'接口无音源'}

    def local_ncm(self, number='123', name='download.ncm'):
        path=self.root/name
        fixture_ncm(self.mp3,path,{'musicId':number,'musicName':'Same title','duration':3000})
        return path

    def test_local_ncm_completes_exact_queue_id_and_preserves_memberships(self):
        job=self.library.enqueue(self.online_song())
        other=self.library.enqueue(self.online_song('456'))
        source=self.local_ncm()
        identifier,added=import_audio(self.library,source)
        self.assertTrue(added)
        jobs={j['id']:j for j in self.library.online_jobs()}
        self.assertEqual((jobs[job['id']]['state'],jobs[job['id']]['track_id']),('complete',identifier))
        self.assertEqual(jobs[other['id']]['state'],'unavailable')
        group=self.library.create_collection('keep','group');self.library.add_member(group,identifier)
        again=self.library.enqueue(self.online_song())
        self.assertEqual(again['state'],'complete')
        self.assertEqual(self.library.memberships(identifier),{group})
        self.assertEqual(import_audio(self.library,source),(identifier,False))


    def test_id_match_does_not_complete_ignored_or_wrong_duration(self):
        ignored=self.library.enqueue(self.online_song())
        self.library.set_job_state(ignored['id'],'ignored')
        import_audio(self.library,self.local_ncm())
        self.assertEqual(self.library.online_jobs()[0]['state'],'ignored')
        wrong=self.library.enqueue(self.online_song('456',duration=180))
        import_audio(self.library,self.local_ncm('456','another.ncm'))
        self.assertEqual(next(j for j in self.library.online_jobs() if j['id']==wrong['id'])['state'],'unavailable')

    def test_missing_local_file_cannot_satisfy_later_queue(self):
        identifier,_=import_audio(self.library,self.local_ncm())
        Path(self.library.track(identifier)['path']).unlink()
        self.assertEqual(self.library.enqueue(self.online_song())['state'],'unavailable')


    def test_changed_ncm_id_between_scan_and_import_is_rejected(self):
        source=self.local_ncm('456')
        with self.assertRaisesRegex(AudioError,'ID 不符'):
            import_audio(self.library,source,expected_netease_ids={'123'})
        self.assertEqual(self.library.tracks(),[])
        self.assertEqual(list(self.library.audio_dir.iterdir()),[])
        self.assertTrue(source.is_file())

    def test_header_reader_is_bounded_and_id_is_not_title(self):
        source=self.local_ncm('123')
        self.assertEqual(music_id(read_metadata(source)),'123')
        for value in ('Same title','123.0',True,-1,0,'１２３'):
            self.assertIsNone(music_id({'musicId':value}))
        source.write_bytes(b'CTENFDAM\0\0'+struct.pack('<I',99999999))
        with self.assertRaises(AudioError):read_metadata(source)

    def test_mp3_container_preserves_source_and_audio_and_deduplicates(self):
        source=self.root/'song.ncm'
        fixture_ncm(self.mp3,source,{'musicName':'标题','artist':[['A',1],['B',2]],'album':'Album','format':'flac','duration':3000})
        original=source.read_bytes()
        identifier,added=import_audio(self.library,source)
        self.assertTrue(added)
        track=self.library.track(identifier)
        self.assertEqual(track['title'],'标题')
        self.assertEqual(track['artist'],'A / B')
        self.assertIsNone(track['bpm_estimate'])
        self.assertEqual(track['bpm_tag'],93)
        self.assertEqual(track['source_path'],str(source.resolve()))
        self.assertIn('original-custom',track['tags_json'])
        self.assertEqual(source.read_bytes(),original)
        def audio_hash(path):
            return subprocess.check_output(['ffmpeg','-v','error','-nostdin','-i',str(path),'-map','0:a:0','-f','hash','-'])
        self.assertEqual(audio_hash(self.mp3),audio_hash(track['path']))
        details=json.loads(self.library.local_sources_for_track(identifier)[0]['metadata_json'])
        self.assertEqual(details['source_format'],'MP3')  # 不信任 metadata.format=flac。
        renamed=self.root/'renamed.ncm';shutil.copyfile(source,renamed)
        with patch('djtool.intake.convert',side_effect=AssertionError('不应重复转换')):
            duplicate,added=import_audio(self.library,renamed)
        self.assertEqual(duplicate,identifier);self.assertFalse(added)
        self.assertEqual(list((self.library.root/'.djtool/converting').iterdir()),[])

    def test_flac_container_is_reencoded_as_real_mp3_with_tags(self):
        flac=self.root/'tagged.flac';shutil.copyfile(self.flac,flac)
        tags=FLAC(flac);tags['title']='FLAC Title';tags['artist']=['One','Two']
        tags['lyrics']='Test lyric';tags['isrc']='TESTISRC';tags['custom']='original field';tags.save()
        source=self.root/'lossless.ncm'
        fixture_ncm(flac,source,{'format':'mp3','musicName':'fallback'})
        identifier,_=import_audio(self.library,source)
        track=self.library.track(identifier)
        self.assertEqual(MP3(track['path']).info.layer,3)
        self.assertAlmostEqual(track['duration'],FLAC(flac).info.length,delta=.25)
        self.assertEqual(track['title'],'FLAC Title');self.assertEqual(track['artist'],'One / Two')
        self.assertEqual(track['lyrics'],'Test lyric')
        details=json.loads(self.library.local_sources_for_track(identifier)[0]['metadata_json'])
        self.assertEqual(details['source_format'],'FLAC')
        self.assertEqual(details['source_flac_tags']['custom'],['original field'])

    def test_corrupt_and_truncated_files_leave_no_partial_import(self):
        source=self.root/'bad.ncm'
        for data in [b'not ncm',b'CTENFDAM\x00\x00'+struct.pack('<I',2**31)]:
            source.write_bytes(data)
            with self.assertRaises(AudioError):import_audio(self.library,source)
        fixture_ncm(self.mp3,source,{'duration':300_000})
        with self.assertRaisesRegex(AudioError,'时长'):import_audio(self.library,source)
        self.assertEqual(self.library.tracks(),[])
        self.assertEqual(list(self.library.audio_dir.iterdir()),[])
        self.assertEqual(list((self.library.root/'.djtool/converting').iterdir()),[])

    def test_cancel_during_conversion_leaves_source_untouched(self):
        source=self.root/'cancel.ncm';fixture_ncm(self.mp3,source)
        digest=hashlib.sha256(source.read_bytes()).hexdigest()
        stopped=False
        def progress(message):
            nonlocal stopped
            stopped='提取' in message
        with self.assertRaises(AudioCancelled):import_audio(self.library,source,progress,lambda:stopped)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),digest)
        self.assertEqual(self.library.tracks(),[])
        self.assertEqual(list((self.library.root/'.djtool/converting').iterdir()),[])

    def test_mp3_intake_never_starts_analysis_and_retains_legacy_values(self):
        with patch('subprocess.Popen',side_effect=AssertionError('MP3 入库不能启动分析进程')):
            identifier,added=import_audio(self.library,self.mp3)
        self.assertTrue(added)
        with self.library.connection() as db:
            db.execute("UPDATE tracks SET bpm_manual=87,bpm_estimate=120,bpm_status='estimated' WHERE id=?",(identifier,))
        identifier,added=import_audio(self.library,self.mp3)
        self.assertFalse(added)
        track=Library(self.library.root).track(identifier)
        self.assertEqual(track['bpm_manual'],87)
        self.assertEqual(track['bpm_estimate'],120)  # 留存历史值，但不再用作当前 BPM。
        self.assertEqual(effective_bpm({**track,'bpm_manual':None}),(93,'标签'))
        self.assertEqual(effective_bpm({'bpm_estimate':120}),(None,'无标签'))

    def test_source_record_failure_rolls_back_files_and_track(self):
        source=self.root/'rollback.ncm';fixture_ncm(self.mp3,source)
        with patch.object(self.library,'_record_local_source',side_effect=sqlite3.OperationalError('write error')):
            with self.assertRaises(sqlite3.OperationalError):import_audio(self.library,source)
        self.assertEqual(self.library.tracks(),[])
        self.assertEqual(list(self.library.audio_dir.iterdir()),[])
        self.assertEqual(list(self.library.art_dir.iterdir()),[])

    def test_extraction_crosses_chunk_boundary_without_trusting_metadata_format(self):
        payload=self.root/'payload'
        payload.write_bytes(bytes(range(256))*5000+b'last incomplete block')
        source=self.root/'chunks.ncm'
        fixture_ncm(payload,source,{'format':'unsupported'},artwork=b'cover bytes')
        output=self.root/'extracted'
        meta,cover=extract(source,output)
        self.assertEqual(output.read_bytes(),payload.read_bytes())
        self.assertEqual(cover,b'cover bytes')

    def test_online_worker_finishes_without_analysis(self):
        from djtool.workers import DownloadWorker
        identifier,_=self.library.import_file(self.mp3)
        job=self.library.enqueue({'provider':'soundcloud','remote_id':'1','title':'test','artist':'A','url':'https://soundcloud.com/a/b'})
        completed=[]
        worker=DownloadWorker(self.library,[job]);worker.completed.connect(completed.append)
        with patch('djtool.online.download_track',return_value=(identifier,True)), patch('subprocess.Popen',side_effect=AssertionError('不能启动分析')):
            worker.run()
        self.assertEqual(completed[0]['added'],1)
        self.assertIsNone(self.library.track(identifier)['bpm_estimate'])

    def test_v2_upgrade_preserves_manual_bpm_set_and_source_queue(self):
        identifier,_=self.library.import_file(self.mp3)
        self.library.update_track(identifier,{'bpm_manual':91})
        collection=self.library.create_collection('Original Set','set');self.library.add_member(collection,identifier)
        self.library.enqueue({'provider':'soundcloud','remote_id':'1','title':'test','artist':'A','url':'https://soundcloud.com/a/b'})
        with self.library.connection() as db:
            for key in ('bpm_status','bpm_message','bpm_method','bpm_analyzed_at'):
                db.execute(f'ALTER TABLE tracks DROP COLUMN {key}')
            db.execute('DROP TABLE local_sources');db.execute('PRAGMA user_version=2')
        upgraded=Library(self.library.root)
        self.assertEqual(upgraded.track(identifier)['bpm_manual'],91)
        self.assertEqual(upgraded.tracks(collection)[0]['id'],identifier)
        self.assertEqual(len(upgraded.online_jobs()),1)
        self.assertEqual(len(list((self.library.root/'.djtool/backups').glob('before-v4-*.sqlite3'))),1)
        Library(self.library.root)
        self.assertEqual(len(list((self.library.root/'.djtool/backups').glob('*.sqlite3'))),1)


class FileProcessTests(unittest.TestCase):
    def test_running_file_process_can_be_cancelled(self):
        started=time.monotonic()
        with self.assertRaises(AudioCancelled):
            run_file_process([sys.executable,'-c','import time; time.sleep(30)'],
                             cancel=lambda:time.monotonic()-started>.3)
        self.assertLess(time.monotonic()-started,5)


if __name__=='__main__':unittest.main()
