"""Synthetic contact fixtures; no real WeChat process, account or key is accessed."""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/chat-relationship-workflow/scripts'
sys.path.insert(0, str(SCRIPTS))
import desktop_export as subject
import desktop_db_reader as reader

OWNER = 'wxid_syntheticowner'
PEER = 'wxid_syntheticpeer'
KEY = bytes(range(32))
SALT = bytes(range(16, 32))


def contact_connection(rows, missing_columns=False):
    connection = sqlite3.connect(':memory:')
    if missing_columns:
        connection.execute('CREATE TABLE contact(username TEXT)')
    else:
        connection.execute('CREATE TABLE contact(username TEXT, remark TEXT, nick_name TEXT)')
        connection.executemany('INSERT INTO contact VALUES (?,?,?)', rows)
    connection.commit()
    return connection


def encrypt_small_sqlite(connection):
    """Move small, compact leaf payloads into an 80-byte-reserve synthetic DB.

    This fixture only accepts freshly constructed leaf-only databases without
    freeblocks/overflow, and is not a production database converter.
    """
    source = connection.serialize()
    if source[16:18] != struct.pack('>H', reader.PAGE_SIZE):
        raise AssertionError('Unexpected test fixture page size')
    pages = []
    for index in range(0, len(source), reader.PAGE_SIZE):
        number = index // reader.PAGE_SIZE + 1
        original = source[index:index + reader.PAGE_SIZE]
        page = bytearray(original)
        header = 100 if number == 1 else 0
        if page[header] != 13 or page[header + 1:header + 3] != b'\0\0' or page[header + 7] != 0:
            raise AssertionError('Fixture must contain compact table leaf pages')
        cells = struct.unpack_from('>H', page, header + 3)[0]
        start = struct.unpack_from('>H', page, header + 5)[0]
        if start < header + 8 + cells * 2 + reader.RESERVE_SIZE:
            raise AssertionError('Synthetic leaf fixture is too full')
        page[start - reader.RESERVE_SIZE:reader.IV_OFFSET] = original[start:]
        page[reader.IV_OFFSET:] = bytes(reader.RESERVE_SIZE)
        struct.pack_into('>H', page, header + 5, start - reader.RESERVE_SIZE)
        for cell in range(cells):
            pointer = header + 8 + cell * 2
            offset = struct.unpack_from('>H', page, pointer)[0]
            struct.pack_into('>H', page, pointer, offset - reader.RESERVE_SIZE)
        if number == 1:
            page[20] = reader.RESERVE_SIZE
        iv = bytes((number + value) % 256 for value in range(16))
        body_start = 16 if number == 1 else 0
        encryptor = Cipher(algorithms.AES(KEY), modes.CBC(iv)).encryptor()
        ciphertext = encryptor.update(page[body_start:reader.IV_OFFSET]) + encryptor.finalize()
        mac = hmac.new(reader._hmac_key(KEY, SALT), ciphertext + iv + struct.pack('<I', number), hashlib.sha512).digest()
        pages.append((SALT if number == 1 else b'') + ciphertext + iv + mac)
    return b''.join(pages)


class ContactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='synthetic-contacts-')
        self.base = Path(self.temp.name)
        self.root = self.base / (OWNER + '_ab12') / 'db_storage'
        (self.root / 'contact').mkdir(parents=True)
        self.path = self.root / 'contact/contact.db'
        self.path.write_bytes(b'SYNTHETIC')
        self.mapping = {'databases': {'contact/contact.db': {'key_hex': KEY.hex()}}}
        self.rows = [(OWNER, '', '自己'), (PEER, '合成联系人', '示例昵称')]

    def tearDown(self):
        self.temp.cleanup()

    def resolve(self, query='合成联系人', self_id=None, missing_columns=False):
        with patch.object(reader, 'read_decrypted_db', side_effect=lambda *_: contact_connection(self.rows, missing_columns)):
            return subject.resolve_contact(self.root, self.mapping, query, self_id)

    def test_exact_remark_nickname_and_username(self):
        for query in ('合成联系人', '示例昵称', PEER):
            result = self.resolve(query)
            self.assertEqual(result['self_id'], OWNER)
            self.assertEqual(result['target_id'], PEER)
            self.assertEqual(result['target_name'], '合成联系人')
            self.assertNotIn(KEY.hex(), str(result))

    def test_duplicate_names_require_selection_by_stable_id(self):
        self.rows.append(('wxid_anotherpeer', '合成联系人', '另一人'))
        with self.assertRaises(subject.ExportError) as caught:
            self.resolve()
        self.assertEqual(caught.exception.code, 'target_ambiguous')
        self.assertEqual(len(caught.exception.details['candidates']), 2)
        self.assertEqual(self.resolve(PEER)['target_id'], PEER)

    def test_query_is_exact_not_like_or_fuzzy(self):
        for query in ('合成', '%', "' OR 1=1 --"):
            with self.assertRaises(subject.ExportError) as caught:
                self.resolve(query)
            self.assertEqual(caught.exception.code, 'target_not_found')

    def test_self_missing_requires_explicit_identity_instead_of_guessing(self):
        self.rows = [self.rows[1]]
        with self.assertRaises(subject.ExportError) as caught:
            self.resolve()
        self.assertEqual(caught.exception.code, 'self_identity_required')
        result = self.resolve(self_id=OWNER)
        self.assertEqual(result['self_id'], OWNER)
        self.assertEqual(result['self_identity_source'], 'explicit_account_id')

    def test_explicit_owner_cannot_override_a_known_account_folder(self):
        with self.assertRaises(subject.ExportError) as caught:
            self.resolve(self_id=PEER)
        self.assertEqual(caught.exception.code, 'self_identity_mismatch')

    def test_custom_account_directory_requires_confirmed_self_id(self):
        moved = self.base / 'custom-account' / 'db_storage'
        moved.parent.mkdir()
        self.root.rename(moved)
        self.root = moved
        with self.assertRaises(subject.ExportError) as caught:
            self.resolve()
        self.assertEqual(caught.exception.code, 'self_identity_required')
        self.assertEqual(self.resolve(self_id=OWNER)['self_id'], OWNER)

    def test_group_and_self_are_not_private_peer_exports(self):
        self.rows.append(('123@chatroom', '群聊', '群聊'))
        for query in (OWNER, '群聊'):
            with self.assertRaises(subject.ExportError) as caught:
                self.resolve(query)
            self.assertEqual(caught.exception.code, 'target_not_private')

    def test_unknown_schema_stops(self):
        with self.assertRaises(subject.ExportError) as caught:
            self.resolve(missing_columns=True)
        self.assertEqual(caught.exception.code, 'contact_schema_unsupported')

    def test_real_authenticated_encrypted_synthetic_contact_database(self):
        connection = contact_connection(self.rows)
        try:
            encrypted = encrypt_small_sqlite(connection)
        finally:
            connection.close()
        self.path.write_bytes(encrypted)
        result = subject.resolve_contact(self.root, self.mapping, '合成联系人')
        self.assertEqual(result['target_id'], PEER)
        self.assertEqual(self.path.read_bytes(), encrypted)
        self.assertEqual([path.name for path in self.path.parent.iterdir()], ['contact.db'])
        self.mapping['databases']['contact/contact.db']['key_hex'] = bytes(32).hex()
        with self.assertRaises(subject.ExportError) as caught:
            subject.resolve_contact(self.root, self.mapping, '合成联系人')
        self.assertEqual(caught.exception.code, 'contact_database_invalid')

    def test_encrypted_contact_to_filtered_export_integration(self):
        contact = contact_connection(self.rows)
        messages = sqlite3.connect(':memory:')
        table = 'Msg_' + hashlib.md5(PEER.encode()).hexdigest()
        messages.execute('CREATE TABLE Name2Id(user_name TEXT)')
        messages.executemany('INSERT INTO Name2Id(rowid,user_name) VALUES (?,?)', [(1, OWNER), (2, PEER)])
        messages.execute(f'CREATE TABLE "{table}"(local_id INTEGER,sort_seq INTEGER,server_id INTEGER,real_sender_id INTEGER,create_time INTEGER,local_type INTEGER,message_content BLOB)')
        messages.executemany(f'INSERT INTO "{table}" VALUES (?,?,?,?,?,?,?)', [
            (1, 1, 7001, 1, 1700000000, 1, '合成消息一'),
            (2, 2, 7002, 2, 1700000010, 1, '合成消息二'),
            (3, 3, 7003, None, 1700000010, 10000, '合成系统事件'),
        ])
        messages.commit()
        try:
            self.path.write_bytes(encrypt_small_sqlite(contact))
            (self.root / 'message').mkdir()
            message_path = self.root / 'message/message_0.db'
            encrypted_messages = encrypt_small_sqlite(messages)
            message_path.write_bytes(encrypted_messages)
        finally:
            contact.close()
            messages.close()
        self.mapping['databases']['message/message_0.db'] = {'key_hex': KEY.hex(), 'salt_hex': SALT.hex()}
        result = subject.resolve_contact(self.root, self.mapping, '合成联系人')
        output = self.base / 'synthetic-export'
        audit = subject.export_chat(self.root, self_id=result['self_id'], target_id=result['target_id'],
                                    output=output, key_mapping=self.mapping, since=1700000010,
                                    target_name=result['target_name'])
        self.assertEqual(audit['coverage']['source_records'], 3)
        self.assertEqual(audit['exported_messages'], 2)
        self.assertEqual(audit['system_records'], 1)
        exported = json.loads((output / 'chatlab.json').read_text(encoding='utf-8'))
        self.assertEqual(exported['messages'][0]['sender'], PEER)
        self.assertEqual(exported['messages'][1]['sender'], 'SYSTEM')
        self.assertEqual(message_path.read_bytes(), encrypted_messages)


if __name__ == '__main__':
    unittest.main(verbosity=2)
