"""显式检查现有目录，或只保存未来音乐位置；从不索引、不打开 Serato/SQLite。"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from djtool.storage import DEFAULT_SERATO_ROOT, checked_path, configure_music_root, preview_existing


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--preview', action='store_true', help='只读枚举现有音乐目录')
    action.add_argument('--apply', action='store_true', help='只保存未来音乐目标目录，不修改 SQLite 或既有音频')
    parser.add_argument('--serato-root', type=Path, default=DEFAULT_SERATO_ROOT)
    parser.add_argument('--music-root', type=Path, help='未来音乐目标，默认 Serato/DJTOOL Audio')
    parser.add_argument('--library', type=Path, help='现有 DJTOOL 曲库根目录；--apply 时必须明确提供')
    args = parser.parse_args()
    if args.preview:
        report = preview_existing(args.serato_root)
    else:
        if args.library is None:
            parser.error('--apply 必须提供 --library，以保留现有曲库数据')
        library = checked_path(args.library)
        if not (library / '.djtool' / 'library.sqlite3').is_file():
            parser.error('--library 必须指向已有 DJTOOL 曲库，不会创建或重置数据库')
        target = args.music_root or (args.serato_root / 'DJTOOL Audio')
        destination = configure_music_root(library, target)
        report = {'library': str(library), 'music_root': str(destination), 'indexed_count': 0,
                  'copied_count': 0, 'moved_count': 0, 'sqlite_opened': False}
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
