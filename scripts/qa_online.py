"""显式联网验收：仅使用用户提供的链接和项目内隔离曲库。

用法：scripts/qa_online.py <网易云歌单链接> <歌单应有的歌曲数>
"""
from pathlib import Path
import json
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from djtool.library import Library
from djtool.online import download_track, fetch_listing
root=Path(__file__).resolve().parents[1]/'work/online-qa'
root.mkdir(exist_ok=True)
library=Library(root/'library')
if len(sys.argv)!=3:
    sys.exit(__doc__)
name,songs=fetch_listing(sys.argv[1],library=library)
assert len(songs)==int(sys.argv[2])
for song in songs:library.enqueue(song)
print('PASS: 网易云完整歌单',name,len(songs),flush=True)
_,songs=fetch_listing('https://soundcloud.com/past12official/countingedit')
identifier,added=download_track(songs[0],library,lambda text,p: print(p,text,flush=True))
track=library.track(identifier)
assert track['album_artist'] is not None
assert track['cover_path'] and Path(track['cover_path']).is_file()
assert track['artist']=='PAST12'
assert track['genre']=='BaileFunk'
assert abs(track['duration']-125.584)<2
assert json.loads(track['tags_json'])['WOAS'].startswith('https://soundcloud.com/')
assert library.source_for_track(identifier)['state']=='complete'
second,again=download_track(songs[0],library)
assert second==identifier and not again
summary={'netease_entries':14,'soundcloud':{'title':track['title'],'artist':track['artist'],
 'duration':track['duration'],'cover':bool(track['cover_path']),'genre':track['genre'],
 'duplicate_skipped':True,'source_quality':json.loads(library.source_for_track(identifier)['metadata_json'])['source_quality']}}
(root/'result.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
print('PASS: SoundCloud 完整音频、封面、标签、来源、重复入库检查',flush=True)
