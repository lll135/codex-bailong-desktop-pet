import io
import unittest

from antigravity_quota import normalize_quota, quota_view
from antigravity_usage import blob_fields, usage_record


def integer(value):
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def field(number, value):
    if isinstance(value, int):
        return integer(number * 8) + integer(value)
    return integer(number * 8 + 2) + integer(len(value)) + value


class UsageFieldsTests(unittest.TestCase):
    def test_skips_large_unrelated_field_and_uses_step_timestamp(self):
        usage = field(2, 20) + field(5, 100) + field(4, 5) + field(3, 10) + field(11, b'request')
        chat = field(3, b'x' * 2_000_000) + field(4, usage) + field(21, b'Model')
        message = field(1, chat) + field(2, b'\x04\x05')
        root = blob_fields(io.BytesIO(message), len(message), {1: {4: True, 21: True}, 2: True})
        self.assertEqual(set(root[1][0]), {4, 21})
        result = usage_record(root, root[1][0], lambda step: 1_790_000_000 if step == 5 else None, None)
        self.assertEqual(result[1:], (1_790_000_000, 'Model', 20, 100, 5, 10))
        self.assertTrue(result[0].startswith('0:response:'))


class QuotaSnapshotTests(unittest.TestCase):
    def test_zero_remaining_missing_window_and_stale_values(self):
        document = {'version': 1, 'updatedAtMs': 1_790_000_000_000, 'groups': [
            {'displayName': 'Weekly group', 'buckets': [{'window': 'weekly', 'remainingFraction': 0}]},
            {'displayName': 'Two windows', 'buckets': [
                {'window': 'weekly', 'remainingFraction': .55},
                {'window': '5h', 'remainingFraction': 'unknown'}]},
        ]}
        quota = normalize_quota(document, 1_790_000_010)
        windows, _ = quota_view(quota, 'Weekly group', 1_790_000_010)
        self.assertEqual([(w['name'], w['remaining']) for w in windows], [('一周', 0)])
        windows, _ = quota_view(quota, 'Two windows', 1_790_000_010)
        self.assertEqual([w['remaining'] for w in windows], [None, 55])
        windows, caption = quota_view(quota, 'Two windows', 1_790_000_121)
        self.assertTrue(all(w['pending'] and w['remaining'] is None for w in windows))
        self.assertIn('待刷新', caption)
