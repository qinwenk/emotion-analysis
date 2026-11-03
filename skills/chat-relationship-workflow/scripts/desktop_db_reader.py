"""Read a verified SQLCipher-4 database and its committed WAL into memory.

Input is a *derived*, per-database 32-byte AES key, not a passphrase.  This
module never writes source files or plaintext databases.  It intentionally
rejects nonstandard layouts, unverified WAL boundaries, and unstable files.
Run ``python desktop_db_reader.py --self-test`` for synthetic-only tests.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import os
from pathlib import Path
import sqlite3
import struct
import time

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PAGE_SIZE = 4096
RESERVE_SIZE = 80
IV_OFFSET = PAGE_SIZE - RESERVE_SIZE
MAC_OFFSET = IV_OFFSET + 16
MAX_FILE_BYTES = 1024 * 1024 * 1024
MAX_SHM_BYTES = 16 * 1024 * 1024
SQLITE_MAGIC = b"SQLite format 3\x00"
WAL_VERSION = 3007000


class DatabaseReadError(RuntimeError):
    """The input cannot be read as a trustworthy supported snapshot."""


class UnstableSnapshotError(DatabaseReadError):
    pass


class AuthenticationError(DatabaseReadError):
    pass


@dataclass(frozen=True)
class _Snapshot:
    database: bytes
    wal: bytes | None
    shm: bytes | None
    metadata: tuple


def _stat_set(paths: tuple[Path, ...]) -> tuple:
    values = []
    for index, path in enumerate(paths):
        try:
            if path.is_symlink():
                raise DatabaseReadError("Database sidecar symlinks are not supported.")
            st = path.stat()
        except FileNotFoundError:
            if index == 0:
                raise DatabaseReadError("Database file is missing.") from None
            values.append(None)
            continue
        if not path.is_file():
            raise DatabaseReadError("Database or sidecar is not a regular file.")
        limit = MAX_SHM_BYTES if index == 2 else MAX_FILE_BYTES
        if st.st_size > limit:
            raise DatabaseReadError("File exceeds this in-memory reader's size limit.")
        values.append((st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns))
    return tuple(values)


def _read_once(paths: tuple[Path, ...]) -> _Snapshot:
    before = _stat_set(paths)
    payloads = []
    for path, metadata in zip(paths, before):
        if metadata is None:
            payloads.append(None)
            continue
        try:
            with path.open("rb") as stream:
                payload = stream.read(metadata[2] + 1)
        except FileNotFoundError:
            raise UnstableSnapshotError("A sidecar changed while reading.") from None
        if len(payload) != metadata[2]:
            raise UnstableSnapshotError("A file changed length while reading.")
        payloads.append(payload)
    after = _stat_set(paths)
    if before != after:
        raise UnstableSnapshotError("Database or sidecar metadata changed while reading.")
    return _Snapshot(payloads[0], payloads[1], payloads[2], after)


def _stable_snapshot(db_path: Path, attempts: int = 3) -> _Snapshot:
    paths = (db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm"))
    last_error = None
    for attempt in range(attempts):
        try:
            first = _read_once(paths)
            second = _read_once(paths)
            if (first.metadata == second.metadata
                    and first.database == second.database
                    and first.wal == second.wal
                    and first.shm == second.shm):
                return second
            last_error = UnstableSnapshotError("Consecutive database/WAL/SHM snapshots differ.")
        except UnstableSnapshotError as exc:
            last_error = exc
        if attempt + 1 < attempts:
            time.sleep(0.03)
    raise UnstableSnapshotError("Could not obtain two identical stable snapshots; retry when WeChat is idle.") from last_error


def _wal_checksum(data: bytes | memoryview, byte_order: str,
                  state: tuple[int, int] = (0, 0)) -> tuple[int, int]:
    if len(data) % 8:
        raise DatabaseReadError("Invalid WAL checksum input length.")
    a, b = state
    for x, y in struct.iter_unpack(byte_order + "II", data):
        a = (a + x + b) & 0xFFFFFFFF
        b = (b + y + a) & 0xFFFFFFFF
    return a, b


def _wal_index_header(shm: bytes | None, wal: bytes | None) -> tuple[int, int, tuple[int, int]] | None:
    """Authenticate the duplicated little-endian WalIndexHdr boundary.

    SQLite's index header fields are native-endian.  This reader supports the
    Windows/little-endian layout.  aSalt is copied byte-for-byte from the WAL,
    unlike the numeric checksum fields, so it is compared as raw bytes.
    """
    if not shm:
        return None
    if len(shm) < 96:
        raise DatabaseReadError("SHM is too short for both WAL index headers.")
    first, second = shm[:48], shm[48:96]
    if first != second:
        raise UnstableSnapshotError("The two WAL index headers disagree.")
    expected = struct.unpack("<2I", first[40:48])
    if _wal_checksum(first[:40], "<") != expected:
        raise DatabaseReadError("WAL index header checksum failed.")
    version, _unused, _change, initialized, big_end_checksum, page_size, frame_count, page_count = struct.unpack(
        "<IIIBBHII", first[:24])
    frame_checksums = struct.unpack("<2I", first[24:32])
    # mxFrame is the valid-frame boundary.  SQLite walRestartHdr() resets it
    # without clearing aFrameCksum; retained checksum words have no last-frame
    # meaning when there are zero frames, and are not required to be zero.
    empty_index = page_size == 0 and frame_count == 0 and page_count == 0
    if version != WAL_VERSION or initialized != 1 or (page_size != PAGE_SIZE and not empty_index):
        raise DatabaseReadError("Unsupported or uninitialized WAL index header.")
    if big_end_checksum not in (0, 1):
        raise DatabaseReadError("WAL index checksum byte-order flag is invalid.")
    if page_count > MAX_FILE_BYTES // PAGE_SIZE or (frame_count and not page_count):
        raise DatabaseReadError("WAL index committed database size is invalid.")
    if not wal:
        if frame_count:
            raise DatabaseReadError("WAL index refers to frames but its WAL file is empty or missing.")
    else:
        if len(wal) < 32:
            raise DatabaseReadError("WAL header is truncated.")
        magic = struct.unpack(">I", wal[:4])[0]
        if magic not in (0x377F0682, 0x377F0683) or big_end_checksum != (magic & 1):
            raise DatabaseReadError("WAL index checksum byte order disagrees with its WAL.")
        if first[32:40] != wal[16:24]:
            raise DatabaseReadError("WAL index salts disagree with its WAL.")
        if 32 + frame_count * (24 + PAGE_SIZE) > len(wal):
            raise DatabaseReadError("WAL is shorter than its authenticated index boundary.")
    return frame_count, page_count, frame_checksums


def _committed_wal_pages(wal: bytes | None, shm: bytes | None = None) -> tuple[dict[int, memoryview], int | None]:
    """Validate every complete frame; return pages at the last commit only.

    An authenticated duplicated WalIndexHdr limits the logical frame count;
    allocated/stale physical bytes after that boundary are not active WAL.
    Without SHM, a trailing uncommitted *valid* transaction is ignored but
    partial/stale frames are rejected instead of guessing a recovery boundary.
    """
    index = _wal_index_header(shm, wal)
    if not wal:
        return {}, (index[1] or None) if index is not None else None
    if len(wal) < 32:
        raise DatabaseReadError("WAL header is truncated.")
    magic, version, size, _, salt1, salt2, checksum1, checksum2 = struct.unpack(">8I", wal[:32])
    if magic not in (0x377F0682, 0x377F0683):
        raise DatabaseReadError("Unsupported WAL magic.")
    if version != WAL_VERSION or size != PAGE_SIZE:
        raise DatabaseReadError("Unsupported WAL version or page size.")
    order = "<" if magic == 0x377F0682 else ">"
    state = _wal_checksum(wal[:24], order)
    if state != (checksum1, checksum2):
        raise DatabaseReadError("WAL header checksum failed.")
    frame_size = 24 + PAGE_SIZE
    logical_size = 32 + index[0] * frame_size if index is not None else len(wal)
    if (logical_size - 32) % frame_size:
        raise UnstableSnapshotError("WAL ends with a partial frame; no snapshot was imported.")
    if index is not None and index[0] == 0:
        # After checkpoint/reset the physical WAL may remain preallocated.
        # There is no final active frame whose checksum could be compared.
        return {}, index[1] or None
    frames: list[tuple[int, memoryview]] = []
    committed_count = 0
    committed_size = None
    view = memoryview(wal)
    max_pages = MAX_FILE_BYTES // PAGE_SIZE
    for offset in range(32, logical_size, frame_size):
        frame_header = view[offset:offset + 24]
        page_number, database_size, fs1, fs2, fc1, fc2 = struct.unpack(">6I", frame_header)
        if (fs1, fs2) != (salt1, salt2):
            raise DatabaseReadError("WAL contains a stale or mismatched-salt frame; a quiet/checkpointed source is required.")
        if page_number < 1 or page_number > max_pages or database_size > max_pages:
            raise DatabaseReadError("WAL page number or committed size is outside supported bounds.")
        page = view[offset + 24:offset + frame_size]
        state = _wal_checksum(frame_header[:8], order, state)
        state = _wal_checksum(page, order, state)
        if state != (fc1, fc2):
            raise DatabaseReadError("WAL frame checksum failed; no recovery guesses are made.")
        frames.append((page_number, page))
        if database_size:
            committed_count = len(frames)
            committed_size = database_size
    if index is not None:
        if committed_count != index[0] or committed_size != index[1]:
            raise DatabaseReadError("WAL index boundary is not the matching committed frame.")
        if state != index[2]:
            raise DatabaseReadError("Last WAL frame checksum disagrees with the WAL index.")
    if committed_size is None:
        return {}, None
    pages = {}
    for page_number, page in frames[:committed_count]:
        if page_number <= committed_size:
            pages[page_number] = page
    return pages, committed_size


def _hmac_key(enc_key: bytes, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha512", enc_key, bytes(x ^ 0x3A for x in salt), 2, 32)


def _decrypt_page(page: bytes | memoryview, page_number: int,
                  enc_key: bytes, mac_key: bytes, salt: bytes) -> bytes:
    if len(page) != PAGE_SIZE:
        raise DatabaseReadError("Encrypted database page has the wrong size.")
    start = 16 if page_number == 1 else 0
    if page_number == 1 and not hmac.compare_digest(bytes(page[:16]), salt):
        raise AuthenticationError("Page-one salt differs between the database and WAL.")
    mac = hmac.new(mac_key, digestmod=hashlib.sha512)
    mac.update(page[start:MAC_OFFSET])
    mac.update(struct.pack("<I", page_number))
    if not hmac.compare_digest(mac.digest(), bytes(page[MAC_OFFSET:])):
        raise AuthenticationError(f"SQLCipher HMAC failed for page {page_number}; wrong key, changed input, or unsupported format.")
    decryptor = Cipher(algorithms.AES(enc_key), modes.CBC(bytes(page[IV_OFFSET:MAC_OFFSET]))).decryptor()
    plaintext = decryptor.update(page[start:IV_OFFSET]) + decryptor.finalize()
    prefix = SQLITE_MAGIC if page_number == 1 else b""
    return prefix + plaintext + bytes(RESERVE_SIZE)


def _validate_plain_header(page_one: bytes) -> int:
    if page_one[:16] != SQLITE_MAGIC or len(page_one) != PAGE_SIZE:
        raise DatabaseReadError("Decrypted SQLite header is invalid.")
    if struct.unpack(">H", page_one[16:18])[0] != PAGE_SIZE:
        raise DatabaseReadError("Decrypted SQLite page size is not 4096.")
    if page_one[18] not in (1, 2) or page_one[19] not in (1, 2):
        raise DatabaseReadError("Unsupported SQLite read/write format.")
    if page_one[20] != RESERVE_SIZE or page_one[21:24] != bytes((64, 32, 32)):
        raise DatabaseReadError("Unsupported SQLite reserve or payload fractions.")
    return struct.unpack(">I", page_one[28:32])[0]


def _read_authorizer(action: int, arg1: str | None, arg2: str | None,
                     database: str | None, trigger: str | None) -> int:
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
                  getattr(sqlite3, "SQLITE_RECURSIVE", 33)):
        if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() == "load_extension":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA:
        read_pragmas = {"table_info", "table_xinfo", "index_list", "index_info", "index_xinfo",
                        "foreign_key_list", "quick_check", "integrity_check", "database_list",
                        "compile_options", "collation_list", "function_list", "module_list"}
        scalar_pragmas = {"page_count", "page_size", "schema_version", "user_version", "encoding",
                          "query_only", "freelist_count", "application_id", "journal_mode"}
        if (arg1 or "").lower() in read_pragmas:
            return sqlite3.SQLITE_OK
        if (arg1 or "").lower() in scalar_pragmas and arg2 is None:
            return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def read_decrypted_db(db_path: str | os.PathLike[str], enc_key: bytes) -> sqlite3.Connection:
    """Return a read-only, in-memory SQLite connection from an authenticated snapshot.

    The 32-byte AES key must already be derived for this database.  The original
    salt is read from its first 16 bytes.  Every page in the selected committed
    image is authenticated.  No plaintext database is ever written to disk.

    A live writer is not locked: matching consecutive complete DB/WAL/SHM reads
    are the consistency condition.  If files keep changing, this function fails.
    """
    if not isinstance(enc_key, bytes) or len(enc_key) != 32:
        raise ValueError("enc_key must be the already-derived 32-byte AES key.")
    if not hasattr(sqlite3.Connection, "deserialize"):
        raise DatabaseReadError("This Python SQLite runtime lacks in-memory deserialize().")
    path = Path(db_path).expanduser().absolute()
    try:
        snapshot = _stable_snapshot(path)
    except OSError as exc:
        raise DatabaseReadError("Cannot read the database snapshot.") from exc
    raw = snapshot.database
    if not raw or len(raw) % PAGE_SIZE:
        raise DatabaseReadError("Database length is not a positive multiple of 4096.")
    if raw[:16] == SQLITE_MAGIC:
        raise DatabaseReadError("Input is already plaintext; this function only accepts SQLCipher input.")
    salt = raw[:16]
    mac_key = _hmac_key(enc_key, salt)
    wal_pages, committed_size = _committed_wal_pages(snapshot.wal, snapshot.shm)
    # Authenticate the base header as well as the ultimately selected header.
    base_first = _decrypt_page(memoryview(raw)[:PAGE_SIZE], 1, enc_key, mac_key, salt)
    base_count = _validate_plain_header(base_first)
    first = (_decrypt_page(wal_pages[1], 1, enc_key, mac_key, salt)
             if 1 in wal_pages else base_first)
    header_count = _validate_plain_header(first)
    physical_count = len(raw) // PAGE_SIZE
    if committed_size is not None:
        page_count = committed_size
        if header_count not in (0, page_count):
            raise DatabaseReadError("Committed WAL size disagrees with the selected SQLite header.")
    else:
        # SQLite considers header size valid only when both counters agree.
        valid_size = raw is not None and base_first[24:28] == base_first[92:96]
        page_count = base_count if valid_size and base_count else physical_count
        if page_count != physical_count:
            raise DatabaseReadError("Base SQLite page count disagrees with its physical size.")
    if page_count < 1 or page_count * PAGE_SIZE > MAX_FILE_BYTES:
        raise DatabaseReadError("Unsupported committed database size.")
    image = bytearray(page_count * PAGE_SIZE)
    image[:PAGE_SIZE] = first
    raw_view = memoryview(raw)
    for page_number in range(2, page_count + 1):
        if page_number in wal_pages:
            encrypted = wal_pages[page_number]
        elif page_number <= physical_count:
            encrypted = raw_view[(page_number - 1) * PAGE_SIZE:page_number * PAGE_SIZE]
        else:
            raise DatabaseReadError("A committed page is absent from both the database and WAL.")
        image[(page_number - 1) * PAGE_SIZE:page_number * PAGE_SIZE] = _decrypt_page(
            encrypted, page_number, enc_key, mac_key, salt)
    # deserialize cannot open a WAL sidecar for its memory-backed database.
    image[18:20] = b"\x01\x01"
    connection = sqlite3.connect(":memory:")
    try:
        connection.deserialize(bytes(image))
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        checked = connection.execute("PRAGMA quick_check").fetchall()
        if checked != [("ok",)]:
            raise DatabaseReadError("Decrypted committed image failed SQLite quick_check.")
        connection.set_authorizer(_read_authorizer)
    except Exception:
        connection.close()
        raise
    finally:
        # Best effort: remove this mutable copy; Python/SQLite can retain copies.
        image[:] = bytes(len(image))
    return connection


def _self_test() -> None:
    """Synthetic fixtures only: no user's database or key is accessed."""
    import tempfile
    import unittest
    from unittest.mock import patch

    key = bytes(range(32))
    salt = bytes(range(16, 32))

    def plain_page(user_version=0):
        page = bytearray(PAGE_SIZE)
        page[:16] = SQLITE_MAGIC
        struct.pack_into(">H", page, 16, PAGE_SIZE)
        page[18:24] = bytes((2, 2, RESERVE_SIZE, 64, 32, 32))
        for offset, value in ((24, 1), (28, 1), (40, 1), (44, 4),
                              (56, 1), (60, user_version), (92, 1), (96, 3040000)):
            struct.pack_into(">I", page, offset, value)
        page[100] = 13  # Empty sqlite_schema table leaf.
        struct.pack_into(">H", page, 105, IV_OFFSET)
        return bytes(page)

    def encrypt(page, number=1):
        start = 16 if number == 1 else 0
        iv = bytes(range(32, 48))
        encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        cipher = encryptor.update(page[start:IV_OFFSET]) + encryptor.finalize()
        mac = hmac.new(_hmac_key(key, salt), cipher + iv + struct.pack("<I", number), hashlib.sha512).digest()
        return (salt if number == 1 else b"") + cipher + iv + mac

    def make_wal(frames, order="<"):
        magic = 0x377F0682 if order == "<" else 0x377F0683
        header = struct.pack(">6I", magic, WAL_VERSION, PAGE_SIZE, 0, 1357, 2468)
        state = _wal_checksum(header, order)
        data = bytearray(header + struct.pack(">2I", *state))
        for number, commit_size, payload in frames:
            prefix = struct.pack(">4I", number, commit_size, 1357, 2468)
            state = _wal_checksum(prefix[:8], order, state)
            state = _wal_checksum(payload, order, state)
            data += prefix + struct.pack(">2I", *state) + payload
        return bytes(data)

    def make_shm(wal, frame_count, page_count, page_size=PAGE_SIZE):
        if frame_count:
            offset = 32 + (frame_count - 1) * (24 + PAGE_SIZE)
            checksums = struct.unpack(">2I", wal[offset + 16:offset + 24])
        else:
            checksums = (0, 0)
        byte_order_flag = struct.unpack(">I", wal[:4])[0] & 1
        header = (struct.pack("<IIIBBHII2I", WAL_VERSION, 0, 1, 1, byte_order_flag,
                              page_size, frame_count, page_count, *checksums)
                  + wal[16:24])
        header += struct.pack("<2I", *_wal_checksum(header, "<"))
        return header + header + bytes(32768 - 96)

    class ReaderTests(unittest.TestCase):
        def setUp(self):
            self.temp = tempfile.TemporaryDirectory(prefix="synthetic-sqlcipher-")
            self.db = Path(self.temp.name) / "sample.db"
            self.db.write_bytes(encrypt(plain_page()))
        def tearDown(self):
            self.temp.cleanup()
        def read_version(self):
            connection = read_decrypted_db(self.db, key)
            try:
                self.assertEqual(connection.execute("PRAGMA quick_check").fetchone(), ("ok",))
                return connection.execute("PRAGMA user_version").fetchone()[0]
            finally:
                connection.close()
        def test_base_and_source_unchanged(self):
            before = self.db.read_bytes()
            self.assertEqual(self.read_version(), 0)
            self.assertEqual(self.db.read_bytes(), before)
            self.assertEqual(len(list(self.db.parent.iterdir())), 1)
        def test_wrong_key_and_modified_page_rejected(self):
            with self.assertRaises(AuthenticationError):
                read_decrypted_db(self.db, bytes(32))
            data = bytearray(self.db.read_bytes()); data[150] ^= 1; self.db.write_bytes(data)
            with self.assertRaises(AuthenticationError):
                read_decrypted_db(self.db, key)
        def test_committed_wal_only_in_both_checksum_orders(self):
            for order in ("<", ">"):
                wal = make_wal([(1, 1, encrypt(plain_page(123))), (1, 0, encrypt(plain_page(456)))], order)
                Path(str(self.db) + "-wal").write_bytes(wal)
                self.assertEqual(self.read_version(), 123)
        def test_fully_uncommitted_wal_ignored(self):
            Path(str(self.db) + "-wal").write_bytes(make_wal([(1, 0, encrypt(plain_page(456)))]))
            self.assertEqual(self.read_version(), 0)
        def test_wal_checksum_and_partial_frame_rejected(self):
            wal = bytearray(make_wal([(1, 1, encrypt(plain_page(123)))]))
            wal[100] ^= 1
            Path(str(self.db) + "-wal").write_bytes(wal)
            with self.assertRaises(DatabaseReadError): self.read_version()
            Path(str(self.db) + "-wal").write_bytes(bytes(wal[:-1]))
            with self.assertRaises(UnstableSnapshotError): self.read_version()
        def test_wal_authenticated_after_valid_checksum(self):
            ciphertext = bytearray(encrypt(plain_page(123))); ciphertext[150] ^= 1
            Path(str(self.db) + "-wal").write_bytes(make_wal([(1, 1, bytes(ciphertext))]))
            with self.assertRaises(AuthenticationError): self.read_version()
        def test_index_zero_frames_ignores_preallocated_wal(self):
            wal = make_wal([(1, 1, encrypt(plain_page(999)))])
            Path(str(self.db) + "-wal").write_bytes(wal + bytes(4194304 - len(wal)))
            Path(str(self.db) + "-shm").write_bytes(make_shm(wal, 0, 1))
            self.assertEqual(self.read_version(), 0)
        def test_zero_size_empty_index_with_old_or_empty_wal(self):
            old_wal = make_wal([(1, 1, encrypt(plain_page(999)))])
            for physical_wal in (old_wal + bytes(4194304 - len(old_wal)), b""):
                Path(str(self.db) + "-wal").write_bytes(physical_wal)
                Path(str(self.db) + "-shm").write_bytes(make_shm(old_wal, 0, 0, page_size=0))
                self.assertEqual(self.read_version(), 0)
            Path(str(self.db) + "-wal").write_bytes(old_wal)
            Path(str(self.db) + "-shm").write_bytes(make_shm(old_wal, 1, 1, page_size=0))
            with self.assertRaises(DatabaseReadError): self.read_version()
        def test_native_sqlite_zero_size_empty_index(self):
            native_path = self.db.parent / "native_empty.sqlite"
            writer = sqlite3.connect(native_path)
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("CREATE TABLE sample(id INTEGER)")
            writer.commit()
            writer.close()
            reader = sqlite3.connect(native_path)
            try:
                self.assertEqual(reader.execute("SELECT COUNT(*) FROM sample").fetchone(), (0,))
                wal = Path(str(native_path) + "-wal").read_bytes()
                shm = Path(str(native_path) + "-shm").read_bytes()
                self.assertEqual(wal, b"")
                self.assertEqual(struct.unpack("<HII2I", shm[14:32]), (0, 0, 0, 0, 0))
                self.assertEqual(_committed_wal_pages(wal, shm), ({}, None))
            finally:
                reader.close()
        def test_zero_frame_index_retains_old_frame_checksum(self):
            old_wal = make_wal([(1, 1, encrypt(plain_page(999)))])
            header = bytearray(make_shm(old_wal, 0, 0, page_size=0)[:48])
            header[24:32] = struct.pack("<2I", 0x12345678, 0xABCDEF12)
            header[40:48] = struct.pack("<2I", *_wal_checksum(header[:40], "<"))
            Path(str(self.db) + "-shm").write_bytes(header + header + bytes(32768 - 96))
            for physical_wal in (old_wal + bytes(4194304 - len(old_wal)), b""):
                Path(str(self.db) + "-wal").write_bytes(physical_wal)
                self.assertEqual(self.read_version(), 0)
        def test_index_limits_committed_frames_and_preallocated_tail(self):
            wal = make_wal([(1, 1, encrypt(plain_page(123))), (1, 1, encrypt(plain_page(999)))])
            Path(str(self.db) + "-wal").write_bytes(wal + bytes(4194304 - len(wal)))
            Path(str(self.db) + "-shm").write_bytes(make_shm(wal, 1, 1))
            self.assertEqual(self.read_version(), 123)
        def test_invalid_index_header_and_commit_rejected(self):
            wal = make_wal([(1, 1, encrypt(plain_page(123)))])
            Path(str(self.db) + "-wal").write_bytes(wal)
            shm = bytearray(make_shm(wal, 1, 1)); shm[10] ^= 1
            Path(str(self.db) + "-shm").write_bytes(shm)
            with self.assertRaises(UnstableSnapshotError): self.read_version()
            shm[58] ^= 1
            Path(str(self.db) + "-shm").write_bytes(shm)
            with self.assertRaises(DatabaseReadError): self.read_version()
            Path(str(self.db) + "-shm").write_bytes(make_shm(wal, 1, 2))
            with self.assertRaises(DatabaseReadError): self.read_version()
        def test_index_salts_and_frame_checksum_rejected(self):
            wal = make_wal([(1, 1, encrypt(plain_page(123)))])
            Path(str(self.db) + "-wal").write_bytes(wal)
            for offset in (24, 32):
                header = bytearray(make_shm(wal, 1, 1)[:48]); header[offset] ^= 1
                header[40:48] = struct.pack("<2I", *_wal_checksum(header[:40], "<"))
                Path(str(self.db) + "-shm").write_bytes(header + header + bytes(32768 - 96))
                with self.assertRaises(DatabaseReadError): self.read_version()
        def test_read_only_connection(self):
            connection = read_decrypted_db(self.db, key)
            try:
                for sql in ("CREATE TABLE forbidden(a)", "PRAGMA query_only=OFF", "ATTACH DATABASE ':memory:' AS extra"):
                    with self.assertRaises(sqlite3.DatabaseError): connection.execute(sql)
            finally: connection.close()
        def test_unstable_snapshot_rejected(self):
            with patch(__name__ + "._read_once", side_effect=UnstableSnapshotError("synthetic change")):
                with self.assertRaises(UnstableSnapshotError): read_decrypted_db(self.db, key)
        def test_page_number_is_authenticated(self):
            encrypted = encrypt(bytes(PAGE_SIZE), 2)
            self.assertEqual(_decrypt_page(encrypted, 2, key, _hmac_key(key, salt), salt), bytes(PAGE_SIZE))
            with self.assertRaises(AuthenticationError):
                _decrypt_page(encrypted, 3, key, _hmac_key(key, salt), salt)
        def test_native_sqlite_wal_without_index_independent_fixture(self):
            # Native SQLite, not our WAL builder, supplies the checksums/frame
            # sequence for this multi-page fixture.
            native_path = self.db.parent / "native.sqlite"
            writer = sqlite3.connect(native_path)
            reader = None
            try:
                writer.execute("PRAGMA page_size=4096")
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("PRAGMA wal_autocheckpoint=0")
                writer.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY, value TEXT)")
                writer.executemany("INSERT INTO sample(value) VALUES (?)", [("x" * 400,) for _ in range(200)])
                writer.commit()
                original = native_path.read_bytes()
                pages, count = _committed_wal_pages(Path(str(native_path) + "-wal").read_bytes())
                self.assertGreater(count, 2)
                merged = bytearray(count * PAGE_SIZE)
                merged[:len(original)] = original
                for number, payload in pages.items():
                    merged[(number - 1) * PAGE_SIZE:number * PAGE_SIZE] = payload
                merged[18:20] = b"\x01\x01"
                reader = sqlite3.connect(":memory:")
                reader.deserialize(bytes(merged))
                self.assertEqual(reader.execute("SELECT COUNT(*), SUM(length(value)) FROM sample").fetchone(), (200, 80000))
                self.assertEqual(reader.execute("PRAGMA quick_check").fetchone(), ("ok",))
            finally:
                if reader is not None: reader.close()
                writer.close()
        def test_native_sqlite_wal_independent_fixture(self):
            # Native SQLite supplies both the WAL and duplicated SHM headers.
            native_path = self.db.parent / "native.sqlite"
            writer = sqlite3.connect(native_path)
            reader = None
            try:
                writer.execute("PRAGMA page_size=4096")
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("PRAGMA wal_autocheckpoint=0")
                writer.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY, value TEXT)")
                writer.executemany("INSERT INTO sample(value) VALUES (?)", [("x" * 400,) for _ in range(200)])
                writer.commit()
                original = native_path.read_bytes()
                pages, count = _committed_wal_pages(Path(str(native_path) + "-wal").read_bytes(),
                                                  Path(str(native_path) + "-shm").read_bytes())
                self.assertGreater(count, 2)
                merged = bytearray(count * PAGE_SIZE)
                merged[:len(original)] = original
                for number, payload in pages.items():
                    merged[(number - 1) * PAGE_SIZE:number * PAGE_SIZE] = payload
                merged[18:20] = b"\x01\x01"
                reader = sqlite3.connect(":memory:")
                reader.deserialize(bytes(merged))
                self.assertEqual(reader.execute("SELECT COUNT(*), SUM(length(value)) FROM sample").fetchone(), (200, 80000))
                self.assertEqual(reader.execute("PRAGMA quick_check").fetchone(), ("ok",))
            finally:
                if reader is not None: reader.close()
                writer.close()

    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ReaderTests))
    if not result.wasSuccessful():
        raise SystemExit(1)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true", help="Run generated fixtures only.")
    args = parser.parse_args()
    if args.self_test:
        _self_test()
    else:
        parser.print_help()
