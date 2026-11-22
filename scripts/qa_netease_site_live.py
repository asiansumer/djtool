"""显式站点联网验收：完整歌单/完整解码/跨页队列，仅写项目内隔离库。"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from djtool.library import Library
from djtool.netease_site import NeteaseSiteClient, SiteRateLimit
from djtool.online import download_track, friendly_error

parser = argparse.ArgumentParser()
parser.add_argument('identifier', help='公开网易云歌单或单曲数字 ID')
parser.add_argument('--kind', choices=('playlist', 'song'), default='playlist')
parser.add_argument('--download', type=int, default=0)
parser.add_argument('--download-all', action='store_true')
parser.add_argument('--song-id', action='append', default=[],
                    help='仅验证当前歌单内指定歌曲，可重复提供；与下载数量选项互斥')
parser.add_argument('--quality', choices=('exhigh', 'lossless', 'hires'), default='exhigh')
args = parser.parse_args()
if not args.identifier.isdigit() or args.download < 0:
    parser.error('ID 必须为数字，下载数量不能小于零')
if any(not identifier.isdigit() for identifier in args.song_id) or (
        args.song_id and (args.download or args.download_all)):
    parser.error('指定歌曲 ID 必须为数字，且不能同时指定下载数量或全部下载')
destination = ROOT/'work/netease-site'/f'{args.kind}-{args.identifier}-{args.quality}'
destination.mkdir(parents=True, exist_ok=True)
report = {'backend': 'site', 'kind': args.kind, 'identifier': args.identifier,
          'quality_requested': args.quality, 'playback': False, 'account_read': False,
          'ip_persisted': False, 'media_url_persisted': False, 'server_cookie_used': False,
          'downloaded': [], 'errors': []}

def forbidden(*args, **kwargs):
    raise AssertionError('站点验收不读取账号或钥匙串')

try:
    if True:   # 这个入口不读取任何账号；保留缩进以减少改动
        client = NeteaseSiteClient(quality=args.quality)
        name, tracks = client.listing(args.kind, args.identifier)
        report.update(name=name, count=len(tracks), ids=[track['remote_id'] for track in tracks],
                      listing_passed=True)
        library = Library(destination/'library')
        library.configure_music_root(destination/'Serato/DJTOOL Audio')
        library.enqueue_many(tracks)
        if args.song_id:
            requested = set(args.song_id)
            chosen = [track for track in tracks if track['remote_id'] in requested]
            if {track['remote_id'] for track in chosen} != requested:
                raise ValueError('待验证的歌曲不在本次完整歌单中')
        else:
            chosen = tracks if args.download_all else tracks[:args.download]
        report['requested_downloads'] = len(chosen)
        report['requested_song_ids'] = [track['remote_id'] for track in chosen]
        for index, track in enumerate(chosen, 1):
            try:
                identifier, added = download_track(track, library, music_client=client)
                saved = library.track(identifier)
                source = library.source_for_track(identifier)
                report['downloaded'].append({'song_id': track['remote_id'], 'title': saved['title'],
                    'duration': saved['duration'], 'bitrate': saved['bitrate'], 'lyrics': bool(saved['lyrics']),
                    'artwork': bool(saved['cover_path']), 'decoded': True, 'added': added,
                    'source_quality': json.loads(source['metadata_json'])['source_quality']})
                print(json.dumps({'processed': index, 'total': len(chosen), 'song_id': track['remote_id'],
                                  'state': 'complete', 'added': added}), flush=True)
            except Exception as error:
                report['errors'].append({'song_id': track['remote_id'], 'error': friendly_error(error)})
                print(json.dumps({'processed': index, 'total': len(chosen), 'song_id': track['remote_id'],
                                  'state': 'failed', 'error': friendly_error(error)}, ensure_ascii=False), flush=True)
                if isinstance(error, SiteRateLimit) or getattr(error, 'stop_batch', False):
                    report['rate_limited'] = True
                    break
        if report['downloaded']:
            # 完成任务重复加入不得再下载或复制；同一平台 ID 不依赖歌名。
            first = next(track for track in chosen if track['remote_id'] == report['downloaded'][0]['song_id'])
            job_before = library.enqueue(first)
            repeat_id, added_again = download_track(first, library, music_client=client)
            assert repeat_id == job_before['track_id'] and not added_again
            report['deduplicated'] = True
        with library.connection() as db:
            assert db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
            serialized = '\n'.join(row[0] for row in db.execute('SELECT metadata_json FROM online_jobs'))
            for blocked in ('MUSIC_U', 'access_token', '"cookie"', '"ip"', '/api/getSongUrl'):
                assert blocked not in serialized
            if client._ip:
                assert client._ip not in serialized
            report['complete_jobs'] = db.execute("SELECT COUNT(*) FROM online_jobs WHERE state='complete'").fetchone()[0]
        assert not list((library.root/'.djtool/downloads').iterdir()) if chosen else True
        report['remaining'] = len(chosen) - len(report['downloaded'])
        report['passed'] = not report['errors'] and report['remaining'] == 0
except Exception as error:
    report.update(passed=False, error=friendly_error(error))
path = destination/('result-all.json' if args.download_all else 'result.json')
path.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
print(json.dumps({'passed': report['passed'], 'count': report.get('count'),
                  'downloaded': len(report['downloaded']), 'errors': len(report['errors']),
                  'report': str(path)}, ensure_ascii=False), flush=True)
raise SystemExit(0 if report['passed'] else 1)
