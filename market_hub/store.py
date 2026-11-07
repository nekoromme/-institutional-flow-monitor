"""SQLiteを共通の置き場にする。生データはGitの管理対象外。

取得のたびに新しい版を作るので、過去の結果がいつの間にか変わらない。
価格と出所を別々に保存せず、一つの取引として確定する。
"""
import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timezone_required')
    return parsed.astimezone(timezone.utc)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':'))


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def validate_bars(rows, sessions):
    """通常取引日一覧と突き合わせる。欠けた価格を前日の値で埋めない。"""
    if not rows:
        raise ValueError('empty_prices')
    days = set()
    for row in rows:
        day = row['date']
        if day in days:
            raise ValueError('duplicate_session')
        days.add(day)
        if day not in sessions:
            raise ValueError('price_outside_calendar')
        values = [row[k] for k in ('open', 'high', 'low', 'close', 'volume')]
        if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in values):
            raise ValueError('invalid_numeric_price')
        o, h, l, c, v = values
        if min(o, h, l, c) <= 0 or v <= 0 or not l <= min(o, c) <= max(o, c) <= h:
            raise ValueError('invalid_ohlcv')
    return {'rows': len(rows), 'missing_sessions': sorted(set(sessions) - days)}


class Store:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          PRAGMA foreign_keys=ON;
          CREATE TABLE IF NOT EXISTS datasets (
            id TEXT PRIMARY KEY, source TEXT NOT NULL, symbol TEXT NOT NULL,
            metadata TEXT NOT NULL, retrieved_at TEXT NOT NULL, sha256 TEXT NOT NULL,
            payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS bars (
            dataset_id TEXT NOT NULL REFERENCES datasets(id), date TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            PRIMARY KEY(dataset_id,date));
          CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY, created_at TEXT NOT NULL, specification TEXT NOT NULL,
            result TEXT NOT NULL);
        ''')

    def close(self):
        self.db.close()

    def put(self, source, symbol, metadata, payload, rows=None):
        """メタデータもハッシュに含め、同じ価格でも取得版を区別する。"""
        metadata = dict(metadata)
        if rows is not None:
            validate_bars(rows, metadata['sessions'])
            metadata['normalized_sha256'] = fingerprint([{**r, **{k: float(r[k]) for k in ('open','high','low','close','volume')}} for r in sorted(rows, key=lambda r: r['date'])])
        retrieved = metadata.get('retrieved_at_utc', utcnow())
        timestamp(retrieved)
        encoded = canonical({'source': source, 'symbol': symbol, 'metadata': metadata, 'payload': payload})
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO datasets VALUES (?,?,?,?,?,?,?)',
                            (digest, source, symbol, canonical(metadata), retrieved, digest, canonical(payload)))
            if rows is not None:
                validate_bars(rows, metadata['sessions'])
                self.db.executemany('INSERT OR IGNORE INTO bars VALUES (?,?,?,?,?,?,?)',
                    [(digest, r['date'], *[r[k] for k in ('open', 'high', 'low', 'close', 'volume')]) for r in rows])
        return digest

    def dataset(self, dataset_id):
        row = self.db.execute('SELECT * FROM datasets WHERE id=?', (dataset_id,)).fetchone()
        if row is None:
            raise ValueError('unknown_dataset')
        result = dict(row)
        result['metadata'] = json.loads(result['metadata'])
        result['payload'] = json.loads(result['payload'])
        expected = fingerprint({k: result[k] for k in ('source', 'symbol', 'metadata', 'payload')})
        if expected != result['sha256']:
            raise ValueError('dataset_integrity_failure')
        return result

    def prices(self, dataset_id):
        data = self.dataset(dataset_id)
        rows = [dict(r) for r in self.db.execute('SELECT date,open,high,low,close,volume FROM bars WHERE dataset_id=? ORDER BY date', (dataset_id,))]
        # SQLiteでは整数が浮動小数へ変わるので、保存時と同じ数値型に揃える。
        normalized = [{**r, **{k: float(r[k]) for k in ('open','high','low','close','volume')}} for r in rows]
        if fingerprint(normalized) != data['metadata'].get('normalized_sha256'):
            raise ValueError('normalized_prices_integrity_failure')
        return rows

    def inventory(self):
        return [dict(r) for r in self.db.execute('SELECT id,source,symbol,retrieved_at FROM datasets ORDER BY retrieved_at,symbol')]

    def save_run(self, specification, result):
        run_id = fingerprint(specification)
        with self.db:
            existing = self.db.execute('SELECT result FROM runs WHERE id=?', (run_id,)).fetchone()
            if existing and existing['result'] != canonical(result):
                raise ValueError('same_specification_changed_result')
            self.db.execute('INSERT OR IGNORE INTO runs VALUES (?,?,?,?)',
                            (run_id, utcnow(), canonical(specification), canonical(result)))
        return run_id
