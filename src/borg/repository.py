import errno
import os
import shutil
import struct
from binascii import unhexlify
from collections import defaultdict
from configparser import ConfigParser
from datetime import datetime
from functools import partial
from itertools import islice
from zlib import crc32

import msgpack

import logging
logger = logging.getLogger(__name__)

from .constants import *  # NOQA
from .hashindex import NSIndex
from .helpers import Error, ErrorWithTraceback, IntegrityError, InternalOSError
from .helpers import Location
from .helpers import ProgressIndicatorPercent
from .helpers import bin_to_hex
from .locking import UpgradableLock, LockError, LockErrorT
from .lrucache import LRUCache
from .platform import SyncFile, sync_dir

MAX_OBJECT_SIZE = 20 * 1024 * 1024
MAGIC = b'BORG_SEG'
MAGIC_LEN = len(MAGIC)
TAG_PUT = 0
TAG_DELETE = 1
TAG_COMMIT = 2

FreeSpace = partial(defaultdict, int)


class Repository:
    """
    Filesystem based transactional key value store

    Transactionality is achieved by using a log (aka journal) to record changes. The log is a series of numbered files
    called segments. Each segment is a series of log entries. The segment number together with the offset of each
    entry relative to its segment start establishes an ordering of the log entries. This is the "definition" of
    time for the purposes of the log.

    Log entries are either PUT, DELETE or COMMIT.

    A COMMIT is always the final log entry in a segment and marks all data from the beginning of the log until the
    segment ending with the COMMIT as committed and consistent. The segment number of a segment ending with a COMMIT
    is called the transaction ID of that commit, and a segment ending with a COMMIT is called committed.

    When reading from a repository it is first checked whether the last segment is committed. If it is not, then
    all segments after the last committed segment are deleted; they contain log entries whose consistency is not
    established by a COMMIT.

    Note that the COMMIT can't establish consistency by itself, but only manages to do so with proper support from
    the platform (including the hardware). See platform.base.SyncFile for details.

    A PUT inserts a key-value pair. The value is stored in the log entry, hence the repository implements
    full data logging, meaning that all data is consistent, not just metadata (which is common in file systems).

    A DELETE marks a key as deleted.

    For a given key only the last entry regarding the key, which is called current (all other entries are called
    superseded), is relevant: If there is no entry or the last entry is a DELETE then the key does not exist.
    Otherwise the last PUT defines the value of the key.

    By superseding a PUT (with either another PUT or a DELETE) the log entry becomes obsolete. A segment containing
    such obsolete entries is called sparse, while a segment containing no such entries is called compact.

    Sparse segments can be compacted and thereby disk space freed. This destroys the transaction for which the
    superseded entries where current.

    On disk layout:

    dir/README
    dir/config
    dir/data/<X // SEGMENTS_PER_DIR>/<X>
    dir/index.X
    dir/hints.X
    """

    class DoesNotExist(Error):
        """Repository {} does not exist."""

    class AlreadyExists(Error):
        """Repository {} already exists."""

    class InvalidRepository(Error):
        """{} is not a valid repository. Check repo config."""

    class CheckNeeded(ErrorWithTraceback):
        """Inconsistency detected. Please run "borg check {}"."""

    class ObjectNotFound(ErrorWithTraceback):
        """Object with key {} not found in repository {}."""

    def __init__(self, path, create=False, exclusive=False, lock_wait=None, lock=True, append_only=False):
        self.path = os.path.abspath(path)
        self._location = Location('file://%s' % self.path)
        self.io = None
        self.lock = None
        self.index = None
        self._active_txn = False
        self.lock_wait = lock_wait
        self.do_lock = lock
        self.do_create = create
        self.exclusive = exclusive
        self.append_only = append_only

    def __del__(self):
        if self.lock:
            self.close()
            assert False, "cleanup happened in Repository.__del__"

    def __repr__(self):
        return '<%s %s>' % (self.__class__.__name__, self.path)

    def __enter__(self):
        if self.do_create:
            self.do_create = False
            self.create(self.path)
        self.open(self.path, self.exclusive, lock_wait=self.lock_wait, lock=self.do_lock)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.rollback()
        self.close()

    @property
    def id_str(self):
        return bin_to_hex(self.id)

    def create(self, path):
        """Create a new empty repository at `path`
        """
        if os.path.exists(path) and (not os.path.isdir(path) or os.listdir(path)):
            raise self.AlreadyExists(path)
        if not os.path.exists(path):
            os.mkdir(path)
        with open(os.path.join(path, 'README'), 'w') as fd:
            fd.write('This is a Borg repository\n')
        os.mkdir(os.path.join(path, 'data'))
        config = ConfigParser(interpolation=None)
        config.add_section('repository')
        config.set('repository', 'version', '1')
        config.set('repository', 'segments_per_dir', str(DEFAULT_SEGMENTS_PER_DIR))
        config.set('repository', 'max_segment_size', str(DEFAULT_MAX_SEGMENT_SIZE))
        config.set('repository', 'append_only', '0')
        config.set('repository', 'id', bin_to_hex(os.urandom(32)))
        self.save_config(path, config)

    def save_config(self, path, config):
        config_path = os.path.join(path, 'config')
        with open(config_path, 'w') as fd:
            config.write(fd)

    def save_key(self, keydata):
        assert self.config
        keydata = keydata.decode('utf-8')  # remote repo: msgpack issue #99, getting bytes
        self.config.set('repository', 'key', keydata)
        self.save_config(self.path, self.config)

    def load_key(self):
        keydata = self.config.get('repository', 'key')
        return keydata.encode('utf-8')  # remote repo: msgpack issue #99, returning bytes

    def destroy(self):
        """Destroy the repository at `self.path`
        """
        if self.append_only:
            raise ValueError(self.path + " is in append-only mode")
        self.close()
        os.remove(os.path.join(self.path, 'config'))  # kill config first
        shutil.rmtree(self.path)

    def get_index_transaction_id(self):
        indices = sorted(int(fn[6:])
                         for fn in os.listdir(self.path)
                         if fn.startswith('index.') and fn[6:].isdigit() and os.stat(os.path.join(self.path, fn)).st_size != 0)
        if indices:
            return indices[-1]
        else:
            return None

    def check_transaction(self):
        index_transaction_id = self.get_index_transaction_id()
        segments_transaction_id = self.io.get_segments_transaction_id()
        if index_transaction_id is not None and segments_transaction_id is None:
            raise self.CheckNeeded(self.path)
        # Attempt to automatically rebuild index if we crashed between commit
        # tag write and index save
        if index_transaction_id != segments_transaction_id:
            if index_transaction_id is not None and index_transaction_id > segments_transaction_id:
                replay_from = None
            else:
                replay_from = index_transaction_id
            self.replay_segments(replay_from, segments_transaction_id)

    def get_transaction_id(self):
        self.check_transaction()
        return self.get_index_transaction_id()

    def break_lock(self):
        UpgradableLock(os.path.join(self.path, 'lock')).break_lock()

    def open(self, path, exclusive, lock_wait=None, lock=True):
        self.path = path
        if not os.path.isdir(path):
            raise self.DoesNotExist(path)
        if lock:
            self.lock = UpgradableLock(os.path.join(path, 'lock'), exclusive, timeout=lock_wait).acquire()
        else:
            self.lock = None
        self.config = ConfigParser(interpolation=None)
        self.config.read(os.path.join(self.path, 'config'))
        if 'repository' not in self.config.sections() or self.config.getint('repository', 'version') != 1:
            raise self.InvalidRepository(path)
        self.max_segment_size = self.config.getint('repository', 'max_segment_size')
        self.segments_per_dir = self.config.getint('repository', 'segments_per_dir')
        # append_only can be set in the constructor
        # it shouldn't be overridden (True -> False) here
        self.append_only = self.append_only or self.config.getboolean('repository', 'append_only', fallback=False)
        self.id = unhexlify(self.config.get('repository', 'id').strip())
        self.io = LoggedIO(self.path, self.max_segment_size, self.segments_per_dir)

    def close(self):
        if self.lock:
            if self.io:
                self.io.close()
            self.io = None
            self.lock.release()
            self.lock = None

    def commit(self, save_space=False):
        """Commit transaction
        """
        self.io.write_commit()
        if not self.append_only:
            self.compact_segments(save_space=save_space)
        self.write_index()
        self.rollback()

    def open_index(self, transaction_id, auto_recover=True):
        if transaction_id is None:
            return NSIndex()
        index_path = os.path.join(self.path, 'index.%d' % transaction_id).encode('utf-8')
        try:
            return NSIndex.read(index_path)
        except RuntimeError as error:
            assert str(error) == 'hashindex_read failed'  # everything else means we're in *deep* trouble
            logger.warning('Repository index missing or corrupted, trying to recover')
            try:
                os.unlink(index_path)
            except OSError as e:
                raise InternalOSError(e) from None
            if not auto_recover:
                raise
            self.prepare_txn(self.get_transaction_id())
            # don't leave an open transaction around
            self.commit()
            return self.open_index(self.get_transaction_id())
        except OSError as e:
            raise InternalOSError(e) from None

    def prepare_txn(self, transaction_id, do_cleanup=True):
        self._active_txn = True
        try:
            self.lock.upgrade()
        except (LockError, LockErrorT):
            # if upgrading the lock to exclusive fails, we do not have an
            # active transaction. this is important for "serve" mode, where
            # the repository instance lives on - even if exceptions happened.
            self._active_txn = False
            raise
        if not self.index or transaction_id is None:
            try:
                self.index = self.open_index(transaction_id, False)
            except RuntimeError:
                self.check_transaction()
                self.index = self.open_index(transaction_id, False)
        if transaction_id is None:
            self.segments = {}  # XXX bad name: usage_count_of_segment_x = self.segments[x]
            self.compact = FreeSpace()  # XXX bad name: freeable_space_of_segment_x = self.compact[x]
        else:
            if do_cleanup:
                self.io.cleanup(transaction_id)
            hints_path = os.path.join(self.path, 'hints.%d' % transaction_id)
            index_path = os.path.join(self.path, 'index.%d' % transaction_id)
            try:
                with open(hints_path, 'rb') as fd:
                    hints = msgpack.unpack(fd)
            except (msgpack.UnpackException, msgpack.ExtraData, FileNotFoundError) as e:
                logger.warning('Repository hints file missing or corrupted, trying to recover')
                if not isinstance(e, FileNotFoundError):
                    os.unlink(hints_path)
                # index must exist at this point
                os.unlink(index_path)
                self.check_transaction()
                self.prepare_txn(transaction_id)
                return
            except OSError as os_error:
                raise InternalOSError(os_error) from None
            if hints[b'version'] == 1:
                logger.debug('Upgrading from v1 hints.%d', transaction_id)
                self.segments = hints[b'segments']
                self.compact = FreeSpace()
                for segment in sorted(hints[b'compact']):
                    logger.debug('Rebuilding sparse info for segment %d', segment)
                    self._rebuild_sparse(segment)
                logger.debug('Upgrade to v2 hints complete')
            elif hints[b'version'] != 2:
                raise ValueError('Unknown hints file version: %d' % hints[b'version'])
            else:
                self.segments = hints[b'segments']
                self.compact = FreeSpace(hints[b'compact'])

    def write_index(self):
        hints = {b'version': 2,
                 b'segments': self.segments,
                 b'compact': self.compact}
        transaction_id = self.io.get_segments_transaction_id()
        hints_file = os.path.join(self.path, 'hints.%d' % transaction_id)
        with open(hints_file + '.tmp', 'wb') as fd:
            msgpack.pack(hints, fd)
            fd.flush()
            os.fsync(fd.fileno())
        os.rename(hints_file + '.tmp', hints_file)
        self.index.write(os.path.join(self.path, 'index.tmp'))
        os.rename(os.path.join(self.path, 'index.tmp'),
                  os.path.join(self.path, 'index.%d' % transaction_id))
        if self.append_only:
            with open(os.path.join(self.path, 'transactions'), 'a') as log:
                print('transaction %d, UTC time %s' % (transaction_id, datetime.utcnow().isoformat()), file=log)
        # Remove old auxiliary files
        current = '.%d' % transaction_id
        for name in os.listdir(self.path):
            if not name.startswith(('index.', 'hints.')):
                continue
            if name.endswith(current):
                continue
            os.unlink(os.path.join(self.path, name))
        self.index = None

    def compact_segments(self, save_space=False):
        """Compact sparse segments by copying data into new segments
        """
        if not self.compact:
            return
        index_transaction_id = self.get_index_transaction_id()
        segments = self.segments
        unused = []  # list of segments, that are not used anymore

        def complete_xfer():
            # complete the transfer (usually exactly when some target segment
            # is full, or at the very end when everything is processed)
            nonlocal unused
            # commit the new, compact, used segments
            self.io.write_commit()
            # get rid of the old, sparse, unused segments. free space.
            for segment in unused:
                assert self.segments.pop(segment) == 0
                self.io.delete_segment(segment)
                del self.compact[segment]
            unused = []

        for segment, freeable_space in sorted(self.compact.items()):
            if not self.io.segment_exists(segment):
                del self.compact[segment]
                continue
            segment_size = self.io.segment_size(segment)
            if segment_size > 0.2 * self.max_segment_size and freeable_space < 0.15 * segment_size:
                logger.debug('not compacting segment %d for later (only %d bytes are sparse)',
                             segment, freeable_space)
                continue
            segments.setdefault(segment, 0)
            for tag, key, offset, data in self.io.iter_objects(segment, include_data=True):
                if tag == TAG_PUT and self.index.get(key, (-1, -1)) == (segment, offset):
                    try:
                        new_segment, offset = self.io.write_put(key, data, raise_full=save_space)
                    except LoggedIO.SegmentFull:
                        complete_xfer()
                        new_segment, offset = self.io.write_put(key, data)
                    self.index[key] = new_segment, offset
                    segments.setdefault(new_segment, 0)
                    segments[new_segment] += 1
                    segments[segment] -= 1
                elif tag == TAG_DELETE:
                    if index_transaction_id is None or segment > index_transaction_id:
                        try:
                            self.io.write_delete(key, raise_full=save_space)
                        except LoggedIO.SegmentFull:
                            complete_xfer()
                            self.io.write_delete(key)
            assert segments[segment] == 0
            unused.append(segment)
        complete_xfer()

    def replay_segments(self, index_transaction_id, segments_transaction_id):
        self.prepare_txn(index_transaction_id, do_cleanup=False)
        try:
            segment_count = sum(1 for _ in self.io.segment_iterator())
            pi = ProgressIndicatorPercent(total=segment_count, msg="Replaying segments %3.0f%%", same_line=True)
            for i, (segment, filename) in enumerate(self.io.segment_iterator()):
                pi.show(i)
                if index_transaction_id is not None and segment <= index_transaction_id:
                    continue
                if segment > segments_transaction_id:
                    break
                objects = self.io.iter_objects(segment)
                self._update_index(segment, objects)
            pi.finish()
            self.write_index()
        finally:
            self.rollback()

    def _update_index(self, segment, objects, report=None):
        """some code shared between replay_segments and check"""
        self.segments[segment] = 0
        for tag, key, offset, size in objects:
            if tag == TAG_PUT:
                try:
                    # If this PUT supersedes an older PUT, mark the old segment for compaction and count the free space
                    s, _ = self.index[key]
                    self.compact[s] += size
                    self.segments[s] -= 1
                except KeyError:
                    pass
                self.index[key] = segment, offset
                self.segments[segment] += 1
            elif tag == TAG_DELETE:
                try:
                    # if the deleted PUT is not in the index, there is nothing to clean up
                    s, offset = self.index.pop(key)
                except KeyError:
                    pass
                else:
                    if self.io.segment_exists(s):
                        # the old index is not necessarily valid for this transaction (e.g. compaction); if the segment
                        # is already gone, then it was already compacted.
                        self.segments[s] -= 1
                        size = self.io.read(s, offset, key, read_data=False)
                        self.compact[s] += size
            elif tag == TAG_COMMIT:
                continue
            else:
                msg = 'Unexpected tag {} in segment {}'.format(tag, segment)
                if report is None:
                    raise self.CheckNeeded(msg)
                else:
                    report(msg)
        if self.segments[segment] == 0:
            self.compact[segment] += self.io.segment_size(segment)

    def _rebuild_sparse(self, segment):
        """Rebuild sparse bytes count for a single segment relative to the current index."""
        self.compact[segment] = 0
        if self.segments[segment] == 0:
            self.compact[segment] += self.io.segment_size(segment)
            return
        for tag, key, offset, size in self.io.iter_objects(segment, read_data=False):
            if tag == TAG_PUT:
                if self.index.get(key, (-1, -1)) != (segment, offset):
                    # This PUT is superseded later
                    self.compact[segment] += size
            elif tag == TAG_DELETE:
                # The outcome of the DELETE has been recorded in the PUT branch already
                self.compact[segment] += size

    def check(self, repair=False, save_space=False):
        """Check repository consistency

        This method verifies all segment checksums and makes sure
        the index is consistent with the data stored in the segments.
        """
        if self.append_only and repair:
            raise ValueError(self.path + " is in append-only mode")
        error_found = False

        def report_error(msg):
            nonlocal error_found
            error_found = True
            logger.error(msg)

        logger.info('Starting repository check')
        assert not self._active_txn
        try:
            transaction_id = self.get_transaction_id()
            current_index = self.open_index(transaction_id)
        except Exception:
            transaction_id = self.io.get_segments_transaction_id()
            current_index = None
        if transaction_id is None:
            transaction_id = self.get_index_transaction_id()
        if transaction_id is None:
            transaction_id = self.io.get_latest_segment()
        if repair:
            self.io.cleanup(transaction_id)
        segments_transaction_id = self.io.get_segments_transaction_id()
        self.prepare_txn(None)  # self.index, self.compact, self.segments all empty now!
        segment_count = sum(1 for _ in self.io.segment_iterator())
        pi = ProgressIndicatorPercent(total=segment_count, msg="Checking segments %3.1f%%", step=0.1, same_line=True)
        for i, (segment, filename) in enumerate(self.io.segment_iterator()):
            pi.show(i)
            if segment > transaction_id:
                continue
            try:
                objects = list(self.io.iter_objects(segment))
            except IntegrityError as err:
                report_error(str(err))
                objects = []
                if repair:
                    self.io.recover_segment(segment, filename)
                    objects = list(self.io.iter_objects(segment))
            self._update_index(segment, objects, report_error)
        pi.finish()
        # self.index, self.segments, self.compact now reflect the state of the segment files up to <transaction_id>
        # We might need to add a commit tag if no committed segment is found
        if repair and segments_transaction_id is None:
            report_error('Adding commit tag to segment {}'.format(transaction_id))
            self.io.segment = transaction_id + 1
            self.io.write_commit()
        if current_index and not repair:
            # current_index = "as found on disk"
            # self.index = "as rebuilt in-memory from segments"
            if len(current_index) != len(self.index):
                report_error('Index object count mismatch. {} != {}'.format(len(current_index), len(self.index)))
            elif current_index:
                for key, value in self.index.iteritems():
                    if current_index.get(key, (-1, -1)) != value:
                        report_error('Index mismatch for key {}. {} != {}'.format(key, value, current_index.get(key, (-1, -1))))
        if repair:
            self.compact_segments(save_space=save_space)
            self.write_index()
        self.rollback()
        if error_found:
            if repair:
                logger.info('Completed repository check, errors found and repaired.')
            else:
                logger.error('Completed repository check, errors found.')
        else:
            logger.info('Completed repository check, no problems found.')
        return not error_found or repair

    def rollback(self):
        """
        """
        self.index = None
        self._active_txn = False

    def __len__(self):
        if not self.index:
            self.index = self.open_index(self.get_transaction_id())
        return len(self.index)

    def __contains__(self, id):
        if not self.index:
            self.index = self.open_index(self.get_transaction_id())
        return id in self.index

    def list(self, limit=None, marker=None):
        if not self.index:
            self.index = self.open_index(self.get_transaction_id())
        return [id_ for id_, _ in islice(self.index.iteritems(marker=marker), limit)]

    def get(self, id_):
        if not self.index:
            self.index = self.open_index(self.get_transaction_id())
        try:
            segment, offset = self.index[id_]
            return self.io.read(segment, offset, id_)
        except KeyError:
            raise self.ObjectNotFound(id_, self.path) from None

    def get_many(self, ids, is_preloaded=False):
        for id_ in ids:
            yield self.get(id_)

    def put(self, id, data, wait=True):
        if not self._active_txn:
            self.prepare_txn(self.get_transaction_id())
        try:
            segment, offset = self.index[id]
        except KeyError:
            pass
        else:
            self.segments[segment] -= 1
            size = self.io.read(segment, offset, id, read_data=False)
            self.compact[segment] += size
            segment, size = self.io.write_delete(id)
            self.compact[segment] += size
            self.segments.setdefault(segment, 0)
        segment, offset = self.io.write_put(id, data)
        self.segments.setdefault(segment, 0)
        self.segments[segment] += 1
        self.index[id] = segment, offset

    def delete(self, id, wait=True):
        if not self._active_txn:
            self.prepare_txn(self.get_transaction_id())
        try:
            segment, offset = self.index.pop(id)
        except KeyError:
            raise self.ObjectNotFound(id, self.path) from None
        self.segments[segment] -= 1
        size = self.io.read(segment, offset, id, read_data=False)
        self.compact[segment] += size
        segment, size = self.io.write_delete(id)
        self.compact[segment] += size
        self.segments.setdefault(segment, 0)

    def preload(self, ids):
        """Preload objects (only applies to remote repositories)
        """


class LoggedIO:

    class SegmentFull(Exception):
        """raised when a segment is full, before opening next"""

    header_fmt = struct.Struct('<IIB')
    assert header_fmt.size == 9
    put_header_fmt = struct.Struct('<IIB32s')
    assert put_header_fmt.size == 41
    header_no_crc_fmt = struct.Struct('<IB')
    assert header_no_crc_fmt.size == 5
    crc_fmt = struct.Struct('<I')
    assert crc_fmt.size == 4

    _commit = header_no_crc_fmt.pack(9, TAG_COMMIT)
    COMMIT = crc_fmt.pack(crc32(_commit)) + _commit

    def __init__(self, path, limit, segments_per_dir, capacity=90):
        self.path = path
        self.fds = LRUCache(capacity,
                            dispose=self.close_fd)
        self.segment = 0
        self.limit = limit
        self.segments_per_dir = segments_per_dir
        self.offset = 0
        self._write_fd = None

    def close(self):
        self.close_segment()
        self.fds.clear()
        self.fds = None  # Just to make sure we're disabled

    def close_fd(self, fd):
        if hasattr(os, 'posix_fadvise'):  # only on UNIX
            os.posix_fadvise(fd.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
        fd.close()

    def segment_iterator(self, reverse=False):
        data_path = os.path.join(self.path, 'data')
        dirs = sorted((dir for dir in os.listdir(data_path) if dir.isdigit()), key=int, reverse=reverse)
        for dir in dirs:
            filenames = os.listdir(os.path.join(data_path, dir))
            sorted_filenames = sorted((filename for filename in filenames
                                       if filename.isdigit()), key=int, reverse=reverse)
            for filename in sorted_filenames:
                yield int(filename), os.path.join(data_path, dir, filename)

    def get_latest_segment(self):
        for segment, filename in self.segment_iterator(reverse=True):
            return segment
        return None

    def get_segments_transaction_id(self):
        """Return the last committed segment.
        """
        for segment, filename in self.segment_iterator(reverse=True):
            if self.is_committed_segment(segment):
                return segment
        return None

    def cleanup(self, transaction_id):
        """Delete segment files left by aborted transactions
        """
        self.segment = transaction_id + 1
        for segment, filename in self.segment_iterator(reverse=True):
            if segment > transaction_id:
                os.unlink(filename)
            else:
                break

    def is_committed_segment(self, segment):
        """Check if segment ends with a COMMIT_TAG tag
        """
        try:
            iterator = self.iter_objects(segment)
        except IntegrityError:
            return False
        with open(self.segment_filename(segment), 'rb') as fd:
            try:
                fd.seek(-self.header_fmt.size, os.SEEK_END)
            except OSError as e:
                # return False if segment file is empty or too small
                if e.errno == errno.EINVAL:
                    return False
                raise e
            if fd.read(self.header_fmt.size) != self.COMMIT:
                return False
        seen_commit = False
        while True:
            try:
                tag, key, offset, _ = next(iterator)
            except IntegrityError:
                return False
            except StopIteration:
                break
            if tag == TAG_COMMIT:
                seen_commit = True
                continue
            if seen_commit:
                return False
        return seen_commit

    def segment_filename(self, segment):
        return os.path.join(self.path, 'data', str(segment // self.segments_per_dir), str(segment))

    def get_write_fd(self, no_new=False, raise_full=False):
        if not no_new and self.offset and self.offset > self.limit:
            if raise_full:
                raise self.SegmentFull
            self.close_segment()
        if not self._write_fd:
            if self.segment % self.segments_per_dir == 0:
                dirname = os.path.join(self.path, 'data', str(self.segment // self.segments_per_dir))
                if not os.path.exists(dirname):
                    os.mkdir(dirname)
                    sync_dir(os.path.join(self.path, 'data'))
            self._write_fd = SyncFile(self.segment_filename(self.segment))
            self._write_fd.write(MAGIC)
            self.offset = MAGIC_LEN
        return self._write_fd

    def get_fd(self, segment):
        try:
            return self.fds[segment]
        except KeyError:
            fd = open(self.segment_filename(segment), 'rb')
            self.fds[segment] = fd
            return fd

    def close_segment(self):
        if self._write_fd:
            self.segment += 1
            self.offset = 0
            self._write_fd.close()
            self._write_fd = None

    def delete_segment(self, segment):
        if segment in self.fds:
            del self.fds[segment]
        try:
            os.unlink(self.segment_filename(segment))
        except FileNotFoundError:
            pass

    def segment_exists(self, segment):
        return os.path.exists(self.segment_filename(segment))

    def segment_size(self, segment):
        return os.path.getsize(self.segment_filename(segment))

    def iter_objects(self, segment, include_data=False, read_data=True):
        """
        Return object iterator for *segment*.

        If read_data is False then include_data must be False as well.
        Integrity checks are skipped: all data obtained from the iterator must be considered informational.

        The iterator returns four-tuples of (tag, key, offset, data|size).
        """
        fd = self.get_fd(segment)
        fd.seek(0)
        if fd.read(MAGIC_LEN) != MAGIC:
            raise IntegrityError('Invalid segment magic [segment {}, offset {}]'.format(segment, 0))
        offset = MAGIC_LEN
        header = fd.read(self.header_fmt.size)
        while header:
            size, tag, key, data = self._read(fd, self.header_fmt, header, segment, offset,
                                              (TAG_PUT, TAG_DELETE, TAG_COMMIT),
                                              read_data=read_data)
            if include_data:
                yield tag, key, offset, data
            else:
                yield tag, key, offset, size
            offset += size
            header = fd.read(self.header_fmt.size)

    def recover_segment(self, segment, filename):
        if segment in self.fds:
            del self.fds[segment]
        with open(filename, 'rb') as fd:
            data = memoryview(fd.read())
        os.rename(filename, filename + '.beforerecover')
        logger.info('attempting to recover ' + filename)
        with open(filename, 'wb') as fd:
            fd.write(MAGIC)
            while len(data) >= self.header_fmt.size:
                crc, size, tag = self.header_fmt.unpack(data[:self.header_fmt.size])
                if size < self.header_fmt.size or size > len(data):
                    data = data[1:]
                    continue
                if crc32(data[4:size]) & 0xffffffff != crc:
                    data = data[1:]
                    continue
                fd.write(data[:size])
                data = data[size:]

    def read(self, segment, offset, id, read_data=True):
        """
        Read entry from *segment* at *offset* with *id*.

        If read_data is False the size of the entry is returned instead and integrity checks are skipped.
        The return value should thus be considered informational.
        """
        if segment == self.segment and self._write_fd:
            self._write_fd.sync()
        fd = self.get_fd(segment)
        fd.seek(offset)
        header = fd.read(self.put_header_fmt.size)
        size, tag, key, data = self._read(fd, self.put_header_fmt, header, segment, offset, (TAG_PUT, ), read_data)
        if id != key:
            raise IntegrityError('Invalid segment entry header, is not for wanted id [segment {}, offset {}]'.format(
                segment, offset))
        return data if read_data else size

    def _read(self, fd, fmt, header, segment, offset, acceptable_tags, read_data=True):
        # some code shared by read() and iter_objects()
        try:
            hdr_tuple = fmt.unpack(header)
        except struct.error as err:
            raise IntegrityError('Invalid segment entry header [segment {}, offset {}]: {}'.format(
                segment, offset, err)) from None
        if fmt is self.put_header_fmt:
            crc, size, tag, key = hdr_tuple
        elif fmt is self.header_fmt:
            crc, size, tag = hdr_tuple
            key = None
        else:
            raise TypeError("_read called with unsupported format")
        if size > MAX_OBJECT_SIZE or size < fmt.size:
            raise IntegrityError('Invalid segment entry size [segment {}, offset {}]'.format(
                segment, offset))
        length = size - fmt.size
        if read_data:
            data = fd.read(length)
            if len(data) != length:
                raise IntegrityError('Segment entry data short read [segment {}, offset {}]: expected {}, got {} bytes'.format(
                    segment, offset, length, len(data)))
            if crc32(data, crc32(memoryview(header)[4:])) & 0xffffffff != crc:
                raise IntegrityError('Segment entry checksum mismatch [segment {}, offset {}]'.format(
                    segment, offset))
            if key is None and tag in (TAG_PUT, TAG_DELETE):
                key, data = data[:32], data[32:]
        else:
            if key is None and tag in (TAG_PUT, TAG_DELETE):
                key = fd.read(32)
                length -= 32
                if len(key) != 32:
                    raise IntegrityError('Segment entry key short read [segment {}, offset {}]: expected {}, got {} bytes'.format(
                        segment, offset, 32, len(key)))
            oldpos = fd.tell()
            seeked = fd.seek(length, os.SEEK_CUR) - oldpos
            data = None
            if seeked != length:
                raise IntegrityError('Segment entry data short seek [segment {}, offset {}]: expected {}, got {} bytes'.format(
                        segment, offset, length, seeked))
        if tag not in acceptable_tags:
            raise IntegrityError('Invalid segment entry header, did not get acceptable tag [segment {}, offset {}]'.format(
                segment, offset))
        return size, tag, key, data

    def write_put(self, id, data, raise_full=False):
        fd = self.get_write_fd(raise_full=raise_full)
        size = len(data) + self.put_header_fmt.size
        offset = self.offset
        header = self.header_no_crc_fmt.pack(size, TAG_PUT)
        crc = self.crc_fmt.pack(crc32(data, crc32(id, crc32(header))) & 0xffffffff)
        fd.write(b''.join((crc, header, id, data)))
        self.offset += size
        return self.segment, offset

    def write_delete(self, id, raise_full=False):
        fd = self.get_write_fd(raise_full=raise_full)
        header = self.header_no_crc_fmt.pack(self.put_header_fmt.size, TAG_DELETE)
        crc = self.crc_fmt.pack(crc32(id, crc32(header)) & 0xffffffff)
        fd.write(b''.join((crc, header, id)))
        self.offset += self.put_header_fmt.size
        return self.segment, self.put_header_fmt.size

    def write_commit(self):
        self.close_segment()
        fd = self.get_write_fd()
        header = self.header_no_crc_fmt.pack(self.header_fmt.size, TAG_COMMIT)
        crc = self.crc_fmt.pack(crc32(header) & 0xffffffff)
        fd.write(b''.join((crc, header)))
        self.close_segment()
